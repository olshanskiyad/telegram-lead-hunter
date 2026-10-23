"""Опрос клиентских ботов (long polling) на подставном боте: Telegram и сеть не нужны."""
import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetUpdates

import client_bots

BLOCK = object()  # «Telegram молчит»: get_updates висит, пока задачу не отменят


class FakeBot:
    def __init__(self, script):
        self.script = list(script)  # каждый шаг: список обновлений, исключение или BLOCK
        self.offsets = []
        self.webhook_deleted = 0
        self.closed = False
        self.session = SimpleNamespace(close=self._close)

    async def _close(self):
        self.closed = True

    async def delete_webhook(self, drop_pending_updates=False):
        self.webhook_deleted += 1

    async def get_updates(self, offset=None, timeout=None, allowed_updates=None):
        self.offsets.append(offset)
        step = self.script.pop(0)
        if step is BLOCK:
            await asyncio.sleep(3600)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Каждый тест начинает с пустого состояния модуля и подставного диспетчера."""
    monkeypatch.setattr(client_bots, "bots_cache", {})
    monkeypatch.setattr(client_bots, "clients_cache", {})
    monkeypatch.setattr(client_bots, "poll_tasks", {})
    fed = []

    async def fake_feed_update(bot, update, **kwargs):
        fed.append((update.update_id, kwargs))

    monkeypatch.setattr(client_bots.dp, "feed_update", fake_feed_update)
    return fed


async def wait_for(condition, timeout=5.0):
    async def poll():
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def test_updates_reach_dispatcher_with_client_id_and_offset_moves(clean_state):
    fed = clean_state
    bot = FakeBot([[SimpleNamespace(update_id=10), SimpleNamespace(update_id=11)], BLOCK])

    async def scenario():
        task = asyncio.create_task(client_bots._poll_client_bot(bot, "tok", 7))
        await wait_for(lambda: len(fed) == 2 and len(bot.offsets) == 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert fed == [(10, {"client_id": 7}), (11, {"client_id": 7})]
    assert bot.offsets == [None, 12]  # после обновления 11 просим начиная с 12
    assert bot.webhook_deleted == 1   # старый вебхук снимается один раз
    assert bot.closed                 # при остановке сессия бота закрывается


def test_rejected_token_stops_polling_and_forgets_the_bot():
    bot = FakeBot([TelegramUnauthorizedError(method=GetUpdates(), message="Unauthorized")])
    client_bots.bots_cache["tok"] = bot
    client_bots.clients_cache["tok"] = 7

    asyncio.run(asyncio.wait_for(client_bots._poll_client_bot(bot, "tok", 7), 5))

    assert "tok" not in client_bots.bots_cache
    assert "tok" not in client_bots.clients_cache
    assert bot.closed
    assert len(bot.offsets) == 1  # повторных попыток нет


def test_network_error_is_retried(clean_state):
    fed = clean_state
    bot = FakeBot([OSError("network is down"), [SimpleNamespace(update_id=5)], BLOCK])

    async def scenario():
        task = asyncio.create_task(client_bots._poll_client_bot(bot, "tok", 3))
        await wait_for(lambda: len(fed) == 1)  # пауза перед повтором 1 секунда
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert fed == [(5, {"client_id": 3})]
    assert bot.offsets[:2] == [None, None]  # после сбоя offset не сдвигается


def test_register_and_unregister_in_polling_mode(monkeypatch):
    monkeypatch.setattr(client_bots.config, "WEBHOOK_HOST", "")
    started = []

    async def fake_poll(bot, token, client_id):
        started.append(client_id)
        await asyncio.sleep(3600)

    monkeypatch.setattr(client_bots, "_poll_client_bot", fake_poll)
    record = {"id": 4, "bot_token": "12345:abcDEF_-xyz"}

    async def scenario():
        await client_bots.register_new_client_bot(None, record)
        await client_bots.register_new_client_bot(None, record)  # повторная регистрация ничего не меняет
        await asyncio.sleep(0)
        task = client_bots.poll_tasks["12345:abcDEF_-xyz"]
        await client_bots.unregister_client_bot("12345:abcDEF_-xyz")
        await asyncio.gather(task, return_exceptions=True)
        return task

    task = asyncio.run(scenario())

    assert started == [4]
    assert task.cancelled()
    assert client_bots.bots_cache == {} and client_bots.poll_tasks == {} and client_bots.clients_cache == {}
