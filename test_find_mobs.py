import cv2
import numpy as np

def find_mobs_screen(img, ox=0, oy=0):
    h, w = img.shape[:2]
    # Exact colors for KetraWars Lvl:XX tags
    # Aggressive (coral / salmon pink)
    m_aggro = cv2.inRange(img, np.array([110, 108, 212], np.uint8), np.array([135, 135, 240], np.uint8))
    # Passive (light green)
    m_pass = cv2.inRange(img, np.array([140, 210, 150], np.uint8), np.array([165, 238, 178], np.uint8))
    mask = m_aggro | m_pass

    # Dynamic UI exclusion relative to client origin (ox, oy)
    # Status / buffs:
    mask[:oy + 160, :ox + 520] = 0
    # Target bar:
    mask[:oy + 130, ox + 540 : ox + 760] = 0
    # Center CP icon:
    mask[:oy + 130, ox + 1040 : ox + 1180] = 0
    # Top-right radar:
    mask[:oy + 210, ox + 1740 :] = 0
    # Chat & Skillbars bottom:
    mask[oy + 600 :, :ox + 880] = 0
    mask[oy + 850 :, :] = 0

    # Center player exclusion (character body & title)
    cx, cy = w // 2, h // 2
    mask[cy - 120 : cy + 40, cx - 80 : cx + 80] = 0

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (18, 5))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    cands = []
    for c in cnts:
        bx, by, bw, bh = cv2.boundingRect(c)
        is_aggro = (m_aggro[by : by + bh, bx : bx + bw] > 0).sum() > 4
        # Aggressive tags can have 'x2 S' etc (bw up to 130)
        # Passive tags 'Lvl:XX' bw usually 30..56
        if (is_aggro and 18 <= bw <= 135 and 6 <= bh <= 35) or (not is_aggro and 30 <= bw <= 60 and 6 <= bh <= 25):
            mx = bx + bw // 2
            my = min(h - 50, by + bh + 38)
            dist = (mx - cx) ** 2 + (my - cy) ** 2
            # Prioritize aggro, then distance to center
            cands.append((0 if is_aggro else 1, dist, mx, my))

    cands.sort()
    return [(mx, my) for _, _, mx, my in cands]

for fn, ox, oy in [('passive_now2.png', 6, 62), ('live_max.png', 0, 0), ('after_stepA.png', 0, 0)]:
    img = cv2.imread(fn)
    if img is not None:
        mobs = find_mobs_screen(img, ox, oy)
        print(f"{fn}: found {len(mobs)} mobs -> {mobs}")
