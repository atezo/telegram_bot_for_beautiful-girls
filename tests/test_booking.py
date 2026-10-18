from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from sqlalchemy import select
import pytest

from app.calendar import iso
from app.db import Booking, Closure, Master, Outbox, Service, now
from app.domain import UserError
from app.scheduling import Interval, allowed
from app.telegram import TelegramError
from conftest import reserve, approve


def test_overtime_and_break_boundaries():
    hours = [Interval(10 * 3600, 18 * 3600)]
    assert allowed(17 * 3600, 90, 20, hours, [], True) == 18 * 3600
    assert allowed(17 * 3600, 90, 20, hours, [], False) is None
    assert allowed(18 * 3600, 30, 0, hours, [], True) is None
    assert allowed(14 * 3600, 90, 20, hours, [Interval(15 * 3600 + 50 * 60, 18 * 3600)], False)
    assert allowed(14 * 3600, 90, 20, hours, [Interval(15 * 3600 + 49 * 60, 18 * 3600)], False) is None


def test_pending_reserves_break_and_approval_creates_both_events(env):
    db, cal, tg, service, worker, _, start = env
    bid = reserve(env)
    assert not cal.events_data
    with pytest.raises(UserError):
        reserve(env, client=301, start=start + 100 * 60)
    other = reserve(env, client=301, start=start + 110 * 60)
    approve(env, bid)
    with db.transaction() as s:
        assert s.get(Booking, bid).status == 'confirmed'
        assert s.get(Booking, other).status == 'pending'
    assert (1, 'a' + bid) in cal.events_data
    assert (1, 'b' + bid) in cal.events_data
    assert any('Запись подтверждена!' in text for _, text, _ in tg.messages)


def test_two_simultaneous_clients_cannot_reserve_same_time(env):
    barrier = Barrier(2)
    def run(uid):
        barrier.wait()
        try:
            return reserve(env, client=uid)
        except UserError:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, [300, 301]))
    assert sum(x is not None for x in results) == 1


def test_tenants_and_permission_checks(env):
    bid = reserve(env)
    reserve(env, mid=2, client=301)
    db, _, _, service, _, _, _ = env
    with db.transaction() as s:
        with pytest.raises(UserError):
            service.decide(s, bid, 200, True)
        with pytest.raises(UserError):
            service.cancel(s, bid, 999)


def test_expiry_releases_slot_and_stale_approval_fails(env):
    bid = reserve(env)
    db, _, tg, service, worker, _, _ = env
    with db.transaction() as s:
        s.get(Booking, bid).expires = now() - 1
    worker.tick()
    with db.transaction() as s:
        assert s.get(Booking, bid).status == 'expired'
        with pytest.raises(UserError):
            service.decide(s, bid, 100, True)
    reserve(env, client=301)
    assert any('Срок подтверждения истёк' in text for _, text, _ in tg.messages)


def test_settings_and_prices_are_snapshotted(env):
    bid = reserve(env)
    db, _, _, _, _, _, _ = env
    with db.transaction() as s:
        s.get(Master, 1).break_minutes = 60
        s.get(Master, 1).approval_minutes = 1
        s.get(Service, 1).price = 300000
    with db.transaction() as s:
        b = s.get(Booking, bid)
        assert b.services[0]['price'] == 250000
        assert b.busy_until - b.end == 20 * 60
        assert b.expires > now() + 60


def test_rejected_move_keeps_original(env):
    db, cal, _, service, worker, _, start = env
    old = reserve(env)
    approve(env, old)
    new = reserve(env, start=start + 3600, replaces=old)
    with db.transaction() as s:
        service.decide(s, new, 100, False)
    worker.tick()
    with db.transaction() as s:
        assert s.get(Booking, old).status == 'confirmed'
        assert s.get(Booking, new).status == 'rejected'
    assert (1, 'a' + old) in cal.events_data


def test_approved_move_and_retry_after_partial_remote_delete(env):
    db, cal, tg, service, worker, _, start = env
    old = reserve(env)
    approve(env, old)
    new = reserve(env, start=start + 3600, replaces=old)
    with db.transaction() as s:
        service.decide(s, new, 100, True)
    cal.fail_delete_once = True
    worker.tick()
    worker.tick()
    worker.tick()
    with db.transaction() as s:
        assert s.get(Booking, old).status == 'replaced'
        assert s.get(Booking, new).status == 'confirmed'
    assert (1, 'a' + old) not in cal.events_data
    assert (1, 'a' + new) in cal.events_data
    assert sum('Перенос подтверждён!' in text for _, text, _ in tg.messages) == 1


