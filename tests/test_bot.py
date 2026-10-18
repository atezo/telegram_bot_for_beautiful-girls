from sqlalchemy import select
from app.bot import Bot
from app.db import Booking, Conversation, Master, Outbox, Service, now


def message(bot, db, uid, text):
    with db.transaction() as s:
        bot.handle(s, {'message': {'chat': {'id': uid, 'type': 'private'},
                                  'from': {'id': uid, 'first_name': 'Анна'}, 'text': text}})


def click(bot, db, uid, data):
    with db.transaction() as s:
        bot.handle(s, {'callback_query': {'from': {'id': uid, 'first_name': 'Анна'}, 'data': data,
                                        'message': {'chat': {'id': uid, 'type': 'private'}}}})


def test_complete_client_conversation_and_duplicate_submit(env):
    db, cal, tg, service, worker, config, start = env
    bot = Bot(config, service)
    click(bot, db, 300, 'book:1')
    with db.transaction() as s:
        nonce = s.get(Conversation, 300).data['nonce']
    click(bot, db, 300, f'svc:{nonce}:1')
    click(bot, db, 300, f'days:{nonce}:0')
    from datetime import datetime, timezone
    date = datetime.fromtimestamp(start, timezone.utc).date().isoformat()
    click(bot, db, 300, f'day:{nonce}:{date}')
    click(bot, db, 300, f'time:{nonce}:{start}')
    message(bot, db, 300, 'Анна, +79001234567')
    click(bot, db, 300, f'submit:{nonce}')
    click(bot, db, 300, f'submit:{nonce}')
    with db.transaction() as s:
        assert len(list(s.scalars(select(Booking)))) == 1
        assert 'Анна' in s.scalar(select(Booking)).contact


def test_master_edit_and_cross_tenant_service_denied(env):
    db, _, _, service, _, config, _ = env
    bot = Bot(config, service)
    click(bot, db, 100, 'editservice:1')
    message(bot, db, 100, 'Покрытие | 1800,50 | 45')
    click(bot, db, 200, 'toggleservice:1')
    with db.transaction() as s:
        item = s.get(Service, 1)
        assert (item.price, item.minutes, item.active) == (180050, 45, True)
        assert any('доступ' in x.text.lower() for x in s.scalars(select(Outbox)))


def test_invalid_service_price_is_rejected(env):
    db, _, _, service, _, config, _ = env
    bot = Bot(config, service)
    click(bot, db, 100, 'editservice:1')
    message(bot, db, 100, 'Маникюр | NaN | 90')
    with db.transaction() as s:
        assert s.get(Service, 1).price == 250000


def test_master_is_recognized_on_start_and_cannot_be_enrolled(env):
    db, _, _, service, _, config, _ = env
    bot = Bot(config, service)
    message(bot, db, 100, '/start')
    message(bot, db, 999, '/start')
    with db.transaction() as s:
        owner_messages = list(s.scalars(select(Outbox).where(Outbox.chat_id == 100)))
        assert any('Ссылка для клиентов: https://t.me/testbot' in item.text for item in owner_messages)
        assert len(list(s.scalars(select(Master)))) == 2
        outsider_messages = list(s.scalars(select(Outbox).where(Outbox.chat_id == 999)))
        assert all('Мои услуги' not in str(item.keyboard) for item in outsider_messages)


def test_reopen_period_requires_owner_and_removes_remote_block(env):
    db, cal, _, service, worker, config, start = env
    bot = Bot(config, service)
    with db.transaction() as s:
        closure = service.close_period(s, 1, 100, start, start + 86400)
        s.flush()
        cid = closure.id
    worker.tick()
    from app.db import Closure
    click(bot, db, 200, 'reopen:' + cid)
    with db.transaction() as s:
        assert s.get(Closure, cid)
    click(bot, db, 100, 'reopen:' + cid)
    with db.transaction() as s:
        assert s.get(Closure, cid) is None
    assert (1, 'c' + cid) not in cal.events_data


def test_work_hours_accept_multiple_intervals_with_minutes_and_can_be_edited(env):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    db, cal, _, service, _, config, _ = env
    bot = Bot(config, service)
    click(bot, db, 100, 'addworkhours:0')
    with db.transaction() as s:
        nonce = s.get(Conversation, 100).data['nonce']
    day = datetime.now(ZoneInfo('UTC')).date().isoformat()
    click(bot, db, 100, f'whday:{nonce}:{day}')
    message(bot, db, 100, '10:30 15:00 | 16:00 19:30')
    assert len(cal.work_hours) == 2
    first_id = next(iter(cal.work_hours))[1]

    click(bot, db, 100, 'editworkhoursday:' + day)
    message(bot, db, 100, '11:00 14:30 | 16:00 20:00')
    changed = cal.work_hours[(1, first_id)]
    assert datetime.fromisoformat(changed['start']['dateTime']).strftime('%H:%M') == '11:00'
    assert datetime.fromisoformat(changed['end']['dateTime']).strftime('%H:%M') == '14:30'
    assert len(cal.work_hours) == 2


def test_date_buttons_use_russian_weekdays(env):
    db, _, _, service, _, config, _ = env
    bot = Bot(config, service)
    click(bot, db, 100, 'addworkhours:0')
    with db.transaction() as s:
        message_text = s.scalar(select(Outbox.text).order_by(Outbox.id.desc()))
        keyboard = s.scalar(select(Outbox.keyboard).order_by(Outbox.id.desc()))
    assert message_text == 'Выберите дату для рабочих часов:'
    assert any(f'({day})' in button['text'] for day in ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')
               for row in keyboard for button in row)


def test_master_can_change_setting_without_restart(env):
    db, _, _, service, _, config, _ = env
    bot = Bot(config, service)
    click(bot, db, 100, 'editsetting:break_minutes')
    message(bot, db, 100, '35')
    with db.transaction() as s:
        assert s.get(Master, 1).break_minutes == 35


def test_pending_request_has_telegram_contact_link(env):
    from app.domain import actions
    db, _, _, service, _, _, start = env
    with db.transaction() as s:
        master = s.get(Master, 1)
        booking = service.reserve(s, 1, 300, 'Анна', '+79001234567',
                                  service.services(s, master, [1]), start)
        keyboard = actions(booking)
    assert keyboard[-1][0]['url'] == 'tg://user?id=300'
