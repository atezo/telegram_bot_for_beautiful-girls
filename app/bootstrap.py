from sqlalchemy import select
from .db import Master


def configure_master(db, config):
    """One container database belongs to exactly one master and one bot."""
    with db.transaction() as s:
        masters = list(s.scalars(select(Master).with_for_update()))
        if len(masters) > 1 or (masters and masters[0].telegram_id != config.master_id):
            raise ValueError('Эта база принадлежит другому мастеру. Используйте отдельный контейнер и том.')
        master = masters[0] if masters else Master(telegram_id=config.master_id)
        if not masters:
            s.add(master)
        # Config supplies the initial profile only. Afterwards, the master manages these
        # values in the bot and they remain effective across container restarts.
        if not masters:
            master.name = config.master_name
            for name in ('timezone', 'approval_minutes', 'break_minutes', 'lead_minutes', 'step_minutes', 'overtime'):
                setattr(master, name, getattr(config, name))
        master.hours_calendar = config.hours_calendar
        master.bookings_calendar = config.bookings_calendar
        s.flush()
        return master.id
