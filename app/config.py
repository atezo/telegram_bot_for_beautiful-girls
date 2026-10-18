import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass
class Config:
    database_url: str = 'sqlite:///booking.db'
    bot_token: str = ''
    bot_username: str = ''
    master_id: int = 0
    master_name: str = 'Мастер'
    timezone: str = 'Europe/Moscow'
    approval_minutes: int = 120
    break_minutes: int = 20
    lead_minutes: int = 60
    step_minutes: int = 30
    overtime: bool = True
    caldav_url: str = 'https://caldav.yandex.ru'
    caldav_username: str = ''
    caldav_password: str = ''
    hours_calendar: str = 'Рабочие часы'
    bookings_calendar: str = 'Записи'
    horizon_days: int = 60

    @classmethod
    def load(cls):
        path = os.getenv('CONFIG_FILE')
        values = {}
        if path:
            with Path(path).open('rb') as source:
                values = tomllib.load(source)
        known = {f.name for f in fields(cls)}
        if set(values) - known:
            raise ValueError('Неизвестные параметры config.toml: ' + ', '.join(sorted(set(values) - known)))
        for field in fields(cls):
            value = os.getenv(field.name.upper())
            if value is None:
                continue
            if field.type is int:
                value = int(value)
            elif field.type is bool:
                if value.lower() not in ('true', 'false', '1', '0', 'yes', 'no'):
                    raise ValueError(f'{field.name}: используйте true или false')
                value = value.lower() in ('true', '1', 'yes')
            values[field.name] = value
        result = cls(**values)
        result.bot_username = result.bot_username.lstrip('@')
        return result

    def validate(self):
        if not all((self.bot_token, self.bot_username, self.master_id,
                    self.caldav_username, self.caldav_password)):
            raise ValueError('Заполните BOT_TOKEN, BOT_USERNAME, MASTER_ID, CALDAV_USERNAME, CALDAV_PASSWORD')
        ZoneInfo(self.timezone)
        for key, lo, hi in [('approval_minutes', 1, 10080), ('break_minutes', 0, 240),
                            ('lead_minutes', 0, 10080), ('step_minutes', 5, 120)]:
            value = getattr(self, key)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f'{key}: допустимо {lo}–{hi}')
        if type(self.master_id) is not int or self.master_id <= 0 or type(self.overtime) is not bool:
            raise ValueError('Проверьте master_id и overtime')
        if self.horizon_days != 60:
            raise ValueError('В этой версии horizon_days должен быть 60')
        if 1440 % self.step_minutes or not 1 <= len(self.master_name) <= 80:
            raise ValueError('Проверьте step_minutes и master_name')
        if self.hours_calendar == self.bookings_calendar:
            raise ValueError('Для расписания и записей нужны два разных календаря')
        from urllib.parse import urlparse
        parsed = urlparse(self.caldav_url)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1')):
            raise ValueError('Для CalDAV используйте HTTPS (кроме локальных тестов)')
