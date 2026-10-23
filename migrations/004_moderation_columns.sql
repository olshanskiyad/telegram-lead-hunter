-- 004: колонки, которые читает и пишет код модерации чатов (database.py)
ALTER TABLE pending_requests ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE chats ADD COLUMN IF NOT EXISTS username TEXT;
