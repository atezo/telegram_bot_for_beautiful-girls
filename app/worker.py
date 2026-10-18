import logging
from contextlib import nullcontext
from sqlalchemy import select
from .calendar import CalendarUnavailable, event_time
from .db import ACTIVE, Booking, Closure, Master, Outbox, now, notify, lock_master
from .domain import card
from .scheduling import Interval, allowed, overlaps

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, db, calendar, service, telegram):
        self.db, self.calendar, self.service, self.telegram = db, calendar, service, telegram

    def tick(self):
        with self.db.transaction() as s:
            ids = list(s.scalars(select(Master.id)))
        for mid in ids:
            try:
                with self.db.transaction() as s:
                    m = lock_master(s, mid)
                    self.service.expire(s, m, now())
                    if not m.bookings_calendar or not m.hours_calendar:
                        continue
                    active = list(s.scalars(select(Booking).where(Booking.master_id == m.id,
                                  Booking.status.in_((*ACTIVE, 'replacing')))))
                    start = min([now()] + [b.start for b in active]) - 2 * 86400
                    end = max([now() + 86400] + [b.busy_until for b in active]) + 2 * 86400
                    try:
                        context = self.calendar.cycle(m, start, end) if hasattr(self.calendar, 'cycle') else nullcontext()
                        with context:
                            self.process_master(s, m)
                        m.calendar_error = False
                    except CalendarUnavailable:
                        if not m.calendar_error:
                            notify(s, m.telegram_id, 'Календарь недоступен. Синхронизация будет повторена. '
                                   'При отозванном доступе проверьте пароль приложения в конфиге.')
                        m.calendar_error = True
            except Exception as exc:
                log.error('Worker master=%s error=%s', mid, type(exc).__name__)
        self.deliver()

    def problem(self, s, m, b):
        if not b.last_error:
            notify(s, m.telegram_id, 'Не удалось синхронизировать запись с календарём. '
                   'Повторю автоматически. Проверьте пароль приложения в конфиге.\n' + card(b, m))
            b.last_error = True

    def process_master(self, s, m):
        for c in s.scalars(select(Closure).where(Closure.master_id == m.id, Closure.synced.is_(False))):
            try:
                self.calendar.ensure_closure(m, c)
                c.synced = True
            except CalendarUnavailable:
                if not m.calendar_error:
                    notify(s, m.telegram_id, 'Не удалось обновить календарь. Повторю автоматически; проверьте параметры CalDAV в конфиге.')
                    m.calendar_error = True
        bookings = list(s.scalars(select(Booking).where(Booking.master_id == m.id,
                          Booking.status.in_((*ACTIVE, 'replacing'))).order_by(Booking.start)))
        cleanup_ids = {b.id for b in bookings if b.status == 'replacing'}
        # Finish the replacement saga before interpreting a removed old event as a manual cancellation.
        for b in bookings:
            if b.status == 'confirming':
                try:
                    self.confirm(s, m, b)
                    b.last_error = False
                except CalendarUnavailable:
                    self.problem(s, m, b)
        for b in bookings:
            try:
                if b.status == 'replacing' and b.id in cleanup_ids:
                    self.calendar.delete_booking(m, b)
                    b.status = 'replaced'
                if b.status == 'confirmed' and b.busy_until >= now():
                    self.reconcile(s, m, b)
                if b.status == 'canceling':
                    self.calendar.delete_booking(m, b)
                    b.status = 'cancelled'
                    reason = {'client': 'Клиент отменил запись.', 'master': 'Мастер отменил запись.',
                              'calendar': 'Запись отменена в календаре.'}.get(b.cancel_actor, 'Запись отменена.')
                    notify(s, b.client_id, reason + '\n' + card(b, m))
                    notify(s, m.telegram_id, reason + '\n' + card(b, m))
                b.last_error = False
            except CalendarUnavailable:
                self.problem(s, m, b)
        s.flush()

    def confirm(self, s, m, b):
        old = s.get(Booking, b.replaces) if b.replaces else None
        exclude = [b.id] + ([old.id] if old else [])
        if old and old.status != 'confirmed':
            self.reject_conflict(s, m, b, 'Исходная запись уже отменена или изменена.')
            return
        if b.start <= now():
            self.reject_conflict(s, m, b, 'Время приёма уже наступило.')
            return
        hours, busy = self.service.availability(s, m, b.start - 86400, b.busy_until + 86400, exclude)
        duration = (b.end - b.start) // 60
        pause = (b.busy_until - b.end) // 60
        shift = allowed(b.start, duration, pause, hours, busy, m.overtime)
        if shift is None:
            self.reject_conflict(s, m, b, 'В календаре появилась занятость или изменился рабочий график.')
            return
        b.shift_end = shift
        self.calendar.ensure_booking(m, b)
        if old:
            # Commit the new appointment first. Cleanup is independently retryable.
            old.status = 'replacing'
        b.status = 'confirmed'
        notify(s, b.client_id, ('Перенос подтверждён!\n' if old else 'Запись подтверждена!\n') + card(b, m))
        notify(s, m.telegram_id, 'Запись добавлена в календарь.\n' + card(b, m))

    def reject_conflict(self, s, m, b, reason):
        # Clean up any partial remote writes from an earlier attempt before releasing the reservation.
        self.calendar.delete_booking(m, b)
        b.status = 'rejected'
        notify(s, b.client_id, 'Не удалось подтвердить заявку. ' + reason + '\n' + card(b, m))
        notify(s, m.telegram_id, 'Заявка закрыта. ' + reason + '\n' + card(b, m))

    def reconcile(self, s, m, b):
        event = self.calendar.get(m, 'a' + b.id)
        if event is None:
            b.cancel_actor = 'calendar'
            self.service.mark_cancel(s, b)
            return
        start = event_time(event['start'], m.timezone)
        end = event_time(event['end'], m.timezone)
        if end <= start:
            raise CalendarUnavailable('Некорректная длительность события')
        if (start, end) != (b.start, b.end):
            pause = b.busy_until - b.end
            b.start, b.end, b.busy_until = start, end, end + pause
            notify(s, b.client_id, 'Мастер изменил время записи в календаре.\n' + card(b, m))
            notify(s, m.telegram_id, 'Изменение времени из календаря отправлено клиенту.\n' + card(b, m))
        # Repair the associated break if it was moved or deleted manually.
        self.calendar.ensure_break(m, b)
        excluded = [b.id]
        previous = s.get(Booking, b.replaces) if b.replaces else None
        if previous and previous.status == 'replacing':
            excluded.append(previous.id)
        hours, busy = self.service.availability(s, m, b.start - 86400, b.busy_until + 86400, excluded)
        conflicts = any(overlaps(Interval(b.start, b.busy_until), item) for item in busy)
        # A pending move of this appointment is an intentional overlap.
        children = list(s.scalars(select(Booking).where(Booking.replaces == b.id,
                        Booking.status.in_(['pending', 'confirming']))))
        if children:
            _, busy = self.service.availability(s, m, b.start - 86400, b.busy_until + 86400,
                                               excluded + [child.id for child in children])
            conflicts = any(overlaps(Interval(b.start, b.busy_until), item) for item in busy)
        shift = next((h for h in hours if h.start <= b.start < h.end), None)
        warning = 'conflict' if conflicts else ('hours' if shift is None or (not m.overtime and b.end > shift.end) else None)
        if warning and b.external_warning != warning:
            notify(s, m.telegram_id, ('⚠️ В календаре пересечение записей. Проверьте время вручную.\n'
                   if warning == 'conflict' else '⚠️ Запись вне рабочего графика. Она сохранена; при необходимости отмените её.\n') + card(b, m))
        b.external_warning = warning

    def deliver(self):
        with self.db.transaction() as s:
            items = list(s.scalars(select(Outbox).where(Outbox.sent.is_(False), Outbox.failed.is_(False),
                                Outbox.next_try <= now()).order_by(Outbox.id).limit(50).with_for_update(skip_locked=True)))
            for item in items:
                try:
                    self.telegram.send(item.chat_id, item.text, item.keyboard)
                    item.sent = True
                except Exception as exc:
                    item.attempts += 1
                    code = getattr(exc, 'code', None)
                    if code in (400, 403):
                        item.failed = True
                        log.warning('Telegram delivery failed permanently outbox=%s code=%s', item.id, code)
                        # Alert the affected master when a client cannot be reached.
                        mids = set(s.scalars(select(Booking.master_id).where(Booking.client_id == item.chat_id)))
                        for m in s.scalars(select(Master).where(Master.id.in_(mids))):
                            if m.telegram_id != item.chat_id:
                                notify(s, m.telegram_id, f'Не удалось доставить сообщение клиенту Telegram ID {item.chat_id}. '
                                       'Возможно, он заблокировал бота. Свяжитесь с ним по контакту из записи.')
                    else:
                        retry = getattr(exc, 'retry_after', 0)
                        item.next_try = now() + max(retry, min(3600, 2 ** min(item.attempts, 11)))
                        if code in (0, 429) or (code and code >= 500):
                            # A global outage must not spend 50 network timeouts in one sync pass.
                            for queued in items:
                                if not queued.sent and not queued.failed:
                                    queued.next_try = max(queued.next_try, item.next_try)
                            break
