"""
Lineage 2 Interlude — фарм-бот с управлением и уведомлениями о смерти в Telegram,
автофокусом окна игры KetraWars и встроенной быстрой калибровкой полос HP (F5–F8).

Цикл:  Next Target -> F2, F2, F3 (пока моб жив) -> Next Target ...
Нет мобов / моб недоступен -> Esc, поворот и бег вперёд.
При смерти персонажа -> остановка ввода, скриншот и уведомление в Telegram, ожидание воскрешения.
Управление: кнопками прямо из Telegram-бота (@L2ddggbot) или горячими клавишами в игре!

Запуск:  python bot.py [config.yaml]
"""
import copy
import ctypes
import ctypes.wintypes as wt
import logging
from logging.handlers import RotatingFileHandler
import os
import random
import re
import subprocess
import sys
import threading
import time
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

import cv2
import keyboard
import math
import mss
import numpy as np
import pydirectinput
import yaml

import mobfinder
from notifier import Notifier

# Чтобы консоль Windows (cp1251) не падала на эмодзи и спецсимволах
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Чтобы координаты мыши/скриншотов совпадали при масштабировании Windows (125%, 150%)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = False


class Stop(Exception):
    pass


class Died(Exception):
    """Подтверждена смерть персонажа. Должно ловиться только в Bot.run()."""


class PausedFromTelegram(Exception):
    """Мгновенный выход из текущего действия при нажатии Паузы/Сброса из Telegram."""


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def kill_other_bot_instances() -> None:
    """Закрывает все старые запущенные копии python.exe и pythonw.exe, чтобы они не конфликтовали за Telegram."""
    my_pid = os.getpid()
    for proc in ("python.exe", "pythonw.exe"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/FI", f"IMAGENAME eq {proc}", "/FI", f"PID ne {my_pid}"],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass


def ensure_admin() -> None:
    """
    Запрашивает права Администратора (RunAs), если ещё не получены,
    и закрывает старые копии бота.
    """
    if is_admin():
        kill_other_bot_instances()
        # Регистрируем задачу с высшими правами (/RL HIGHEST), чтобы дальнейшие перезапуски шли без UAC
        try:
            bat_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "5_start_bot_admin.bat")
            subprocess.run(
                [
                    "schtasks", "/Create", "/TN", "L2FarmBotAdmin",
                    "/TR", bat_path, "/SC", "ONCE", "/ST", "00:00",
                    "/IT", "/RL", "HIGHEST", "/F",
                ],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
        return

    if "--elevated" in sys.argv:
        return
    try:
        script = os.path.abspath(__file__)
        workdir = os.path.dirname(script)
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}" --elevated', workdir, 1
        )
        if int(ret) > 32:
            sys.exit(0)
    except Exception:
        pass


def minimize_console() -> None:
    """Сворачивает окно консоли бота, чтобы оно никогда не перекрывало полосы HP в игре."""
    try:
        con = ctypes.windll.kernel32.GetConsoleWindow()
        if con:
            ctypes.windll.user32.ShowWindow(con, 6)  # SW_MINIMIZE
    except Exception:
        pass


def find_game_window() -> int | None:
    """
    Находит ТОЛЬКО настоящее окно клиента игры Lineage 2 / KetraWars.
    Исключает окна браузеров, редакторов (Antigravity), папок и консолей.
    """
    user32 = ctypes.windll.user32
    found_hwnd = None

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def _cb(hwnd, lparam):
        nonlocal found_hwnd
        if not user32.IsWindowVisible(hwnd):
            return True

        cls_buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls_buf, 128)
        cls_name = cls_buf.value.lower()

        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.lower()

        # Исключаем посторонние окна, в заголовке которых может встретиться слово lineage
        excluded = ("antigravity", "visual studio", "chrome", "edge", "opera", "telegram", "cmd.exe", "python", "блокнот", "notepad")
        if any(bad in title for bad in excluded):
            return True

        if "unrealwwindowsviewportwindow" in cls_name or "ketrawars" in title or "lineage" in title:
            found_hwnd = hwnd
            return False
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception:
        pass
    return found_hwnd


def is_game_window_foreground() -> bool:
    """Проверяет, находится ли окно игры сейчас на переднем плане (не перекрыто ли другим окном)."""
    hwnd = find_game_window()
    if not hwnd:
        return False
    fg = ctypes.windll.user32.GetForegroundWindow()
    return fg == hwnd


