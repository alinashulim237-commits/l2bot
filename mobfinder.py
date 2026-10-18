"""
Поиск мобов по цветным надписям «Lvl:XX» над головой.

Модуль чистый (только numpy + OpenCV, без Windows API), поэтому его можно тестировать на любой ОС:
    python test_find_mobs.py            — синтетический тест
    python test_find_mobs.py screen.png — прогон по реальному скриншоту (рисует debug_mobs.png)

Все размеры в конфиге заданы для высоты окна 1080 px и автоматически масштабируются
под реальную высоту клиентской области игры. Зоны исключения заданы ДОЛЯМИ окна (0..1).
"""
from __future__ import annotations

import copy
import math

import cv2
import numpy as np

# Зоны интерфейса, где мобов нет (доли клиентской области: x1, y1, x2, y2).
DEFAULT_EXCLUDE = [
    [0.00, 0.00, 0.27, 0.15],    # статус игрока и баффы
    [0.28, 0.00, 0.40, 0.12],    # полоса цели
    [0.54, 0.00, 0.62, 0.12],    # иконки сверху по центру
    [0.90, 0.00, 1.00, 0.20],    # радар
    [0.00, 0.55, 0.46, 1.00],    # чат слева снизу
    [0.00, 0.80, 1.00, 1.00],    # нижние панели со скиллами
    [0.458, 0.389, 0.542, 0.537],  # сам персонаж в центре экрана (ник/титул)
]

DEFAULT_MOBS = {
    # Диапазоны цвета надписи «Lvl:XX» в формате BGR. Можно несколько (агро, пассивные и т.д.).
    "colors": [
        {"low": [110, 108, 212], "high": [135, 135, 240]},   # агрессивные: кораллово-розовый
    ],
    "min_pixels": 10,        # минимум цветных пикселей в надписи (при высоте 1080)
    "min_w": 18, "max_w": 150,
    "min_h": 5, "max_h": 36,
    "min_aspect": 1.8,       # ширина/высота: текст вытянут по горизонтали
    "min_fill": 0.05,        # доля цветных пикселей в рамке надписи
    "max_fill": 0.80,        # больше — сплошное пятно (сундук/текстура), а не текст
    "close_kernel": [14, 5], # склейка букв «Lvl:XX» в одно слово
    "click_dy": 12,          # на сколько px ниже надписи Lvl находится имя моба (клик по имени)
    "exclude": DEFAULT_EXCLUDE,
    "max_candidates": 6,
}


def merged_cfg(cfg: dict | None) -> dict:
    """Накладывает секцию mobs из config.yaml поверх значений по умолчанию."""
    out = copy.deepcopy(DEFAULT_MOBS)
    for k, v in ((cfg or {}).get("mobs") or {}).items():
        out[k] = copy.deepcopy(v)
    return out


def color_mask(img_bgr: np.ndarray, colors: list[dict]) -> np.ndarray:
    mask = np.zeros(img_bgr.shape[:2], dtype=np.uint8)
    for c in colors:
        lo = np.array(c["low"], np.uint8)
        hi = np.array(c["high"], np.uint8)
        mask |= cv2.inRange(img_bgr, lo, hi)
    return mask


def apply_exclusions(mask: np.ndarray, zones: list[list[float]]) -> None:
    h, w = mask.shape[:2]
    for x1, y1, x2, y2 in zones:
        mask[int(y1 * h):int(math.ceil(y2 * h)), int(x1 * w):int(math.ceil(x2 * w))] = 0


