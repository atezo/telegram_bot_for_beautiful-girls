import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from .calendar import CalendarUnavailable
from .db import (ACTIVE, Booking, Closure, Conversation, Master, Service, now, notify,
                 lock_master)
from .domain import UserError, actions, card, contact_button, money, stamp, timezone_label


WEEKDAYS_RU = ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')


def date_label(value):
    """A locale-independent Russian date label for Telegram buttons."""
    return f'{value:%d.%m} ({WEEKDAYS_RU[value.weekday()]})'


def parse_time_intervals(text, day, timezone):
    """Parse `10:00 13:00 | 15:00 19:30` into non-overlapping timestamps."""
    try:
        result = []
        for part in text.split('|'):
            start_text, end_text = part.strip().split()
            start_time = datetime.strptime(start_text, '%H:%M').time()
            end_time = datetime.strptime(end_text, '%H:%M').time()
            start = datetime.combine(day, start_time, timezone)
            end = datetime.combine(day, end_time, timezone)
            if end <= start:
                raise ValueError()
            result.append((start_text, end_text, int(start.timestamp()), int(end.timestamp())))
        if not result:
            raise ValueError()
        ordered = sorted(result, key=lambda interval: interval[2])
        if any(previous[3] > current[2] for previous, current in zip(ordered, ordered[1:])):
            raise ValueError()
        return ordered
    except ValueError:
        raise UserError('Неверный формат или пересекающиеся интервалы. Пример: 09:00 13:00 | 15:00 20:00.') from None


def button(text, data):
    return {'text': text, 'callback_data': data}


def rows(*buttons):
    return [[b] for b in buttons]


