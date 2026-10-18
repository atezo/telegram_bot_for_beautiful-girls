"""Real CalDAV HTTP integration against a temporary, localhost-only Radicale."""
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import caldav
import pytest
import requests
from icalendar import Calendar, Event

from app.calendar import CalDAVCalendar, CalendarUnavailable, event_time, iso
from app.config import Config


@pytest.fixture
def live_calendar(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config_file = tmp_path / 'radicale.ini'
    config_file.write_text(f'[server]\nhosts = 127.0.0.1:{port}\n[auth]\ntype = none\n'
                           f'[storage]\nfilesystem_folder = {tmp_path / "data"}\n[logging]\nlevel = error\n')
    log = (tmp_path / 'radicale.log').open('w+')
    process = subprocess.Popen([sys.executable, '-m', 'radicale', '--config', str(config_file)], stdout=log, stderr=log)
    url = f'http://127.0.0.1:{port}'
    try:
        for _ in range(80):
            if process.poll() is not None:
                log.seek(0)
                pytest.fail(log.read())
            try:
                requests.get(url, timeout=0.3)
                break
            except requests.RequestException:
                time.sleep(0.1)
        else:
            pytest.fail('Test CalDAV server did not start')
        client = caldav.DAVClient(url=url, username='test', password='test', require_tls=False)
        principal = client.principal()
        hours = principal.make_calendar(name='Рабочие часы')
        bookings = principal.make_calendar(name='Записи')
        config = Config(caldav_url=url, caldav_username='test', caldav_password='test')
        yield CalDAVCalendar(config), hours, bookings
        client.close()
    finally:
        process.terminate()
        process.wait(timeout=10)
        log.close()


def test_create_retry_move_delete_and_restore_break(live_calendar):
    adapter, hours, bookings = live_calendar
    master = SimpleNamespace(id=1, timezone='Europe/Moscow')
    start = int(datetime(2030, 1, 1, 10, tzinfo=timezone.utc).timestamp())
    b = SimpleNamespace(id='f' * 32, client_name='Анна', contact='+79991234567',
                        services=[{'name': 'Маникюр', 'price': 250000}],
                        start=start, end=start + 5400, busy_until=start + 6600)
    adapter.ensure_booking(master, b)
    adapter.ensure_booking(master, b)
    assert len(bookings.events()) == 2
    adapter.delete(master, 'b' + b.id)
    adapter.ensure_break(master, b)
    assert len(bookings.events()) == 2
    b.start += 7200
    b.end += 7200
    b.busy_until += 7200
    adapter.ensure_booking(master, b)
    assert event_time(adapter.get(master, 'a' + b.id)['start'], 'UTC') == b.start
    adapter.delete_booking(master, b)
    adapter.delete_booking(master, b)
    assert not bookings.events()
    assert adapter.get(master, 'a' + b.id) is None


def test_irregular_hours_and_recurring_exceptions(live_calendar):
    adapter, hours, bookings = live_calendar
    master = SimpleNamespace(id=1, timezone='UTC')
    content = Calendar()
    content.add('version', '2.0')
    content.add('prodid', '-//Test//')
    event = Event()
    event.add('uid', 'schedule')
    event.add('dtstart', datetime(2030, 1, 1, 14, tzinfo=timezone.utc))
    event.add('dtend', datetime(2030, 1, 1, 18, tzinfo=timezone.utc))
    event.add('rrule', {'freq': 'daily', 'count': 3})
    event.add('exdate', datetime(2030, 1, 2, 14, tzinfo=timezone.utc))
    content.add_component(event)
    hours.save_event(content.to_ical().decode())
    hours.save_event(dtstart=datetime(2030, 1, 2, 9, tzinfo=timezone.utc),
                     dtend=datetime(2030, 1, 2, 12, tzinfo=timezone.utc), summary='До учёбы')
    hours.save_event(dtstart=datetime(2030, 1, 2, 16, tzinfo=timezone.utc),
                     dtend=datetime(2030, 1, 2, 19, tzinfo=timezone.utc), summary='После учёбы')
    start = int(datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
    with adapter.cycle(master, start, start + 86400 * 4):
        available, busy = adapter.snapshot(master, start, start + 86400 * 4)
    assert len(available) == 4
    assert not busy
    spans = [(datetime.fromtimestamp(h.start, timezone.utc).day,
              datetime.fromtimestamp(h.start, timezone.utc).hour,
              datetime.fromtimestamp(h.end, timezone.utc).hour) for h in available]
    assert sorted(spans) == [(1, 14, 18), (2, 9, 12), (2, 16, 19), (3, 14, 18)]
