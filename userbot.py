import asyncio
import base64
import html
import logging
import os
import re

from aiogram import Bot
from telethon import TelegramClient, events
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.utils import get_peer_id

from ai_mod import qualify_lead
from client_bots import bots_cache
from config import config
from database import Database
from lead_filters import is_broadcast_channel, is_not_lead, match_keywords, message_link

logger = logging.getLogger(__name__)

# Файл сессии Telethon (в Docker лежит на постоянном томе)
SESSION_NAME = config.session_path

# Клиент Telethon, нужен админ-боту для вступления в чаты
telethon_client = None


async def join_chat_via_userbot(link: str) -> tuple[int, str]:
    """Вступить в чат (или найти его) от имени аккаунта-читателя. Вернуть (id чата, название)."""
    if not telethon_client:
        raise RuntimeError("Юзербот не в сети (нет сессии или не запущен).")

    link = link.strip()
    chat_entity = None

    # Числовой id
    try:
        numeric_id = int(link)
        entity = await telethon_client.get_entity(numeric_id)
        return get_peer_id(entity), getattr(entity, "title", link)
    except ValueError:
        pass

    try:
        if "joinchat/" in link or "+" in link:
            match = re.search(r"(joinchat/|\+)([\w-]+)", link)
            if not match:
                raise ValueError("Неверный формат приватной ссылки")
            hash_val = match.group(2)

            try:
                updates = await telethon_client(ImportChatInviteRequest(hash_val))
                if updates.chats:
                    chat_entity = updates.chats[0]
            except UserAlreadyParticipantError:
                invite = await telethon_client(CheckChatInviteRequest(hash_val))
                if hasattr(invite, "chat"):
                    chat_entity = invite.chat
        else:
            chat_entity = await telethon_client.get_entity(link)
            try:
                await telethon_client(JoinChannelRequest(chat_entity))
            except UserAlreadyParticipantError:
                pass

        if chat_entity:
            return get_peer_id(chat_entity), getattr(chat_entity, "title", link)

        raise ValueError("Telethon не вернул данные чата")
    except Exception as e:
        logger.error(f"join_chat_via_userbot failed for {link}: {e}")
        raise ValueError(f"{e}")


async def _idle():
    """Ничего не делать, пока приложение живо (админ-бот и клиентские боты при этом работают)."""
    while True:
        await asyncio.sleep(3600)


