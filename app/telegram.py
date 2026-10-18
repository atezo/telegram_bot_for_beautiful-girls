import requests


class TelegramError(Exception):
    def __init__(self, code, retry_after=0):
        self.code, self.retry_after = code, retry_after
        super().__init__(f'Telegram HTTP {code}')


class Telegram:
    def __init__(self, token):
        self.base = f'https://api.telegram.org/bot{token}/'

    def call(self, method, payload):
        try:
            response = requests.post(self.base + method, json=payload, timeout=40 if method == 'getUpdates' else 15)
            data = response.json()
        except Exception:
            raise TelegramError(0) from None
        if not data.get('ok'):
            raise TelegramError(data.get('error_code', response.status_code),
                                data.get('parameters', {}).get('retry_after', 0))
        return data['result']

    def send(self, chat_id, text, keyboard=None):
        payload = {'chat_id': chat_id, 'text': text, 'link_preview_options': {'is_disabled': True}}
        if keyboard:
            payload['reply_markup'] = {'inline_keyboard': keyboard}
        return self.call('sendMessage', payload)

    def updates(self, offset):
        return self.call('getUpdates', {'offset': offset, 'timeout': 25, 'allowed_updates': ['message', 'callback_query']})

    def answer(self, callback_id):
        return self.call('answerCallbackQuery', {'callback_query_id': callback_id})
