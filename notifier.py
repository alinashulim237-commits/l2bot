"""
Неблокирующая отправка уведомлений и двустороннее управление через Telegram Bot API.
- Отправка сообщений и скриншотов в фоновом потоке через очередь (главный поток не ждёт сеть).
- Автоматический обход ошибок локальных SSL-сертификатов Windows/антивирусов.
- Автоматическое определение и сохранение chat_id при первом сообщении /start боту.
- Приём команд и нажатий кнопок из Telegram через long polling (getUpdates).
"""
import json
import logging
import os
import queue
import sys
import threading
import time

import requests
import urllib3

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_STOP = object()
_PLACEHOLDERS = {
    "",
    "AUTO",
    "PASTE_BOT_TOKEN_HERE",
    "PASTE_CHAT_ID_HERE",
    "YOUR_BOT_TOKEN",
    "YOUR_CHAT_ID",
}

# Постоянная клавиатура управления внизу чата Telegram
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "▶️ Старт фарма"}, {"text": "⏹ Стоп фарма (Пауза)"}],
        [{"text": "📸 Скриншот"}, {"text": "📊 Статус и HP"}],
        [{"text": "🎯 Сброс цели"}, {"text": "🧪 Тест смерти"}],
        [{"text": "🚪 Закрыть программу"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def _mask_token(token: str) -> str:
    """Маскирует токен для безопасного вывода в логи (оставляет только последние 4 символа)."""
    if not token or len(token) <= 4:
        return "***"
    return f"***{token[-4:]}"


class Notifier:
    def __init__(self, cfg: dict, secrets_path: str = "secrets.yaml"):
        self.cfg = cfg or {}
        self.secrets_path = secrets_path
        tg_cfg = self.cfg.get("telegram") or {}

        self.enabled: bool = bool(tg_cfg.get("enabled", False))
        self.token: str = str(tg_cfg.get("token") or "").strip()
        raw_chat_id = str(tg_cfg.get("chat_id") or "").strip()
        self.chat_id: str = "" if raw_chat_id in _PLACEHOLDERS else raw_chat_id
        self.timeout: float = float(tg_cfg.get("timeout_sec", 10))
        self.retries: int = max(1, int(tg_cfg.get("retries", 3)))
        self.remote_control: bool = bool(tg_cfg.get("remote_control", True))

        self._masked = _mask_token(self.token)
        self._queue: queue.Queue = queue.Queue()
        self.cmd_queue: queue.Queue[str] = queue.Queue()
        self._closed = False
        self._verify_ssl = True
        self._session = requests.Session()
        self._last_update_id = 0
        self.bot_username = "L2ddggbot"

        self._send_thread: threading.Thread | None = None
        self._poll_thread: threading.Thread | None = None

        token_valid = self.token not in _PLACEHOLDERS

        if not self.enabled:
            self.active = False
            self._log("ℹ Telegram-уведомления отключены в config.yaml (telegram.enabled: false).")
        elif not token_valid:
            self.active = False
            self._log(
                "⚠ Telegram-уведомления ОТКЛЮЧЕНЫ: не задан token в secrets.yaml."
            )
        else:
            self.active = True
            # Пробуем сразу найти chat_id в свежих сообщениях бота, если он ещё не задан
            self._init_bot_and_discover_chat()

            self._send_thread = threading.Thread(
                target=self._send_worker, name="TelegramSender", daemon=True
            )
            self._send_thread.start()

            if self.remote_control:
                self._poll_thread = threading.Thread(
                    target=self._poll_worker, name="TelegramPoller", daemon=True
                )
                self._poll_thread.start()

    def _log(self, msg: str) -> None:
        safe_msg = self._sanitize(msg)
        logger = logging.getLogger("l2bot")
        if logger.handlers:
            logger.info(safe_msg)
        else:
            print(time.strftime("[%H:%M:%S]"), safe_msg, flush=True)

    def _sanitize(self, text: str) -> str:
        """Удаляет реальный токен из любых строк и текстов исключений."""
        if self.token and self.token not in _PLACEHOLDERS:
            return str(text).replace(self.token, self._masked)
        return str(text)

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Выполняет HTTP-запрос с автоматическим обходом ошибки локального SSL-сертификата."""
        kwargs.setdefault("timeout", self.timeout)
        url = self._api_url(endpoint)
        try:
            return self._session.request(method, url, verify=self._verify_ssl, **kwargs)
        except requests.exceptions.SSLError:
            if self._verify_ssl:
                self._verify_ssl = False
                return self._session.request(method, url, verify=False, **kwargs)
            raise

    def _save_chat_id_to_secrets(self, chat_id: str) -> None:
        """Сохраняет автоматически найденный chat_id в secrets.yaml."""
        try:
            content = (
                "telegram:\n"
                f'  token: "{self.token}"\n'
                f'  chat_id: "{chat_id}"\n'
            )
            with open(self.secrets_path, "w", encoding="utf-8") as f:
                f.write(content)
            self._log(f"✓ chat_id ({chat_id}) автоматически сохранён в {self.secrets_path}!")
        except Exception as exc:
            self._log(f"⚠ Не удалось сохранить chat_id в {self.secrets_path}: {exc}")

    def _init_bot_and_discover_chat(self) -> None:
        """Проверяет имя бота, регистрирует меню команд и ищет chat_id в getUpdates."""
        try:
            resp = self._request("GET", "getMe", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    self.bot_username = (data.get("result") or {}).get("username") or self.bot_username

            # Регистрируем меню команд в Telegram
            commands = [
                {"command": "start", "description": "Открыть панель управления"},
                {"command": "run", "description": "▶️ Запустить / продолжить фарм"},
                {"command": "pause", "description": "⏹ Остановить фарм (Пауза)"},
                {"command": "screen", "description": "📸 Скриншот экрана игры"},
                {"command": "status", "description": "📊 Статус, HP и статистика"},
                {"command": "unstuck", "description": "🎯 Сбросить цель и повернуться"},
                {"command": "simdeath", "description": "🧪 Тест смерти / воскрешения"},
                {"command": "exit", "description": "🚪 Полностью закрыть бота на ПК"},
            ]
            self._request(
                "POST",
                "setMyCommands",
                data={"commands": json.dumps(commands, ensure_ascii=False)},
                timeout=5,
            )

            # Читаем накопившиеся апдейты, чтобы не выполнять старые команды при старте
            # и заодно автоматически узнать chat_id, если пользователь уже нажал /start
            up_resp = self._request("GET", "getUpdates", params={"timeout": 0}, timeout=5)
            if up_resp.status_code == 200:
                updates = (up_resp.json() or {}).get("result") or []
                for upd in updates:
                    self._last_update_id = max(self._last_update_id, int(upd.get("update_id", 0)))
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    chat = msg.get("chat") or {}
                    if chat.get("id") and not self.chat_id:
                        self.chat_id = str(chat["id"])
                        self._save_chat_id_to_secrets(self.chat_id)

            if not self.chat_id:
                self._log(
                    f"📱 Открой своего бота @{self.bot_username} в Telegram и нажми Start (/start) — "
                    "он автоматически привяжет твой аккаунт!"
                )
            else:
                self._log(f"✓ Telegram-бот @{self.bot_username} подключён (chat_id: {self.chat_id}).")
        except Exception as exc:
            self._log(f"⚠ Предупреждение при инициализации Telegram: {exc}")

    def send_text(self, text: str, with_keyboard: bool = True) -> None:
        """Кладёт текстовое сообщение в очередь (не блокирует главный поток)."""
        if not self.active or self._closed or not text:
            return
        if len(text) > 4096:
            text = text[:4095] + "…"
        self._queue.put(("text", text, with_keyboard))

    def send_photo(self, jpeg_bytes: bytes, caption: str = "", with_keyboard: bool = True) -> None:
        """Кладёт фото (байты JPEG) с подписью в очередь (не блокирует главный поток)."""
        if not self.active or self._closed:
            return
        if not jpeg_bytes:
            if caption:
                self.send_text(caption, with_keyboard=with_keyboard)
            return
        if caption and len(caption) > 1024:
            caption = caption[:1023] + "…"
        self._queue.put(("photo", jpeg_bytes, caption))

    def get_command_nowait(self) -> str | None:
        """Возвращает следующую команду из Telegram или None (не блокирует)."""
        try:
            return self.cmd_queue.get_nowait()
        except queue.Empty:
            return None

    def test_connection(self) -> tuple[bool, str]:
        """
        Блокирующая проверка связи с Telegram через getMe (используется в test_telegram.py).
        Если chat_id ещё не привязан, ждёт до 30 секунд нажатия /start в Telegram.
        """
        if self.token in _PLACEHOLDERS:
            return False, "Не заполнен token в secrets.yaml."
        try:
            resp = self._request("GET", "getMe", timeout=self.timeout)
            data = resp.json() if resp.content else {}
            if resp.status_code != 200 or not data.get("ok"):
                desc = data.get("description") or f"HTTP {resp.status_code}"
                return False, self._sanitize(f"Ошибка Telegram API: {desc}")

            bot_info = data.get("result") or {}
            username = bot_info.get("username", "unknown")
            first_name = bot_info.get("first_name", "")

            if not self.chat_id:
                print(
                    f"\n👉 Открой Telegram, найди бота @{username} и нажми кнопку START (/start)."
                )
                print("Ожидаю сообщение от тебя в Telegram (до 45 секунд)...")
                deadline = time.time() + 45
                while time.time() < deadline and not self.chat_id:
                    time.sleep(0.5)

            if not self.chat_id:
                return (
                    False,
                    f"Бот @{username} работает, но ты ещё не нажал START в диалоге с @{username}.",
                )

            return (
                True,
                f"Бот @{username} ({first_name}) доступен, токен {self._masked}, chat_id={self.chat_id}",
            )
        except Exception as exc:
            return False, self._sanitize(f"Ошибка соединения с Telegram: {exc}")

    def close(self, timeout: float = 5.0) -> None:
        """Дожидается отправки сообщений в очереди (не дольше timeout сек) и останавливает потоки."""
        if not self.active or self._closed:
            return
        self._closed = True
        self._queue.put(_STOP)
        if self._send_thread is not None and self._send_thread.is_alive():
            self._send_thread.join(timeout=timeout)

    def _poll_worker(self) -> None:
        """Фоновый поток приёма команд и кнопок из Telegram."""
        while not self._closed:
            try:
                params = {"timeout": 8, " offset": self._last_update_id + 1}
                params = {"timeout": 8, "offset": self._last_update_id + 1}
                resp = self._request("GET", "getUpdates", params=params, timeout=12)
                if resp.status_code != 200:
                    time.sleep(2.0)
                    continue

                updates = (resp.json() or {}).get("result") or []
                for upd in updates:
                    upd_id = int(upd.get("update_id", 0))
                    if upd_id > self._last_update_id:
                        self._last_update_id = upd_id

                    msg = upd.get("message") or {}
                    chat = msg.get("chat") or {}
                    sender_chat_id = str(chat.get("id") or "")
                    text = str(msg.get("text") or "").strip()
                    if not sender_chat_id or not text:
                        continue

                    # Автопривязка первого написавшего пользователя, если chat_id ещё не был задан
                    if not self.chat_id:
                        self.chat_id = sender_chat_id
                        self._save_chat_id_to_secrets(self.chat_id)
                        self._log(f"📱 Привязан Telegram-аккаунт chat_id={self.chat_id}")
                        self.send_text(
                            "✅ Твой Telegram успешно привязан к L2 Farm Bot!\n"
                            "Используй кнопки внизу экрана для управления ботом."
                        )

                    # Игнорируем сообщения от чужих chat_id
                    if sender_chat_id != self.chat_id:
                        continue

                    self._handle_incoming_text(text)
            except Exception:
                time.sleep(2.0)

    def _handle_incoming_text(self, text: str) -> None:
        low = text.lower()
        if low in ("/start", "/help", "меню", "помощь"):
            self.cmd_queue.put("menu")
        elif low.startswith("/run") or "старт фарма" in low or low == "старт":
            self.cmd_queue.put("run")
        elif low.startswith("/pause") or "стоп фарма" in low or "пауза" in low:
            self.cmd_queue.put("pause")
        elif low.startswith("/screen") or "скриншот" in low:
            self.cmd_queue.put("screen")
        elif low.startswith("/status") or "статус" in low or "статистика" in low:
            self.cmd_queue.put("status")
        elif low.startswith("/unstuck") or "сброс цели" in low:
            self.cmd_queue.put("unstuck")
        elif low.startswith("/simdeath") or "тест смерти" in low:
            self.cmd_queue.put("simdeath")
        elif low.startswith("/exit") or low.startswith("/stop") or "закрыть программу" in low:
            self.cmd_queue.put("exit")
        else:
            self.cmd_queue.put("menu")

    def _send_worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    break
                kind, payload, extra = item
                if not self.chat_id:
                    # Ждём до 10 секунд привязки chat_id, если сообщение уже в очереди
                    waited = 0.0
                    while not self.chat_id and not self._closed and waited < 10.0:
                        time.sleep(0.5)
                        waited += 0.5
                if not self.chat_id:
                    self._log("⚠ Сообщение в Telegram пропущено: chat_id ещё не привязан (нажми /start в боте).")
                    continue

                if kind == "text":
                    self._send_with_retries("text", payload, extra)
                elif kind == "photo":
                    self._send_with_retries("photo", payload, extra)
            except Exception as exc:
                self._log(f"⚠ Внутренняя ошибка потока Telegram: {exc}")
            finally:
                self._queue.task_done()

    def _send_with_retries(self, kind: str, payload, extra) -> None:
        delays = [1.0, 2.0, 4.0]
        last_error = ""
        reply_markup_json = json.dumps(MAIN_KEYBOARD, ensure_ascii=False)

        for attempt in range(1, self.retries + 1):
            try:
                if kind == "text":
                    data = {
                        "chat_id": self.chat_id,
                        "text": payload,
                        "reply_markup": reply_markup_json,
                    }
                    resp = self._request("POST", "sendMessage", data=data)
                else:
                    data = {
                        "chat_id": self.chat_id,
                        "reply_markup": reply_markup_json,
                    }
                    if extra:
                        data["caption"] = extra
                    resp = self._request(
                        "POST",
                        "sendPhoto",
                        data=data,
                        files={"photo": ("screen.jpg", payload, "image/jpeg")},
                    )

                if resp.status_code == 200:
                    body = resp.json() if resp.content else {}
                    if body.get("ok"):
                        return
                    last_error = body.get("description") or "ok=false"
                else:
                    try:
                        body = resp.json()
                        last_error = body.get("description") or f"HTTP {resp.status_code}"
                    except Exception:
                        last_error = f"HTTP {resp.status_code}"
            except Exception as exc:
                last_error = self._sanitize(str(exc))

            if attempt < self.retries:
                pause = delays[min(attempt - 1, len(delays) - 1)]
                self._log(
                    f"⚠ Не удалось отправить уведомление в Telegram (попытка {attempt}/{self.retries}): "
                    f"{last_error}. Повтор через {pause:.0f}с..."
                )
                time.sleep(pause)

        self._log(
            f"❌ Ошибка отправки в Telegram после {self.retries} попыток: {last_error}"
        )