async def start_userbot(db: Database):
    """Запустить слушатель Telethon: читает чаты и раздаёт заявки клиентам."""
    global telethon_client
    session_file = SESSION_NAME + ".session"

    # Сессию можно передать строкой base64 (для хостингов без постоянного тома)
    if config.SESSION_BASE64 and not os.path.exists(session_file):
        os.makedirs(os.path.dirname(session_file) or ".", exist_ok=True)
        with open(session_file, "wb") as f:
            f.write(base64.b64decode(config.SESSION_BASE64))
        logger.info(f"Session restored from SESSION_BASE64 to {session_file}")

    if not os.path.exists(session_file):
        logger.warning(
            "=" * 50 + "\n"
            "  USERBOT SESSION NOT FOUND!\n"
            f"  Expected: {session_file}\n"
            "  Userbot is DISABLED. Admin bot and client bots work normally.\n"
            "  Create the session with: python gen_session.py (see README)\n"
            + "=" * 50
        )
        await _idle()

    client = TelegramClient(
        SESSION_NAME,
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH.get_secret_value(),
    )

    # Файл сессии не удаляем: сбой сети при старте не повод просить вход заново
    while True:
        try:
            await client.connect()
            break
        except Exception as e:
            logger.error(f"Failed to connect Telethon: {e.__class__.__name__}: {e}. Retry in 60 s")
            await asyncio.sleep(60)

    if not await client.is_user_authorized():
        logger.error("Session file exists but user is NOT authorized! Create a new one with gen_session.py")
        await _idle()

    me = await client.get_me()
    logger.info(f"Userbot started as {me.first_name} (@{me.username})")

    telethon_client = client

    @client.on(events.NewMessage)
    async def handler(event):
        try:
            # 1. Общий переключатель мониторинга (админка: «Статус системы»)
            if await db.get_setting("monitoring_active") == "false":
                return

            chat_id = event.chat_id
            text = event.raw_text or ""
            if not text:
                return

            # 2. Каналы-трансляции пропускаем
            chat = await event.get_chat()
            if is_broadcast_channel(chat):
                return

            # 3. Все клиенты, подписанные на этот чат
            clients_for_chat = await db.get_clients_for_chat(chat_id)
            if not clients_for_chat:
                return

            # 4. Для каждого клиента проверяем его ключевые слова
            sender = await event.get_sender()
            username = getattr(sender, "username", None)

            first_name = getattr(sender, "first_name", "") or ""
            last_name = getattr(sender, "last_name", "") or ""
            full_name = f"{first_name} {last_name}".strip() or "Без имени"

            username_str = f"@{username}" if username else full_name

            if username:
                sender_display = f"<a href=\"https://t.me/{username}\">@{html.escape(username)}</a>"
            else:
                sender_id = getattr(event, "sender_id", None)
                if sender_id:
                    sender_display = f"<a href=\"tg://user?id={sender_id}\">{html.escape(full_name)}</a> <i>(скрыт, ID: <code>{sender_id}</code>)</i>"
                else:
                    sender_display = f"{html.escape(full_name)} <i>(скрыт)</i>"

            chat_title = getattr(chat, "title", "Private")
            link = message_link(chat_id, event.id)

            ai_cache = {}  # ответы ИИ на пару «промпт + текст», чтобы не платить дважды

            for cl in clients_for_chat:
                client_id = cl["id"]

                # Сначала дубли: так дешевле, чем подбирать слова
                if await db.is_duplicate_lead(client_id, chat_id, event.id):
                    continue

                kw_texts = await db.get_client_keyword_texts(client_id)
                if not kw_texts:
                    continue  # нет слов, нет совпадений

                matched = match_keywords(text, kw_texts)
                if not matched:
                    continue

                # Решение ИИ: заявка или нет, и готовый ответ
                prompt = cl.get("custom_prompt") or await db.get_niche_prompt()
                cache_key = (prompt, text)

                if cache_key not in ai_cache:
                    try:
                        ai_cache[cache_key] = await qualify_lead(text, prompt)
                    except Exception as e:
                        logger.error(f"AI qualification failed: {e}")
                        continue

                ai_response = ai_cache[cache_key]

                if is_not_lead(ai_response):
                    continue

                # Сохраняем заявку (дубль по уникальному ключу вернёт False)
                was_added = await db.add_lead(client_id, chat_id, event.id, username_str, text, ai_response, matched)

                if was_added:
                    # Присылаем клиенту через его собственного бота
                    try:
                        token = cl["bot_token"]
                        if token in bots_cache:
                            client_bot = bots_cache[token]  # общий бот из кеша, чтобы не плодить сессии
                            should_close = False
                        else:
                            client_bot = Bot(token=token)
                            should_close = True

                        notification = (
                            f"🔔 <b>Новый лид!</b>\n\n"
                            f"<b>Чат:</b> {html.escape(chat_title)}\n"
                            f"<b>От:</b> {sender_display}\n"
                            f"<b>По словам:</b> {html.escape(', '.join(matched))}\n\n"
                            f"<b>Запрос:</b>\n<i>{html.escape(text[:700])}</i>\n\n"
                            f"🤖 <b>Ответ-заготовка (копировать):</b>\n"
                            f"<code>{html.escape(ai_response[:700])}</code>\n\n"
                            f"<a href=\"{link}\">ПЕРЕЙТИ К СООБЩЕНИЮ →</a>"
                        )
                        await client_bot.send_message(cl["telegram_id"], notification, parse_mode="HTML")

                        # Копия администраторам (они должны хотя бы раз нажать /start в боте клиента)
                        for admin_id in config.admin_ids_list:
                            if admin_id != cl["telegram_id"]:
                                try:
                                    admin_note = f"👀 <b>[ADMIN COPY]</b> для клиента ID {cl['id']}\n" + notification
                                    await client_bot.send_message(admin_id, admin_note, parse_mode="HTML")
                                except Exception as ae:
                                    logger.warning(f"Could not send admin copy to {admin_id}: {ae}")

                        if should_close:
                            await client_bot.session.close()
                    except Exception as e:
                        logger.error(f"Failed to send lead notification to client {client_id}: {e}")

        except Exception as e:
            logger.error(f"Error in userbot handler: {e}", exc_info=True)

    await client.run_until_disconnected()
