"""Бот клиента отвечает только владельцу и администраторам, посторонних не замечает."""
import asyncio
import inspect

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText

import client_bot
import client_bots
from fakes import FakeDb, callback_update, make_bot, message_update

OWNER = 100
ADMIN = 9
STRANGER = 555


@pytest.fixture
def setup(monkeypatch):
    """Пустая база с владельцем OWNER и администратором ADMIN; бот без сети."""
    db = FakeDb(owner_id=OWNER)
    monkeypatch.setattr(client_bot, "db", db)
    monkeypatch.setattr(client_bot.config, "ADMIN_IDS", str(ADMIN))
    bot, session = make_bot()
    return db, bot, session


def feed(bot, *updates, client_id=7):
    async def run():
        for update in updates:
            await client_bots.dp.feed_update(bot, update, client_id=client_id)

    asyncio.run(run())


# Все кнопки и команды клиентского бота: чужой не должен достучаться ни до одной
STRANGER_ACTIONS = [
    message_update(1, STRANGER, "/start"),
    message_update(2, STRANGER, "/menu"),
    message_update(3, STRANGER, "🏠 Меню"),
    message_update(4, STRANGER, "фотограф"),
    message_update(5, STRANGER, "t.me/some_chat"),
    callback_update(6, STRANGER, "client_status"),
    callback_update(7, STRANGER, "client_chats"),
    callback_update(8, STRANGER, "del_chat_12345"),
    callback_update(9, STRANGER, "add_chat"),
    callback_update(10, STRANGER, "client_keywords"),
    callback_update(11, STRANGER, "list_kw_del"),
    callback_update(12, STRANGER, "del_kw_3"),
    callback_update(13, STRANGER, "add_keyword"),
    callback_update(14, STRANGER, "client_help"),
    callback_update(15, STRANGER, "client_back"),
]


@pytest.mark.parametrize("update", STRANGER_ACTIONS, ids=lambda u: (u.message.text if u.message else u.callback_query.data))
def test_stranger_gets_nothing_and_changes_nothing(setup, update):
    db, bot, session = setup
    feed(bot, update)
    assert session.calls == []                   # ни ответа, ни правки сообщения, ни ответа на кнопку
    assert set(db.calls) <= {"get_client_info"}  # из базы смотрят только, кто владелец; ничего не пишут


def test_stranger_cannot_start_adding_a_chat_or_keyword(setup):
    db, bot, session = setup
    # Даже если бы посторонний прислал ссылку или слова, состояния ввода у него нет: запросов админам не создаётся
    feed(bot, callback_update(1, STRANGER, "add_chat"), message_update(2, STRANGER, "t.me/some_chat"),
         callback_update(3, STRANGER, "add_keyword"), message_update(4, STRANGER, "фотограф"))
    assert db.requests == []
    assert "create_pending_request" not in db.calls and "add_client_keyword" not in db.calls


def test_strangers_start_does_not_overwrite_owners_name(setup):
    db, bot, session = setup
    feed(bot, message_update(1, STRANGER, "/start", name="Чужой"))
    assert "update_client" not in db.calls


def test_owner_start_works_and_keeps_unchanged_name(setup):
    db, bot, session = setup
    feed(bot, message_update(1, OWNER, "/start", name="Owner", username="owner"))
    assert len(session.sent()) == 2
    assert "update_client" not in db.calls  # имя то же, что в базе: обновлять нечего


def test_owner_start_updates_changed_name(setup):
    db, bot, session = setup
    feed(bot, message_update(1, OWNER, "/start", name="Новое имя"))
    assert "update_client" in db.calls


def test_admin_can_open_clients_bot_but_cannot_rename_the_client(setup):
    db, bot, session = setup
    feed(bot, message_update(1, ADMIN, "/start", name="Admin"))
    texts = [c.text for c in session.sent()]
    assert any("Режим администратора" in t for t in texts)
    assert "update_client" not in db.calls


def test_admin_can_use_buttons_of_clients_bot(setup):
    db, bot, session = setup
    feed(bot, callback_update(1, ADMIN, "client_keywords"))
    assert len(session.sent(EditMessageText)) == 1


def test_expired_access_owner_is_told_stranger_is_not(setup):
    db, bot, session = setup
    db.active = False
    feed(bot, message_update(1, STRANGER, "/start"), callback_update(2, STRANGER, "client_status"))
    assert session.calls == []

    feed(bot, message_update(3, OWNER, "/start"))
    assert [("истекли" in c.text) for c in session.sent()] == [True]

    feed(bot, callback_update(4, OWNER, "client_status"))
    answers = session.sent(AnswerCallbackQuery)
    assert len(answers) == 1 and answers[0].show_alert


def test_update_without_sender_is_ignored(setup):
    db, bot, session = setup
    feed(bot, message_update(1, OWNER, "/start", sender=False))  # например, пост канала
    assert session.calls == []


def test_unknown_client_context_is_ignored(setup):
    db, bot, session = setup
    asyncio.run(client_bots.dp.feed_update(bot, message_update(1, OWNER, "/start")))  # без client_id
    assert session.calls == []


def test_both_middlewares_exist_on_the_router():
    # Защита висит на роутере клиентского бота и покрывает все его обработчики
    assert client_bot.check_sub_msg in client_bot.router.message.outer_middleware
    assert client_bot.check_sub_cb in client_bot.router.callback_query.outer_middleware


def test_client_bot_has_no_forwarded_chat_path():
    source = inspect.getsource(client_bot)
    assert "forward_origin" not in source and "пересланное" not in source


def test_access_check_uses_owner_id_from_the_database(setup):
    db, bot, session = setup
    db.client["telegram_id"] = STRANGER  # владелец бота по базе: этот пользователь
    feed(bot, message_update(1, STRANGER, "/start", name="Owner"), message_update(2, OWNER, "/start"))
    texts = [c.text for c in session.sent()]
    assert len(texts) == 2  # ответил только новому владельцу
