"""Тексты клиентского бота: HTML вместо Markdown, значения экранируются, ссылки на чаты принимаются только текстом."""
import asyncio
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.methods import EditMessageText, SendMessage

import client_bot
import client_bots
from fakes import FakeDb, assert_valid_telegram_html, callback_update, make_bot, message_update

OWNER = 100
NASTY = "Чат_с *звёздой* [скобки] <b>&amp;"       # символы, ломающие разметку Markdown
NASTY_ESCAPED = "Чат_с *звёздой* [скобки] &lt;b&gt;&amp;amp;"


@pytest.fixture
def setup(monkeypatch):
    db = FakeDb(owner_id=OWNER)
    monkeypatch.setattr(client_bot, "db", db)
    monkeypatch.setattr(client_bot.config, "ADMIN_IDS", "")
    bot, session = make_bot()
    return db, bot, session


def feed(bot, *updates):
    async def run():
        for update in updates:
            await client_bots.dp.feed_update(bot, update, client_id=7)

    asyncio.run(run())


def test_all_messages_of_client_bot_use_html(setup):
    db, bot, session = setup
    db.chats = [{"chat_id": -1001, "title": "Чат", "is_approved": True}]
    db.keywords = [{"id": 1, "keyword": "фотограф"}]
    updates = [message_update(1, OWNER, "/start", username="owner", name="Owner")]
    updates += [callback_update(10 + i, OWNER, data) for i, data in enumerate(
        ["client_status", "client_chats", "add_chat", "client_keywords", "list_kw_del", "add_keyword", "client_help", "client_back"])]
    updates.append(message_update(30, OWNER, "🏠 Меню"))
    feed(bot, *updates)

    methods = [c for c in session.calls if isinstance(c, (SendMessage, EditMessageText))]
    assert len(methods) >= 10
    # Тексты с тегами идут с parse_mode="HTML", Markdown нигде не используется
    for m in methods:
        assert m.parse_mode not in ("Markdown", "MarkdownV2"), m.text
        if "<b>" in m.text or "<i>" in m.text or "<code>" in m.text:
            assert m.parse_mode == "HTML", m.text
            assert_valid_telegram_html(m.text)
    assert not any("**" in m.text for m in methods)


def test_chat_titles_with_markdown_characters_are_escaped(setup):
    db, bot, session = setup
    db.chats = [{"chat_id": -1001, "title": NASTY, "is_approved": True}]
    feed(bot, callback_update(1, OWNER, "client_chats"))
    [edit] = session.sent(EditMessageText)
    assert edit.parse_mode == "HTML"
    assert_valid_telegram_html(edit.text)
    assert NASTY_ESCAPED in edit.text
    assert "<b>&amp;" not in edit.text  # чужой тег не прошёл


def test_keywords_with_markdown_characters_are_escaped(setup):
    db, bot, session = setup
    db.keywords = [{"id": 1, "keyword": "кот_и_пёс *x* [y] <z>"}]
    feed(bot, callback_update(1, OWNER, "client_keywords"))
    [edit] = session.sent(EditMessageText)
    assert edit.parse_mode == "HTML"
    assert "<code>кот_и_пёс *x* [y] &lt;z&gt;</code>" in edit.text


def test_added_keywords_are_confirmed_in_html(setup):
    db, bot, session = setup

    async def add_client_keyword(client_id, keyword):
        return True

    db.add_client_keyword = add_client_keyword
    feed(bot, callback_update(1, OWNER, "add_keyword"), message_update(2, OWNER, "кот_и_пёс, <b>x</b>, фотографа"))
    [answer] = session.sent(SendMessage)
    assert answer.parse_mode == "HTML"
    assert "<b>кот_и_пёс</b>" in answer.text
    assert "&lt;b&gt;x&lt;/b&gt;" in answer.text
    assert "<b>фотографа</b> (→фотограф)" in answer.text


def test_help_shows_admin_contact_with_underscores_and_no_markdown_leftovers(setup, monkeypatch):
    db, bot, session = setup
    monkeypatch.setattr(client_bot.config, "ADMIN_CONTACT", "@a_<name>")
    feed(bot, callback_update(1, OWNER, "client_help"))
    [edit] = session.sent(EditMessageText)
    assert edit.parse_mode == "HTML"
    assert_valid_telegram_html(edit.text)
    assert "@a_&lt;name&gt;" in edit.text
    assert "{admin_contact}" not in edit.text
    lines = edit.text.splitlines()
    assert not any(line.startswith("#") or line.strip() == "---" for line in lines)


