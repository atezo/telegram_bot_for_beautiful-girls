import logging
import fcntl
import threading
import time
from sqlalchemy import text
from .bot import Bot
from .calendar import CalDAVCalendar
from .bootstrap import configure_master
from .config import Config
from .db import Cursor, Database
from .domain import BookingService
from .telegram import Telegram
from .worker import Worker

log = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    config = Config.load()
    config.validate()
    db = Database(config.database_url)
    instance_lock = None
    if db.engine.dialect.name == 'sqlite':
        instance_lock = open(db.engine.url.database + '.lock', 'a')
        try:
            fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Bot process already running for this database') from None
    db.initialize()
    # Keep one poller/worker leader. A second process exits instead of replaying Telegram updates.
    leader = None
    if db.engine.dialect.name == 'postgresql':
        leader = db.engine.connect()
        if not leader.scalar(text('SELECT pg_try_advisory_lock(725418203)')):
            raise RuntimeError('Bot process already running')
        leader.commit()
    configure_master(db, config)
    calendar = CalDAVCalendar(config)
    telegram = Telegram(config.bot_token)
    service = BookingService(calendar)
    bot = Bot(config, service)
    worker = Worker(db, calendar, service, telegram)
    telegram.call('deleteWebhook', {'drop_pending_updates': False})
    telegram.call('setMyCommands', {'commands': [
        {'command': 'start', 'description': 'Записаться'},
        {'command': 'my', 'description': 'Мои записи'},
        {'command': 'help', 'description': 'Помощь'},
        {'command': 'cancel', 'description': 'Прервать текущий ввод'}]})

    def background():
        sync_at = 0
        while True:
            try:
                if time.monotonic() >= sync_at:
                    worker.tick()
                    sync_at = time.monotonic() + 60
                else:
                    worker.deliver()
            except Exception as exc:
                log.error('Background failure: %s', type(exc).__name__)
            time.sleep(2)

    threading.Thread(target=background, daemon=True).start()
    failures = 0
    while True:
        try:
            if leader is not None:
                # Lost leadership terminates the process rather than continuing as a second writer.
                try:
                    leader.execute(text('SELECT 1'))
                    leader.commit()
                except Exception:
                    raise SystemExit('Lost database leadership; restart required')
            with db.transaction() as s:
                cursor = s.get(Cursor, 'telegram')
                offset = cursor.value if cursor else 0
            for update in telegram.updates(offset):
                with db.transaction() as s:
                    cursor = s.get(Cursor, 'telegram')
                    if not cursor:
                        cursor = Cursor(key='telegram', value=0)
                        s.add(cursor)
                    if update['update_id'] < cursor.value:
                        continue
                    bot.handle(s, update)
                    cursor.value = update['update_id'] + 1
                if query := update.get('callback_query'):
                    try:
                        telegram.answer(query['id'])
                    except Exception:
                        pass
            failures = 0
        except Exception as exc:
            failures += 1
            log.error('Polling failure: %s', type(exc).__name__)
            time.sleep(min(30, 2 ** min(failures, 5)))


if __name__ == '__main__':
    main()