def focus_game_window(wait_render: float = 0.25) -> int | None:
    """Находит окно KetraWars / Lineage 2 и надёжно выводит его на передний план с обходом Windows Foreground Lock."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    found_hwnd = find_game_window()
    if found_hwnd:
        try:
            fg = user32.GetForegroundWindow()
            if fg != found_hwnd:
                minimize_console()
                fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
                cur_tid = kernel32.GetCurrentThreadId()
                target_tid = user32.GetWindowThreadProcessId(found_hwnd, None)

                if target_tid:
                    user32.AttachThreadInput(cur_tid, target_tid, True)
                if fg_tid and fg_tid != cur_tid:
                    user32.AttachThreadInput(cur_tid, fg_tid, True)

                try:
                    user32.AllowSetForegroundWindow(0xFFFF)
                except Exception:
                    pass

                user32.keybd_event(0x12, 0, 0, 0)
                user32.keybd_event(0x12, 0, 2, 0)

                if user32.IsIconic(found_hwnd):
                    user32.ShowWindow(found_hwnd, 9)  # SW_RESTORE
                    time.sleep(0.3)
                else:
                    user32.ShowWindow(found_hwnd, 5)  # SW_SHOW

                user32.BringWindowToTop(found_hwnd)
                user32.SetForegroundWindow(found_hwnd)
                user32.SetActiveWindow(found_hwnd)

                if fg_tid and fg_tid != cur_tid:
                    user32.AttachThreadInput(cur_tid, fg_tid, False)
                if target_tid:
                    user32.AttachThreadInput(cur_tid, target_tid, False)

                time.sleep(wait_render)
        except Exception:
            pass
    return found_hwnd


_VK_MAP = {
    "esc": 0x1B,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "space": 0x20,
    "enter": 0x0D,
    "tab": 0x09,
}

_EXTENDED_KEYS = {"up", "down", "left", "right"}


def l2_key_down(key: str, hwnd: int | None = None) -> None:
    """Отправляет чистое аппаратное нажатие клавиши в DirectInput игры."""
    k = key.lower()
    vk = _VK_MAP.get(k)
    user32 = ctypes.windll.user32
    if vk:
        try:
            scan = user32.MapVirtualKeyW(vk, 0)
            user32.keybd_event(vk, scan, 0, 0)
        except Exception:
            pass
    try:
        pydirectinput.keyDown(k)
    except Exception:
        pass


def l2_key_up(key: str, hwnd: int | None = None) -> None:
    """Отправляет чистое аппаратное отпускание клавиши в DirectInput игры."""
    k = key.lower()
    vk = _VK_MAP.get(k)
    user32 = ctypes.windll.user32
    if vk:
        try:
            scan = user32.MapVirtualKeyW(vk, 0)
            user32.keybd_event(vk, scan, 2, 0)  # KEYEVENTF_KEYUP
        except Exception:
            pass
    try:
        pydirectinput.keyUp(k)
    except Exception:
        pass


def cursor_pos() -> tuple[int, int]:
    pt = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _update_yaml_bar(section: str, x: int, y: int, w: int, h: int = 3, path: str = "config.yaml") -> None:
    """Обновляет блок target_bar или player_bar в config.yaml с сохранением комментариев."""
    with open(path, encoding="utf-8") as f:
        text = f.read()

    pattern = rf"(^{section}:\s*\r?\n)(\s+x:\s*\d+\r?\n\s+y:\s*\d+\r?\n\s+w:\s*\d+\r?\n\s+h:\s*\d+)"
    replacement = f"{section}:\n  x: {x}\n  y: {y}\n  w: {w}\n  h: {h}"
    new_text, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count > 0:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)


def _create_mss():
    mss_cls = getattr(mss, "MSS", mss.mss)
    return mss_cls()


# ---------------------------------------------------------------- логирование
def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("l2bot")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False

    os.makedirs("logs", exist_ok=True)
    file_fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = RotatingFileHandler(
        os.path.join("logs", "bot.log"),
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    console_fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)
    return logger


_LOGGER = _setup_logger()


def log(msg):
    _LOGGER.info(msg)


# ---------------------------------------------------------------- конфиг
def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивно сливает словарь override поверх словаря base."""
    result = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def load_config(path="config.yaml", secrets_path="secrets.yaml", calibration_path="calibration.yaml"):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # Локальные результаты калибровки (python calibrate.py mob) — лежат отдельно, config.yaml не трогают
    if calibration_path and os.path.exists(calibration_path):
        try:
            with open(calibration_path, encoding="utf-8") as cf:
                cal = yaml.safe_load(cf) or {}
            if isinstance(cal, dict):
                cfg = _deep_merge(cfg, cal)
        except Exception as exc:
            log(f"⚠ Не удалось прочитать {calibration_path}: {exc}")

    if secrets_path and os.path.exists(secrets_path):
        try:
            with open(secrets_path, encoding="utf-8") as sf:
                secrets = yaml.safe_load(sf) or {}
            if isinstance(secrets, dict):
                cfg = _deep_merge(cfg, secrets)
        except Exception as exc:
            log(f"⚠ Не удалось прочитать {secrets_path}: {exc}")

    env_token = os.environ.get("L2BOT_TG_TOKEN", "").strip()
    env_chat_id = os.environ.get("L2BOT_TG_CHAT_ID", "").strip()
    if env_token:
        cfg.setdefault("telegram", {})["token"] = env_token
    if env_chat_id:
        cfg.setdefault("telegram", {})["chat_id"] = env_chat_id

    return cfg


def rnd(rng):
    return random.uniform(rng[0], rng[1])


def jitter(value, pct=0.2):
    return value * random.uniform(1 - pct, 1 + pct)


# ---------------------------------------------------------------- зрение
def bar_percent(img_bgr, ranges, min_fill=0.5):
    """Процент заполнения полосы: доля столбцов, где >= min_fill пикселей нужного цвета."""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for lo, hi in ranges:
        mask |= cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    filled = (mask > 0).mean(axis=0) >= min_fill
    return float(filled.mean() * 100)


