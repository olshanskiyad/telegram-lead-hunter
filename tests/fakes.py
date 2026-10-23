"""Подставные Telegram и база для тестов: сеть, настоящий Telegram и PostgreSQL не нужны."""
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Chat, Message, Update

BOT_TOKEN = "12345:abcDEF_-xyz"


class RecordingSession(BaseSession):
    """Сессия бота без сети: запоминает вызванные методы Bot API и отвечает заглушками."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=1, type="private"), text=method.text)
        return True

    def sent(self, kind=SendMessage):
        """Вызовы одного вида (по умолчанию отправленные сообщения)."""
        return [c for c in self.calls if isinstance(c, kind)]


def make_bot():
    session = RecordingSession()
    return Bot(token=BOT_TOKEN, session=session), session


def message_update(update_id, user_id, text, name="Test", sender=True, username=None):
    entities = []
    if text.startswith("/"):
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    message = {
        "message_id": update_id,
        "date": int(time.time()),
        "chat": {"id": user_id, "type": "private", "first_name": name},
        "text": text,
        "entities": entities,
    }
    if sender:  # без отправителя приходят, например, посты каналов
        message["from"] = {"id": user_id, "is_bot": False, "first_name": name}
        if username:
            message["from"]["username"] = username
    return Update.model_validate({"update_id": update_id, "message": message})


def callback_update(update_id, user_id, data, name="Test"):
    return Update.model_validate({
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": user_id, "is_bot": False, "first_name": name},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": update_id,
                "date": int(time.time()),
                "chat": {"id": user_id, "type": "private", "first_name": name},
                "from": {"id": 1, "is_bot": True, "first_name": "bot"},
                "text": "menu",
            },
        },
    })


class FakeDb:
    """База клиентского бота в памяти. Любой незнакомый метод записывается в calls и возвращает None."""

    def __init__(self, owner_id=100, active=True, chats=None, keywords=None):
        now = datetime.now(timezone.utc)
        self.client = {
            "telegram_id": owner_id,
            "bot_token": BOT_TOKEN,
            "username": "owner",
            "display_name": "Owner",
            "custom_help": None,
            "trial_expires_at": None,
            "subscription_expires_at": now + timedelta(days=10),
        }
        self.active = active
        self.chats = chats or []
        self.keywords = keywords or []
        self.calls = []
        self.requests = []

    async def get_client_info(self, client_id):
        self.calls.append("get_client_info")
        return dict(self.client)

    async def is_subscription_active(self, client_id):
        self.calls.append("is_subscription_active")
        return self.active

    async def get_client_chats(self, client_id):
        self.calls.append("get_client_chats")
        return list(self.chats)

    async def get_client_keywords(self, client_id):
        self.calls.append("get_client_keywords")
        return list(self.keywords)

    async def get_today_leads_count(self, client_id=None):
        self.calls.append("get_today_leads_count")
        return 0

    async def check_rejection_history(self, chat_link):
        self.calls.append("check_rejection_history")
        return None

    async def create_pending_request(self, client_id, chat_link):
        self.calls.append("create_pending_request")
        self.requests.append(chat_link)
        return len(self.requests)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        async def record(*args, **kwargs):
            self.calls.append(name)

        return record


class _TagChecker(HTMLParser):
    ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote"}  # теги из подмножества HTML, которое понимает Telegram

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in self.ALLOWED, f"тег <{tag}> Telegram не поддерживает"
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, f"закрывающий </{tag}> без пары"


def assert_valid_telegram_html(text):
    """Проверка текста для parse_mode="HTML": теги из разрешённых и парные, лишних <, > и & нет."""
    checker = _TagChecker()
    checker.feed(text)
    checker.close()
    assert not checker.stack, f"не закрыты теги {checker.stack}"
    plain = re.sub(r"<[^>]*>", "", text)
    plain = re.sub(r"&(lt|gt|amp|quot);", "", plain)
    assert not any(ch in plain for ch in "<>&"), f"неэкранированные символы в тексте: {text!r}"
