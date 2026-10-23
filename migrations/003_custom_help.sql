-- 003: своя справка для клиента в формате Telegram HTML (если пусто, бот показывает client_instructions.html)
ALTER TABLE clients ADD COLUMN IF NOT EXISTS custom_help TEXT;
