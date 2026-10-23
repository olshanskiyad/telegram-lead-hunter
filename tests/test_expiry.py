"""Конец доступа и предупреждение об окончании: учитываются и пробный период, и подписка."""
import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import notifier
from database import Database, access_end
from fakes import assert_valid_telegram_html

UTC = timezone.utc
TRIAL = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
SUBSCRIPTION = datetime(2026, 1, 20, 9, 30, tzinfo=UTC)


def client(trial=None, subscription=None, **extra):
    return {"id": 1, "telegram_id": 42, "bot_token": "12345:abcDEF_-xyz",
            "trial_expires_at": trial, "subscription_expires_at": subscription, **extra}


def test_access_end_is_the_later_date():
    assert access_end(client(TRIAL, SUBSCRIPTION)) == SUBSCRIPTION
    assert access_end(client(SUBSCRIPTION, TRIAL)) == SUBSCRIPTION  # подписка короче пробного периода


def test_access_end_with_one_date_or_none():
    assert access_end(client(trial=TRIAL)) == TRIAL
    assert access_end(client(subscription=SUBSCRIPTION)) == SUBSCRIPTION
    assert access_end(client()) is None


def test_message_names_the_subscription_when_it_ends_last():
    text = notifier.expiry_message(client(TRIAL, SUBSCRIPTION))
    assert "подписки" in text and "2026-01-20 09:30" in text
    assert "2026-01-10" not in text  # дата пробного периода в сообщение не попадает


def test_message_names_the_trial_when_there_is_no_subscription():
    text = notifier.expiry_message(client(trial=TRIAL))
    assert "пробного периода" in text and "2026-01-10 12:00" in text


def test_message_converts_the_date_to_utc():
    msk = timezone(timedelta(hours=3))
    text = notifier.expiry_message(client(trial=datetime(2026, 1, 10, 15, 0, tzinfo=msk)))
    assert "2026-01-10 12:00 (UTC)" in text


def test_no_dates_no_message():
    assert notifier.expiry_message(client()) is None


def test_message_is_html_and_escapes_admin_contact(monkeypatch):
    monkeypatch.setattr(notifier.config, "ADMIN_CONTACT", "@a_<x>")
    text = notifier.expiry_message(client(trial=TRIAL))
    assert_valid_telegram_html(text)
    assert "@a_&lt;x&gt;" in text
    assert "<b>Внимание!</b>" in text and "<code>2026-01-10 12:00 (UTC)</code>" in text
    assert "Markdown" not in inspect.getsource(notifier)


class FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []
        self.session = SimpleNamespace(close=self._close)
        self.closed = False

    async def _close(self):
        self.closed = True

    async def send_message(self, chat_id, text, parse_mode=None):
        if self.fail:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text, parse_mode))


class FakeDb:
    def __init__(self, clients):
        self.clients = clients
        self.warned = []

    async def get_expiring_clients(self, hours):
        assert hours == 24
        return self.clients

    async def mark_expiry_warning_sent(self, client_id):
        self.warned.append(client_id)


def test_warning_goes_out_for_a_client_whose_subscription_is_ending(monkeypatch):
    # предупреждают и о конце подписки, а не только пробного периода
    bot = FakeBot()
    monkeypatch.setitem(notifier.bots_cache, "t1", bot)
    db = FakeDb([client(TRIAL, SUBSCRIPTION, id=5, bot_token="t1", telegram_id=77)])
    assert asyncio.run(notifier.send_expiry_warnings(db)) == 1
    [(chat_id, text, parse_mode)] = bot.sent
    assert chat_id == 77 and parse_mode == "HTML" and "подписки" in text
    assert db.warned == [5]


def test_failed_delivery_is_not_marked_as_warned(monkeypatch):
    ok, broken = FakeBot(), FakeBot(fail=True)
    monkeypatch.setitem(notifier.bots_cache, "t1", broken)
    monkeypatch.setitem(notifier.bots_cache, "t2", ok)
    db = FakeDb([client(TRIAL, id=1, bot_token="t1"), client(TRIAL, id=2, bot_token="t2")])
    assert asyncio.run(notifier.send_expiry_warnings(db)) == 1
    assert db.warned == [2]  # у первого повтор при следующей проверке


def test_temporary_bot_is_closed_after_sending(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(notifier, "Bot", lambda token: bot)
    notifier.bots_cache.pop("not-cached", None)
    db = FakeDb([client(TRIAL, id=1, bot_token="not-cached")])
    assert asyncio.run(notifier.send_expiry_warnings(db)) == 1
    assert bot.closed


def test_expiring_query_uses_the_latest_access_date_for_window_and_repeat_guard():
    sql = inspect.getsource(Database.get_expiring_clients)
    # окно «ближайшие N часов» и защита от повторного предупреждения считаются от одной и той же даты конца доступа
    assert sql.count("GREATEST(trial_expires_at, subscription_expires_at)") == 2
    assert "LEAST" not in sql and "trial_expires_at BETWEEN" not in sql
