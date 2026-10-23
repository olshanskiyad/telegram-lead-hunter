import asyncio
import logging
import sys

from admin_bot import start_admin_bot
from client_bots import start_client_bots
from config import config
from database import Database
from notifier import start_expiry_notifier
from userbot import start_userbot

# Логи в stdout (в Docker: docker compose logs -f app)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("main")

# Шумные библиотеки
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)


async def main():
    logger.info("Starting LeadHunter services...")

    # 1. База данных и миграции
    db = Database(config.DATABASE_URL)
    await db.init_db()

    # 2. Четыре компонента работают одновременно
    try:
        await asyncio.gather(
            start_admin_bot(db),          # бот-админка (long polling)
            start_client_bots(db),        # боты клиентов (long polling или вебхуки)
            start_userbot(db),            # Telethon: чтение чатов
            start_expiry_notifier(db),    # предупреждения об окончании доступа
        )

    except KeyboardInterrupt:
        logger.info("Interrupted by user. Shutting down...")
    except Exception as e:
        logger.error(f"Critical error in main loop: {e}", exc_info=True)
    finally:
        await db.close()
        logger.info("All services stopped.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