def test_help_file_is_valid_for_telegram_html():
    text = pathlib.Path(client_bot.HELP_FILE).read_text(encoding="utf-8")
    assert len(text) < 4096
    assert_valid_telegram_html(text.replace("{admin_contact}", "@abcd"))


def test_custom_help_of_client_overrides_the_file(setup):
    db, bot, session = setup
    db.client["custom_help"] = "<b>Своя справка</b> клиента"
    feed(bot, callback_update(1, OWNER, "client_help"))
    [edit] = session.sent(EditMessageText)
    assert edit.text == "<b>Своя справка</b> клиента" and edit.parse_mode == "HTML"


def test_status_counts_the_later_of_trial_and_subscription(setup):
    db, bot, session = setup
    now = datetime.now(timezone.utc)
    db.client["trial_expires_at"] = now + timedelta(days=5)
    db.client["subscription_expires_at"] = now - timedelta(days=1)  # подписка давно кончилась, пробный период идёт
    feed(bot, callback_update(1, OWNER, "client_status"))
    [edit] = session.sent(EditMessageText)
    assert "Активна" in edit.text and "Истекла" not in edit.text


@pytest.fixture
def admin_notices(monkeypatch):
    notices = []

    async def fake_notify(text, reply_markup=None):
        notices.append((text, reply_markup))

    monkeypatch.setattr(client_bot, "_notify_admins", fake_notify)
    return notices


@pytest.mark.parametrize("text", [
    "привет",
    "Смотрите, какое объявление: ищу фотографа",   # текст пересланного сообщения из группы
    "t.me/chat название",
    "@ab",
    "http://example.com",
])
def test_not_a_link_is_not_sent_to_admins(setup, admin_notices, text):
    db, bot, session = setup
    feed(bot, callback_update(1, OWNER, "add_chat"), message_update(2, OWNER, text))
    assert db.requests == [] and admin_notices == []
    assert "create_pending_request" not in db.calls
    # клиент получает подсказку и может прислать ссылку ещё раз
    assert any("ссылку на чат" in c.text for c in session.sent())


def test_link_goes_to_moderation_and_state_is_cleared(setup, admin_notices):
    db, bot, session = setup
    feed(bot, callback_update(1, OWNER, "add_chat"), message_update(2, OWNER, "https://t.me/+AbC_d<e>"),
         message_update(3, OWNER, "t.me/second_chat"))
    assert db.requests == ["https://t.me/+AbC_d<e>"]  # второе сообщение уже не ввод ссылки
    [(text, markup)] = admin_notices
    assert 'href="https://t.me/+AbC_d&lt;e&gt;"' in text and "&lt;e&gt;</a>" in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert callbacks == ["req_approve_1", "req_reject_1"]
    answer = [c for c in session.sent() if "модерацию" in c.text][0]
    assert answer.parse_mode == "HTML"


def test_username_goes_to_moderation(setup, admin_notices):
    db, bot, session = setup
    feed(bot, callback_update(1, OWNER, "add_chat"), message_update(2, OWNER, "@abcd"))
    assert db.requests == ["@abcd"]
    assert "<code>@abcd</code>" in admin_notices[0][0]


def test_rejection_warning_escapes_name_of_the_requester(setup, admin_notices):
    db, bot, session = setup

    async def rejected(chat_link):
        return {"id": 4, "display_name": "<i>Имя_клиента</i>"}

    db.check_rejection_history = rejected
    feed(bot, callback_update(1, OWNER, "add_chat"), message_update(2, OWNER, "@abcd"))
    text = admin_notices[0][0]
    assert_valid_telegram_html(text)
    assert "&lt;i&gt;Имя_клиента&lt;/i&gt;" in text and "запрос #4" in text


def test_welcome_escapes_the_name(setup):
    db, bot, session = setup
    feed(bot, message_update(1, OWNER, "/start", name="Клиент <Тест>", username="owner"))
    welcome = session.sent()[-1]
    assert "Клиент &lt;Тест&gt;" in welcome.text and welcome.parse_mode == "HTML"


@pytest.mark.parametrize("name", ["client_bot.py", "admin_bot.py", "notifier.py", "client_bots.py"])
def test_legacy_markdown_is_not_used(name):
    source = (pathlib.Path(__file__).parent.parent / name).read_text(encoding="utf-8")
    assert "Markdown" not in source
