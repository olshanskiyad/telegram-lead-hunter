"""Клиентские боты: у каждого клиента свой бот, обновления обрабатывает один общий диспетчер.

Два режима получения обновлений от Telegram:
- long polling (по умолчанию, WEBHOOK_HOST пуст): боты сами опрашивают Telegram, домен и HTTPS не нужны;
- вебхуки (WEBHOOK_HOST задан): нужен публичный HTTPS-адрес, подробности в README.
"""
import asyncio
import logging
import os
import ssl
from typing import Dict

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.types import FSInputFile, Update
from aiohttp import web

import client_bot
from config import config
from database import Database

logger = logging.getLogger(__name__)

POLL_TIMEOUT = 30  # секунд, сколько Telegram держит запрос getUpdates открытым
MAX_RETRY_DELAY = 60  # секунд, потолок паузы между повторами после сбоя сети

# Состояние: токен -> бот, токен -> id клиента в базе, токен -> задача опроса
bots_cache: Dict[str, Bot] = {}
clients_cache: Dict[str, int] = {}
poll_tasks: Dict[str, asyncio.Task] = {}
_update_tasks: set = set()  # ссылки на задачи обработки, иначе их может собрать сборщик мусора

# Один диспетчер на всех клиентских ботов; id клиента передаётся в обработчики как client_id
dp = Dispatcher()
dp.include_router(client_bot.setup_client_router())


def _spawn(coro) -> None:
    """Запустить обработку обновления в фоне и не потерять ссылку на задачу."""
    task = asyncio.create_task(coro)
    _update_tasks.add(task)
    task.add_done_callback(_update_tasks.discard)


async def _feed(bot: Bot, update: Update, client_id: int) -> None:
    try:
        await dp.feed_update(bot, update, client_id=client_id)
    except Exception:
        logger.exception(f"Client {client_id}: error while handling update {update.update_id}")


async def init_client_bots(db: Database):
    """Поднять ботов всех активных клиентов."""
    client_bot.db = db  # обработчики клиентского бота ходят в базу через этот модуль

    clients = await db.get_active_clients()
    mode = "webhook" if config.WEBHOOK_HOST else "long polling"
    logger.info(f"Initializing client bots ({mode}) for {len(clients)} active clients...")

    for client in clients:
        await register_new_client_bot(db, client, startup=True)


async def register_new_client_bot(db: Database, client_record: dict, startup: bool = False):
    """Подключить бота клиента: при старте и динамически из админ-бота."""
    token = client_record["bot_token"]
    client_id = client_record["id"]

    if token in bots_cache:
        return  # уже подключён

    try:
        bot = Bot(token=token)
    except Exception as e:
        logger.error(f"Client {client_id}: invalid bot token format ({e.__class__.__name__})")
        return

    bots_cache[token] = bot
    clients_cache[token] = client_id

    if config.WEBHOOK_HOST:
        await _set_webhook(bot, token, client_id)
    else:
        poll_tasks[token] = asyncio.create_task(_poll_client_bot(bot, token, client_id))

    if not startup:
        logger.info(f"Dynamically registered bot for client {client_id}")


async def unregister_client_bot(token: str):
    """Отключить бота (клиент сменил токен). Остановить опрос или снять вебхук."""
    task = poll_tasks.pop(token, None)
    if task:
        task.cancel()  # сессию бота закроет сама задача
    bot = bots_cache.pop(token, None)
    clients_cache.pop(token, None)
    if bot and not task:
        try:
            await bot.delete_webhook()
        except Exception as e:
            logger.warning(f"Could not delete webhook of the old bot: {e.__class__.__name__}")
        await bot.session.close()


# --- Long polling ---

async def _poll_client_bot(bot: Bot, token: str, client_id: int):
    """Опрашивать Telegram за одного клиентского бота, пока приложение работает."""
    allowed_updates = dp.resolve_used_update_types()
    offset = None
    delay = 1
    webhook_cleared = False
    try:
        while True:
            try:
                if not webhook_cleared:
                    # Если у бота остался вебхук, getUpdates вернёт ошибку конфликта.
                    # Накопившееся за время простоя не разбираем (как и у админ-бота)
                    await bot.delete_webhook(drop_pending_updates=True)
                    webhook_cleared = True
                updates = await bot.get_updates(offset=offset, timeout=POLL_TIMEOUT, allowed_updates=allowed_updates)
                delay = 1
            except TelegramUnauthorizedError:
                logger.error(f"Client {client_id}: bot token rejected by Telegram, polling stopped")
                bots_cache.pop(token, None)
                clients_cache.pop(token, None)
                poll_tasks.pop(token, None)
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                continue
            except Exception as e:
                logger.warning(f"Client {client_id}: polling error {e.__class__.__name__}, retry in {delay} s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RETRY_DELAY)
                continue

            for update in updates:
                offset = update.update_id + 1
                _spawn(_feed(bot, update, client_id))
    finally:
        await bot.session.close()


# --- Вебхуки (необязательный режим) ---

async def _set_webhook(bot: Bot, token: str, client_id: int):
    webhook_url = f"https://{config.WEBHOOK_HOST}:{config.WEBHOOK_PORT}/bot/{token}"

    cert_params = {}
    # Для самоподписанного сертификата Telegram нужно передать публичную часть
    if config.CERT_PATH and os.path.exists(config.CERT_PATH):
        cert_params["certificate"] = FSInputFile(config.CERT_PATH)

    try:
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True, **cert_params)
    except Exception as e:
        logger.error(f"Failed to set webhook for client {client_id}: {e}")


async def handle_webhook(request: web.Request) -> web.Response:
    """Точка входа для всех вебхуков от Telegram."""
    token = request.match_info.get("token")
    if not token or token not in bots_cache:
        logger.warning("Received webhook for unknown token")
        return web.Response(status=404, text="Not Found")

    body = await request.json()
    update = Update(**body)

    # Обрабатываем в фоне, чтобы сразу ответить Telegram «200 OK»
    _spawn(_feed(bots_cache[token], update, clients_cache[token]))
    return web.Response(status=200, text="OK")


async def handle_healthcheck(request: web.Request) -> web.Response:
    return web.Response(status=200, text="OK")


def create_webhook_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthcheck", handle_healthcheck)
    app.router.add_post("/bot/{token}", handle_webhook)
    return app


async def start_client_bots(db: Database):
    """Запустить клиентских ботов. В режиме вебхуков ещё и HTTPS-сервер для них."""
    await init_client_bots(db)

    if not config.WEBHOOK_HOST:
        return  # опрос идёт в фоновых задачах

    runner = web.AppRunner(create_webhook_app())
    await runner.setup()

    ssl_context = None
    if config.CERT_PATH and config.KEY_PATH:
        if os.path.exists(config.CERT_PATH) and os.path.exists(config.KEY_PATH):
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(config.CERT_PATH, config.KEY_PATH)
        else:
            logger.warning("SSL cert/key not found. Starting WITHOUT SSL! Telegram webhook requires HTTPS!")

    site = web.TCPSite(runner, host="0.0.0.0", port=config.WEBHOOK_PORT, ssl_context=ssl_context)
    await site.start()
    logger.info(f"Webhook server running on port {config.WEBHOOK_PORT} (SSL: {bool(ssl_context)})")
