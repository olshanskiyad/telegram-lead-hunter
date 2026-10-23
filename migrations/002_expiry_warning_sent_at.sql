-- 002: колонка для учёта отправленных предупреждений об окончании доступа
ALTER TABLE clients ADD COLUMN IF NOT EXISTS last_expiry_warning_sent_at TIMESTAMPTZ;
