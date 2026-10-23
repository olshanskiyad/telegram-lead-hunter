"""Фильтры, которым не нужны сеть, Telegram и база данных (lead_filters)."""
import pathlib
from types import SimpleNamespace

import pytest

from lead_filters import (
    NOT_LEAD_MARKER,
    is_broadcast_channel,
    is_not_lead,
    is_start_command,
    looks_like_bot_token,
    looks_like_chat_link,
    message_link,
)


@pytest.mark.parametrize("answer", [
    "[НЕ ЛИД]",
    "[не лид]",
    "  [НЕ ЛИД]\n",
    "[Не лид] обычный разговор",
])
def test_ai_marker_means_not_a_lead(answer):
    assert is_not_lead(answer)


@pytest.mark.parametrize("answer", [
    "Здравствуйте! Свободен в субботу, пришлю примеры работ в личные сообщения.",
    "Не лидирую, но могу помочь",
    "",
])
def test_ready_reply_is_a_lead(answer):
    assert not is_not_lead(answer)


def test_niche_prompt_in_migration_uses_the_same_marker():
    sql = (pathlib.Path(__file__).parent.parent / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    assert NOT_LEAD_MARKER in sql


@pytest.mark.parametrize("chat, expected", [
    (SimpleNamespace(broadcast=True, megagroup=False), True),    # канал-трансляция
    (SimpleNamespace(broadcast=True, megagroup=None), True),     # флаг не заполнен
    (SimpleNamespace(broadcast=False, megagroup=True), False),   # супергруппа
    (SimpleNamespace(broadcast=False, megagroup=False), False),  # обычная группа
    (SimpleNamespace(title="Личный чат"), False),                # у пользователя нет этих полей
])
def test_only_broadcast_channels_are_skipped(chat, expected):
    assert is_broadcast_channel(chat) is expected


@pytest.mark.parametrize("text", [
    "12345:abcDEF_-xyz",
    "  12345:abcDEF_-xyz \n",
])
def test_bot_token_shape_is_recognised(text):
    assert looks_like_bot_token(text)


@pytest.mark.parametrize("text", [
    "привет",
    "12345",
    "12345:",
    ":abcdef",
    "12345:abc def",
    "мой токен 12345:abcDEF",
    "",
])
def test_other_messages_are_not_tokens(text):
    assert not looks_like_bot_token(text)


def test_message_without_text_is_not_a_token():
    assert not looks_like_bot_token(None)


def test_message_link_for_supergroup_drops_the_100_prefix():
    assert message_link(-100123, 7) == "https://t.me/c/123/7"


@pytest.mark.parametrize("text", [
    "t.me/some_chat",
    "https://t.me/some_chat",
    "https://t.me/+AbCdEfGh",
    "https://t.me/joinchat/AbCdEfGh",
    "telegram.me/some_chat",
    "@abcd",
    "  @abcd  ",
])
def test_chat_links_are_accepted(text):
    assert looks_like_chat_link(text)


@pytest.mark.parametrize("text", [
    "",
    "привет",
    "t.me/",
    "@ab",
    "@1chat",
    "t.me/some_chat название чата",
    "ищу фотографа t.me/some_chat",
    "http://example.com/t.me/some_chat",
    None,
])
def test_other_texts_are_not_chat_links(text):
    assert not looks_like_chat_link(text)


@pytest.mark.parametrize("text", ["/start", "/START", "/start@some_bot", "/start ref123", "  /start  "])
def test_start_command_in_all_spellings(text):
    assert is_start_command(text)


@pytest.mark.parametrize("text", ["/starting", "start", "/menu", "привет /start", "", None])
def test_other_texts_are_not_start(text):
    assert not is_start_command(text)
