import copy
import uuid
import pytest
from app.calendar import CalendarUnavailable, event_time, iso
from app.config import Config
from app.db import Database, Master, Service, now
from app.domain import BookingService
from app.scheduling import Interval
from app.worker import Worker


class FakeCalendar:
    def __init__(self, start):
        self.events_data = {}
        self.work_hours = {}
        self.hours = [Interval(start - 3600 * 2, start + 3600 * 8)]
        self.fail = False
        self.fail_delete_once = False
        self.fail_break_once = False

    def check(self):
        if self.fail:
            raise CalendarUnavailable('test outage')

    def snapshot(self, master, start, end, exclude=()):
        self.check()
        busy = []
        for (mid, eid), event in self.events_data.items():
            if mid == master.id and eid not in exclude:
                a, b = event_time(event['start'], 'UTC'), event_time(event['end'], 'UTC')
                if a < end and b > start:
                    busy.append(Interval(a, b, eid))
        return list(self.hours), busy

    def get(self, master, eid):
        self.check()
        return copy.deepcopy(self.events_data.get((master.id, eid)))

    def put(self, master, eid, body):
        self.check()
        self.events_data[(master.id, eid)] = copy.deepcopy(body)

    def delete(self, master, eid):
        self.check()
        self.events_data.pop((master.id, eid), None)
        if self.fail_delete_once:
            self.fail_delete_once = False
            raise CalendarUnavailable('response lost after deletion')

    def ensure_booking(self, master, b):
        self.put(master, 'a' + b.id, {'start': {'dateTime': iso(b.start)}, 'end': {'dateTime': iso(b.end)}})
        self.ensure_break(master, b)

    def ensure_break(self, master, b):
        if self.fail_break_once:
            self.fail_break_once = False
            raise CalendarUnavailable('partial appointment creation')
        if b.busy_until > b.end:
            self.put(master, 'b' + b.id, {'start': {'dateTime': iso(b.end)}, 'end': {'dateTime': iso(b.busy_until)}})

    def delete_booking(self, master, b):
        self.delete(master, 'a' + b.id)
        self.delete(master, 'b' + b.id)

    def ensure_closure(self, master, c):
        self.put(master, 'c' + c.id, {'start': {'dateTime': iso(c.start)}, 'end': {'dateTime': iso(c.end)}})

    def create_work_hours(self, master, start, end, summary='Рабочие часы'):
        uid = 'wh-' + uuid.uuid4().hex
        self.work_hours[(master.id, uid)] = {'id': uid, 'summary': summary,
            'start': {'dateTime': iso(start)}, 'end': {'dateTime': iso(end)}, 'recurring': False}
        return uid

    def list_work_hours(self, master, start, end):
        self.check()
        return [copy.deepcopy(item) for (master_id, _), item in self.work_hours.items()
                if master_id == master.id and event_time(item['start'], master.timezone) < end
                and event_time(item['end'], master.timezone) > start]

    def update_work_hours(self, master, uid, start, end, summary='Рабочие часы'):
        self.check()
        key = (master.id, uid)
        if key not in self.work_hours:
            raise CalendarUnavailable('Рабочий интервал уже удалён из календаря.')
        self.work_hours[key].update(summary=summary, start={'dateTime': iso(start)}, end={'dateTime': iso(end)})

    def delete_work_hours(self, master, uid):
        return self.work_hours.pop((master.id, uid), None) is not None


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.error = None

    def send(self, chat, text, keyboard=None):
        if self.error:
            raise self.error
        self.messages.append((chat, text, keyboard))


@pytest.fixture
def env(tmp_path):
    db = Database('sqlite:///' + str(tmp_path / 'test.db'))
    db.initialize()
    start = now() // 86400 * 86400 + 86400 + 10 * 3600
    cal = FakeCalendar(start)
    tg = FakeTelegram()
    service = BookingService(cal)
    config = Config(master_id=100, bot_username='testbot')
    with db.transaction() as s:
        for tid in [100, 200]:
            m = Master(telegram_id=tid, name=f'Master {tid}', timezone='UTC',
                       hours_calendar=f'hours{tid}', bookings_calendar=f'bookings{tid}')
            s.add(m)
            s.flush()
            s.add(Service(master_id=m.id, name='Маникюр', price=250000, minutes=90))
    return db, cal, tg, service, Worker(db, cal, service, tg), config, start


def reserve(env, client=300, start=None, mid=1, replaces=None):
    db, cal, tg, service, worker, config, default_start = env
    with db.transaction() as s:
        m = s.get(Master, mid)
        items = service.services(s, m, [mid])
        b = service.reserve(s, mid, client, 'Анна', '+79001234567', items, start or default_start, replaces)
        return b.id


def approve(env, bid, actor=100):
    db, _, _, service, worker, _, _ = env
    with db.transaction() as s:
        service.decide(s, bid, actor, True)
    worker.tick()
