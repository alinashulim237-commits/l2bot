"""
Скрипт проверки связи с Telegram.
Проверяет токен (getMe), отправляет тестовое текстовое сообщение и скриншот экрана.

Запуск:  python test_telegram.py
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from bot import Vision, load_config
from notifier import Notifier


def main() -> int:
    cfg = load_config()
    notifier = Notifier(cfg)

    print("Проверка подключения к Telegram Bot API (getMe)...")
    ok, info = notifier.test_connection()
    if not ok:
        print(f"❌ {info}")
        notifier.close()
        return 1

    print(f"✓ {info}")

    if not notifier.active:
        print("⚠ В config.yaml параметр telegram.enabled установлен в false. Включи его для отправки.")
        notifier.close()
        return 1

    print("Отправляю тестовое текстовое сообщение...")
    notifier.send_text("✅ L2-бот: связь с Telegram работает")

    print("Делаю скриншот основного монитора и отправляю фото...")
    try:
        vision = Vision(cfg)
        jpeg_bytes = vision.screenshot_jpeg(quality=80)
        notifier.send_photo(jpeg_bytes, caption="📸 Тестовый скриншот экрана от L2-бота")
    except Exception as exc:
        print(f"⚠ Не удалось снять или закодировать скриншот: {exc}")

    print("Ожидаю завершения отправки очереди...")
    notifier.close(timeout=15.0)
    print("Готово! Проверь сообщения в Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
