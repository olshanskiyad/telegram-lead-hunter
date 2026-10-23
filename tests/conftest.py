import os
import sys

import pytest

# Настройки читаются при импорте config: для тестов хватает заглушек, сеть и база не нужны
for key, value in {
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "test",
    "ADMIN_BOT_TOKEN": "1:test",
    "DEEPSEEK_API_KEY": "test",
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
}.items():
    os.environ.setdefault(key, value)


@pytest.fixture(autouse=True)
def reset_fsm_storage():
    """Состояния ввода (добавление чата, слов) не переходят из теста в тест."""
    yield
    for name in ("client_bots", "admin_bot"):
        module = sys.modules.get(name)
        if module is not None:
            module.dp.fsm.storage.storage.clear()
