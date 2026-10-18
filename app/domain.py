from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select

from .db import ACTIVE, Booking, Closure, Master, Service, now, notify, lock_master
from .scheduling import Interval, allowed, overlaps, slots


class UserError(Exception):
    pass


def money(value):
    rubles, kopecks = divmod(value, 100)
    whole = f'{rubles:,}'.replace(',', ' ')
    return f'{whole} ₽' if not kopecks else f'{whole},{kopecks:02d} ₽'


def timezone_label(value):
    return 'Москва (МСК)' if value == 'Europe/Moscow' else value


def stamp(value, master):
    return datetime.fromtimestamp(value, ZoneInfo(master.timezone)).strftime('%d.%m.%Y %H:%M')


def card(b, m):
    return (f'Мастер: {m.name}\nКлиент: {b.client_name}\nКонтакт: {b.contact}\n'
            + '\n'.join(f'• {s["name"]}: {money(s["price"])}' for s in b.services)
            + f'\nИтого: {money(sum(s["price"] for s in b.services))}'
            + f'\nПриём: {stamp(b.start, m)} — {stamp(b.end, m)} ({timezone_label(m.timezone)})'
            + f'\nС учётом перерыва: до {stamp(b.busy_until, m)}'
            + (f'\n⚠️ За пределами графика: рабочий интервал до {stamp(b.shift_end, m)}'
               if b.busy_until > b.shift_end else ''))


def actions(b):
    return [[{'text': 'Подтвердить', 'callback_data': 'approve:' + b.id},
             {'text': 'Отклонить', 'callback_data': 'reject:' + b.id}],
            [contact_button(b)]]


def contact_button(b):
    # A client has already opened this bot, so Telegram permits a deep link to their profile.
    return {'text': '✉️ Написать клиенту в Telegram', 'url': f'tg://user?id={b.client_id}'}