class Vision:
    BAR_PAD = 6   # запас вокруг полосы HP (px), чтобы небольшой сдвиг не ломал чтение

    def __init__(self, cfg):
        self.cfg = cfg
        b = cfg.get("target_bar", {"x": 571, "y": 66, "w": 146, "h": 3})
        self.region = {"left": b["x"], "top": b["y"], "width": b["w"], "height": b["h"]}
        self.ranges = cfg["bar_color"]

        pb = cfg.get("player_bar", {"x": 16, "y": 51, "w": 150, "h": 2})
        self.player_region = {
            "left": pb["x"],
            "top": pb["y"],
            "width": pb["w"],
            "height": pb["h"],
        }
        self.player_ranges = cfg.get("player_bar_color", self.ranges)

        death_cfg = cfg.get("death") or {}
        self.use_template = bool(death_cfg.get("use_template", False))
        self.template_path = str(death_cfg.get("template_path", "templates/revive.png"))
        self.template_threshold = float(death_cfg.get("template_threshold", 0.8))
        self.template_roi = death_cfg.get("template_roi", None)
        self._template_img = None
        self._template_warned = False

        self.sct = _create_mss()

    def get_origin(self) -> tuple[int, int]:
        """Возвращает экранные координаты (left, top) клиентской области окна игры."""
        hwnd = find_game_window()
        if hwnd:
            pt = wt.POINT(0, 0)
            ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
            if 0 <= pt.x < 3000 and 0 <= pt.y < 2000:
                return int(pt.x), int(pt.y)
        return 0, 0

    def get_client_rect(self) -> tuple[int, int, int, int]:
        """Клиентская область окна игры в экранных координатах: (left, top, width, height).
        Если окно не найдено — весь основной монитор."""
        user32 = ctypes.windll.user32
        virt = self.sct.monitors[0]   # весь виртуальный рабочий стол
        hwnd = find_game_window()
        if hwnd:
            rc = wt.RECT()
            pt = wt.POINT(0, 0)
            if user32.GetClientRect(hwnd, ctypes.byref(rc)) and user32.ClientToScreen(hwnd, ctypes.byref(pt)):
                l, t, w, h = int(pt.x), int(pt.y), int(rc.right - rc.left), int(rc.bottom - rc.top)
                # обрезаем по границам экрана
                l2, t2 = max(l, virt["left"]), max(t, virt["top"])
                r2 = min(l + w, virt["left"] + virt["width"])
                b2 = min(t + h, virt["top"] + virt["height"])
                if r2 - l2 >= 640 and b2 - t2 >= 480:
                    return l2, t2, r2 - l2, b2 - t2
        mon = self.sct.monitors[1]
        return int(mon["left"]), int(mon["top"]), int(mon["width"]), int(mon["height"])

    def _grab(self, region: dict):
        try:
            raw = self.sct.grab(region)
        except Exception:
            try:
                self.sct = _create_mss()
                raw = self.sct.grab(region)
            except Exception as exc:
                log(f"⚠ Ошибка захвата экрана (mss): {exc}")
                h = max(1, int(region.get("height", 6)))
                w = max(1, int(region.get("width", 100)))
                return np.zeros((h, w, 3), dtype=np.uint8)
        img = np.array(raw)[:, :, :3]  # BGRA -> BGR
        return np.ascontiguousarray(img)

    def _bar_region(self, region: dict) -> dict:
        """Область полосы из config.yaml (смещение от клиентской области окна) -> экранные координаты с запасом."""
        ox, oy = self.get_origin()
        p = self.BAR_PAD
        return {
            "left": max(0, ox + region["left"] - p),
            "top": max(0, oy + region["top"] - p),
            "width": region["width"] + 2 * p,
            "height": region["height"] + 2 * p,
        }

    def grab(self):
        """Область полосы HP цели (берётся из target_bar в config.yaml)."""
        return self._grab(self._bar_region(self.region))

    def grab_player(self):
        """Область полосы HP персонажа (берётся из player_bar в config.yaml)."""
        return self._grab(self._bar_region(self.player_region))

    def _band_hp(self, img_bgr, ranges, min_px: int = 2, trim: int = 0) -> float:
        if img_bgr is None or img_bgr.size == 0:
            return 0.0
        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for lo, hi in ranges:
            mask |= cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
        filled = (mask > 0).sum(axis=0) >= min_px
        if trim and len(filled) > 2 * trim:
            filled = filled[trim:len(filled) - trim]   # процент считаем по самой полосе, без запаса
        return float(filled.mean() * 100)

    def target_hp(self) -> float:
        """Процент полосы HP цели (0..100)."""
        return self._band_hp(self.grab(), self.ranges, min_px=2, trim=self.BAR_PAD)

    def player_hp(self) -> float:
        """Процент полосы HP персонажа (0..100)."""
        return self._band_hp(self.grab_player(), self.player_ranges, min_px=2, trim=self.BAR_PAD)

    def find_mobs_info(self):
        """
        Снимает клиентскую область окна игры и ищет мобов по цветным надписям 'Lvl:XX' (см. mobfinder.py).
        Возвращает (картинка, [ {"x","y","box","pixels","sx","sy"} ]), где sx/sy — ЭКРАННЫЕ координаты клика.
        """
        l, t, w, h = self.get_client_rect()
        img = self._grab({"left": l, "top": t, "width": w, "height": h})
        mobs = mobfinder.find_mobs(img, self.cfg)
        for m in mobs:
            m["sx"], m["sy"] = l + m["x"], t + m["y"]
        return img, mobs

    def find_mobs(self) -> list[tuple[int, int]]:
        """Экранные координаты клика по именам мобов, ближайшие к центру первыми."""
        _, mobs = self.find_mobs_info()
        return [(m["sx"], m["sy"]) for m in mobs]

    def mob_debug_jpeg(self, img, mobs) -> bytes:
        """Сохраняет debug_mobs.png (с разметкой) и возвращает JPEG для Telegram."""
        dbg = mobfinder.draw_debug(img, self.cfg, mobs)
        cv2.imwrite("debug_mobs.png", dbg)
        ok, buf = cv2.imencode(".jpg", dbg, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        return buf.tobytes() if ok else b""

    def screenshot_jpeg(self, quality: int = 80) -> bytes:
        """Скриншот ВСЕГО основного монитора в JPEG. Вызывать ТОЛЬКО из главного потока (ограничение mss)."""
        monitor = self.sct.monitors[1]
        img = self._grab(monitor)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            raise RuntimeError("Не удалось закодировать скриншот в JPEG")
        return buf.tobytes()

    def revive_dialog_visible(self) -> bool:
        """Опциональная проверка окна воскрешения по шаблону (Шаг 8a)."""
        if not self.use_template:
            return False
        if self._template_img is None:
            if not os.path.exists(self.template_path):
                if not self._template_warned:
                    log(f"⚠ Шаблон {self.template_path} не найден — проверка по шаблону пропущена.")
                    self._template_warned = True
                return False
            tpl = cv2.imread(self.template_path, cv2.IMREAD_COLOR)
            if tpl is None:
                if not self._template_warned:
                    log(f"⚠ Не удалось прочитать шаблон {self.template_path}.")
                    self._template_warned = True
                return False
            self._template_img = tpl

        if self.template_roi and len(self.template_roi) == 4:
            rx, ry, rw, rh = self.template_roi
            region = {"left": int(rx), "top": int(ry), "width": int(rw), "height": int(rh)}
        else:
            region = self.sct.monitors[1]

        screen = self._grab(region)
        th, tw = self._template_img.shape[:2]
        sh, sw = screen.shape[:2]
        if sh < th or sw < tw:
            return False

        res = cv2.matchTemplate(screen, self._template_img, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(res)
        return float(max_val) >= self.template_threshold


# ---------------------------------------------------------------- детектор смерти
class DeathDetector:
    def __init__(self, cfg: dict, vision: Vision):
        d = cfg.get("death") or {}
        self.enabled: bool = bool(d.get("enabled", False))
        self.check_interval: float = float(d.get("check_interval", 0.5))
        self.confirm_reads: int = max(1, int(d.get("confirm_reads", 3)))
        self.empty_threshold: float = float(d.get("empty_threshold", 1.0))
        self.alive_threshold: float = float(d.get("alive_threshold", 10.0))
        self.revive_confirm_sec: float = float(d.get("revive_confirm_sec", 3.0))
        self.use_template: bool = bool(d.get("use_template", False))

        self.vision = vision
        self.low_reads: int = 0
        self._last_check: float = 0.0
        self._last_template_check: float = 0.0
        self._alive_since: float | None = None

    def self_check(self) -> bool:
        """
        Самопроверка при снятии с паузы.
        Если HP персонажа <= alive_threshold, отключает детектор и возвращает False.
        """
        if not self.enabled:
            return True
        hp = self.vision.player_hp()
        if hp <= self.alive_threshold:
            log(
                "⚠ Полоса HP персонажа не видна (0%) — возможно, окно игры перекрыто. Определение смерти временно отключено."
            )
            self.enabled = False
            return False
        log(f"✓ Контроль полосы HP персонажа активен (HP: {hp:.0f}%)")
        return True

    def check(self) -> bool:
        """
        Периодическая проверка смерти (с троттлингом не чаще check_interval сек).
        Проверяет смерть ТОЛЬКО когда окно игры активно на переднем плане (чтобы Alt+Tab не вызывал ложную тревогу).
        """
        if not self.enabled:
            return False

        now = time.monotonic()
        if now - self._last_check < self.check_interval:
            return False
        self._last_check = now

        if not is_game_window_foreground():
            self.low_reads = 0
            return False

        hp = self.vision.player_hp()
        if hp <= self.empty_threshold:
            self.low_reads += 1
        else:
            self.low_reads = 0

        if self.low_reads >= self.confirm_reads:
            return True

        if self.use_template and (now - self._last_template_check >= 1.0):
            self._last_template_check = now
            if self.vision.revive_dialog_visible():
                return True

        return False

    def alive_again(self) -> bool:
        """
        Возвращает True, если HP > alive_threshold непрерывно в течение revive_confirm_sec секунд.
        """
        hp = self.vision.player_hp()
        if self.use_template and self.vision.revive_dialog_visible():
            self._alive_since = None
            return False

        now = time.monotonic()
        if hp > self.alive_threshold:
            if self._alive_since is None:
                self._alive_since = now
            return (now - self._alive_since) >= self.revive_confirm_sec
        else:
            self._alive_since = None
            return False

    def reset(self) -> None:
        """Сбрасывает счётчики и таймеры детектора."""
        self.low_reads = 0
        self._last_check = 0.0
        self._last_template_check = 0.0
        self._alive_since = None


# ---------------------------------------------------------------- бот
class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.vision = Vision(cfg)
        self.notifier = Notifier(cfg)
        self.detector = DeathDetector(cfg, self.vision)

        self.empty = cfg.get("empty_threshold", 1.0)
        self.stop = threading.Event()
        self.paused = threading.Event()
        self.paused.set()  # старт на паузе

        self.dead = False
        self.simulate_death = False
        self._first_unpause_done = False

        self.kills = 0
        self.skipped = 0
        self.miss = 0
        self.deaths = 0
        self.started = time.time()

        # Точки экрана, по которым клик не взял моба в цель (временный чёрный список)
        self._blacklist: list[tuple[float, float, float]] = []
        self._last_mob_debug = -1e9
        self._last_mob_tg = -1e9

        # Точки быстрой калибровки F5-F8
        self._cal_pts: dict[str, tuple[int, int] | None] = {
            "target_left": None,
            "player_left": None,
        }

        # Таймеры для stall_alert (Шаг 8b)
        stall_cfg = cfg.get("stall_alert") or {}
        self.stall_enabled = bool(stall_cfg.get("enabled", False))
        self.stall_minutes = float(stall_cfg.get("minutes", 10))
        self._last_kill_mono = time.monotonic()
        self._last_stall_alert_mono = 0.0

    def reload_config(self) -> None:
        """Перечитывает config.yaml на лету."""
        try:
            new_cfg = load_config()
            self.cfg = new_cfg
            self.empty = new_cfg.get("empty_threshold", 1.0)
            self.vision.cfg = new_cfg

            b = new_cfg["target_bar"]
            self.vision.region = {"left": b["x"], "top": b["y"], "width": b["w"], "height": b["h"]}
            self.vision.ranges = new_cfg["bar_color"]

            pb = new_cfg.get("player_bar", {"x": 28, "y": 127, "w": 150, "h": 2})
            self.vision.player_region = {
                "left": pb["x"],
                "top": pb["y"],
                "width": pb["w"],
                "height": pb["h"],
            }
            self.vision.player_ranges = new_cfg.get("player_bar_color", self.vision.ranges)

            self.detector = DeathDetector(new_cfg, self.vision)

            stall_cfg = new_cfg.get("stall_alert") or {}
            self.stall_enabled = bool(stall_cfg.get("enabled", False))
            self.stall_minutes = float(stall_cfg.get("minutes", 10))
        except Exception as exc:
            log(f"⚠ Не удалось перечитать config.yaml: {exc}")

    # ---- быстрая калибровки F5–F8 (вызывается на паузе)
    def on_f5_hotkey(self):
        if not self.paused.is_set():
            return
        self._cal_pts["target_left"] = cursor_pos()
        self.notifier.cmd_queue.put("cal_f5")

    def on_f6_hotkey(self):
        if not self.paused.is_set():
            return
        self._cal_pts["target_right"] = cursor_pos()
        self.notifier.cmd_queue.put("cal_f6")

    def on_f7_hotkey(self):
        if not self.paused.is_set():
            return
        self._cal_pts["player_left"] = cursor_pos()
        self.notifier.cmd_queue.put("cal_f7")

    def on_f8_hotkey(self):
        if not self.paused.is_set():
            return
        self._cal_pts["player_right"] = cursor_pos()
        self.notifier.cmd_queue.put("cal_f8")

    # ---- управление
    def start_farming(self, source: str = "hotkey") -> bool:
        """Запускает или возобновляет фарм (предварительно активируя окно игры KetraWars)."""
        if self.dead:
            if self.simulate_death:
                self.simulate_death = False
                return True
            if self.vision.player_hp() > self.detector.alive_threshold:
                self.dead = False
            else:
                msg = "⚠ Персонаж мёртв! Сначала воскресни в игре."
                log(msg)
                if source == "tg":
                    self.notifier.send_text(msg)
                return False

        # Выводим ТОЛЬКО настоящее окно Lineage 2 (KetraWars) на передний план
        hwnd = focus_game_window(wait_render=0.4)

        if not self.paused.is_set():
            if source == "tg":
                self.notifier.send_text("▶️ Бот уже работает в режиме фарма.")
            return True

        self.reload_config()
        ok = self.detector.self_check()
        admin_status = "ДА" if is_admin() else "НЕТ"

        self._first_unpause_done = True
        self._reset_stall_timer()
        self.paused.clear()
        log(f"▶ РАБОТАЮ (Окно KetraWars HWND: {hwnd}, Права Админа: {admin_status})")
        if source == "tg" or (self.cfg.get("telegram") or {}).get("notify_on_start", False):
            self.notifier.send_text(
                f"▶️ Фарм ЗАПУЩЕН!\n"
                f"❤️ Твоё HP: {self.vision.player_hp():.0f}% | 🎯 HP цели: {self.vision.target_hp():.0f}%\n"
                f"🛡 Контроль смерти: {'ВКЛ' if ok else 'ВЫКЛ'} | Права Админа: {admin_status}"
            )
        return True

    def pause_farming(self, source: str = "hotkey") -> None:
        """Ставит фарм на паузу и отпускает все клавиши движения."""
        was_running = not self.paused.is_set()
        self.simulate_death = False
        self.paused.set()
        self.release_all()
        log("⏸ ПАУЗА")
        if source == "tg":
            self.notifier.send_text(f"⏸ Бот поставлен на ПАУЗУ.\n{self.stats()}")
        elif was_running and (self.cfg.get("telegram") or {}).get("notify_on_start", False):
            self.notifier.send_text(f"⏸ Бот на паузе (нажата клавиша в игре).\n{self.stats()}")

    def toggle(self):
        if self.dead and not self.simulate_death:
            log("⚠ Персонаж мёртв, сначала воскресни.")
            return
        if self.paused.is_set() or self.simulate_death:
            self.simulate_death = False
            self.start_farming(source="hotkey")
        else:
            self.pause_farming(source="hotkey")

    def toggle_simulate_death(self):
        """Обработчик горячей клавиши симуляции смерти (только переключение флага)."""
        if not self.dead:
            self.simulate_death = True
        else:
            self.simulate_death = False

    def status_report(self) -> str:
        """Формирует подробную сводку состояния для Telegram."""
        if self.dead:
            state_str = "🧪 ТЕСТ СМЕРТИ" if self.simulate_death else "💀 МЁРТВ (ждёт воскрешения)"
        elif self.paused.is_set():
            state_str = "⏸ НА ПАУЗЕ"
        else:
            state_str = "🟢 РАБОТАЕТ (фарм)"

        p_hp = self.vision.player_hp()
        t_hp = self.vision.target_hp()
        uptime_min = int((time.time() - self.started) // 60)
        admin_str = "✅ Есть" if is_admin() else "⚠️ Нет"
        return (
            f"📊 Статус L2-бота: {state_str}\n"
            f"❤️ HP персонажа: {p_hp:.0f}%\n"
            f"🎯 HP цели: {t_hp:.0f}%\n"
            f"🔑 Права Администратора: {admin_str}\n"
            f"⏱ Время работы: {uptime_min} мин\n"
            f"📈 Сводка: {self.stats()}"
        )

    def _process_tg_commands(self, in_death_loop: bool = False) -> None:
        """Выполняет команды из Telegram и быструю калибровку F5-F8 (всегда в главном потоке)."""
        while True:
            cmd = self.notifier.get_command_nowait()
            if cmd is None:
                break

            if cmd == "menu":
                self.notifier.send_text(
                    "🤖 Панель управления L2 Farm Bot:\n\n"
                    "• ▶️ Старт фарма — переключиться в окно L2 и начать фарм\n"
                    "• ⏹ Стоп фарма (Пауза) — поставить бота на паузу\n"
                    "• 📸 Скриншот — прислать экран игры\n"
                    "• 📊 Статус и HP — проверить HP и статистику\n"
                    "• 🎯 Сброс цели — нажать Esc в игре\n"
                    "• 🧪 Тест смерти — проверить учебную тревогу\n"
                    "• 🚪 Закрыть программу — выключить скрипт на ПК\n\n"
                    + self.status_report()
                )
            elif cmd == "run":
                if self.dead and self.simulate_death:
                    self.simulate_death = False
                else:
                    self.start_farming(source="tg")
            elif cmd == "pause":
                if self.simulate_death:
                    self.simulate_death = False
                was_active = not self.paused.is_set()
                self.pause_farming(source="tg")
                if was_active and not in_death_loop:
                    raise PausedFromTelegram
            elif cmd == "status":
                self.notifier.send_text(self.status_report())
            elif cmd == "screen":
                try:
                    focus_game_window(wait_render=0.3)
                    jpeg = self.vision.screenshot_jpeg(quality=80)
                    self.notifier.send_photo(jpeg, caption=self.status_report())
                except Exception as exc:
                    self.notifier.send_text(f"⚠ Не удалось снять скриншот: {exc}")
            elif cmd == "unstuck":
                if self.dead and not self.simulate_death:
                    self.notifier.send_text("⚠ Персонаж мёртв — сброс цели недоступен.")
                else:
                    focus_game_window()
                    self.release_all()
                    self.press("esc")
                    self.notifier.send_text("🎯 Нажат Esc (цель сброшена).")
                    if not self.paused.is_set() and not in_death_loop:
                        raise PausedFromTelegram
            elif cmd == "simdeath":
                self.toggle_simulate_death()
            elif cmd == "exit":
                log("🚪 Получена команда закрытия из Telegram.")
                self.stop.set()
                raise Stop
            elif cmd == "cal_f5":
                pt = self._cal_pts.get("target_left")
                if pt:
                    log(f"🎯 [F5] Запомнен ЛЕВЫЙ край полосы ЦЕЛИ: x={pt[0]}, y={pt[1]}. Наведи на правый край и нажми F6!")
            elif cmd == "cal_f6":
                p1 = self._cal_pts.get("target_left")
                p2 = self._cal_pts.get("target_right")
                if not p1 or not p2:
                    log("⚠ Сначала нажми F5 на левом краю полосы цели!")
                else:
                    ox, oy = self.vision.get_origin()   # в конфиг пишем смещение от клиентской области окна
                    x = max(0, min(p1[0], p2[0]) - ox)
                    w = max(10, abs(p2[0] - p1[0]))
                    y = max(0, int((p1[1] + p2[1]) / 2) - 1 - oy)
                    _update_yaml_bar("target_bar", x, y, w, 3)
                    self.reload_config()
                    cv2.imwrite("roi_debug.png", cv2.resize(self.vision.grab(), None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
                    hp = self.vision.target_hp()
                    msg = f"✅ [F6] Полоса ЦЕЛИ (target_bar) сохранена: x={x}, y={y}, w={w}, h=3 | Сейчас видит HP цели: {hp:.1f}%"
                    log(msg)
                    self.notifier.send_text(msg)
            elif cmd == "cal_f7":
                pt = self._cal_pts.get("player_left")
                if pt:
                    log(f"❤️ [F7] Запомнен ЛЕВЫЙ край полосы ПЕРСОНАЖА: x={pt[0]}, y={pt[1]}. Наведи на правый край и нажми F8!")
            elif cmd == "cal_f8":
                p1 = self._cal_pts.get("player_left")
                p2 = self._cal_pts.get("player_right")
                if not p1 or not p2:
                    log("⚠ Сначала нажми F7 на левом краю своей полосы HP!")
                else:
                    ox, oy = self.vision.get_origin()
                    x = max(0, min(p1[0], p2[0]) - ox)
                    w = max(10, abs(p2[0] - p1[0]))
                    y = max(0, int((p1[1] + p2[1]) / 2) - oy)
                    _update_yaml_bar("player_bar", x, y, w, 2)
                    self.reload_config()
                    cv2.imwrite("roi_player_debug.png", cv2.resize(self.vision.grab_player(), None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
                    hp = self.vision.player_hp()
                    msg = f"✅ [F8] Полоса ПЕРСОНАЖА (player_bar) сохранена: x={x}, y={y}, w={w}, h=2 | Сейчас видит твоё HP: {hp:.1f}%"
                    log(msg)
                    self.notifier.send_text(msg)

    def _poll_death(self):
        if self.detector.check() or self.simulate_death:
            raise Died

    def _reset_stall_timer(self):
        self._last_kill_mono = time.monotonic()
        self._last_stall_alert_mono = 0.0

    def _check_stall(self):
        if not self.stall_enabled or self.stall_minutes <= 0:
            return
        now = time.monotonic()
        elapsed_sec = now - self._last_kill_mono
        threshold_sec = self.stall_minutes * 60.0
        if elapsed_sec >= threshold_sec and (now - self._last_stall_alert_mono) >= threshold_sec:
            self._last_stall_alert_mono = now
            mins = int(elapsed_sec // 60)
            msg = f"⚠️ Нет убийств уже {mins} мин — возможно, бот застрял."
            log(msg)
            self.notifier.send_text(msg)

    def checkpoint(self):
        while self.paused.is_set():
            if self.stop.is_set():
                raise Stop
            self._process_tg_commands()
            if self.simulate_death:
                raise Died
            time.sleep(0.1)
        if self.stop.is_set():
            raise Stop
        self._process_tg_commands()
        self._poll_death()
        self._check_stall()

    def wait(self, sec):
        end = time.time() + sec
        while time.time() < end:
            if self.stop.is_set():
                raise Stop
            self._process_tg_commands()
            self._poll_death()
            time.sleep(0.03)

    # ---- ввод
    def press(self, key):
        hwnd = focus_game_window()
        l2_key_down(key, hwnd)
        time.sleep(random.uniform(0.16, 0.22))
        l2_key_up(key, hwnd)

    def hold(self, key, sec):
        self.checkpoint()
        hwnd = focus_game_window()
        l2_key_down(key, hwnd)
        try:
            end = time.time() + sec
            while time.time() < end and not self.paused.is_set():
                if self.stop.is_set():
                    raise Stop
                self._process_tg_commands()
                self._poll_death()
                time.sleep(0.03)
        finally:
            l2_key_up(key, hwnd)

    def release_all(self):
        m = self.cfg["move"]
        hwnd = find_game_window()
        for k in [m["forward_key"], *m["turn_keys"]]:
            try:
                l2_key_up(k, hwnd)
            except Exception:
                pass

    def click_screen(self, x: int, y: int) -> None:
        """
        Наводит курсор на (x, y) и кликает левой кнопкой мыши.
        Для надёжности в клиенте KetraWars / Interlude клик синхронизирован
        (SendInput + mouse_event + PostMessageW) и удерживается 100-140 мс.
        """
        hwnd = focus_game_window()
        user32 = ctypes.windll.user32
        x, y = int(x), int(y)
        method = str((self.cfg.get("input") or {}).get("click_method", "legacy")).lower()

        user32.SetCursorPos(x, y)
        time.sleep(0.04)
        for dx in (1, -1):
            user32.mouse_event(0x0001, dx, 0, 0, 0)   # MOUSEEVENTF_MOVE
            time.sleep(0.02)
        user32.SetCursorPos(x, y)
        time.sleep(0.05)

        if method == "pydirectinput":
            try:
                pydirectinput.click(x, y)
            except Exception as exc:
                log(f"⚠ pydirectinput.click не сработал: {exc}")
        elif method == "mouse_event":
            user32.mouse_event(0x0002, 0, 0, 0, 0)    # LEFTDOWN
            time.sleep(random.uniform(0.10, 0.14))
            user32.mouse_event(0x0004, 0, 0, 0, 0)    # LEFTUP
        else:
            # legacy: синхронный клик по всем слоям ввода сразу с удержанием 120 мс
            try:
                pydirectinput.mouseDown(x, y)
            except Exception:
                pass
            user32.mouse_event(0x0002, 0, 0, 0, 0)    # LEFTDOWN
            if hwnd:
                pt = wt.POINT(x, y)
                user32.ScreenToClient(hwnd, ctypes.byref(pt))
                lparam = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
                user32.PostMessageW(hwnd, 0x0201, 1, lparam)  # WM_LBUTTONDOWN

            time.sleep(random.uniform(0.10, 0.14))

            try:
                pydirectinput.mouseUp(x, y)
            except Exception:
                pass
            user32.mouse_event(0x0004, 0, 0, 0, 0)    # LEFTUP
            if hwnd:
                pt = wt.POINT(x, y)
                user32.ScreenToClient(hwnd, ctypes.byref(pt))
                lparam = ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)
                user32.PostMessageW(hwnd, 0x0202, 0, lparam)  # WM_LBUTTONUP

    # ---- чёрный список точек, куда клик не сработал
    def _is_blacklisted(self, x: float, y: float) -> bool:
        now = time.time()
        self._blacklist = [b for b in self._blacklist if b[2] > now]
        r = float((self.cfg.get("mobs") or {}).get("blacklist_radius", 45))
        return any(math.hypot(x - bx, y - by) < r for bx, by, _ in self._blacklist)

    def _blacklist_add(self, x: float, y: float) -> None:
        sec = float((self.cfg.get("mobs") or {}).get("blacklist_sec", 8))
        self._blacklist.append((x, y, time.time() + sec))

    def _report_no_mobs(self, img) -> None:
        """Мобов не видно: сохраняем debug_mobs.png (и раз в 5 минут шлём в Telegram), чтобы понять почему."""
        now = time.monotonic()
        if now - self._last_mob_debug < 20:
            return
        self._last_mob_debug = now
        try:
            jpeg = self.vision.mob_debug_jpeg(img, [])
            log("📷 Мобы на экране не найдены — разметка сохранена в debug_mobs.png "
                "(жёлтое = совпало по цвету). Проверь: python bot.py --diagnose")
            mcfg = self.cfg.get("mobs") or {}
            if mcfg.get("debug_telegram", True) and jpeg and now - self._last_mob_tg > 300:
                self._last_mob_tg = now
                self.notifier.send_photo(jpeg, caption="🔍 Бот не видит мобов. Жёлтое = совпало по цвету, "
                                                       "зелёное = принято как моб.", with_keyboard=False)
        except Exception as exc:
            log(f"⚠ Не удалось сохранить debug_mobs.png: {exc}")

    # ---- действия
    def select_target(self) -> bool:
        """
        Выбирает моба и начинает атаку (порядок как в задании):
        1. Если моб уже в таргете — сразу атакуем.
        2. (опционально, keys.use_next_target) жмём клавишу «следующая цель».
        3. Ищем мобов на экране по надписи Lvl:XX -> наводим курсор -> ЛКМ -> F2.
        4. Ждём, пока в верхней полосе появится HP цели.
        """
        keys = self.cfg.get("keys") or {}
        timing = self.cfg.get("timing") or {}
        mcfg = self.cfg.get("mobs") or {}
        attack = str(keys.get("attack", "f2"))

        # Если цель уже есть
        if self.vision.target_hp() > self.empty:
            return True

        # «Следующая цель» — только если включено (у тебя F1 — скилл, поэтому по умолчанию выключено)
        if keys.get("use_next_target", False):
            self.press(str(keys.get("target", "f1")))
            time.sleep(0.12)
            if self.vision.target_hp() > self.empty:
                self.press(attack)
                return True

        img, found = self.vision.find_mobs_info()
        mobs = [m for m in found if not self._is_blacklisted(m["sx"], m["sy"])]
        if not mobs:
            if not found:
                self._report_no_mobs(img)
            return False

        confirm_sec = float(timing.get("target_confirm", 1.2))
        for m in mobs[:4]:
            self.checkpoint()
            mx, my = m["sx"], m["sy"]
            log(f"🎯 Моб найден: клик в тело ({mx}, {my}) на 1 см ниже ника...")
            self.click_screen(mx, my)

            # Ожидаем появление полосы HP цели
            acquired = False
            deadline = time.time() + confirm_sec
            while time.time() < deadline:
                if self.vision.target_hp() > self.empty:
                    acquired = True
                    break
                self.wait(0.06)

            # Если после первого клика полоса цели ещё не появилась — повторный клик прямо в тело моба
            if not acquired:
                self.click_screen(mx, my)
                self.wait(0.25)
                if self.vision.target_hp() > self.empty:
                    acquired = True

            if acquired or mcfg.get("assume_target_after_click", True):
                hp_val = self.vision.target_hp()
                log(f"✓ Моб взят в таргет! (HP: {hp_val:.0f}%) -> Начинаю атаку {attack.upper()}!")
                self.press(attack)
                return True

            log("✗ После клика цель не взята (полоса HP цели не появилась) — пробую следующего моба.")
            self._blacklist_add(mx, my)

        return False

    def fight(self):
        """Бьёт выбранного моба клавишами ротации (F2, F1), пока моб не умрёт (target_hp <= empty)."""
        t = self.cfg["timing"]
        start = time.time()
        last_hp = self.vision.target_hp()
        last_drop = time.time()

        rotation = self.cfg.get("rotation", [{"key": "f2", "delay": 0.75}])

        log("⚔️ Атакую моба (F2/F1) пока не умрёт...")
        while True:
            for step in rotation:
                self.checkpoint()
                self.press(step["key"])
                self.wait(jitter(step.get("delay", 0.75), 0.05))

                hp = self.vision.target_hp()
                if hp <= self.empty:
                    self.wait(0.15)
                    if self.vision.target_hp() <= self.empty:
                        return "dead"

                if hp < last_hp - 1:
                    last_drop = time.time()
                last_hp = hp

                no_dmg_limit = float(t.get("no_damage_timeout", 15))
                if time.time() - last_drop > no_dmg_limit:
                    log(f"⚠️ Урон по мобу не проходит {int(no_dmg_limit)} сек — сбрасываю цель")
                    return "stuck"
                if time.time() - start > t.get("fight_timeout", 60):
                    log("⚠️ Бой длится более 60 сек — сбрасываю цель")
                    return "timeout"

    def roam(self):
        """Повернуться и при необходимости пробежать вперёд, чтобы найти новых мобов."""
        m = self.cfg["move"]
        turn_time = rnd(m["turn_time"])
        if self.miss >= 3:
            turn_time *= m.get("lost_turn_multiplier", 2.5)
        self.hold(random.choice(m["turn_keys"]), turn_time)
        if self.miss >= 2:
            self.hold(m["forward_key"], rnd(m["forward_time"]))

    def stats(self):
        hours = (time.time() - self.started) / 3600
        per_hour = self.kills / hours if hours > 0 else 0
        return (
            f"убито: {self.kills}, пропущено: {self.skipped}, "
            f"смертей: {self.deaths}, ~{per_hour:.0f} мобов/час"
        )

    def handle_death(self):
        """Обработка смерти персонажа: стоп ввода, скриншот, уведомления и ожидание воскрешения."""
        was_simulated = self.simulate_death
        self.dead = True
        self.deaths += 1
        self.release_all()

        death_time_str = time.strftime("%H:%M:%S")
        death_mono = time.monotonic()
        last_reminder_mono = death_mono
        repeats_sent = 0

        tg_cfg = self.cfg.get("telegram") or {}
        send_screenshot = bool(tg_cfg.get("send_screenshot", True))
        repeat_every_sec = float(tg_cfg.get("repeat_every_sec", 60))
        max_repeats = int(tg_cfg.get("max_repeats", 10))
        auto_resume = bool((self.cfg.get("death") or {}).get("auto_resume", False))
        toggle_key = self.cfg["keys"]["toggle"]

        if was_simulated:
            log("🧪 ТЕСТ СМЕРТИ: ввод остановлен, отправляю тестовое уведомление в Telegram...")
            caption = (
                "🧪 [ТЕСТ ТРЕВОГИ] Проверка уведомления о смерти (персонаж ЖИВ)\n"
                f"Время: {death_time_str}\n"
                f"Убито мобов за сессию: {self.kills}\n"
                "Нажми «▶️ Старт фарма» или «🧪 Тест смерти» в Telegram, чтобы выйти из теста."
            )
        else:
            log("💀 ПЕРСОНАЖ УМЕР: ввод остановлен, отправляю уведомление в Telegram...")
            caption = (
                "💀 ПЕРСОНАЖ УМЕР (или пропала полоса HP — проверь клиент)\n"
                f"Время: {death_time_str}\n"
                f"Убито мобов за сессию: {self.kills}\n"
                f"Смертей за сессию: {self.deaths}\n"
                "Бот остановлен и ждёт воскрешения."
            )

        if send_screenshot:
            try:
                jpeg = self.vision.screenshot_jpeg()
                self.notifier.send_photo(jpeg, caption=caption)
            except Exception as exc:
                log(f"⚠ Не удалось сделать скриншот ({exc}), отправляю только текст.")
                self.notifier.send_text(caption)
        else:
            self.notifier.send_text(caption)

        self.detector.reset()

        # Цикл ожидания воскрешения (без wait() и checkpoint(), чтобы не вызвать рекурсию Died)
        while True:
            if self.stop.is_set():
                raise Stop

            self._process_tg_commands(in_death_loop=True)

            if was_simulated:
                is_alive = not self.simulate_death
            else:
                is_alive = self.detector.alive_again()

            if is_alive:
                break

            now = time.monotonic()
            if (
                repeat_every_sec > 0
                and repeats_sent < max_repeats
                and (now - last_reminder_mono) >= repeat_every_sec
            ):
                repeats_sent += 1
                last_reminder_mono = now
                mins = max(1, round((now - death_mono) / 60))
                rem_msg = (
                    f"⚠️ Персонаж всё ещё мёртв: {mins} мин "
                    f"(напоминание {repeats_sent}/{max_repeats})"
                )
                log(rem_msg)
                self.notifier.send_text(rem_msg)

            time.sleep(0.2)

        # Персонаж снова жив
        self.detector.reset()
        self.miss = 0
        self.dead = False
        self.simulate_death = False
        self._reset_stall_timer()

        if not auto_resume:
            self.paused.set()
            alive_msg = (
                f"✅ Персонаж снова жив (тревога снята). Бот на паузе: нажми «▶️ Старт фарма» в Telegram или {toggle_key} в игре."
            )
            log(f"⏸ Бот на паузе. Нажми «▶️ Старт фарма» в Telegram или {toggle_key} в игре.")
        else:
            self.paused.clear()
            alive_msg = "✅ Персонаж снова жив. Бот продолжает работу."
            log("▶ Персонаж снова жив. Бот продолжает работу.")

        self.notifier.send_text(alive_msg)

    def _iteration(self):
        self.checkpoint()

        if not self.select_target():
            self.miss += 1
            log(f"цели нет (попытка {self.miss}) — бегу искать")
            self.roam()
            return

        self.miss = 0
        result = self.fight()

        if result == "dead":
            self.kills += 1
            self._reset_stall_timer()
            log(f"моб убит. {self.stats()}")
            self.wait(rnd(self.cfg["timing"]["after_kill"]))
        else:
            self.skipped += 1
            log(f"моб пропущен ({result}) — сбрасываю цель")
            self.press("esc")
            self.wait(0.3)
            self.roam()

    def run(self):
        while True:
            try:
                self._iteration()
            except PausedFromTelegram:
                self.release_all()
            except Died:
                self.handle_death()


def diagnose() -> None:
    """python bot.py --diagnose — один снимок окна игры: что бот видит (мобы, полосы HP) + картинки для проверки."""
    cfg = load_config()
    v = Vision(cfg)
    hwnd = find_game_window()
    print("=" * 70)
    print(" ДИАГНОСТИКА (запускай при открытой игре; мобы должны быть в кадре)")
    print("=" * 70)
    print(f" Права Администратора: {'да' if is_admin() else 'НЕТ — клавиши/клики в игру могут не доходить!'}")
    if hwnd:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
        print(f" Окно игры: найдено (HWND {hwnd}, «{buf.value}»)")
    else:
        print(" Окно игры: НЕ найдено — беру весь основной монитор. Заголовок окна должен содержать ketrawars / Lineage II.")
    l, t, w, h = v.get_client_rect()
    print(f" Клиентская область: x={l} y={t} размер {w}x{h}")
    if h < 600:
        print(" ⚠ Окно очень маленькое — проверь, что игра в оконном режиме / без рамки и не свёрнута.")

    img, mobs = v.find_mobs_info()
    cv2.imwrite("screen.png", img)
    cv2.imwrite("debug_mobs.png", mobfinder.draw_debug(img, cfg, mobs))
    print(f"\n Найдено мобов: {len(mobs)}")
    for i, m in enumerate(mobs, 1):
        print(f"   {i}) клик в экранную точку ({m['sx']}, {m['sy']}), надпись {m['box']}, пикселей {m['pixels']}")
    if not mobs:
        print("   Ничего не найдено. Открой debug_mobs.png: ЖЁЛТОЕ — пиксели, совпавшие по цвету.")
        print("   • жёлтого на надписях нет  -> цвет другой: python calibrate.py mob (наведи курсор на «Lvl» и нажми F9)")
        print("   • жёлтое есть, зелёной рамки нет -> режут размеры/зоны: правь секцию mobs в config.yaml")

    thp, php = v.target_hp(), v.player_hp()
    print(f"\n HP цели (target_bar): {thp:.0f}%   | HP персонажа (player_bar): {php:.0f}%")
    print("   Выдели моба вручную: HP цели должно стать ~100%, иначе подправь target_bar (python calibrate.py).")
    t_roi = v.grab()
    if t_roi is not None and t_roi.size > 0:
        cv2.imwrite("roi_debug.png", cv2.resize(t_roi, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
    p_roi = v.grab_player()
    if p_roi is not None and p_roi.size > 0:
        cv2.imwrite("roi_player_debug.png", cv2.resize(p_roi, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
    print("\n Файлы: screen.png (кадр), debug_mobs.png (разметка), roi_debug.png / roi_player_debug.png (полосы HP)")


def test_keys() -> None:
    """python bot.py --test-keys — через 5 сек фокусирует игру и один раз жмёт F2, показывает, дошла ли клавиша."""
    key = str((load_config().get("keys") or {}).get("attack", "f2"))
    print(f"Через 5 секунд активирую окно игры и нажму {key.upper()} (выдели моба/смотри на панель скиллов)...")
    time.sleep(5)
    hwnd = focus_game_window(wait_render=0.5)
    fg = ctypes.windll.user32.GetForegroundWindow()
    print(f"Окно игры: {hwnd}; на переднем плане: {fg}; админ: {is_admin()}")
    if not hwnd or fg != hwnd:
        print("⚠ Окно игры не на переднем плане — клавиша уйдёт не туда.")
    l2_key_down(key, hwnd)
    time.sleep(0.18)
    l2_key_up(key, hwnd)
    print(f"{key.upper()} отправлена (удержание 180 мс). Если скилл сработал — клавиатура работает отлично!")


def main():
    if "--diagnose" in sys.argv:
        diagnose()
        return
    if "--test-keys" in sys.argv:
        test_keys()
        return
    # Отправляем сигнал остановки (ctrl+alt+q) всем старым запущенным копиям бота,
    # прежде чем регистрировать собственные горячие клавиши
    if "--elevated" not in sys.argv:
        try:
            keyboard.send("ctrl+alt+q")
            time.sleep(0.6)
        except Exception:
            pass
    ensure_admin()
    cfg_path = "config.yaml"
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            cfg_path = arg
            break
    cfg = load_config(cfg_path)
    bot = Bot(cfg)

    sim_key = cfg["keys"].get("simulate_death", "ctrl+alt+d")
    keyboard.add_hotkey(cfg["keys"]["toggle"], bot.toggle)
    keyboard.add_hotkey(cfg["keys"]["stop"], bot.stop.set)
    if sim_key:
        keyboard.add_hotkey(sim_key, bot.toggle_simulate_death)

    # Горячие клавиши быстрой калибровки прямо внутри бота (работают на паузе)
    keyboard.add_hotkey("f5", bot.on_f5_hotkey)
    keyboard.add_hotkey("f6", bot.on_f6_hotkey)
    keyboard.add_hotkey("f7", bot.on_f7_hotkey)
    keyboard.add_hotkey("f8", bot.on_f8_hotkey)

    print("======================================================================")
    print(" L2 FARM BOT + TELEGRAM (@L2ddggbot) — KETRAWARS READY")
    print("======================================================================")
    print(f" Права Администратора: {'ДА (нажатия в L2 работают)' if is_admin() else 'НЕТ (подтверди UAC)'}")
    print(f" ▶️ Старт / Пауза:  {cfg['keys']['toggle']}  (или кнопка в Telegram)")
    print(f" 🧪 Тест смерти:    {sim_key}  (или кнопка в Telegram)")
    print(f" 🚪 Выход:          {cfg['keys']['stop']}")
    print("======================================================================\n")

    log("Бот готов. Фокусирую окно KetraWars для проверки полос HP...")
    focus_game_window(wait_render=0.6)

    welcome_caption = (
        "🟢 L2 Farm Bot перезапущен (исправлен захват окна KetraWars)!\n\n"
        + bot.status_report()
        + "\n\nЗапускаю фарм автоматически!"
    )
    try:
        img_full = bot.vision._grab(bot.vision.sct.monitors[1])
        cv2.imwrite("screen.png", img_full)
        startup_jpeg = bot.vision.screenshot_jpeg(quality=80)
        bot.notifier.send_photo(startup_jpeg, caption=welcome_caption)
        log("✓ Стартовый скриншот экрана успешно отправлен в Telegram.")
    except Exception as exc:
        log(f"⚠ Не удалось снять стартовый скриншот ({exc}), отправляю текст.")
        bot.notifier.send_text(welcome_caption)

    # Автоматический старт фарма при запуске, чтобы сразу проверить работу в окне L2
    bot.start_farming(source="tg")
    try:
        bot.run()
    except Stop:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        bot.release_all()
        final_stats = bot.stats()
        bot.notifier.send_text(f"🔴 L2 Farm Bot выключен.\nИтог сессии: {final_stats}")
        bot.notifier.close()
        log("Остановлен. " + final_stats)


if __name__ == "__main__":
    main()
