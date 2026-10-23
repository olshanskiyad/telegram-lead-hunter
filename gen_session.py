"""Создать сессию Telegram для аккаунта, который будет читать чаты (Telethon).

Запускается один раз, до первого старта сервиса. Нужны TELEGRAM_API_ID и TELEGRAM_API_HASH из .env.

    python gen_session.py                  # спросит телефон, код из Telegram и пароль 2FA, если он включён
    python gen_session.py --print-base64   # то же и вывести сессию строкой для SESSION_BASE64

В Docker (сервис при этом лучше остановить, чтобы он не держал файл сессии):

    docker compose run --rm --no-deps app python gen_session.py
"""
import argparse
import asyncio
import base64
import getpass
import os

from telethon import TelegramClient, errors

from config import config

WARNING = """ВНИМАНИЕ. Файл сессии (и строка base64) даёт ПОЛНЫЙ ДОСТУП к аккаунту: чтение всех переписок,
отправку сообщений от его имени, вход в чаты. Используйте отдельный аккаунт, а не основной. Не публикуйте
файл, не добавляйте его в git, не пересылайте. Если сессия попала в чужие руки, завершите её в Telegram:
Настройки, Устройства, завершить сеанс."""

CODE_DELIVERY = {
    "SentCodeTypeApp": "в приложение Telegram (чат «Telegram» на другом вашем устройстве)",
    "SentCodeTypeSms": "по SMS",
    "SentCodeTypeCall": "звонком (робот продиктует код)",
    "SentCodeTypeFlashCall": "входящим звонком (код: последние цифры номера звонящего)",
    "SentCodeTypeMissedCall": "пропущенным звонком (код: последние цифры номера)",
    "SentCodeTypeFragmentSms": "через Fragment (ссылка придёт в Telegram)",
}


def describe_delivery(sent) -> str:
    name = type(sent.type).__name__
    return CODE_DELIVERY.get(name, name)


async def login(client: TelegramClient) -> bool:
    """Войти в аккаунт по телефону и коду. Вернуть True, если вход удался."""
    phone = input("Телефон аккаунта-читателя в международном формате (+<код страны><номер>): ").strip()
    try:
        sent = await client.send_code_request(phone)
    except errors.FloodWaitError as e:
        print(f"Telegram просит подождать {e.seconds} с ({e.seconds // 60} мин) и попробовать снова.")
        return False

    print(f"Код отправлен: {describe_delivery(sent)}.")
    code = input("Введите код (или 'resend', если код не пришёл): ").strip()
    if code.lower() == "resend":
        sent = await client.send_code_request(phone, force_sms=True)
        print(f"Код отправлен повторно: {describe_delivery(sent)}.")
        code = input("Введите код: ").strip()

    try:
        await client.sign_in(phone, code)
    except errors.SessionPasswordNeededError:
        await client.sign_in(password=getpass.getpass("Пароль двухфакторной аутентификации: "))
    except errors.PhoneCodeInvalidError:
        print("Неверный код. Запустите скрипт заново.")
        return False
    except errors.PhoneCodeExpiredError:
        print("Код истёк. Запустите скрипт заново.")
        return False
    return True


async def main(print_base64: bool) -> None:
    print(WARNING)
    print()

    session_file = config.session_path + ".session"
    if os.path.exists(session_file):
        answer = input(f"Сессия уже есть ({session_file}). Заменить новой? [y/N] ").strip().lower()
        if answer != "y":
            print("Ничего не изменено.")
            return
        os.remove(session_file)

    os.makedirs(config.SESSION_DIR, exist_ok=True)
    client = TelegramClient(
        config.session_path,
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH.get_secret_value(),
    )
    await client.connect()
    ok = False
    try:
        ok = await login(client)
        if ok:
            me = await client.get_me()
            print(f"Готово. Аккаунт: {me.first_name} (@{me.username}). Файл сессии: {session_file}")
    finally:
        await client.disconnect()
        if not ok and os.path.exists(session_file):
            os.remove(session_file)  # файл без входа только мешал бы

    if ok and print_base64:
        print("\nSESSION_BASE64 (храните как пароль):")
        with open(session_file, "rb") as f:
            print(base64.b64encode(f.read()).decode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Создать сессию Telethon для аккаунта-читателя чатов")
    parser.add_argument("--print-base64", action="store_true", help="вывести сессию строкой base64 для SESSION_BASE64")
    asyncio.run(main(parser.parse_args().print_base64))