def test_partial_create_no_false_confirmation_or_duplicate_event(env):
    db, cal, tg, service, worker, _, _ = env
    bid = reserve(env)
    with db.transaction() as s:
        service.decide(s, bid, 100, True)
    cal.fail_break_once = True
    worker.tick()
    with db.transaction() as s:
        assert s.get(Booking, bid).status == 'confirming'
    assert not any('Запись подтверждена!' in text for _, text, _ in tg.messages)
    worker.tick()
    assert len(cal.events_data) == 2
    assert sum('Запись подтверждена!' in text for _, text, _ in tg.messages) == 1


def test_new_external_conflict_rejects_approval(env):
    db, cal, tg, service, worker, _, start = env
    bid = reserve(env)
    cal.events_data[(1, 'manual')] = {'start': {'dateTime': iso(start)}, 'end': {'dateTime': iso(start + 3600)}}
    approve(env, bid)
    with db.transaction() as s:
        assert s.get(Booking, bid).status == 'rejected'
    assert not any('Запись подтверждена!' in text for _, text, _ in tg.messages)


def test_calendar_delete_cancels_and_notifies(env):
    db, cal, tg, service, worker, _, _ = env
    bid = reserve(env)
    approve(env, bid)
    del cal.events_data[(1, 'a' + bid)]
    worker.tick()
    with db.transaction() as s:
        assert s.get(Booking, bid).status == 'cancelled'
    assert (1, 'b' + bid) not in cal.events_data
    assert any(chat == 300 and 'Запись отменена в календаре' in text for chat, text, _ in tg.messages)


def test_calendar_move_updates_break_and_notifies_once(env):
    db, cal, tg, service, worker, _, start = env
    bid = reserve(env)
    approve(env, bid)
    cal.events_data[(1, 'a' + bid)] = {'start': {'dateTime': iso(start + 3600)},
                                     'end': {'dateTime': iso(start + 9000)}}
    worker.tick()
    worker.tick()
    with db.transaction() as s:
        assert s.get(Booking, bid).start == start + 3600
        assert s.get(Booking, bid).busy_until == start + 10200
    assert sum(chat == 300 and 'изменил время' in text for chat, text, _ in tg.messages) == 1


def test_calendar_outage_does_not_cancel_existing_booking(env):
    db, cal, _, _, worker, _, _ = env
    bid = reserve(env)
    approve(env, bid)
    cal.fail = True
    worker.tick()
    with db.transaction() as s:
        assert s.get(Booking, bid).status == 'confirmed'


def test_client_cancels_original_also_cancels_move(env):
    db, cal, tg, service, worker, _, start = env
    old = reserve(env)
    approve(env, old)
    new = reserve(env, start=start + 3600, replaces=old)
    with db.transaction() as s:
        service.cancel(s, old, 300)
    worker.tick()
    with db.transaction() as s:
        assert s.get(Booking, old).status == s.get(Booking, new).status == 'cancelled'
    assert not cal.events_data


def test_close_period_cancels_records_and_blocks_new_requests(env):
    db, cal, tg, service, worker, _, start = env
    bid = reserve(env)
    approve(env, bid)
    with db.transaction() as s:
        service.close_period(s, 1, 100, start - 60, start + 86400)
    worker.tick()
    with pytest.raises(UserError):
        reserve(env, client=301)
    with db.transaction() as s:
        assert s.get(Booking, bid).status == 'cancelled'
        assert s.scalar(select(Closure)).synced


def test_telegram_temporary_failure_retains_message(env):
    db, _, tg, _, worker, _, _ = env
    reserve(env)
    tg.error = TelegramError(429, retry_after=30)
    worker.deliver()
    with db.transaction() as s:
        items = list(s.scalars(select(Outbox)))
        assert all(not i.sent and i.next_try >= now() + 29 for i in items)
        for i in items:
            i.next_try = 0
    tg.error = None
    worker.deliver()
    assert len(tg.messages) == 2
