"""
LeadHunter: слой базы данных (asyncpg + PostgreSQL).
Все CRUD-методы для многоклиентской работы.
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


def access_end(client) -> Optional[datetime]:
    """Когда у клиента кончается доступ: действует и пробный период, и подписка, берётся более поздняя дата.

    Нет ни той ни другой даты: None. Подходит для словаря и для записи asyncpg.
    """
    dates = [d for d in (client["trial_expires_at"], client["subscription_expires_at"]) if d is not None]
    return max(dates) if dates else None


class Database:
    """asyncpg-обёртка над PostgreSQL для LeadHunter."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    # ================================================================
    # Инициализация / Закрытие
    # ================================================================

    async def init_db(self):
        """Создать пул и применить новые миграции из папки migrations/ (каждый файл один раз)."""
        self.pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=10)

        migrations_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
        if not os.path.isdir(migrations_dir):
            logger.warning(f"Migrations directory not found: {migrations_dir}")
            return

        async with self.pool.acquire() as conn:
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       filename TEXT PRIMARY KEY,
                       applied_at TIMESTAMPTZ DEFAULT NOW()
                   )"""
            )
            applied = {r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")}

            # Сортировка по имени: 001 выполняется первым
            for filename in sorted(f for f in os.listdir(migrations_dir) if f.endswith(".sql")):
                if filename in applied:
                    continue
                with open(os.path.join(migrations_dir, filename), "r", encoding="utf-8") as f:
                    sql = f.read()
                try:
                    async with conn.transaction():
                        await conn.execute(sql)
                        await conn.execute("INSERT INTO schema_migrations (filename) VALUES ($1)", filename)
                except Exception:
                    # Работать на недостроенной схеме нельзя: останавливаемся и показываем причину
                    logger.error(f"Error applying migration {filename}")
                    raise
                logger.info(f"Migration applied: {filename}")

    async def get_expiring_clients(self, hours: int = 24) -> list[dict]:
        """Активные клиенты, у которых доступ кончается в ближайшие `hours` часов, а за этот срок их ещё не предупреждали.

        Конец доступа это более поздняя из дат пробного периода и подписки (как в access_end).
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM clients
                   WHERE is_active = true
                     AND GREATEST(trial_expires_at, subscription_expires_at)
                         BETWEEN NOW() AND NOW() + ($1 * INTERVAL '1 hour')
                     AND (last_expiry_warning_sent_at IS NULL
                          OR last_expiry_warning_sent_at
                             < GREATEST(trial_expires_at, subscription_expires_at) - ($1 * INTERVAL '1 hour'))""",
                hours,
            )
            return [dict(r) for r in rows]

    async def mark_expiry_warning_sent(self, client_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE clients SET last_expiry_warning_sent_at = NOW() WHERE id = $1",
                client_id,
            )


    async def close(self):
        """Закрыть пул соединений."""
        if self.pool:
            await self.pool.close()
            logger.info("Database pool closed")

    # ================================================================
    # Settings
    # ================================================================

    async def get_setting(self, key: str) -> Optional[str]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM settings WHERE key = $1", key)
            return row["value"] if row else None

    async def set_setting(self, key: str, value: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO settings (key, value) VALUES ($1, $2)
                   ON CONFLICT (key) DO UPDATE SET value = $2""",
                key, value,
            )

    async def get_niche_prompt(self) -> str:
        """Shortcut: промпт ниши из settings."""
        prompt = await self.get_setting("niche_prompt")
        return prompt or ""

    # ================================================================
    # Clients
    # ================================================================

    async def add_client(
        self,
        telegram_id: int,
        bot_token: str,
        display_name: str = "",
        username: str = "",
    ) -> int:
        """Создать клиента и вернуть его id."""
        trial_end = datetime.now(timezone.utc) + timedelta(days=7)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO clients (telegram_id, bot_token, display_name, username, trial_expires_at, is_active)
                   VALUES ($1, $2, $3, $4, $5, true)
                   ON CONFLICT (telegram_id) DO UPDATE
                   SET bot_token = EXCLUDED.bot_token,
                       display_name = EXCLUDED.display_name,
                       username = EXCLUDED.username,
                       trial_expires_at = EXCLUDED.trial_expires_at,
                       is_active = true
                   RETURNING id""",
                telegram_id, bot_token, display_name, username, trial_end,
            )
            return row["id"]

    async def get_client_by_telegram_id(self, telegram_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM clients WHERE telegram_id = $1", telegram_id
            )
            return dict(row) if row else None

    async def get_client_info(self, client_id: int) -> Optional[dict]:
        """Получить расширенную инфу о клиенте (telegram_id, bot_token, custom_help, expires)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT telegram_id, bot_token, username, display_name, custom_help,
                          subscription_expires_at, trial_expires_at
                   FROM clients WHERE id = $1""",
                client_id
            )
            return dict(row) if row else None

    async def get_all_clients(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM clients ORDER BY created_at DESC")
            return [dict(r) for r in rows]

    async def get_active_clients(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM clients WHERE is_active = TRUE ORDER BY created_at DESC"
            )
            return [dict(r) for r in rows]

    async def update_client(self, client_id: int, **fields):
        """Обновить произвольные поля клиента.

        Допустимые поля: display_name, username, bot_token, custom_prompt, is_active,
        trial_expires_at, subscription_expires_at.
        """
        allowed = {
            "display_name", "username", "bot_token", "custom_prompt", "custom_help",
            "is_active", "trial_expires_at", "subscription_expires_at",
        }
        parts, vals, idx = [], [], 1
        for k, v in fields.items():
            if k not in allowed:
                continue
            parts.append(f"{k} = ${idx}")
            vals.append(v)
            idx += 1
        if not parts:
            return
        vals.append(client_id)
        query = f"UPDATE clients SET {', '.join(parts)} WHERE id = ${idx}"
        async with self.pool.acquire() as conn:
            await conn.execute(query, *vals)

    async def is_subscription_active(self, client_id: int) -> bool:
        """Проверка: trial ИЛИ subscription ещё действует."""
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT 1 FROM clients
                   WHERE id = $1 AND is_active = TRUE
                     AND (trial_expires_at > $2 OR subscription_expires_at > $2)""",
                client_id, now,
            )
            return row is not None

    # ================================================================
    # Chats (глобальный пул)
    # ================================================================

    async def get_or_create_chat(
        self, chat_id: int, title: str = "", invite_link: str = "",
        added_by_client_id: int = None,
    ) -> dict:
        """Вернуть существующий чат или создать новый."""
        chat_id = int(chat_id)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM chats WHERE chat_id = $1", chat_id)
            if row:
                return dict(row)
            row = await conn.fetchrow(
                """INSERT INTO chats (chat_id, title, invite_link, added_by_client_id)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title
                   RETURNING *""",
                chat_id, title, invite_link, added_by_client_id,
            )
            return dict(row)

    # ================================================================
    # Client Chats (M2M)
    # ================================================================

    async def add_client_chat(
        self,
        client_id: int,
        chat_id: int,
        is_default: bool = False,
        is_approved: bool = False,
    ):
        """Привязать чат к клиенту. UNIQUE(client_id, chat_id): дубль игнорируется."""
        chat_id = int(chat_id)
        approved_at = datetime.now(timezone.utc) if is_approved else None
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO client_chats (client_id, chat_id, is_default, is_approved, approved_at)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (client_id, chat_id) DO UPDATE
                   SET is_approved = EXCLUDED.is_approved,
                       approved_at = COALESCE(client_chats.approved_at, EXCLUDED.approved_at)""",
                client_id, chat_id, is_default, is_approved, approved_at,
            )

    async def remove_client_chat(self, client_id: int, chat_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM client_chats WHERE client_id = $1 AND chat_id = $2",
                client_id, chat_id,
            )

    async def get_client_chats(self, client_id: int) -> list[dict]:
        """Чаты конкретного клиента с title из глобального пула.
        LEFT JOIN гарантирует показ чата даже если его нет в глобальной таблице chats.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT cc.*, COALESCE(c.title, 'Чат ' || cc.chat_id) as title, c.invite_link
                   FROM client_chats cc
                   LEFT JOIN chats c ON c.chat_id = cc.chat_id
                   WHERE cc.client_id = $1
                   ORDER BY cc.is_default DESC, cc.created_at""",
                client_id,
            )
            return [dict(r) for r in rows]

    async def get_clients_for_chat(self, chat_id: int) -> list[dict]:
        """КЛЮЧЕВОЙ МЕТОД: все активные клиенты, подписанные на chat_id
        (approved + active + trial/subscription не истекли).
        Используется userbot'ом для маршрутизации лидов.
        """
        now = datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT c.*
                   FROM clients c
                   JOIN client_chats cc ON c.id = cc.client_id
                   WHERE cc.chat_id = $1
                     AND cc.is_approved = TRUE
                     AND c.is_active = TRUE
                     AND (c.trial_expires_at > $2 OR c.subscription_expires_at > $2)""",
                chat_id, now,
            )
            return [dict(r) for r in rows]

    # ================================================================
    # Client Keywords
    # ================================================================

    async def add_client_keyword(
        self, client_id: int, keyword: str, is_default: bool = False
    ) -> bool:
        """Добавить keyword клиенту. Возвращает True если добавлен, False если дубль."""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO client_keywords (client_id, keyword, is_default)
                       VALUES ($1, $2, $3)""",
                    client_id, keyword.strip().lower(), is_default,
                )
                return True
        except asyncpg.UniqueViolationError:
            return False

    async def remove_client_keyword(self, client_id: int, keyword_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM client_keywords WHERE id = $1 AND client_id = $2",
                keyword_id, client_id,
            )

    async def get_client_keywords(self, client_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM client_keywords
                   WHERE client_id = $1 ORDER BY is_default DESC, keyword""",
                client_id,
            )
            return [dict(r) for r in rows]

    async def get_client_keyword_texts(self, client_id: int) -> list[str]:
        """Только тексты keywords (для match_keywords)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT keyword FROM client_keywords WHERE client_id = $1", client_id
            )
            return [r["keyword"] for r in rows]

    # ================================================================
    # Pending Requests (модерация чатов)
    # ================================================================

    async def create_pending_request(self, client_id: int, chat_link: str) -> int:
        """Создать запрос на добавление чата: ждёт решения администратора."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO pending_requests (client_id, chat_link)
                   VALUES ($1, $2)
                   RETURNING id""",
                client_id, chat_link,
            )
            return row["id"]

    async def get_pending_requests(self) -> list[dict]:
        """Все необработанные запросы с данными клиента."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT pr.*, c.display_name, c.username AS client_username
                   FROM pending_requests pr
                   JOIN clients c ON c.id = pr.client_id
                   WHERE pr.status = 'pending'
                   ORDER BY pr.created_at""",
            )
            return [dict(r) for r in rows]

    async def resolve_request(self, req_id: int, status: str, chat_id: int = None, title: str = None, note: str = None):
        """Завершить обработку запроса (approved/rejected)."""
        if chat_id is not None:
             chat_id = int(chat_id)
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_requests SET status = $1, resolved_at = now(), resolved_chat_id = $2, resolved_title = $3, admin_note = $4 WHERE id = $5",
                status, chat_id, title, note, req_id
            )

    async def check_rejection_history(self, chat_link: str) -> Optional[dict]:
        """Проверить, отклоняли ли этот чат/ссылку ранее."""
        async with self.pool.acquire() as conn:
            # Ищем последнюю отклоненную запись для этой ссылки
            row = await conn.fetchrow(
                """SELECT pr.*, c.display_name
                   FROM pending_requests pr
                   JOIN clients c ON pr.client_id = c.id
                   WHERE pr.chat_link = $1 AND pr.status = 'rejected'
                   ORDER BY pr.resolved_at DESC LIMIT 1""",
                chat_link
            )
            return dict(row) if row else None

    # ================================================================
    # Leads
    # ================================================================

    async def add_lead(
        self,
        client_id: int,
        chat_id: int,
        message_id: int,
        from_user: str,
        text: str,
        ai_response: str,
        keywords_matched: list[str],
    ) -> bool:
        """Сохранить лид. Возвращает True если новый, False если дубль."""
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO leads
                           (client_id, chat_id, message_id, from_user,
                            message_text, ai_response, keywords_matched)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    client_id, chat_id, message_id, from_user,
                    text, ai_response, keywords_matched,
                )
                return True
        except asyncpg.UniqueViolationError:
            return False

    async def is_duplicate_lead(
        self, client_id: int, chat_id: int, message_id: int
    ) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT 1 FROM leads
                   WHERE client_id = $1 AND chat_id = $2 AND message_id = $3""",
                client_id, chat_id, message_id,
            )
            return row is not None

    async def get_today_leads_count(self, client_id: int = None) -> int:
        """Кол-во лидов за сегодня. client_id=None → все клиенты."""
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with self.pool.acquire() as conn:
            if client_id is not None:
                row = await conn.fetchrow(
                    """SELECT COUNT(*) AS cnt FROM leads
                       WHERE client_id = $1 AND created_at >= $2""",
                    client_id, today_start,
                )
            else:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS cnt FROM leads WHERE created_at >= $1",
                    today_start,
                )
            return row["cnt"]

    # ================================================================
    # Niche Defaults
    # ================================================================

    async def get_default_keywords(self) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT value FROM niche_defaults WHERE type = 'keyword' ORDER BY value"
            )
            return [r["value"] for r in rows]

    async def get_default_chats(self) -> list[dict]:
        """Дефолтные чаты: value=chat_id (строка), extra=title."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM niche_defaults WHERE type = 'chat' ORDER BY id"
            )
            return [dict(r) for r in rows]

    async def add_default_keyword(self, keyword: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO niche_defaults (type, value)
                   VALUES ('keyword', $1)
                   ON CONFLICT (type, value) DO NOTHING""",
                keyword.strip().lower(),
            )

    async def add_default_chat(self, chat_id: int, title: str = ""):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO niche_defaults (type, value, extra)
                   VALUES ('chat', $1, $2)
                   ON CONFLICT (type, value) DO NOTHING""",
                str(chat_id), title,
            )

    async def sync_default_chat_to_all_clients(self, chat_id: int, title: str = ""):
        """Добавить дефолтный чат всем активным клиентам (для ретроактивной синхронизации)."""
        clients = await self.get_active_clients()
        added_count = 0
        for cl in clients:
            try:
                await self.get_or_create_chat(chat_id, title=title)
                await self.add_client_chat(
                    cl['id'], chat_id, is_default=True, is_approved=True
                )
                added_count += 1
            except Exception as e:
                logger.error(f"Error syncing default chat {chat_id} to client {cl['id']}: {e}")
        return added_count

    async def remove_default_keyword(self, keyword: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM niche_defaults WHERE type = 'keyword' AND value = $1",
                keyword.strip().lower(),
            )

    async def remove_default_chat(self, chat_id: str):
        chat_identifier = str(chat_id).strip()

        # 1. Resolve ID if it's a link/username
        resolved_id = None
        if chat_identifier.lstrip("-").isdigit():
            resolved_id = chat_identifier
        else:
            username = chat_identifier
            if "t.me/" in chat_identifier:
                parts = chat_identifier.split("t.me/")
                if len(parts) > 1 and parts[1] and not parts[1].startswith("+"):
                    username = parts[1].split("/")[0].split("?")[0]

            async with self.pool.acquire() as conn:
                # Try to find numeric ID in chats table
                resolved_id_val = await conn.fetchval(
                    "SELECT chat_id FROM chats WHERE invite_link = $1 OR username = $2 OR username = $3 LIMIT 1",
                    chat_identifier, username, chat_identifier
                )
                if resolved_id_val:
                    resolved_id = str(resolved_id_val)

        async with self.pool.acquire() as conn:
            # 2. Delete from niche_defaults (try both the input and the resolved ID)
            await conn.execute(
                "DELETE FROM niche_defaults WHERE type = 'chat' AND (value = $1 OR value = $2)",
                chat_identifier, resolved_id
            )

            # 3. Propagate to clients (delete where is_default is true)
            if resolved_id:
                try:
                    await conn.execute(
                        "DELETE FROM client_chats WHERE chat_id = $1::bigint AND is_default = TRUE",
                        int(resolved_id),
                    )
                except Exception as e:
                    logger.error(f"Error removing default chat from clients: {e}")

    async def copy_default_chats_to_client(self, client_id: int):
        """Подключить клиенту дефолтные чаты ниши (в наборе они хранятся числовыми id)."""
        chats = await self.get_default_chats()
        for c in chats:
            val = c["value"]
            if not (val.isdigit() or (val.startswith("-") and val[1:].isdigit())):
                logger.warning(f"Default chat {val!r} is not a numeric id, skipped")
                continue
            try:
                cid = int(val)
                # Чат должен быть в общем пуле
                await self.get_or_create_chat(cid, title=c.get("extra") or "Дефолтный чат")
                await self.add_client_chat(
                    client_id, cid, is_default=True, is_approved=True
                )
            except Exception as e:
                logger.error(f"Error copying default chat {val}: {e}")

    async def copy_default_keywords_to_client(self, client_id: int):
        keywords = await self.get_default_keywords()
        async with self.pool.acquire() as conn:
            for kw in keywords:
                await conn.execute(
                    """INSERT INTO client_keywords (client_id, keyword)
                       VALUES ($1, $2)
                       ON CONFLICT (client_id, keyword) DO NOTHING""",
                    client_id,
                    kw,
                )

    async def extend_subscription(self, telegram_id: int, days: int):
        """Продлить подписку на days дней от конца доступа (пробного периода или подписки, что позже), но не от прошедшей даты."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE clients
                   SET subscription_expires_at = GREATEST(trial_expires_at, subscription_expires_at, NOW()) + ($1 * INTERVAL '1 day'),
                       is_active = true
                   WHERE telegram_id = $2""",
                days,
                telegram_id,
            )

    async def deactivate_client(self, telegram_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE clients SET is_active = false WHERE telegram_id = $1",
                telegram_id,
            )