def find_mobs(img_bgr: np.ndarray, cfg: dict | None = None) -> list[dict]:
    """
    Возвращает список кандидатов (ближайшие к центру экрана первыми):
        {"x": click_x, "y": click_y, "box": (bx, by, bw, bh), "pixels": n}
    Координаты — в системе изображения (клиентской области окна).
    """
    m = merged_cfg(cfg)
    h, w = img_bgr.shape[:2]
    s = max(0.4, h / 1080.0)

    if img_bgr is None or img_bgr.size == 0:
        return []

    raw = color_mask(img_bgr, m["colors"])
    mask = raw.copy()
    apply_exclusions(mask, m["exclude"])

    kx = max(3, int(round(m["close_kernel"][0] * s)))
    ky = max(3, int(round(m["close_kernel"][1] * s)))
    elem = cv2.getStructuringElement(cv2.MORPH_RECT, (kx, ky))
    try:
        closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, elem)
    except Exception:
        try:
            closed = cv2.erode(cv2.dilate(mask, elem), elem)
        except Exception:
            closed = mask
    try:
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    except Exception:
        contours = []

    cx, cy = w / 2.0, h / 2.0
    cands = []
    for cnt in contours:
        bx, by, bw, bh = cv2.boundingRect(cnt)
        if not (m["min_w"] * s <= bw <= m["max_w"] * s):
            continue
        if not (m["min_h"] * s <= bh <= m["max_h"] * s):
            continue
        if bw / max(1, bh) < m["min_aspect"]:
            continue
        pixels = int(cv2.countNonZero(mask[by:by + bh, bx:bx + bw]))
        if pixels < m["min_pixels"] * s * s:
            continue
        fill = pixels / float(bw * bh)
        if not (m["min_fill"] <= fill <= m["max_fill"]):   # текст «дырявый», сплошное пятно — не моб
            continue
        px = bx + bw // 2
        py = min(h - 2, by + bh + int(round(m["click_dy"] * s)))
        cands.append(((px - cx) ** 2 + (py - cy) ** 2, {"x": px, "y": py, "box": (bx, by, bw, bh), "pixels": pixels}))

    cands.sort(key=lambda t: t[0])
    return [c for _, c in cands][: int(m["max_candidates"])]


def draw_debug(img_bgr: np.ndarray, cfg: dict | None = None, mobs: list[dict] | None = None) -> np.ndarray:
    """Картинка с разметкой: серые рамки — зоны исключения, зелёные — найденные надписи, красная точка — клик."""
    if img_bgr is None or img_bgr.size == 0:
        return np.zeros((100, 100, 3), dtype=np.uint8)
    m = merged_cfg(cfg)
    out = img_bgr.copy()
    h, w = out.shape[:2]

    # подсветим всё, что подошло по цвету (до фильтров по форме/зонам) жёлтым
    raw = color_mask(img_bgr, m["colors"])
    out[raw > 0] = (0, 255, 255)

    for x1, y1, x2, y2 in m["exclude"]:
        cv2.rectangle(out, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), (110, 110, 110), 1)

    if mobs is None:
        mobs = find_mobs(img_bgr, cfg)
    for i, mob in enumerate(mobs):
        bx, by, bw, bh = mob["box"]
        cv2.rectangle(out, (bx, by), (bx + bw, by + bh), (0, 255, 0), 2)
        cv2.circle(out, (mob["x"], mob["y"]), 6, (0, 0, 255) if i == 0 else (255, 0, 0), -1)
        cv2.putText(out, str(i + 1), (bx, max(12, by - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    cv2.putText(out, f"mobs: {len(mobs)}  (yellow = color match)", (10, h - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    return out


def sample_color(img_bgr: np.ndarray, x: int, y: int, radius: int = 10, tol: int = 22) -> dict | None:
    """
    Подбирает диапазон цвета надписи вокруг точки (x, y): берёт самые яркие/насыщенные пиксели в окрестности
    (текст), а не фон. Возвращает {"low": [b,g,r], "high": [b,g,r]} или None.
    """
    h, w = img_bgr.shape[:2]
    x1, x2 = max(0, x - radius), min(w, x + radius + 1)
    y1, y2 = max(0, y - radius), min(h, y + radius + 1)
    patch = img_bgr[y1:y2, x1:x2].reshape(-1, 3).astype(np.int32)
    if len(patch) == 0:
        return None
    hsv = cv2.cvtColor(img_bgr[y1:y2, x1:x2], cv2.COLOR_BGR2HSV).reshape(-1, 3)
    score = hsv[:, 1].astype(np.int32) * (hsv[:, 2].astype(np.int32) > 150)  # насыщенные и яркие
    if score.max() < 40:
        return None
    best = patch[np.argsort(score)[-max(3, len(score) // 25):]]
    med = np.median(best, axis=0).astype(np.int32)
    lo = np.clip(med - tol, 0, 255)
    hi = np.clip(med + tol, 0, 255)
    return {"low": [int(v) for v in lo], "high": [int(v) for v in hi]}
