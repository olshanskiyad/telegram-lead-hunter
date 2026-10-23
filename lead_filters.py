"""Отбор сообщений без сети, Telegram и базы данных.

Подбор ключевых слов с учётом склонений (pymorphy3) и простые фильтры. Вынесено из userbot.py и ботов,
чтобы это можно было проверять тестами.
"""
import re

import pymorphy3

# Маркер, которым ИИ помечает сообщение, не являющееся заявкой (прописан в промпте ниши).
NOT_LEAD_MARKER = "[НЕ ЛИД]"

_morph = pymorphy3.MorphAnalyzer()
_WORD = re.compile(r"[\w\u0400-\u04ff]+")
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_KEYWORD_SEPARATORS = re.compile(r"[,;\n]+")
_BOT_TOKEN = re.compile(r"^\d+:[\w-]+$")
_CHAT_LINK = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,31}|(?:https?://)?(?:www\.)?(?:t|telegram)\.me/\S+")


def get_lemmas(word: str) -> set:
    """Все возможные начальные формы слова (у неоднозначных слов их несколько)."""
    return {p.normal_form for p in _morph.parse(word)}


def normal_form(word: str) -> str:
    """Самая вероятная начальная форма слова (для подсказки клиенту при добавлении ключа)."""
    parsed = _morph.parse(word)
    return parsed[0].normal_form if parsed else word


def is_cyrillic(word: str) -> bool:
    """Есть ли в слове кириллица."""
    return bool(_CYRILLIC.search(word))


def match_keywords(text: str, keywords: list) -> list:
    """Вернуть ключевые слова и фразы, которые встретились в тексте.

    Русские слова сравниваются по начальной форме, поэтому «фотографа», «фотографу» и «с фотографом» дают
    совпадение с ключом «фотограф». Во фразе из нескольких слов должны встретиться все слова, порядок и
    соседство не важны. Нерусские слова (латиница, цифры) сравниваются как есть, без учёта регистра.
    """
    words = _WORD.findall(text.lower())

    # Начальные формы всех слов сообщения собираем один раз
    msg_lemmas = set()
    for w in words:
        if is_cyrillic(w):
            msg_lemmas.update(get_lemmas(w))
        else:
            msg_lemmas.add(w)

    matched = []
    for kw in keywords:
        parts = _WORD.findall(kw.lower())
        if not parts:
            continue
        if all((get_lemmas(p) if is_cyrillic(p) else {p}) & msg_lemmas for p in parts):
            matched.append(kw)
    return matched


def parse_keywords(raw: str) -> list:
    """Разобрать ввод клиента: слова через запятую, точку с запятой или перенос строки, в нижнем регистре."""
    return [kw.strip().lower() for kw in _KEYWORD_SEPARATORS.split(raw) if kw.strip()]


def is_not_lead(ai_response: str) -> bool:
    """ИИ счёл сообщение не заявкой (ответил маркером «[НЕ ЛИД]» в любом регистре)."""
    return NOT_LEAD_MARKER in ai_response.upper()


def is_broadcast_channel(chat) -> bool:
    """Канал-трансляция (не группа): в нём пишут только администраторы, заявок там нет."""
    return bool(getattr(chat, "broadcast", False)) and not getattr(chat, "megagroup", False)


def looks_like_bot_token(text) -> bool:
    """Похоже ли сообщение на токен бота от @BotFather (так клиент регистрируется в админ-боте).

    Принимает и None: у сообщений без текста (фото, стикер) поле text пустое.
    """
    return isinstance(text, str) and bool(_BOT_TOKEN.match(text.strip()))


def looks_like_chat_link(text) -> bool:
    """Похоже ли сообщение на ссылку на чат (t.me/...) или @username: то, что клиент присылает на модерацию."""
    return isinstance(text, str) and bool(_CHAT_LINK.fullmatch(text.strip()))


def is_start_command(text) -> bool:
    """Это команда /start (в том числе вида /start@имя_бота или /start с параметром)."""
    if not isinstance(text, str):
        return False
    parts = text.split(None, 1)
    return bool(parts) and parts[0].split("@")[0].lower() == "/start"


def message_link(chat_id: int, message_id: int) -> str:
    """Ссылка на сообщение в супергруппе (для id вида -100XXXXXXXXXX)."""
    cid = str(chat_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{message_id}"
