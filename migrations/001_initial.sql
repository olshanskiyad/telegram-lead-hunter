-- 001_initial.sql: схема PostgreSQL и пример набора для ниши.
-- Файл применяется один раз (список применённых в таблице schema_migrations).

-- Системные настройки
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Клиенты
CREATE TABLE IF NOT EXISTS clients (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    display_name TEXT,
    bot_token TEXT UNIQUE NOT NULL,        -- токен клиентского бота, хранится открытым текстом
    custom_prompt TEXT,                    -- NULL = используется niche_prompt из settings
    is_active BOOLEAN DEFAULT TRUE,
    trial_expires_at TIMESTAMPTZ,
    subscription_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Глобальный пул чатов (без дублей по chat_id)
CREATE TABLE IF NOT EXISTS chats (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT UNIQUE NOT NULL,
    title TEXT,
    invite_link TEXT,
    is_joined BOOLEAN DEFAULT FALSE,       -- зарезервировано, кодом не используется
    added_by_client_id INTEGER REFERENCES clients(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Связь клиент <-> чат (многие ко многим)
CREATE TABLE IF NOT EXISTS client_chats (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    chat_id BIGINT NOT NULL,
    is_default BOOLEAN DEFAULT FALSE,
    is_approved BOOLEAN DEFAULT FALSE,
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(client_id, chat_id)
);

-- Ключевые слова клиента (без модерации)
CREATE TABLE IF NOT EXISTS client_keywords (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    keyword TEXT NOT NULL,
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(client_id, keyword)
);

-- Запросы на добавление чатов (модерация)
CREATE TABLE IF NOT EXISTS pending_requests (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    chat_link TEXT NOT NULL,
    resolved_chat_id BIGINT,
    resolved_title TEXT,
    status TEXT DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'auto_approved')),
    admin_note TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Найденные заявки (лиды). Содержат текст сообщений и имена авторов: это персональные данные
CREATE TABLE IF NOT EXISTS leads (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    chat_id BIGINT,
    message_id BIGINT,
    from_user TEXT,
    message_text TEXT,
    ai_response TEXT,
    keywords_matched TEXT[],
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(client_id, chat_id, message_id)
);

-- Дефолтный набор ниши (копируется новым клиентам)
CREATE TABLE IF NOT EXISTS niche_defaults (
    id SERIAL PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('chat', 'keyword')),
    value TEXT NOT NULL,
    extra TEXT,                            -- для chat: название
    UNIQUE(type, value)
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_client_chats_chat ON client_chats(chat_id);
CREATE INDEX IF NOT EXISTS idx_client_chats_client ON client_chats(client_id);
CREATE INDEX IF NOT EXISTS idx_client_chats_approved ON client_chats(is_approved);
CREATE INDEX IF NOT EXISTS idx_leads_client ON leads(client_id);
CREATE INDEX IF NOT EXISTS idx_leads_date ON leads(created_at);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_requests(status);

-- Начальные настройки. Промпт ниши и ключевые слова ниже - пример (услуги фотографа):
-- замените на свою нишу в админ-боте (разделы «Промпт ниши» и «Дефолтный набор»).
INSERT INTO settings (key, value) VALUES
    ('monitoring_active', 'true'),
    ('niche_prompt', 'Ты помогаешь фотографу находить заказы в Telegram-чатах.

Тебе приходит одно сообщение из чата.

ЗАДАЧА: определи, ищет ли автор сообщения исполнителя для фотосъёмки.

ЗАЯВКА - это когда человек:
- ищет фотографа или просит посоветовать фотографа;
- просит сделать съёмку (портретную, семейную, свадебную, предметную);
- спрашивает цену или сроки съёмки.

НЕ ЗАЯВКА:
- обсуждение техники, обработки и советов по съёмке для себя;
- фотограф сам предлагает свои услуги (конкурент);
- реклама, разговоры, флуд, мемы.

ЕСЛИ ЗАЯВКА - напиши КОРОТКИЙ готовый ответ от лица фотографа (2-3 предложения, для копирования в чат). Вежливо, по делу, без воды. Не выдумывай цены и факты о себе.

ЕСЛИ НЕ ЗАЯВКА - ответь ровно одним словом: [НЕ ЛИД]')
ON CONFLICT (key) DO NOTHING;

-- Ключевые слова для примера. Склонения подбираются автоматически: пишите слова в начальной форме
INSERT INTO niche_defaults (type, value) VALUES
    ('keyword', 'фотограф'),
    ('keyword', 'фотосессия'),
    ('keyword', 'фотосъёмка'),
    ('keyword', 'нужен фотограф'),
    ('keyword', 'ищу фотографа'),
    ('keyword', 'посоветуйте фотографа'),
    ('keyword', 'свадебная съёмка'),
    ('keyword', 'семейная съёмка'),
    ('keyword', 'портретная съёмка'),
    ('keyword', 'предметная съёмка')
ON CONFLICT (type, value) DO NOTHING;
