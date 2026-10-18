"""CalDAV adapter. No cloud project, OAuth callback or public web server required."""
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import caldav
from caldav.lib.error import NotFoundError
from icalendar import Calendar, Event

from .scheduling import Interval


class CalendarUnavailable(Exception):
    pass


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def event_time(value, tz):
    if 'dateTime' in value:
        return int(datetime.fromisoformat(value['dateTime'].replace('Z', '+00:00')).timestamp())
    return int(datetime.fromisoformat(value['date']).replace(tzinfo=ZoneInfo(tz)).timestamp())


def timestamp(value, tz):
    if not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time())
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(tz))
    return int(value.timestamp())


class CalDAVCalendar:
    def __init__(self, config):
        self.config = config
        self.local = threading.local()

    @contextmanager
    def connection(self):
        client = caldav.DAVClient(url=self.config.caldav_url,
            username=self.config.caldav_username, password=self.config.caldav_password,
            timeout=20, ssl_verify_cert=True, require_tls=not self.config.caldav_url.startswith('http://localhost')
            and not self.config.caldav_url.startswith('http://127.0.0.1'))
        try:
            calendars = client.principal().calendars()
            selected = {}
            for kind, wanted in [('hours', self.config.hours_calendar), ('bookings', self.config.bookings_calendar)]:
                matches = [c for c in calendars if str(c.url).rstrip('/') == wanted.rstrip('/')
                           or c.name == wanted]
                if len(matches) != 1:
                    raise CalendarUnavailable(f'Нужен один календарь «{wanted}». Создайте его вручную или укажите точный CalDAV URL.')
                selected[kind] = matches[0]
            yield selected
        except CalendarUnavailable:
            raise
        except Exception as exc:
            # Library exceptions can contain credentials/URLs: do not expose their messages.
            raise CalendarUnavailable(type(exc).__name__) from None
        finally:
            client.close()

    @contextmanager
    def remote(self):
        active = getattr(self.local, 'remote', None)
        if active:
            yield active
        else:
            with self.connection() as selected:
                yield selected

    def convert(self, component, tz):
        if str(component.get('STATUS', '')).upper() == 'CANCELLED':
            return None
        start = component.decoded('DTSTART')
        end = component.decoded('DTEND', None)
        if end is None:
            duration = component.decoded('DURATION', None)
            if duration is None:
                duration = timedelta(days=1) if not isinstance(start, datetime) else timedelta(0)
            end = start + duration
        return {'id': str(component['UID']), 'summary': str(component.get('SUMMARY', '')),
                'description': str(component.get('DESCRIPTION', '')),
                'start': {'dateTime': iso(timestamp(start, tz))},
                'end': {'dateTime': iso(timestamp(end, tz))},
                'transparency': str(component.get('TRANSP', 'OPAQUE')).lower(),
                'recurring': bool(component.get('RRULE') or component.get('RECURRENCE-ID'))}

    def read(self, calendar, start, end, tz):
        # Expansion happens client-side: repeat rules and exceptions work consistently across servers.
        objects = calendar.search(start=datetime.fromtimestamp(start, timezone.utc),
                                  end=datetime.fromtimestamp(end, timezone.utc), event=True, expand=True)
        return [event for obj in objects if (event := self.convert(obj.component, tz)) is not None]

    @contextmanager
    def cycle(self, master, start, end):
        with self.connection() as selected:
            self.local.remote = selected
            self.local.cache = None
            try:
                self.local.cache = {'start': start, 'end': end,
                    'hours': self.read(selected['hours'], start, end, master.timezone),
                    'bookings': self.read(selected['bookings'], start, end, master.timezone)}
                yield
            except CalendarUnavailable:
                raise
            except Exception as exc:
                raise CalendarUnavailable(type(exc).__name__) from None
            finally:
                self.local.remote = None
                self.local.cache = None

    def snapshot(self, master, start, end, exclude=()):
        cache = getattr(self.local, 'cache', None)
        if cache and cache['start'] <= start and end <= cache['end']:
            hours, events = cache['hours'], cache['bookings']
        else:
            with self.remote() as selected:
                hours = self.read(selected['hours'], start, end, master.timezone)
                events = self.read(selected['bookings'], start, end, master.timezone)
        to_interval = lambda e: Interval(event_time(e['start'], master.timezone), event_time(e['end'], master.timezone), e['id'])
        return ([to_interval(e) for e in hours],
                [to_interval(e) for e in events if e['transparency'] != 'transparent' and e['id'] not in exclude])

    def lookup(self, calendar, uid):
        # A failed REPORT (auth/network/missing calendar) is never interpreted as event deletion.
        # Check collection existence before a library NotFoundError can mean "no matching UID".
        calendar.get_display_name()
        try:
            return calendar.event_by_uid(uid)
        except NotFoundError:
            return None

    def get(self, master, uid):
        with self.remote() as selected:
            obj = self.lookup(selected['bookings'], uid)
            return self.convert(obj.component, master.timezone) if obj else None

    def update_cache(self, uid, body):
        cache = getattr(self.local, 'cache', None)
        if cache:
            cache['bookings'] = [e for e in cache['bookings'] if e['id'] != uid]
            if body:
                cache['bookings'].append({'id': uid, **body})

    def put(self, master, uid, body):
        with self.remote() as selected:
            calendar = selected['bookings']
            existing = self.lookup(calendar, uid)
            if existing:
                actual = self.convert(existing.component, master.timezone)
                if actual and all(actual.get(key) == value for key, value in body.items()):
                    return
                if actual and actual['recurring']:
                    raise CalendarUnavailable('Не делайте созданный ботом приём повторяющимся событием')
            event = Event()
            event.add('uid', uid)
            event.add('dtstamp', datetime.now(timezone.utc))
            event.add('dtstart', datetime.fromtimestamp(event_time(body['start'], master.timezone), timezone.utc))
            event.add('dtend', datetime.fromtimestamp(event_time(body['end'], master.timezone), timezone.utc))
            event.add('summary', body['summary'])
            event.add('description', body.get('description', ''))
            event.add('transp', body.get('transparency', 'opaque').upper())
            event.add('status', 'CONFIRMED')
            content = Calendar()
            content.add('prodid', '-//Booking Bot//CalDAV//RU')
            content.add('version', '2.0')
            content.add_component(event)
            if existing:
                existing.data = content.to_ical().decode()
                existing.save()
            else:
                # Stable UID: retries address the same event, never a random new appointment.
                calendar.save_event(content.to_ical().decode())
            self.update_cache(uid, body)

    def delete(self, master, uid):
        with self.remote() as selected:
            existing = self.lookup(selected['bookings'], uid)
            if existing:
                existing.delete()
            self.update_cache(uid, None)

    def ensure_booking(self, master, booking):
        names = ', '.join(s['name'] for s in booking.services)
        total = sum(s['price'] for s in booking.services) / 100
        self.put(master, 'a' + booking.id, {
            'summary': f'{booking.client_name} — {names}',
            'description': f'Контакт: {booking.contact}\nСтоимость: {total:.2f} ₽\nЗапись: {booking.id}',
            'start': {'dateTime': iso(booking.start)}, 'end': {'dateTime': iso(booking.end)},
            'transparency': 'opaque'})
        self.ensure_break(master, booking)

    def ensure_break(self, master, booking):
        if booking.busy_until <= booking.end:
            self.delete(master, 'b' + booking.id)
            return
        self.put(master, 'b' + booking.id, {'summary': 'Перерыв после приёма',
            'start': {'dateTime': iso(booking.end)}, 'end': {'dateTime': iso(booking.busy_until)},
            'transparency': 'opaque'})

    def delete_booking(self, master, booking):
        self.delete(master, 'a' + booking.id)
        self.delete(master, 'b' + booking.id)

    def ensure_closure(self, master, closure):
        self.put(master, 'c' + closure.id, {'summary': 'Запись закрыта',
                 'start': {'dateTime': iso(closure.start)}, 'end': {'dateTime': iso(closure.end)},
                 'transparency': 'opaque'})

    def work_hours_content(self, uid, start_ts, end_ts, summary):
        event = Event()
        event.add('uid', uid)
        event.add('dtstamp', datetime.now(timezone.utc))
        event.add('dtstart', datetime.fromtimestamp(start_ts, timezone.utc))
        event.add('dtend', datetime.fromtimestamp(end_ts, timezone.utc))
        event.add('summary', summary)
        event.add('status', 'CONFIRMED')
        content = Calendar()
        content.add('prodid', '-//Booking Bot//CalDAV//RU')
        content.add('version', '2.0')
        content.add_component(event)
        return content.to_ical().decode()

    def create_work_hours(self, master, start_ts, end_ts, summary='Рабочие часы'):
        """Create one independently editable working-hours interval."""
        uid = 'wh-' + uuid.uuid4().hex
        with self.remote() as selected:
            selected['hours'].save_event(self.work_hours_content(uid, start_ts, end_ts, summary))
        return uid

    def update_work_hours(self, master, uid, start_ts, end_ts, summary='Рабочие часы'):
        """Replace a single working-hours event while keeping its calendar UID."""
        with self.remote() as selected:
            hours = selected['hours']
            hours.get_display_name()
            try:
                event = hours.event_by_uid(uid)
            except NotFoundError:
                raise CalendarUnavailable('Рабочий интервал уже удалён из календаря.') from None
            event.data = self.work_hours_content(uid, start_ts, end_ts, summary)
            event.save()

    def list_work_hours(self, master, start_ts, end_ts):
        """List working hours events in a time range."""
        with self.remote() as selected:
            return self.read(selected['hours'], start_ts, end_ts, master.timezone)

    def delete_work_hours(self, master, uid):
        """Delete a working hours event by UID."""
        with self.remote() as selected:
            try:
                obj = selected['hours'].event_by_uid(uid)
                obj.delete()
                return True
            except Exception:
                return False
