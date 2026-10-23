import os

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Все настройки из переменных окружения или файла .env (описание в .env.example)."""

    # Telegram API аккаунта-читателя (https://my.telegram.org)
    TELEGRAM_API_ID: int
    TELEGRAM_API_HASH: SecretStr

    # Бот-админка
    ADMIN_BOT_TOKEN: SecretStr
    ADMIN_IDS: str = ""  # Telegram id администраторов через запятую
    SELF_REGISTRATION: bool = False  # регистрация любого, кто пришлёт боту-админке токен своего бота
    ADMIN_CONTACT: str = "@ADMIN_CONTACT"  # контакт, который клиенты видят в справке и при окончании доступа

    # ИИ: любой OpenAI-совместимый API
    DEEPSEEK_API_KEY: SecretStr
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # PostgreSQL
    DATABASE_URL: str

    # Вебхуки клиентских ботов. Пусто (по умолчанию): long polling, домен и HTTPS не нужны
    WEBHOOK_HOST: str = ""
    WEBHOOK_PORT: int = 8443
    CERT_PATH: str = "/app/certs/webhook_cert.pem"
    KEY_PATH: str = "/app/certs/webhook_key.pem"

    # Сессия Telethon: папка с файлом userbot.session; можно передать строкой base64 вместо файла
    SESSION_DIR: str = "."
    SESSION_BASE64: str = ""

    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def admin_ids_list(self) -> list[int]:
        return [int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip()]

    def can_register(self, user_id: int) -> bool:
        """Может ли пользователь зарегистрироваться, прислав токен своего бота."""
        return self.SELF_REGISTRATION or user_id in self.admin_ids_list

    @property
    def session_path(self) -> str:
        """Путь к сессии Telethon без расширения (файл будет userbot.session)."""
        return os.path.join(self.SESSION_DIR, "userbot")


config = Settings()
