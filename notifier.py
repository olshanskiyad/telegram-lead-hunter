import asyncio
import html
import logging
from datetime import timezone
from typing import Optional

from aiogram import Bot

from client_bots import bots_cache
from config import config
from database import access_end

logger = logging.getLogger(__name__)

WARN_HOURS = 24  # за сколько часов до конца доступа предупреждать (в тексте сообщения тоже 24 часа)
CHECK_INTERVAL = 86400  # пауза между проверками, секунд


def expiry_message(client) -> Optional[str]:
    """Текст предупреждения (Telegram HTML) для клиента, у которого скоро кончается доступ."""
    end = access_end(client)
    if end is None:
        return None
    # Называем то, что кончается последним: подписку или пробный период
    kind = "подписки" if client["subscription_expires_at"] == end else "пробного периода"
    return (
        "⚠️ <b>Внимание!</b>\n\n"
        "Ваш доступ к сервису LeadHunter истекает менее чем через 24 часа.\n"
        f"Срок действия {kind}: <code>{end.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')} (UTC)</code>.\n\n"
        "Чтобы не пропустить новых клиентов, пожалуйста, напишите администратору для продления: "
        f"{html.escape(config.ADMIN_CONTACT)}"
    )


async def send_expiry_warnings(db) -> int:
    """Одна проверка: предупредить клиентов, у которых доступ кончается в ближайшие сутки. Вернуть число отправленных."""
    sent = 0
    for cl in await db.get_expiring_clients(hours=WARN_HOURS):
        text = expiry_message(cl)
        if text is None:
            continue

        token = cl["bot_token"]
        bot = bots_cache.get(token)
        own_bot = bot is None
        if own_bot:
            bot = Bot(token=token)
        try:
            await bot.send_message(cl["telegram_id"], text, parse_mode="HTML")
            await db.mark_expiry_warning_sent(cl["id"])
            sent += 1
            logger.info(f"Sent expiry warning to client {cl['id']}")
        except Exception as e:
            logger.error(f"Error sending expiry notice to client {cl['id']}: {e}")
        finally:
            if own_bot:
                await bot.session.close()
    return sent


async def start_expiry_notifier(db):
    """Фоновая задача: раз в сутки предупреждает клиентов о скором конце доступа."""
    logger.info("Expiry notifier started.")

    while True:
        try:
            await send_expiry_warnings(db)
        except Exception as e:
            logger.error(f"Error in expiry notifier loop: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)
