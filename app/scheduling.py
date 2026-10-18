from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Interval:
    start: int
    end: int
    event_id: str = ''


def overlaps(a, b):
    return a.start < b.end and b.start < a.end


def merge_hours(hours):
    merged = []
    for item in sorted(hours, key=lambda x: x.start):
        if merged and item.start <= merged[-1].end:
            merged[-1] = Interval(merged[-1].start, max(merged[-1].end, item.end))
        else:
            merged.append(item)
    return merged


def allowed(start, minutes, break_minutes, hours, busy, overtime):
    end = start + minutes * 60
    interval = Interval(start, end + break_minutes * 60)
    shift = next((h for h in merge_hours(hours)
                  if h.start <= start < h.end and (overtime or end <= h.end)), None)
    if shift is None or any(overlaps(interval, b) for b in busy):
        return None
    return shift.end


def slots(day, tz, minutes, break_minutes, step, hours, busy, overtime, earliest):
    zone = ZoneInfo(tz)
    start = datetime.combine(day, datetime.min.time(), zone)
    stop = start + timedelta(days=1)
    # UTC stepping avoids nonexistent/duplicate local times at DST transitions.
    cursor = int(start.timestamp())
    result = []
    while cursor < int(stop.timestamp()):
        if cursor >= earliest and allowed(cursor, minutes, break_minutes, hours, busy, overtime):
            result.append(cursor)
        cursor += step * 60
    return result