class Bot:
    def __init__(self, config, service):
        self.config, self.service = config, service

    def handle(self, s, update):
        query = update.get('callback_query')
        msg = query.get('message', {}) if query else update.get('message', {})
        user = query['from'] if query else msg.get('from')
        if not user or msg.get('chat', {}).get('type') != 'private':
            return
        uid = user['id']
        conv = s.get(Conversation, uid)
        if not conv:
            conv = Conversation(user_id=uid, data={})
            s.add(conv)
            s.flush()
        # Copy JSON: SQLAlchemy intentionally stores complete state on each transition.
        state = dict(conv.data)
        try:
            if query:
                self.callback(s, uid, user, state, query.get('data', ''))
            else:
                self.message(s, uid, user, state, msg)
        except (UserError, CalendarUnavailable) as exc:
            text = str(exc) if isinstance(exc, UserError) else 'Календарь сейчас недоступен. Попробуйте позже; при необходимости мастер может проверить параметры CalDAV.'
            notify(s, uid, text)
        except (ValueError, KeyError, IndexError):
            notify(s, uid, 'Эта форма устарела или содержит неверное значение. Откройте /start .')
        finally:
            conv.data = state

    def owner(self, s, uid):
        m = s.scalar(select(Master).where(Master.telegram_id == uid))
        if not m or uid != self.config.master_id:
            raise UserError('Меню мастера доступно только подключённым мастерам.')
        return lock_master(s, m.id)

    def master_menu(self, s, uid):
        m = self.owner(s, uid)
        link = f'https://t.me/{self.config.bot_username}'
        notify(s, uid, f'Мастер: {m.name}\nСсылка для клиентов: {link}\n'
               f'Календарь: {"подключён" if m.hours_calendar and m.bookings_calendar else "не подключён"}', rows(
               button('Мои услуги', 'services'), button('Настройки', 'settings'),
               button('Заявки и записи', 'masterlist:0'),
               button('📅 Рабочие часы', 'workhours'),
               button('Закрыть период / отменить приёмы', 'close'), button('Открыть закрытый период', 'closures'),
               button('Меню клиента', 'home')))

    def workhours_menu(self, s, uid, state):
        """Show working hours management menu: list upcoming + add button."""
        m = self.owner(s, uid)
        today_ts = int(datetime.now(ZoneInfo(m.timezone)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        end_ts = today_ts + 60 * 86400
        try:
            events = self.service.calendar.list_work_hours(m, today_ts, end_ts)
        except CalendarUnavailable:
            events = []
        events = sorted(events, key=lambda e: e['start']['dateTime'])
        if events:
            notify(s, uid, f'Предстоящие рабочие часы в календаре:')
            from .calendar import event_time
            tz = ZoneInfo(m.timezone)
            grouped = {}
            for event in events:
                start = datetime.fromtimestamp(event_time(event['start'], m.timezone), tz)
                grouped.setdefault(start.date(), []).append(event)
            for day, day_events in list(grouped.items())[:15]:
                intervals = []
                for event in day_events:
                    start = datetime.fromtimestamp(event_time(event['start'], m.timezone), tz)
                    end = datetime.fromtimestamp(event_time(event['end'], m.timezone), tz)
                    intervals.append(f'{start:%H:%M}–{end:%H:%M}')
                label = f'{date_label(day)}\n' + ' • '.join(intervals)
                if any(event.get('recurring') for event in day_events):
                    notify(s, uid, label + '\nПовторяющееся событие меняется в календаре.')
                else:
                    notify(s, uid, label, rows(
                        button('✏️ Изменить интервалы', f'editworkhoursday:{day.isoformat()}'),
                        button('❌ Удалить все интервалы', f'delworkhoursday:{day.isoformat()}')))
        else:
            notify(s, uid, 'На ближайшие 60 дней рабочих часов нет.')
        notify(s, uid, 'Что сделать?', rows(
               button('➕ Добавить рабочие часы', 'addworkhours:0'),
               button('Назад', 'master')))

    def workhours_days(self, s, uid, state, offset):
        """Show date picker for adding work hours."""
        m = self.owner(s, uid)
        state.update(stage='workhours_day', nonce=secrets.token_hex(3))
        today = datetime.now(ZoneInfo(m.timezone)).date()
        offset = max(0, min(53, offset))
        buttons = [button(date_label(today + timedelta(days=d)),
                          f'whday:{state["nonce"]}:{(today + timedelta(days=d)).isoformat()}')
                   for d in range(offset, min(offset + 7, 60))]
        keyboard = rows(*buttons)
        nav = []
        if offset:
            nav.append(button('← Неделя', f'addworkhours:{max(0, offset - 7)}'))
        if offset + 7 < 60:
            nav.append(button('Неделя →', f'addworkhours:{offset + 7}'))
        if nav:
            keyboard.append(nav)
        keyboard.append([button('Назад', 'workhours')])
        notify(s, uid, 'Выберите дату для рабочих часов:', keyboard)

    def home(self, s, uid, state):
        buttons = [button('Мои записи', 'mine:0')]
        m = s.scalar(select(Master).where(Master.telegram_id == self.config.master_id))
        if m:
            buttons.insert(0, button(f'Записаться к {m.name}', f'book:{m.id}'))
        if uid == self.config.master_id:
            buttons.append(button('Меню мастера', 'master'))
        notify(s, uid, 'Выберите действие.', rows(*buttons))

    def begin(self, s, uid, state, mid, old=None):
        m = s.get(Master, mid)
        if not m or m.telegram_id != self.config.master_id or not m.hours_calendar or not m.bookings_calendar:
            raise UserError('Мастер ещё не подключил календарь.')
        state.clear()
        state.update(master=mid, selected=[], stage='services', nonce=secrets.token_hex(3))
        if old:
            state['replaces'] = old.id
        self.service_menu(s, uid, state)

    def service_menu(self, s, uid, state):
        services = list(s.scalars(select(Service).where(Service.master_id == state['master'],
                                                        Service.active.is_(True)).order_by(Service.id)))
        if not services:
            raise UserError('Мастер ещё не добавил услуги.')
        selected = state.get('selected', [])
        keyboard = rows(*[button(f'{"✓ " if item.id in selected else ""}{item.name} · {money(item.price)} · {item.minutes} мин',
                             f'svc:{state["nonce"]}:{item.id}') for item in services[:70]])
        if selected:
            total_price = sum(item.price for item in services if item.id in selected)
            total_min = sum(item.minutes for item in services if item.id in selected)
            keyboard.append([button(f'➡️ Выбрать дату ({total_min} мин · {money(total_price)})', f'days:{state["nonce"]}:0')])
            hint = 'Выбрано услуг: {}. Нажмите «Выбрать дату» или добавьте ещё.'.format(len(selected))
        else:
            hint = 'Выберите одну или несколько услуг.'
        notify(s, uid, hint, keyboard)


    def days(self, s, uid, state, offset):
        m = s.get(Master, state['master'])
        items = self.service.services(s, m, state['selected'])
        state.update(stage='days', items=items)
        today = datetime.now(ZoneInfo(m.timezone)).date()
        offset = max(0, min(53, offset))
        buttons = [button(date_label(today + timedelta(days=d)),
                           f'day:{state["nonce"]}:{(today + timedelta(days=d)).isoformat()}')
                   for d in range(offset, min(offset + 7, 60))]
        keyboard = rows(*buttons)
        nav = []
        if offset:
            nav.append(button('← Неделя', f'days:{state["nonce"]}:{max(0, offset - 7)}'))
        if offset + 7 < 60:
            nav.append(button('Неделя →', f'days:{state["nonce"]}:{offset + 7}'))
        if nav:
            keyboard.append(nav)
        notify(s, uid, f'Стоимость: {money(sum(x["price"] for x in items))}. '
               f'Длительность: {sum(x["minutes"] for x in items)} мин.\n'
               f'Выберите дату. Часовой пояс: {timezone_label(m.timezone)}.', keyboard)

    def times(self, s, uid, state, date):
        m = lock_master(s, state['master'])
        self.service.expire(s, m, now())
        day = datetime.fromisoformat(date).date()
        items = self.service.services(s, m, state['selected'])
        available = self.service.available_slots(s, m, day, items, state.get('replaces'))
        state.update(stage='times', items=items, offered=available)
        self.time_page(s, uid, state, 0)

    def time_page(self, s, uid, state, page):
        m = s.get(Master, state['master'])
        available = state.get('offered', [])
        page = max(0, min(page, max(0, (len(available) - 1) // 24)))
        buttons = [button(datetime.fromtimestamp(t, ZoneInfo(m.timezone)).strftime('%H:%M'),
                          f'time:{state["nonce"]}:{t}') for t in available[page * 24:(page + 1) * 24]]
        keyboard = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
        nav = []
        if page:
            nav.append(button('← Раньше', f'timepage:{state["nonce"]}:{page - 1}'))
        if (page + 1) * 24 < len(available):
            nav.append(button('Позже →', f'timepage:{state["nonce"]}:{page + 1}'))
        if nav:
            keyboard.append(nav)
        keyboard.append([button('Другая дата', f'days:{state["nonce"]}:0')])
        notify(s, uid, 'Свободное время:' if available else 'На эту дату подходящего времени нет.', keyboard)

    def booking_list(self, s, uid, is_master=False, page=0):
        if is_master:
            master = self.owner(s, uid)
            query = select(Booking).where(Booking.master_id == master.id)
        else:
            query = select(Booking).where(Booking.client_id == uid)
        records = list(s.scalars(query.where(Booking.status.in_(ACTIVE), Booking.busy_until > now())
                       .order_by(Booking.start).offset(page * 5).limit(6)))
        names = {'pending': 'Ожидает решения', 'confirming': 'Подтверждается',
                 'confirmed': 'Подтверждена', 'canceling': 'Отменяется'}
        if not records:
            notify(s, uid, 'Предстоящих записей нет.')
        for b in records[:5]:
            m = s.get(Master, b.master_id)
            keyboard = actions(b) if is_master and b.status == 'pending' else []
            if is_master and b.status != 'pending':
                keyboard.append([contact_button(b)])
            if b.status == 'confirmed':
                keyboard.append([button('Отменить запись', 'cancelask:' + b.id)])
            elif not is_master and b.status == 'pending':
                keyboard.append([button('Отозвать заявку', 'cancelask:' + b.id)])
            if not is_master and b.status == 'confirmed':
                keyboard.append([button('Перенести', 'move:' + b.id)])
            notify(s, uid, names[b.status] + '\n' + card(b, m), keyboard)
        if len(records) > 5:
            notify(s, uid, 'Следующие записи', rows(button('Далее', f'{"masterlist" if is_master else "mine"}:{page + 1}')))

    def settings(self, s, uid):
        m = self.owner(s, uid)
        notify(s, uid, f'Мастер: {m.name}\nЧасовой пояс: {timezone_label(m.timezone)}\n'
               f'Срок подтверждения: {m.approval_minutes} мин\nПерерыв: {m.break_minutes} мин\n'
               f'Минимум до записи: {m.lead_minutes} мин\n'
               f'Шаг времени: {m.step_minutes} мин\n'
               f'Выход за график: {"да" if m.overtime else "нет"}\n\n'
               'Выберите настройку — изменения применяются сразу.',
               rows(button('Имя мастера', 'editsetting:name'),
                    button('Часовой пояс', 'editsetting:timezone'),
                    button('Срок подтверждения', 'editsetting:approval_minutes'),
                    button('Перерыв между приёмами', 'editsetting:break_minutes'),
                    button('Минимум до записи', 'editsetting:lead_minutes'),
                    button('Шаг времени', 'editsetting:step_minutes'),
                    button('Выход за график: ' + ('выключить' if m.overtime else 'включить'), 'togglesetting:overtime'),
                    button('Назад', 'master')))

    def catalog(self, s, uid):
        m = self.owner(s, uid)
        services = list(s.scalars(select(Service).where(Service.master_id == m.id).order_by(Service.id)))
        for item in services:
            notify(s, uid, f'{item.name} · {money(item.price)} · {item.minutes} мин · '
                   + ('активна' if item.active else 'скрыта'), [[button('Изменить', f'editservice:{item.id}'),
                   button('Скрыть' if item.active else 'Включить', f'toggleservice:{item.id}')]])
        notify(s, uid, 'Управление услугами', rows(button('Добавить услугу', 'addservice'), button('Назад', 'master')))

    def close_preview(self, s, uid, state):
        m = self.owner(s, uid)
        ids = list(s.scalars(select(Booking.id).where(Booking.master_id == m.id,
                  Booking.status.in_(ACTIVE), Booking.start < state['close_end'], Booking.busy_until > state['close_start'])
                  .order_by(Booking.id)))
        state['close_ids'] = ids
        lines = []
        for bid in ids[:20]:
            b = s.get(Booking, bid)
            lines.append(f'{stamp(b.start, m)} — {b.client_name}')
        notify(s, uid, f'Закрыть период {stamp(state["close_start"], m)} — {stamp(state["close_end"], m)}?\n'
               f'Будет отменено записей: {len(ids)}. Клиенты получат уведомления.\n' + '\n'.join(lines),
               rows(button('Закрыть период и отменить записи', 'closeconfirm:' + state['nonce']), button('Назад', 'master')))

    def callback(self, s, uid, user, state, data):
        args = data.split(':')
        action = args[0]
        if action in ('svc', 'days', 'day', 'time', 'timepage', 'submit', 'closeconfirm'):
            if len(args) < 2 or args[1] != state.get('nonce'):
                raise UserError('Эта кнопка устарела. Откройте /start .')
        if action == 'home':
            state.pop('stage', None)
            self.home(s, uid, state)
        elif action == 'master':
            state.pop('stage', None)
            self.master_menu(s, uid)
        elif action == 'book':
            self.begin(s, uid, state, int(args[1]))
        elif action == 'svc':
            if state.get('stage') != 'services':
                raise UserError('Начните запись заново, чтобы поменять услуги.')
            selected = list(state['selected'])
            sid = int(args[2])
            item = s.get(Service, sid)
            if not item or item.master_id != state['master'] or not item.active:
                raise UserError('Услуга недоступна.')
            selected.remove(sid) if sid in selected else selected.append(sid)
            state['selected'] = selected
            self.service_menu(s, uid, state)
        elif action == 'days':
            self.days(s, uid, state, int(args[2]))
        elif action == 'day':
            self.times(s, uid, state, args[2])
        elif action == 'timepage':
            self.time_page(s, uid, state, int(args[2]))
        elif action == 'time':
            t = int(args[2])
            if t not in state.get('offered', []):
                raise UserError('Выберите время из актуального списка.')
            state.update(start=t, stage='contact')
            notify(s, uid, 'Отправьте имя и контакт для связи одним сообщением. Например: Анна, +7 900 123-45-67. '
                   'Эти данные увидит мастер и они будут сохранены в его календаре.')
        elif action == 'submit':
            if state.get('stage') != 'review':
                raise UserError('Заявка уже отправлена. Посмотрите /my.')
            self.service.reserve(s, state['master'], uid, state['client_name'], state['contact'],
                                 state['items'], state['start'], state.get('replaces'))
            mid = state['master']
            state.clear()
            state['master'] = mid
        elif action in ('mine', 'masterlist'):
            self.booking_list(s, uid, action == 'masterlist', int(args[1]))
        elif action in ('approve', 'reject'):
            m = self.owner(s, uid)
            self.service.decide(s, args[1], uid, action == 'approve')
        elif action == 'cancelask':
            b = s.get(Booking, args[1])
            if not b or uid not in (b.client_id, s.get(Master, b.master_id).telegram_id):
                raise UserError('Нет доступа.')
            notify(s, uid, 'Отменить эту запись?\n' + card(b, s.get(Master, b.master_id)),
                   rows(button('Да, отменить', 'cancel:' + b.id), button('Оставить запись', 'home')))
        elif action == 'cancel':
            self.service.cancel(s, args[1], uid)
        elif action == 'move':
            b = s.get(Booking, args[1])
            if not b or b.client_id != uid or b.status != 'confirmed' or b.start <= now():
                raise UserError('Эту запись нельзя перенести.')
            self.begin(s, uid, state, b.master_id, b)
        elif action == 'services':
            self.catalog(s, uid)
        elif action in ('editservice', 'toggleservice', 'addservice'):
            m = self.owner(s, uid)
            item = s.get(Service, int(args[1])) if len(args) > 1 else None
            if action != 'addservice' and (not item or item.master_id != m.id):
                raise UserError('Нет доступа.')
            if action == 'toggleservice':
                item.active = not item.active
                notify(s, uid, 'Услуга включена.' if item.active else 'Услуга скрыта.')
            else:
                if item is None and s.scalar(select(Service.id).where(Service.master_id == m.id).offset(69)):
                    raise UserError('Лимит первой версии — 70 услуг на мастера.')
                state.update(stage='editservice', service_id=item.id if item else None)
                current = (f'Сейчас: {item.name} | {money(item.price)} | {item.minutes} мин\n'
                           if item else 'Новая услуга\n')
                notify(s, uid, current + 'Отправьте: название | цена в рублях | длительность в минутах\n'
                       'Например: Маникюр | 2500 | 90\n\n'
                       'Для отмены используйте /cancel.')
        elif action == 'settings':
            self.settings(s, uid)
        elif action == 'togglesetting':
            m = self.owner(s, uid)
            if args[1] != 'overtime':
                raise UserError('Неизвестная настройка.')
            m.overtime = not m.overtime
            notify(s, uid, 'Выход за график ' + ('разрешён.' if m.overtime else 'запрещён.') + ' Изменение применено сразу.')
            self.settings(s, uid)
        elif action == 'editsetting':
            m = self.owner(s, uid)
            field = args[1]
            labels = {'name': 'Имя мастера', 'approval_minutes': 'Срок подтверждения',
                      'break_minutes': 'Перерыв между приёмами', 'lead_minutes': 'Минимум до записи',
                      'step_minutes': 'Шаг времени'}
            if field == 'timezone':
                notify(s, uid, 'Выберите часовой пояс:', rows(
                    button('Москва (МСК)', 'settimezone:Europe/Moscow'),
                    button('Калининград (МСК−1)', 'settimezone:Europe/Kaliningrad'),
                    button('Самара (МСК+1)', 'settimezone:Europe/Samara'),
                    button('Екатеринбург (МСК+2)', 'settimezone:Asia/Yekaterinburg'),
                    button('Омск (МСК+3)', 'settimezone:Asia/Omsk'),
                    button('Красноярск (МСК+4)', 'settimezone:Asia/Krasnoyarsk'),
                    button('Иркутск (МСК+5)', 'settimezone:Asia/Irkutsk'),
                    button('Владивосток (МСК+7)', 'settimezone:Asia/Vladivostok'),
                    button('Назад', 'settings')))
            elif field in labels:
                state.update(stage='editsetting', setting=field)
                current = getattr(m, field)
                suffix = '' if field == 'name' else ' в минутах'
                notify(s, uid, f'{labels[field]}: сейчас {current}.\nВведите новое значение{suffix}.')
            else:
                raise UserError('Неизвестная настройка.')
        elif action == 'settimezone':
            m = self.owner(s, uid)
            try:
                ZoneInfo(args[1])
            except ZoneInfoNotFoundError:
                raise UserError('Этот часовой пояс недоступен.') from None
            m.timezone = args[1]
            notify(s, uid, f'Часовой пояс изменён: {timezone_label(m.timezone)}. Изменение применено сразу.')
            self.settings(s, uid)
        elif action == 'closures':
            m = self.owner(s, uid)
            periods = list(s.scalars(select(Closure).where(Closure.master_id == m.id, Closure.end > now()).order_by(Closure.start)))
            if not periods:
                notify(s, uid, 'Закрытых будущих периодов нет.')
            for period in periods:
                notify(s, uid, f'{stamp(period.start, m)} — {stamp(period.end, m)}',
                       rows(button('Открыть для новых записей', 'reopenask:' + period.id)))
        elif action in ('reopenask', 'reopen'):
            m = self.owner(s, uid)
            period = s.get(Closure, args[1])
            if not period or period.master_id != m.id:
                raise UserError('Период не найден.')
            if action == 'reopenask':
                notify(s, uid, 'Открыть этот период для новых записей? Ранее отменённые приёмы не восстановятся.',
                       rows(button('Да, открыть', 'reopen:' + period.id), button('Назад', 'master')))
            else:
                self.service.calendar.delete(m, 'c' + period.id)
                s.delete(period)
                notify(s, uid, 'Период открыт. Доступное время определяется рабочими часами и занятостью.')
        elif action == 'close':
            self.owner(s, uid)
            state.update(stage='close', nonce=secrets.token_hex(3))
            notify(s, uid, 'Введите начало и конец периода по вашему часовому поясу:\n'
                   '2026-10-01 00:00 | 2026-10-03 00:00\nКонец не включается: пример закрывает 1 и 2 октября.')
        elif action == 'closeconfirm':
            m = self.owner(s, uid)
            current = list(s.scalars(select(Booking.id).where(Booking.master_id == m.id,
                          Booking.status.in_(ACTIVE), Booking.start < state['close_end'], Booking.busy_until > state['close_start'])
                          .order_by(Booking.id)))
            if current != state.get('close_ids'):
                notify(s, uid, 'Список записей изменился. Проверьте обновлённый список.')
                self.close_preview(s, uid, state)
            else:
                self.service.close_period(s, m.id, uid, state['close_start'], state['close_end'])
                state.clear()
        elif action == 'workhours':
            state.pop('stage', None)
            self.workhours_menu(s, uid, state)
        elif action == 'addworkhours':
            self.workhours_days(s, uid, state, int(args[1]))
        elif action == 'whday':
            if len(args) < 3 or args[1] != state.get('nonce'):
                raise UserError('Ссылка устарела. Откройте Рабочие часы заново.')
            m = self.owner(s, uid)
            state.update(stage='workhours_time', workhours_date=args[2])
            d = datetime.fromisoformat(args[2])
            notify(s, uid,
                   f'Дата: {d.strftime("%d.%m.%Y")}\n\n'
                   'Введите время в формате Начало Конец:\n'
                   'Например: 10:00 19:00\n\n'
                   'Можно ввести несколько интервалов через вертикальную черту:\n'
                   'Например: 09:00 13:00 | 15:00 20:00\n\n'
               f'Часовой пояс: {timezone_label(m.timezone)}')
        elif action == 'editworkhoursday':
            m = self.owner(s, uid)
            date_str = args[1]
            today_ts = int(datetime.now(ZoneInfo(m.timezone)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            events = self.service.calendar.list_work_hours(m, today_ts, today_ts + 60 * 86400)
            tz = ZoneInfo(m.timezone)
            from .calendar import event_time
            day_events = [event for event in events
                          if datetime.fromtimestamp(event_time(event['start'], m.timezone), tz).date().isoformat() == date_str]
            if not day_events or any(event.get('recurring') for event in day_events):
                raise UserError('Этот рабочий интервал нельзя изменить здесь. Повторяющиеся события меняются в календаре.')
            current = []
            for event in day_events:
                start = datetime.fromtimestamp(event_time(event['start'], m.timezone), tz)
                end = datetime.fromtimestamp(event_time(event['end'], m.timezone), tz)
                current.append(f'{start:%H:%M}–{end:%H:%M}')
            state.update(stage='editworkhours_day', workhours_date=date_str,
                         workhours_events=[{'id': event['id'], 'summary': event.get('summary') or 'Рабочие часы'}
                                           for event in day_events])
            notify(s, uid, f'Рабочие часы на {date_label(datetime.fromisoformat(date_str).date())}: ' + ' • '.join(current)
                   + '\n\nВведите полный новый список интервалов. Например:\n10:30 15:00 | 16:00 19:30')
        elif action == 'delworkhoursday':
            m = self.owner(s, uid)
            date_str = args[1]
            state.update(stage='deleteworkhours_day', workhours_date=date_str)
            notify(s, uid, f'Удалить все рабочие интервалы на {date_label(datetime.fromisoformat(date_str).date())}?',
                   rows(button('Да, удалить интервалы', 'confirmdelworkhoursday:' + date_str),
                        button('Нет, вернуться', 'workhours')))
        elif action == 'confirmdelworkhoursday':
            m = self.owner(s, uid)
            date_str = args[1]
            tz = ZoneInfo(m.timezone)
            from .calendar import event_time
            today_ts = int(datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            events = self.service.calendar.list_work_hours(m, today_ts, today_ts + 60 * 86400)
            matching = [event for event in events if not event.get('recurring')
                        and datetime.fromtimestamp(event_time(event['start'], m.timezone), tz).date().isoformat() == date_str]
            for event in matching:
                self.service.calendar.delete_work_hours(m, event['id'])
            state.pop('stage', None)
            notify(s, uid, 'Рабочие интервалы удалены.')
            self.workhours_menu(s, uid, state)
        else:
            raise UserError('Кнопка устарела. Откройте /start.')

    def message(self, s, uid, user, state, msg):
        text = msg.get('text', '').strip()
        command = text.split(' ', 1)[0].split('@')[0]
        if command == '/start':
            state.pop('stage', None)
            if uid == self.config.master_id:
                self.master_menu(s, uid)
            else:
                self.home(s, uid, state)
        elif command == '/my':
            self.booking_list(s, uid)
        elif command == '/id':
            notify(s, uid, f'Ваш Telegram ID: {uid}')
        elif command == '/cancel':
            state.clear()
            self.home(s, uid, state)
        elif command == '/help':
            notify(s, uid, '/start — меню клиента или мастера\n/my — мои записи, отмена и перенос\n'
                   '/id — ваш Telegram ID\n/cancel — выйти из текущего ввода\n'
                   'Время показывается в часовом поясе мастера. Подтверждение придёт отдельным сообщением.')
        elif state.get('stage') == 'editsetting':
            m = self.owner(s, uid)
            field = state.get('setting')
            limits = {'approval_minutes': (1, 10080), 'break_minutes': (0, 240),
                      'lead_minutes': (0, 10080), 'step_minutes': (5, 120)}
            try:
                if field == 'name':
                    value = text.strip()
                    if not 1 <= len(value) <= 80:
                        raise ValueError()
                elif field in limits:
                    value = int(text)
                    low, high = limits[field]
                    if not low <= value <= high or (field == 'step_minutes' and 1440 % value):
                        raise ValueError()
                else:
                    raise ValueError()
            except ValueError:
                if field == 'name':
                    raise UserError('Введите имя длиной от 1 до 80 символов.') from None
                low, high = limits.get(field, (0, 0))
                raise UserError(f'Введите целое число от {low} до {high}.') from None
            setattr(m, field, value)
            state.pop('stage', None)
            state.pop('setting', None)
            notify(s, uid, 'Настройка сохранена и применяется сразу.')
            self.settings(s, uid)
        elif state.get('stage') == 'contact':
            if not 3 <= len(text) <= 150:
                raise UserError('Напишите имя и контакт: от 3 до 150 символов.')
            state.update(contact=text, client_name=(user.get('first_name', '') + ' ' + user.get('last_name', '')).strip()[:100], stage='review')
            m = s.get(Master, state['master'])
            end = state['start'] + sum(x['minutes'] for x in state['items']) * 60
            notify(s, uid, f'Проверьте заявку:\nМастер: {m.name}\nКонтакт: {text}\n'
                   + '\n'.join(f'• {x["name"]}: {money(x["price"])}' for x in state['items'])
                   + f'\nИтого: {money(sum(x["price"] for x in state["items"]))}\n'
                   f'{stamp(state["start"], m)} — {stamp(end, m)} ({m.timezone})',
                   rows(button('Отправить заявку', 'submit:' + state['nonce']), button('В меню', 'home')))
        elif state.get('stage') == 'editservice':
            m = self.owner(s, uid)
            try:
                name, price, minutes = [x.strip() for x in text.split('|')]
                amount = Decimal(price.replace(',', '.'))
                duration = int(minutes)
                if not amount.is_finite() or amount < 0 or amount > 1_000_000 or amount * 100 != int(amount * 100):
                    raise ValueError()
                if not 1 <= duration <= 1440 or not 1 <= len(name) <= 80:
                    raise ValueError()
            except (ValueError, InvalidOperation):
                raise UserError('Формат: Название | 2500 | 90. Цена до 1 000 000 ₽, длительность 1–1440 мин.')
            item = s.get(Service, state['service_id']) if state.get('service_id') else None
            if item and item.master_id != m.id:
                raise UserError('Нет доступа.')
            if not item:
                item = Service(master_id=m.id)
                s.add(item)
            item.name, item.price, item.minutes = name, int(amount * 100), duration
            state.pop('stage', None)
            notify(s, uid, 'Услуга сохранена. Существующие заявки сохранили прежнюю цену и длительность.',
                   rows(button('Мои услуги', 'services'), button('Главное меню', 'master')))
        elif state.get('stage') == 'close':
            m = self.owner(s, uid)
            try:
                parts = text.split('|')
                start, end = [int(datetime.strptime(x.strip(), '%Y-%m-%d %H:%M')
                                  .replace(tzinfo=ZoneInfo(m.timezone)).timestamp()) for x in parts]
                if end <= max(start, now()):
                    raise ValueError()
            except ValueError:
                raise UserError('Введите две даты: ГГГГ-ММ-ДД ЧЧ:ММ | ГГГГ-ММ-ДД ЧЧ:ММ. Конец должен быть позже начала.')
            state.update(close_start=start, close_end=end, stage='close_review')
            self.close_preview(s, uid, state)
        elif state.get('stage') == 'workhours_time':
            m = self.owner(s, uid)
            tz = ZoneInfo(m.timezone)
            date_str = state.get('workhours_date', '')
            try:
                day = datetime.fromisoformat(date_str).date()
                intervals = parse_time_intervals(text, day, tz)
                created = []
                for start_time, end_time, start_ts, end_ts in intervals:
                    self.service.calendar.create_work_hours(m, start_ts, end_ts)
                    created.append(f'{start_time}–{end_time}')
                state.pop('stage', None)
                d = day
                notify(s, uid,
                       f'✅ Рабочие часы {d.strftime("%d.%m.%Y")} добавлены в календарь:\n'
                       + '\n'.join(f'• {c}' for c in created),
                       rows(button('Добавить ещё', 'addworkhours:0'),
                            button('Рабочие часы', 'workhours'),
                            button('Главное меню', 'master')))
            except (ValueError, AttributeError):
                raise UserError(
                    'Неверный формат. Введите время так:\n'
                    'Один интервал: 10:00 18:00\n'
                    'Два интервала: 09:00 13:00 | 15:00 20:00'
                )
        elif state.get('stage') == 'editworkhours_day':
            m = self.owner(s, uid)
            tz = ZoneInfo(m.timezone)
            try:
                intervals = parse_time_intervals(text, datetime.fromisoformat(state['workhours_date']).date(), tz)
                existing = state['workhours_events']
            except KeyError:
                raise UserError('Интервал больше не найден. Откройте «Рабочие часы» заново.') from None
            updated = []
            for saved, interval in zip(existing, intervals):
                start_text, end_text, start_ts, end_ts = interval
                self.service.calendar.update_work_hours(m, saved['id'], start_ts, end_ts, saved['summary'])
                updated.append(f'{start_text}–{end_text}')
            for interval in intervals[len(existing):]:
                start_text, end_text, start_ts, end_ts = interval
                self.service.calendar.create_work_hours(m, start_ts, end_ts)
                updated.append(f'{start_text}–{end_text}')
            for saved in existing[len(intervals):]:
                self.service.calendar.delete_work_hours(m, saved['id'])
            state.pop('stage', None)
            notify(s, uid, 'Рабочие интервалы обновлены: ' + ' • '.join(updated) + '.',
                   rows(button('Рабочие часы', 'workhours'), button('Главное меню', 'master')))
        else:
            self.home(s, uid, state)