class BookingService:
    def __init__(self, calendar):
        self.calendar = calendar

    def expire(self, session, master, at):
        for b in session.scalars(select(Booking).where(Booking.master_id == master.id,
                                  Booking.status == 'pending', Booking.expires <= at)):
            b.status = 'expired'
            notify(session, b.client_id, 'Срок подтверждения истёк. Выберите другое время.\n' + card(b, master))
            notify(session, master.telegram_id, 'Заявка просрочена.\n' + card(b, master))
        session.flush()

    def services(self, session, master, ids):
        ids = set(ids)
        services = list(session.scalars(select(Service).where(Service.master_id == master.id,
                          Service.id.in_(ids), Service.active.is_(True)).order_by(Service.id)))
        if len(ids) > 10 or sum(item.minutes for item in services) > 1440:
            raise UserError('Выберите до 10 услуг общей длительностью не более 24 часов.')
        if not ids or len(services) != len(ids):
            raise UserError('Список услуг изменился. Начните запись заново.')
        return [{'id': s.id, 'name': s.name, 'price': s.price, 'minutes': s.minutes} for s in services]

    def availability(self, session, master, start, end, exclude=()):
        event_ids = [prefix + bid for bid in exclude for prefix in ('a', 'b')]
        hours, busy = self.calendar.snapshot(master, start, end, event_ids)
        for b in session.scalars(select(Booking).where(Booking.master_id == master.id,
                                  Booking.status.in_(ACTIVE), Booking.start < end, Booking.busy_until > start)):
            if b.id not in exclude:
                busy.append(Interval(b.start, b.busy_until))
        for c in session.scalars(select(Closure).where(Closure.master_id == master.id,
                                  Closure.start < end, Closure.end > start)):
            busy.append(Interval(c.start, c.end))
        return hours, busy

    def available_slots(self, session, master, day, services, replaces=None):
        zone = ZoneInfo(master.timezone)
        start = int(datetime.combine(day, datetime.min.time(), zone).timestamp())
        minutes = sum(s['minutes'] for s in services)
        hours, busy = self.availability(session, master, start, start + 86400 * 3,
                                        [replaces] if replaces else [])
        return slots(day, master.timezone, minutes, master.break_minutes, master.step_minutes,
                     hours, busy, master.overtime, now() + master.lead_minutes * 60)

    def reserve(self, session, master_id, client_id, client_name, contact, items, start, replaces=None):
        master = lock_master(session, master_id)
        at = now()
        self.expire(session, master, at)
        if start < at + master.lead_minutes * 60 or start > at + 60 * 86400:
            raise UserError('Это время уже недоступно. Выберите другое.')
        if list(session.scalars(select(Booking.id).where(Booking.master_id == master_id,
                   Booking.client_id == client_id, Booking.status.in_(['pending', 'confirming'])))):
            raise UserError('У вас уже есть заявка у этого мастера. Дождитесь решения или отмените её.')
        old = None
        if replaces:
            old = session.get(Booking, replaces)
            if not old or old.master_id != master_id or old.client_id != client_id or old.status != 'confirmed' or old.start <= at:
                raise UserError('Эту запись уже нельзя перенести.')
        current = self.services(session, master, [s['id'] for s in items])
        if current != items:
            raise UserError('Стоимость или длительность услуг изменилась. Начните запись заново.')
        duration = sum(s['minutes'] for s in current)
        end = start + duration * 60
        hours, busy = self.availability(session, master, start - 86400, end + 86400,
                                        [replaces] if replaces else [])
        shift_end = allowed(start, duration, master.break_minutes, hours, busy, master.overtime)
        if shift_end is None:
            raise UserError('Время уже занято или график изменился. Выберите другое.')
        b = Booking(master_id=master.id, client_id=client_id, client_name=client_name[:100],
                    contact=contact[:150], services=current, start=start, end=end,
                    busy_until=end + master.break_minutes * 60, shift_end=shift_end,
                    expires=min(at + master.approval_minutes * 60, start), replaces=replaces)
        session.add(b)
        session.flush()
        notify(session, client_id, f'Заявка отправлена. Ожидайте решения до {stamp(b.expires, master)}.\n'
               + ('Старая запись сохраняется до подтверждения переноса.\n' if old else '') + card(b, master))
        notify(session, master.telegram_id, ('Запрос переноса\n' if old else 'Новая заявка\n')
               + card(b, master) + f'\nОтветить до: {stamp(b.expires, master)}', actions(b))
        return b

    def decide(self, session, booking_id, actor, approve):
        b = session.get(Booking, booking_id)
        if not b:
            raise UserError('Запись не найдена.')
        m = lock_master(session, b.master_id)
        session.refresh(b)
        if m.telegram_id != actor:
            raise UserError('Нет доступа.')
        self.expire(session, m, now())
        if b.status != 'pending':
            raise UserError('Заявка уже обработана или срок подтверждения истёк.')
        b.status = 'confirming' if approve else 'rejected'
        if approve:
            notify(session, actor, 'Подтверждение обрабатывается. Результат придёт после синхронизации календаря.')
        else:
            notify(session, b.client_id, 'Мастер отклонил заявку.\n' + card(b, m))
            notify(session, actor, 'Заявка отклонена.')

    def cancel(self, session, booking_id, actor):
        b = session.get(Booking, booking_id)
        if not b:
            raise UserError('Запись не найдена.')
        m = lock_master(session, b.master_id)
        session.refresh(b)
        if actor not in (m.telegram_id, b.client_id):
            raise UserError('Нет доступа.')
        if b.status not in ACTIVE or b.status == 'canceling':
            raise UserError('Запись уже закрыта или отменяется.')
        b.cancel_actor = 'master' if actor == m.telegram_id else 'client'
        self.mark_cancel(session, b)
        notify(session, actor, 'Отмена принята. После обработки уведомлю обе стороны.')

    def mark_cancel(self, session, b):
        b.status = 'canceling'
        for child in session.scalars(select(Booking).where(Booking.replaces == b.id,
                                        Booking.status.in_(['pending', 'confirming']))):
            child.status = 'canceling'
            child.cancel_actor = b.cancel_actor

    def close_period(self, session, master_id, actor, start, end):
        master = lock_master(session, master_id)
        if master.telegram_id != actor or end <= start or end <= now():
            raise UserError('Неверный период или нет доступа.')
        closure = Closure(master_id=master_id, start=start, end=end)
        session.add(closure)
        for b in session.scalars(select(Booking).where(Booking.master_id == master_id,
                     Booking.status.in_(ACTIVE), Booking.start < end, Booking.busy_until > start)):
            b.cancel_actor = 'master'
            self.mark_cancel(session, b)
        notify(session, actor, 'Период закрыт для новых записей. Отмены и уведомления обрабатываются.')
        return closure
