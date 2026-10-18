"""
Помощник калибровки (с быстрой авто-калибровкой по клавишам F5-F8!).

  python calibrate.py          — показывает координаты курсора и цвет пикселя под ним,
                                 а также позволяет в 2 нажатия сохранить полосы HP в config.yaml:
                                   • F5 (левый край) -> F6 (правый край) = сохранить target_bar (HP цели)
                                   • F7 (левый край) -> F8 (правый край) = сохранить player_bar (HP персонажа)
  python calibrate.py roi      — читает область target_bar из config.yaml и показывает
                                 процент HP цели в реальном времени (+ сохраняет roi_debug.png).
  python calibrate.py player   — читает область player_bar из config.yaml и показывает
                                 процент HP персонажа в реальном времени (+ сохраняет roi_player_debug.png).
  python calibrate.py shot     — сохраняет полный скриншот экрана в screen.png.
"""
import ctypes
import ctypes.wintypes as wt
import re
import sys
import time

import cv2
import keyboard
import mss
import numpy as np

from bot import Vision, load_config

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass


def cursor_pos():
    pt = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _update_yaml_bar(section: str, x: int, y: int, w: int, h: int = 6, path: str = "config.yaml") -> None:
    """Обновляет блок target_bar или player_bar в config.yaml с сохранением всех комментариев."""
    with open(path, encoding="utf-8") as f:
        text = f.read()

    pattern = rf"(^{section}:\s*\r?\n)(\s+x:\s*\d+\r?\n\s+y:\s*\d+\r?\n\s+w:\s*\d+\r?\n\s+h:\s*\d+)"
    replacement = f"{section}:\n  x: {x}\n  y: {y}\n  w: {w}\n  h: {h}"
    new_text, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count > 0:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)


def live_cursor():
    print("======================================================================")
    print(" КАЛИБРОВКА ПОЛОС HP (Двигай курсор в окне игры, Ctrl+C — выход)")
    print("======================================================================")
    print(" Быстрое автосохранение в config.yaml:")
    print("  🎯 Полоса HP ЦЕЛИ (выдели моба):")
    print("     1) Наведи мышь на ЛЕВЫЙ край красной полосы цели и нажми  [F5]")
    print("     2) Наведи мышь на ПРАВЫЙ край красной полосы цели и нажми [F6]")
    print("  ❤️ Полоса HP ПЕРСОНАЖА:")
    print("     1) Наведи мышь на ЛЕВЫЙ край своей полосы HP и нажми      [F7]")
    print("     2) Наведи мышь на ПРАВЫЙ край своей полосы HP и нажми     [F8]")
    print("======================================================================\n")

    pts = {"target_left": None, "player_left": None}

    def on_f5():
        pts["target_left"] = cursor_pos()
        print(f"\n[F5] Запомнил ЛЕВЫЙ край полосы ЦЕЛИ: x={pts['target_left'][0]}, y={pts['target_left'][1]}. Теперь наведи на правый край и нажми F6!")

    def on_f6():
        if not pts["target_left"]:
            print("\n[!] Сначала нажми F5 на левом краю полосы цели!")
            return
        x1, y1 = pts["target_left"]
        x2, y2 = cursor_pos()
        x = min(x1, x2)
        w = max(10, abs(x2 - x1))
        y = max(0, int((y1 + y2) / 2) - 3)
        _update_yaml_bar("target_bar", x, y, w, 6)
        cfg = load_config()
        v = Vision(cfg)
        cv2.imwrite("roi_debug.png", cv2.resize(v.grab(), None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
        print(f"\n✅ [F6] Сохранил target_bar в config.yaml: x={x}, y={y}, w={w}, h=6 | Текущее HP цели: {v.target_hp():.1f}% (см. roi_debug.png)")

    def on_f7():
        pts["player_left"] = cursor_pos()
        print(f"\n[F7] Запомнил ЛЕВЫЙ край полосы ПЕРСОНАЖА: x={pts['player_left'][0]}, y={pts['player_left'][1]}. Теперь наведи на правый край и нажми F8!")

    def on_f8():
        if not pts["player_left"]:
            print("\n[!] Сначала нажми F7 на левом краю своей полосы HP!")
            return
        x1, y1 = pts["player_left"]
        x2, y2 = cursor_pos()
        x = min(x1, x2)
        w = max(10, abs(x2 - x1))
        y = max(0, int((y1 + y2) / 2) - 3)
        _update_yaml_bar("player_bar", x, y, w, 6)
        cfg = load_config()
        v = Vision(cfg)
        cv2.imwrite("roi_player_debug.png", cv2.resize(v.grab_player(), None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
        print(f"\n✅ [F8] Сохранил player_bar в config.yaml: x={x}, y={y}, w={w}, h=6 | Текущее HP персонажа: {v.player_hp():.1f}% (см. roi_player_debug.png)")

    keyboard.add_hotkey("f5", on_f5)
    keyboard.add_hotkey("f6", on_f6)
    keyboard.add_hotkey("f7", on_f7)
    keyboard.add_hotkey("f8", on_f8)

    with mss.mss() as sct:
        while True:
            x, y = cursor_pos()
            try:
                px = np.array(sct.grab({"left": x, "top": y, "width": 1, "height": 1}))[0, 0]
                b, g, r = int(px[0]), int(px[1]), int(px[2])
                hsv = cv2.cvtColor(np.uint8([[[b, g, r]]]), cv2.COLOR_BGR2HSV)[0, 0]
                print(
                    f"\rx={x:<5} y={y:<5} RGB=({r:3},{g:3},{b:3})  "
                    f"HSV=({hsv[0]:3},{hsv[1]:3},{hsv[2]:3})   ",
                    end="",
                    flush=True,
                )
            except Exception:
                pass
            time.sleep(0.15)


def live_roi():
    cfg = load_config()
    vision = Vision(cfg)
    img = vision.grab()
    big = cv2.resize(img, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
    cv2.imwrite("roi_debug.png", big)
    print("Сохранил roi_debug.png (увеличенная область ×4). Ctrl+C — выход.\n")
    while True:
        print(f"\rHP цели: {vision.target_hp():6.1f}%   ", end="", flush=True)
        time.sleep(0.2)


def live_player():
    cfg = load_config()
    vision = Vision(cfg)
    img = vision.grab_player()
    big = cv2.resize(img, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
    cv2.imwrite("roi_player_debug.png", big)
    print("Сохранил roi_player_debug.png (увеличенная область ×4). Ctrl+C — выход.\n")
    while True:
        print(f"\rHP персонажа: {vision.player_hp():6.1f}%   ", end="", flush=True)
        time.sleep(0.2)


def save_shot():
    cfg = load_config()
    vision = Vision(cfg)
    img = vision._grab(vision.sct.monitors[1])
    cv2.imwrite("screen.png", img)
    print("Сохранил полный скриншот экрана в screen.png.")


if __name__ == "__main__":
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    try:
        if mode == "roi":
            live_roi()
        elif mode == "player":
            live_player()
        elif mode == "shot":
            save_shot()
        else:
            live_cursor()
    except KeyboardInterrupt:
        print()
