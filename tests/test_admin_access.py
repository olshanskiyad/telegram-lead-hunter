"""Бот-админка: админам всё, постороннему приветствие на /start и регистрация по токену, остальное молча игнорируется."""
import asyncio

import pytest

import admin_bot
import client_bots
from fakes import FakeDb, assert_valid_telegram_html, callback_update, make_bot, message_update

ADMIN = 9
STRANGER = 555
TOKEN = "12345:abcDEF_-xyz"


class AdminDb(FakeDb):
    """База для регистрации клиентов."""

    def __init__(self):
        super().__init__()
        self.clients = {}

    async def get_client_by_telegram_id(self, telegram_id):
        self.calls.append("get_client_by_telegram_id")
        return self.clients.get(telegram_id)

    async def add_client(self, telegram_id, bot_token, display_name="", username=""):
        self.calls.append("add_client")
        self.clients[telegram_id] = {"id": 5, "telegram_id": telegram_id, "bot_token": bot_token}
        return 5


@pytest.fixture
def setup(monkeypatch):
    db = AdminDb()
    monkeypatch.setattr(admin_bot, "db", db)
    monkeypatch.setattr(admin_bot.config, "ADMIN_IDS", str(ADMIN))
    monkeypatch.setattr(admin_bot.config, "SELF_REGISTRATION", False)
    monkeypatch.setattr(admin_bot.config, "ADMIN_CONTACT", "@a_<x>")
    bot, session = make_bot()
    return db, bot, session


def feed(bot, *updates):
    async def run():
        for update in updates:
            await admin_bot.dp.feed_update(bot, update)

    asyncio.run(run())


def test_stranger_start_explains_that_registration_is_closed(setup):
    db, bot, session = setup
    feed(bot, message_update(1, STRANGER, "/start"))
    [greeting] = session.sent()
    assert greeting.parse_mode == "HTML" and greeting.reply_markup is None
    assert_valid_telegram_html(greeting.text)
    assert "Регистрация клиентов закрыта" in greeting.text
    assert "@a_&lt;x&gt;" in greeting.text  # контакт администратора, экранированный для HTML
    assert db.calls == []


def test_stranger_start_explains_how_to_register_when_it_is_open(setup, monkeypatch):
    db, bot, session = setup
    monkeypatch.setattr(admin_bot.config, "SELF_REGISTRATION", True)
    feed(bot, message_update(1, STRANGER, "/start"))
    [greeting] = session.sent()
    assert "@BotFather" in greeting.text and "токен" in greeting.text
    assert "закрыта" not in greeting.text and "**" not in greeting.text


@pytest.mark.parametrize("text", ["/start@some_bot", "/start ref123", "/START"])
def test_start_in_other_spellings_gets_the_greeting_too(setup, text):
    db, bot, session = setup
    feed(bot, message_update(1, STRANGER, text))
    assert len(session.sent()) == 1


@pytest.mark.parametrize("text", ["привет", "/menu", "🏠 Меню", "/list_clients", "/extend 555 3650", "/add_client 1 2:abc"])
def test_stranger_commands_and_text_are_ignored(setup, text):
    db, bot, session = setup
    feed(bot, message_update(1, STRANGER, text))
    assert session.calls == [] and db.calls == []


@pytest.mark.parametrize("data", ["admin_clients", "admin_requests", "req_approve_1", "defaults_del_chat", "admin_toggle_sys"])
def test_stranger_buttons_are_ignored(setup, data):
    db, bot, session = setup
    feed(bot, callback_update(1, STRANGER, data))
    assert session.calls == [] and db.calls == []


def test_update_without_sender_is_ignored(setup):
    db, bot, session = setup
    feed(bot, message_update(1, ADMIN, "/start", sender=False))
    assert session.calls == []


def test_admin_gets_the_menu_not_the_greeting(setup):
    db, bot, session = setup
    feed(bot, message_update(1, ADMIN, "/start"))
    texts = [c.text for c in session.sent()]
    assert any("АДМИНКА" in t for t in texts) and not any("Регистрация" in t for t in texts)


def test_admin_unknown_text_gets_a_hint(setup):
    db, bot, session = setup
    feed(bot, message_update(1, ADMIN, "что-то непонятное"))
    [answer] = session.sent()
    assert "не распознана" in answer.text


def test_stranger_token_is_refused_while_registration_is_closed(setup):
    db, bot, session = setup
    feed(bot, message_update(1, STRANGER, TOKEN))
    [answer] = session.sent()
    assert "Регистрация закрыта" in answer.text
    assert db.calls == []  # в базу ничего не записано


def test_stranger_registers_when_it_is_open_and_gets_html_reply(setup, monkeypatch):
    db, bot, session = setup
    monkeypatch.setattr(admin_bot.config, "SELF_REGISTRATION", True)
    started = []

    async def fake_register(database, record):
        started.append(record["bot_token"])

    monkeypatch.setattr(client_bots, "register_new_client_bot", fake_register)
    feed(bot, message_update(1, STRANGER, TOKEN, name="Клиент <Тест>"))
    assert "add_client" in db.calls and started == [TOKEN]
    reply = [c for c in session.sent() if "Регистрация успешна" in c.text][0]
    assert_valid_telegram_html(reply.text)
    assert reply.parse_mode == "HTML" and "<b>Регистрация успешна!</b>" in reply.text and "**" not in reply.text


def test_admin_registers_even_when_self_registration_is_off(setup, monkeypatch):
    db, bot, session = setup
    monkeypatch.setattr(client_bots, "register_new_client_bot", lambda *a, **k: asyncio.sleep(0))
    feed(bot, message_update(1, ADMIN, TOKEN))
    assert "add_client" in db.calls


def test_error_text_is_escaped_in_html_messages():
    import inspect
    source = inspect.getsource(admin_bot)
    # исключения подставляются в HTML-сообщения только через html.escape
    assert "{e}\\n\\nПопробуйте" not in source and "{e}\\n\\nВозможно" not in source
