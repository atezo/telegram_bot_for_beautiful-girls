from datetime import datetime, date, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from icalendar import Event
from caldav.lib.error import AuthorizationError, NotFoundError
from app.calendar import CalDAVCalendar, event_time


def test_all_day_and_floating_events_use_master_timezone(env):
    calendar = CalDAVCalendar(env[5])
    e = Event()
    e.add('uid', 'test')
    e.add('dtstart', date(2030, 1, 1))
    data = calendar.convert(e, 'Europe/Moscow')
    assert event_time(data['end'], 'UTC') - event_time(data['start'], 'UTC') == 86400
    assert event_time(data['start'], 'UTC') == event_time({'dateTime': '2030-01-01T00:00:00+03:00'}, 'UTC')


def test_missing_calendar_is_not_treated_as_deleted_appointment(env):
    calendar = CalDAVCalendar(env[5])
    remote = Mock()
    remote.get_display_name.side_effect = NotFoundError('missing collection')
    with pytest.raises(NotFoundError):
        calendar.lookup(remote, 'uid')
    remote.event_by_uid.assert_not_called()


def test_only_missing_uid_is_treated_as_absent_event(env):
    calendar = CalDAVCalendar(env[5])
    remote = Mock()
    remote.event_by_uid.side_effect = NotFoundError('missing uid')
    assert calendar.lookup(remote, 'uid') is None
    remote.event_by_uid.side_effect = AuthorizationError('denied')
    with pytest.raises(AuthorizationError):
        calendar.lookup(remote, 'uid')
