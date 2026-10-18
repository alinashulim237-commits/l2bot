"""
Проверка поиска мобов без игры.

  python test_find_mobs.py              — синтетический тест (картинка рисуется кодом)
  python test_find_mobs.py screen.png   — прогон по реальному скриншоту клиента игры,
                                          результат пишет в debug_mobs.png
"""
import sys

import cv2
import numpy as np

import mobfinder

try:
    import yaml
    with open("config.yaml", encoding="utf-8") as f:
        CFG = yaml.safe_load(f) or {}
except Exception:
    CFG = {}

CORAL = (122, 120, 226)  # BGR


def synthetic(w=1920, h=1080):
    img = np.full((h, w, 3), (45, 60, 50), np.uint8)
    s = h / 1080.0
    font, scale = cv2.FONT_HERSHEY_SIMPLEX, 0.55 * s
    expected = []
    for x, y in [(1200, 500), (500, 400), (1500, 700)]:
        cv2.putText(img, "Lvl:52", (int(x * s), int(y * s)), font, scale, CORAL, 2, cv2.LINE_AA)
        cv2.putText(img, "Varka Silenos", (int(x * s) - 20, int(y * s) + 18), font, scale, (255, 255, 255), 1, cv2.LINE_AA)
        expected.append((int(x * s), int(y * s)))
    # то, что НЕ должно находиться
    cv2.rectangle(img, (int(300 * s), int(300 * s)), (int(420 * s), int(330 * s)), CORAL, -1)       # сплошное пятно
    cv2.putText(img, "Lvl:52", (int(60 * s), int(950 * s)), font, scale, CORAL, 2, cv2.LINE_AA)       # чат/нижняя панель
    cv2.putText(img, "Lvl:99", (int(1750 * s), int(60 * s)), font, scale, CORAL, 2, cv2.LINE_AA)      # радар справа сверху
    cv2.putText(img, "Lvl:80", (int(w / 2 - 30), int(h / 2 - 40 * s)), font, scale, CORAL, 2, cv2.LINE_AA)  # сам персонаж
    return img, expected


def run_synthetic():
    ok_all = True
    for w, h in [(1920, 1080), (1600, 900), (1280, 720), (2560, 1440)]:
        img, expected = synthetic(w, h)
        mobs = mobfinder.find_mobs(img, CFG)
        found = [(m["x"], m["y"]) for m in mobs]
        s = h / 1080.0
        good = len(mobs) == 3 and all(
            any(abs(fx - ex) < 60 * s and 0 < fy - ey < 45 * s for fx, fy in found) for ex, ey in expected
        )
        print(f"{w}x{h}: найдено {len(mobs)} (ожидалось 3) -> {'OK' if good else 'FAIL'}  {found}")
        ok_all &= good
        if (w, h) == (1920, 1080):
            cv2.imwrite("debug_mobs_synthetic.png", mobfinder.draw_debug(img, CFG, mobs))
    return ok_all


def run_image(path):
    img = cv2.imread(path)
    if img is None:
        print("Не удалось прочитать", path)
        return False
    mobs = mobfinder.find_mobs(img, CFG)
    cv2.imwrite("debug_mobs.png", mobfinder.draw_debug(img, CFG, mobs))
    print(f"{path}: {img.shape[1]}x{img.shape[0]}, найдено мобов: {len(mobs)}")
    for i, m in enumerate(mobs, 1):
        print(f"  {i}) клик ({m['x']}, {m['y']})  надпись {m['box']}  пикселей {m['pixels']}")
    print("Разметка сохранена в debug_mobs.png (жёлтое = совпало по цвету, зелёное = принято).")
    return True


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(0 if run_image(sys.argv[1]) else 1)
    sys.exit(0 if run_synthetic() else 1)
