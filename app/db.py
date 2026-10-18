from contextlib import contextmanager
from datetime import datetime, timezone
import uuid

from sqlalchemy import (BigInteger, Boolean, ForeignKey, Integer, JSON, String, Text,
                        create_engine, event, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def uid():
    return uuid.uuid4().hex


def now():
    return int(datetime.now(timezone.utc).timestamp())


class Base(DeclarativeBase):
    pass


class Master(Base):
    __tablename__ = 'masters'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(80), default='Europe/Moscow')
    approval_minutes: Mapped[int] = mapped_column(Integer, default=120)
    break_minutes: Mapped[int] = mapped_column(Integer, default=20)
    overtime: Mapped[bool] = mapped_column(Boolean, default=True)
    step_minutes: Mapped[int] = mapped_column(Integer, default=30)
    lead_minutes: Mapped[int] = mapped_column(Integer, default=60)
    hours_calendar: Mapped[str | None] = mapped_column(Text)
    bookings_calendar: Mapped[str | None] = mapped_column(Text)
    calendar_error: Mapped[bool] = mapped_column(Boolean, default=False)


class Service(Base):
    __tablename__ = 'services'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey('masters.id'), index=True)
    name: Mapped[str] = mapped_column(String(100))
    price: Mapped[int] = mapped_column(Integer)  # kopecks, never floating point
    minutes: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


ACTIVE = ('pending', 'confirming', 'confirmed', 'canceling')


class Booking(Base):
    __tablename__ = 'bookings'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    master_id: Mapped[int] = mapped_column(ForeignKey('masters.id'), index=True)
    client_id: Mapped[int] = mapped_column(BigInteger, index=True)
    client_name: Mapped[str] = mapped_column(String(100))
    contact: Mapped[str] = mapped_column(String(150))
    services: Mapped[list] = mapped_column(JSON)
    start: Mapped[int] = mapped_column(BigInteger, index=True)
    end: Mapped[int] = mapped_column(BigInteger)
    busy_until: Mapped[int] = mapped_column(BigInteger)
    shift_end: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    expires: Mapped[int] = mapped_column(BigInteger)
    replaces: Mapped[str | None] = mapped_column(ForeignKey('bookings.id'))
    cancel_actor: Mapped[str | None] = mapped_column(String(30))
    last_error: Mapped[bool] = mapped_column(Boolean, default=False)
    external_warning: Mapped[str | None] = mapped_column(Text)


class Closure(Base):
    __tablename__ = 'closures'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    master_id: Mapped[int] = mapped_column(ForeignKey('masters.id'), index=True)
    start: Mapped[int] = mapped_column(BigInteger)
    end: Mapped[int] = mapped_column(BigInteger)
    synced: Mapped[bool] = mapped_column(Boolean, default=False)


class Conversation(Base):
    __tablename__ = 'conversations'
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Outbox(Base):
    __tablename__ = 'outbox'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    keyboard: Mapped[list | None] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try: Mapped[int] = mapped_column(BigInteger, default=0)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    failed: Mapped[bool] = mapped_column(Boolean, default=False)


class Cursor(Base):
    __tablename__ = 'cursors'
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, default=0)


class Database:
    def __init__(self, url):
        self.engine = create_engine(url, pool_pre_ping=True)
        if url.startswith('sqlite'):
            @event.listens_for(self.engine, 'connect')
            def sqlite_setup(conn, _):
                conn.isolation_level = None
                conn.execute('PRAGMA foreign_keys=ON')
                conn.execute('PRAGMA busy_timeout=30000')
            @event.listens_for(self.engine, 'begin')
            def sqlite_begin(conn):
                conn.exec_driver_sql('BEGIN IMMEDIATE')
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self):
        with self.session.begin() as session:
            yield session


def lock_master(session, master_id):
    # All scheduling writers lock the same master row before reads and writes.
    return session.scalar(select(Master).where(Master.id == master_id).execution_options(populate_existing=True).with_for_update())


def notify(session, chat_id, text, keyboard=None):
    session.add(Outbox(chat_id=chat_id, text=text, keyboard=keyboard))
