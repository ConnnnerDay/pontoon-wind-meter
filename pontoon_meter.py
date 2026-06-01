from luma.core.interface.serial import spi
from luma.lcd.device import ili9341
from PIL import Image, ImageDraw, ImageFont
from urllib.request import urlopen, Request
import json
import logging
import math
import os
import signal
import sys
import textwrap
import threading
import time

from ndbc import ms_to_mph, celsius_to_f, m_to_ft, wind_direction, parse_ndbc, obs_age_minutes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

URL             = "https://www.ndbc.noaa.gov/data/realtime2/41038.txt"
ALERTS_URL      = "https://api.weather.gov/alerts/active?point=34.2108,-77.5986"
POLL_INTERVAL   = 300   # seconds between NDBC refreshes
ALERTS_INTERVAL = 600   # seconds between alert refreshes
FRAME_RATE      = 5     # display frames per second
GAUGE_MAX       = 25    # mph, full-scale
GOOD_MPH        = 12
CAUTION_MPH     = 18
STALE_MINUTES   = 90

# Gauge arc geometry — horseshoe opening at the bottom (PIL degrees)
_GAUGE_ARC_START = 135   # lower-left (0 mph)
_GAUGE_ARC_SWEEP = 270   # clockwise to lower-right (GAUGE_MAX mph)

# Arc boundary angles (PIL degrees, precomputed from thresholds)
GOOD_ARC_END    = round(_GAUGE_ARC_START + (GOOD_MPH    / GAUGE_MAX) * _GAUGE_ARC_SWEEP)
CAUTION_ARC_END = round(_GAUGE_ARC_START + (CAUTION_MPH / GAUGE_MAX) * _GAUGE_ARC_SWEEP)

# Colors
_GREEN  = (0, 160, 70)
_YELLOW = (210, 165, 0)
_RED    = (210, 40, 40)

_STATUS_CONFIG = {
    "GOOD":      (_GREEN,  (0, 55, 22)),
    "CAUTION":   (_YELLOW, (65, 52, 0)),
    "TOO WINDY": (_RED,    (70, 12, 12)),
}

_ALERT_COLORS = {
    "Extreme":  _RED,
    "Severe":   _RED,
    "Moderate": (220, 110, 0),
    "Minor":    _YELLOW,
    "Unknown":  _YELLOW,
}

# Font loading — find the first usable TrueType font once
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]
_FONT_PATH = next((p for p in _FONT_CANDIDATES if os.path.exists(p)), None)
_BOLD_PATH = next((p for p in _BOLD_CANDIDATES if os.path.exists(p)), _FONT_PATH)

def _load_font(size):
    if _FONT_PATH:
        return ImageFont.truetype(_FONT_PATH, size)
    return ImageFont.load_default()

def _load_bold(size):
    if _BOLD_PATH:
        return ImageFont.truetype(_BOLD_PATH, size)
    return _load_font(size)

font_title  = _load_font(15)
font_status = _load_bold(72)   # gust number — massive
font_data   = _load_font(22)
font_label  = _load_font(14)
font_unit   = _load_bold(26)   # mph unit beside gust
font_big    = _load_bold(56)   # status GOOD/CAUTION
font_wide   = _load_bold(44)   # status TOO WINDY (longer text)

try:
    _serial = spi(port=0, device=0, gpio_DC=24, gpio_RST=25)
    device = ili9341(_serial, width=320, height=240, rotate=3)
except Exception as e:
    logging.critical("Display init failed: %s", e)
    sys.exit(1)

# Thread-safe state shared between the data thread and the animation loop
_lock  = threading.Lock()
_state = {
    "wind": None, "gust": None, "wdir": "---",
    "age": None, "wtmp": None, "wvht": None, "atmp": None,
    "gust_history": [],          # most-recent-first list of up to 6 gust readings
    "alerts": [], "error": None,
}

# Animation-loop-only state (not shared; no lock needed)
_needle_gust = 0.0   # smoothed gust value driving the needle
_needle_vel  = 0.0   # velocity for spring-damper overshoot physics


def make_image():
    img = Image.new("RGB", device.size, "black")
    return img, ImageDraw.Draw(img)


def draw_centered(d, y, text, fill, font):
    w = d.textlength(text, font=font)
    d.text((int((device.width - w) / 2), y), text, fill=fill, font=font)


def _fit_text(d, text, font, max_w):
    """Truncate text with ellipsis to fit within max_w pixels."""
    if d.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 1 and d.textlength(text[:-1] + "…", font=font) > max_w:
        text = text[:-1]
    return text[:-1] + "…"


def _draw_status_badge(d, y, msg, frame, font=None, bh=None):
    """Rounded-rectangle badge with a sinusoidally pulsing border."""
    if font is None:
        font = font_status
    if bh is None:
        bh = 30
    accent_base, bg = _STATUS_CONFIG[msg]
    amp = 50 if msg == "TOO WINDY" else 20
    pulse = int(amp * math.sin(frame * math.pi / (FRAME_RATE * 1.2)))
    accent = tuple(min(255, max(0, c + pulse)) for c in accent_base)
    text_w = d.textlength(msg, font=font)
    bw = max(int(text_w) + 24, 120)
    bx = int((device.width - bw) / 2)
    d.rounded_rectangle([bx, y, bx + bw, y + bh], radius=6, fill=bg, outline=accent, width=2)
    d.text((device.width // 2, y + bh // 2), msg, fill="white", font=font, anchor="mm")


def _trend(history):
    """Return 'up', 'down', or 'steady' from a most-recent-first gust list, or None."""
    if len(history) < 4:
        return None
    delta = history[0] - history[3]   # newest vs 15 min ago
    if delta >  1.5: return "up"
    if delta < -1.5: return "down"
    return "steady"


def _draw_trend(d, cx, y, trend):
    """12 px tall directional indicator: up = rising, down = easing, dash = steady."""
    if trend == "up":
        d.polygon([(cx, y), (cx - 6, y + 12), (cx + 6, y + 12)], fill=(240, 130, 0))
    elif trend == "down":
        d.polygon([(cx, y + 12), (cx - 6, y), (cx + 6, y)], fill=(0, 145, 200))
    else:
        d.line([(cx - 7, y + 6), (cx + 7, y + 6)], fill=(85, 85, 85), width=2)


def _draw_speed_lines(d, cx, cy, r):
    """Very faint radial lines spanning the gauge face — speedometer texture."""
    inner  = 18
    outer  = r - 26
    n      = 24
    for i in range(n + 1):
        ang = math.pi * i / n
        ca, sa = math.cos(ang), math.sin(ang)
        d.line([(int(cx + inner * ca), int(cy - inner * sa)),
                (int(cx + outer * ca), int(cy - outer * sa))],
               fill=(14, 14, 14), width=1)


def _draw_compass(d, cx, cy, r, wdir_str):
    """Compact compass rose: dim circle, N tick, and a filled directional arrow."""
    _dir_deg = {"N": 0, "NE": 45, "E": 90, "SE": 135,
                "S": 180, "SW": 225, "W": 270, "NW": 315}

    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(70, 70, 70), width=1)
    # North tick — tiny mark at the top of the circle
    d.line([(cx, cy - r + 1), (cx, cy - r + 4)], fill=(100, 100, 100), width=1)
    # Cardinal marks at E, S, W (single dim pixel each)
    for card_deg in (90, 180, 270):
        cr = math.radians(card_deg)
        d.point((int(cx + (r - 1) * math.sin(cr)), int(cy - (r - 1) * math.cos(cr))),
                fill=(62, 62, 62))

    deg = _dir_deg.get(wdir_str)
    if deg is None:
        d.text((cx, cy), "?", fill=(50, 50, 50), font=font_label, anchor="mm")
        return

    rad = math.radians(deg)
    sin_r, cos_r = math.sin(rad), math.cos(rad)

    # Arrow tip points where wind comes FROM (meteorological convention)
    tip_x = cx + (r - 2) * sin_r
    tip_y = cy - (r - 2) * cos_r
    # Arrowhead base sits 40 % of the way from center toward tip
    base_x = cx + (r * 0.38) * sin_r
    base_y = cy - (r * 0.38) * cos_r
    # Perpendicular half-width for the triangular head
    hw   = 3.0
    pr   = rad + math.pi / 2
    l_x  = base_x + hw * math.sin(pr);  l_y  = base_y - hw * math.cos(pr)
    rr_x = base_x - hw * math.sin(pr);  rr_y = base_y + hw * math.cos(pr)
    # Stem tail extends to the opposite side (shorter)
    tail_x = cx - (r - 5) * sin_r
    tail_y = cy + (r - 5) * cos_r

    d.polygon([(int(tip_x), int(tip_y)), (int(l_x), int(l_y)),
               (int(rr_x), int(rr_y))], fill=(225, 225, 225))
    d.line([(int(base_x), int(base_y)), (int(tail_x), int(tail_y))],
           fill=(130, 130, 130), width=1)
    # Abbreviation in the lower half of the circle, dim so arrow reads over it
    d.text((cx, cy + 3), wdir_str, fill=(95, 95, 95), font=font_label, anchor="mt")


def _draw_wind_streaks(d, cx, cy, r, gust, frame):
    """Animated short dashes flowing along the inner gauge face, speed ∝ wind."""
    speed  = max(2, round(20 - gust * 0.4))
    inner  = r - 24
    n      = 7
    for i in range(n):
        phase = ((frame // speed + i * (100 // n)) % 100) / 100.0
        ang   = math.pi * (1 - phase)
        ca, sa = math.cos(ang), math.sin(ang)
        bright = int(60 + 160 * math.sin(phase * math.pi))   # brighter: 60–220
        perp = ang + math.pi / 2
        cp, sp = math.cos(perp), math.sin(perp)
        hw = 5
        x0 = cx + inner * ca
        y0 = cy - inner * sa
        x1 = int(x0 + hw * cp);  y1 = int(y0 - hw * sp)
        x2 = int(x0 - hw * cp);  y2 = int(y0 + hw * sp)
        d.line([(x1, y1), (x2, y2)], fill=(bright, bright, bright), width=2)


def _draw_weather_icon(d, x, y, status, frame, r=18):
    """Animated weather icon: breathing sun (GOOD), cloud+rain (CAUTION), double bolt (TOO WINDY)."""
    if status == "GOOD":
        breathe = 1 + 0.15 * math.sin(frame * math.pi / FRAME_RATE)
        disc_r  = max(2, int((r - 5) * breathe))
        rot     = (frame * 3) % 360
        for i in range(8):
            ang    = math.radians(rot + i * 45)
            ca, sa = math.cos(ang), math.sin(ang)
            ray_r  = r if i % 2 == 0 else r - 4
            bright = int(200 + 55 * math.sin(frame * math.pi / FRAME_RATE + i * math.pi / 4))
            bright = max(140, min(255, bright))
            x1 = int(x + (disc_r + 2) * ca);  y1 = int(y + (disc_r + 2) * sa)
            x2 = int(x + ray_r * ca);           y2 = int(y + ray_r * sa)
            d.line([(x1, y1), (x2, y2)], fill=(bright, int(bright * 0.78), 0), width=2)
        d.ellipse((x - disc_r, y - disc_r, x + disc_r, y + disc_r), fill=(255, 200, 20))

    elif status == "CAUTION":
        pulse = 0.75 + 0.25 * math.sin(frame * math.pi / (FRAME_RATE * 1.5))
        cc    = tuple(int(c * pulse) for c in (155, 170, 185))
        for bx, by, br in [(-5, 2, 5), (5, 2, 5), (0, -3, 7)]:
            d.ellipse((x+bx-br, y+by-br, x+bx+br, y+by+br), fill=cc)
        cloud_base = y + 7
        for i in range(4):
            dx     = x - 6 + i * 4
            drop_y = cloud_base + (frame * 2 + i * 3) % 12
            if drop_y <= y + r - 2:
                alpha = int(180 + 60 * math.sin(frame * math.pi / FRAME_RATE + i * math.pi / 2))
                d.line([(dx, drop_y), (dx, drop_y + 3)],
                       fill=(70, 130, min(255, alpha)), width=2)

    else:   # TOO WINDY — double lightning bolt with speed lines
        pulse = 0.5 + 0.5 * math.sin(frame * math.pi / (FRAME_RATE * 0.5))
        lc    = (min(255, int(230 * pulse + 40)), min(255, int(100 * pulse)), 0)
        dim   = tuple(max(0, int(c * 0.6)) for c in lc)
        d.polygon([(x-1, y-r+2), (x+2,  y-1), (x-3, y-1)], fill=dim)
        d.polygon([(x-5, y+r-2), (x-2,  y+1), (x-6, y+1)], fill=dim)
        d.polygon([(x+4, y-r+2), (x+7,  y-1), (x+1, y-1)], fill=lc)
        d.polygon([(x,   y+r-2), (x+3,  y+1), (x-3, y+1)], fill=lc)
        for j, (y_off, x_len) in enumerate([(-6, 12), (0, 16), (6, 10)]):
            lc_j = tuple(int(c * pulse * (1 - j * 0.15)) for c in (180, 180, 180))
            d.line([(x - r, y + y_off), (x - r + x_len, y + y_off)], fill=lc_j, width=2)


def _draw_info_bg(d, y_top, y_bot, status, frame):
    """Animated background texture drawn behind the info-section text."""
    if status == "GOOD":
        wl     = 80
        amp    = 4
        scroll = (frame * 2) % wl
        for i, (wy_off, wc) in enumerate([
            (18, (0, 58, 38)),
            (50, (0, 46, 30)),
            (78, (0, 34, 22)),
            (106, (0, 24, 16)),
        ]):
            wy = y_top + wy_off
            if wy >= y_bot:
                continue
            ph   = i * (wl // 4)
            prev = None
            for px in range(device.width + 1):
                sy = wy + int(amp * math.sin(2 * math.pi * (px + scroll + ph) / wl))
                if prev:
                    d.line([prev, (px, sy)], fill=wc, width=1)
                prev = (px, sy)

        # Floating upward particles — slow drift gives GOOD state a lively feel
        span = y_bot - y_top
        for i in range(14):
            px_pos  = (i * 17 + 11) % device.width
            speed   = 1 + (i % 3)
            py_off  = span - (frame * speed + i * (span // 14)) % span
            py_pos  = y_top + int(py_off)
            bright  = int(50 + 35 * math.sin(frame * math.pi / (FRAME_RATE * 2.2) + i * 0.8))
            pc = (0, bright // 2, bright // 3)
            if y_top <= py_pos < y_bot:
                d.point((px_pos, py_pos), fill=pc)
                if py_pos + 1 < y_bot:
                    d.point((px_pos, py_pos + 1), fill=(0, bright // 5, bright // 7))

    elif status == "CAUTION":
        info_h = y_bot - y_top
        for i in range(20):
            bx  = (i * 12) % device.width
            t   = (frame * 2 + i * 7) % (info_h + 14)
            x0, y0 = bx,     y_top + t - 14
            x1, y1 = bx + 8, y0 + 10
            y0c = max(y0, y_top);  y1c = min(y1, y_bot)
            if y1c <= y_top or y0c >= y_bot:
                continue
            bright = 120 + int(80 * math.sin(
                frame * math.pi / (FRAME_RATE * 1.5) + i * math.pi / 5))
            rc = (int(bright * 0.35), int(bright * 0.50), int(bright * 0.80))
            d.line([(x0, y0c), (x1, y1c)], fill=rc, width=2)
            # Splash V-mark when drop reaches the bottom of the info section
            if y1 >= y_bot - 5:
                sp = min(1.0, (y1 - (y_bot - 5)) / 5)
                sw = max(1, int(4 * sp))
                sy_s = min(y_bot - 1, y0 + 10)
                sc_s = (int(rc[0] * 0.5), int(rc[1] * 0.5), int(rc[2] * 0.5))
                d.line([(x0 - sw, sy_s), (x0, sy_s - 2)], fill=sc_s, width=1)
                d.line([(x0, sy_s - 2), (x0 + sw, sy_s)], fill=sc_s, width=1)

    else:   # TOO WINDY — horizontal wind streaks + periodic red alarm flash
        # Brief alarm flash every ~10 s — red wash fades in/out over 4 frames
        flash = (frame * 2) % 100
        if flash < 4:
            flash_r = int(65 * (1 - flash / 4))
            d.rectangle([0, y_top, device.width - 1, y_bot], fill=(flash_r, 0, 0))

        bxs = [8,  52,  96, 140, 184, 228,  30,  74]
        lns = [22, 28,  20,  26,  24,  18,  32,  16]
        spd = [5,   7,   4,   6,   5,   8,   6,   3]
        for row_off in (14, 50, 86, 112):
            ry = y_top + row_off
            if ry >= y_bot:
                continue
            for j in range(8):
                sy = ry + (j % 3 - 1) * 6
                if sy < y_top or sy >= y_bot:
                    continue
                sx = int((bxs[j] - frame * spd[j]) % device.width)
                ex = sx + lns[j]
                bright = 65 + int(55 * math.sin(
                    frame * math.pi / (FRAME_RATE * 0.8) + j * math.pi / 4))
                sc = (bright, bright, bright)
                if ex <= device.width:
                    d.line([(sx, sy), (ex, sy)], fill=sc, width=2)
                else:
                    d.line([(sx, sy), (device.width - 1, sy)], fill=sc, width=2)
                    d.line([(0,  sy), (ex % device.width, sy)], fill=sc, width=2)


def _draw_marine_wave(d, frame, color, y_mid):
    """Scrolling sine wave shown in the bottom strip when no alerts are active."""
    amplitude  = 3
    wavelength = 55
    offset     = (frame * 2) % wavelength
    prev = None
    for x in range(device.width + 1):
        y = y_mid + int(amplitude * math.sin(2 * math.pi * (x + offset) / wavelength))
        if prev is not None:
            d.line([prev, (x, y)], fill=color, width=1)
        prev = (x, y)



def _draw_edge_accents(d, accent, frame):
    """Thin pulsing accent strips on the left and right screen edges."""
    pulse = 0.2 + 0.8 * abs(math.sin(frame * math.pi / (FRAME_RATE * 2.5)))
    for x in range(3):
        fade = (3 - x) / 3.0
        col  = tuple(int(c * pulse * fade * 0.28) for c in accent)
        d.line([(x, 14), (x, device.height - 1)], fill=col)
        d.line([(device.width - 1 - x, 14), (device.width - 1 - x, device.height - 1)], fill=col)


def _draw_alert_strip(d, alerts, frame, status_color, y0, marine_str=None, wind_str=None, age_minutes=None):
    """Top strip: cycles NOAA alerts; when quiet, rotates wind / marine / clock."""
    strip_h = 14
    y_mid = y0 + strip_h // 2
    cx    = device.width // 2
    d.rectangle([0, y0, device.width - 1, y0 + strip_h], fill=(18, 18, 18))
    sep_b = int(38 + 42 * abs(math.sin(frame * math.pi / (FRAME_RATE * 2.0))))
    sep_c = tuple(min(255, int(c * sep_b / 255)) for c in status_color)
    d.line([0, y0 + strip_h, device.width, y0 + strip_h], fill=sep_c)

    if not alerts:
        wave_color = tuple(max(0, c // 4) for c in status_color)
        slots = []
        if wind_str:
            slots.append(("wind",   wind_str))
        if marine_str:
            slots.append(("marine", marine_str))
        slots.append(("clock", None))
        idx  = (frame // (FRAME_RATE * 5)) % len(slots)
        kind, text = slots[idx]
        if kind == "wind":
            d.ellipse((3, y_mid - 3, 9, y_mid + 3), fill=(55, 115, 55))
            d.text((cx, y_mid), text, fill=(175, 195, 175), font=font_label, anchor="mm")
        elif kind == "marine":
            d.ellipse((3, y_mid - 3, 9, y_mid + 3), fill=(35, 70, 115))
            d.text((cx, y_mid), text, fill=(110, 140, 160), font=font_label, anchor="mm")
        else:
            _draw_marine_wave(d, frame, wave_color, y_mid)
            # Tiny clock icon at the left — matches the wind/marine dot position
            d.ellipse((3, y_mid - 3, 9, y_mid + 3), outline=(72, 72, 72), width=1)
            d.line([(6, y_mid), (6, y_mid - 2)], fill=(72, 72, 72), width=1)
            d.line([(6, y_mid), (8, y_mid)],     fill=(72, 72, 72), width=1)
            time_str = time.strftime("%H:%M")
            if age_minutes is not None:
                age_str = f"{int(age_minutes)}m"
                d.text((cx - 4, y_mid), time_str, fill=(110, 110, 110), font=font_label, anchor="rm")
                d.text((cx + 4, y_mid), age_str,  fill=(72, 72, 72),   font=font_label, anchor="lm")
            else:
                d.text((cx, y_mid), time_str, fill=(110, 110, 110), font=font_label, anchor="mm")

        # Horizontal slot progress dots at right edge
        n_slots = len(slots)
        for si in range(n_slots):
            dx = device.width - 4 - (n_slots - 1 - si) * 5
            dc = (tuple(min(255, int(c * 0.65)) for c in status_color) if si == idx
                  else (36, 36, 36))
            d.ellipse((dx - 2, y_mid - 2, dx + 2, y_mid + 2), fill=dc)
        return

    idx = (frame // (FRAME_RATE * 4)) % len(alerts)   # new alert every 4 s
    name, severity = alerts[idx]
    color = _ALERT_COLORS.get(severity, _YELLOW)

    # Pulsing warning dot
    pulse = 0.55 + 0.45 * math.sin(frame * math.pi / (FRAME_RATE * 0.7))
    dot_color = tuple(min(255, int(c * pulse)) for c in color)
    d.ellipse([7, y_mid - 5, 15, y_mid + 5], fill=dot_color)

    # Alert name, truncated to available width
    text = _fit_text(d, name, font_label, device.width - 26)
    d.text((21, y_mid), text, fill=color, font=font_label, anchor="lm")

    # Page indicator when there are multiple alerts
    if len(alerts) > 1:
        count_str = f"{idx + 1}/{len(alerts)}"
        cw = int(d.textlength(count_str, font=font_label))
        d.text((device.width - cw - 4, y_mid), count_str,
               fill=(65, 65, 65), font=font_label, anchor="lm")


def _dim(color, factor):
    """Multiply each channel of a (R,G,B) tuple by factor (0–1)."""
    return tuple(max(0, min(255, int(c * factor))) for c in color)


def _gauge_ang(mph_val):
    """PIL angle (radians) for a given mph value on the horseshoe arc."""
    return math.radians(_GAUGE_ARC_START + (mph_val / GAUGE_MAX) * _GAUGE_ARC_SWEEP)


def _draw_gauge(d, cx, cy, r, needle_gust, actual_gust, frame, stale=False, wind=None, history=None):
    """270-degree horseshoe gauge: arc opens at the bottom; needle, streaks, ticks, labels."""
    box = (cx - r, cy - r, cx + r, cy + r)
    arc_end = _GAUGE_ARC_START + _GAUGE_ARC_SWEEP

    # Outer border ring — thin arc framing the instrument face
    ob  = r + 10
    d.arc((cx - ob, cy - ob, cx + ob, cy + ob),
          _GAUGE_ARC_START - 4, arc_end + 4, fill=(42, 42, 42), width=2)

    # Dark backing arc
    d.arc(box, _GAUGE_ARC_START - 2, arc_end + 2, fill=(30, 30, 30), width=22)

    # Subtle radial speed lines — speedometer texture behind the arc bands
    _draw_speed_lines(d, cx, cy, r)

    # Expanding concentric ripples from the hub — subtle interior animation
    for i in range(3):
        rr = int((frame * 1.5 + i * 20) % 52)
        if 2 <= rr <= 50:
            rb = max(0, int(18 * (1 - rr / 52)))
            if rb:
                d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=(rb, rb, rb), width=1)

    dim = 0.30 if stale else 1.0

    # Soft ambient glow behind zone arcs — wider dim pre-pass for depth
    glow_dim = dim * 0.22
    d.arc(box, _GAUGE_ARC_START, GOOD_ARC_END,    fill=_dim(_GREEN,  glow_dim), width=26)
    d.arc(box, GOOD_ARC_END,    CAUTION_ARC_END,  fill=_dim(_YELLOW, glow_dim), width=26)
    d.arc(box, CAUTION_ARC_END, arc_end,           fill=_dim(_RED,    glow_dim), width=26)

    # Colored zone arcs
    d.arc(box, _GAUGE_ARC_START, GOOD_ARC_END,    fill=_dim(_GREEN,  dim), width=16)
    d.arc(box, GOOD_ARC_END,    CAUTION_ARC_END,  fill=_dim(_YELLOW, dim), width=16)
    d.arc(box, CAUTION_ARC_END, arc_end,          fill=_dim(_RED,    dim), width=16)

    # Pulsing overlay on the currently-active zone arc — breathes to indicate live zone
    if not stale:
        zp = 0.12 + 0.10 * math.sin(frame * math.pi / (FRAME_RATE * 1.8))
        if needle_gust < GOOD_MPH:
            za_s, za_e, zc = _GAUGE_ARC_START, GOOD_ARC_END, _GREEN
        elif needle_gust <= CAUTION_MPH:
            za_s, za_e, zc = GOOD_ARC_END, CAUTION_ARC_END, _YELLOW
        else:
            za_s, za_e, zc = CAUTION_ARC_END, arc_end, _RED
        d.arc(box, za_s, za_e, fill=_dim(zc, zp), width=26)

    # Bright narrow trace from 0 to current needle — highlights the swept arc
    if needle_gust > 0.1 and not stale:
        na_end  = _GAUGE_ARC_START + (needle_gust / GAUGE_MAX) * _GAUGE_ARC_SWEEP
        trace_c = ((0, 220, 100) if needle_gust < GOOD_MPH
                   else ((255, 210, 0) if needle_gust <= CAUTION_MPH
                         else (255, 80, 80)))
        d.arc(box, _GAUGE_ARC_START, na_end, fill=trace_c, width=4)

    # Animated wind streaks flowing inside the arc face
    _draw_wind_streaks(d, cx, cy, r, actual_gust, frame)

    # Tick marks — colored by zone for instant zone-boundary feedback
    tick_outer = r - 8
    tick_inner = r - 18
    for mph_val in range(0, GAUGE_MAX + 1, 5):
        ang = _gauge_ang(mph_val)
        ca, sa = math.cos(ang), math.sin(ang)
        x1, y1 = cx + tick_outer * ca, cy + tick_outer * sa
        x2, y2 = cx + tick_inner * ca, cy + tick_inner * sa
        is_major = (mph_val % 10 == 0) or mph_val == GAUGE_MAX
        t_dim = dim * (0.7 if is_major else 0.45)
        if mph_val <= GOOD_MPH:
            tick_c = _dim(_GREEN, t_dim)
        elif mph_val <= CAUTION_MPH:
            tick_c = _dim(_YELLOW, t_dim)
        else:
            tick_c = _dim(_RED, t_dim)
        d.line([(int(x1), int(y1)), (int(x2), int(y2))],
               fill=tick_c, width=2 if is_major else 1)

    # Zone-boundary labels: just the two threshold values, inside the gauge face
    for mph_val, lbl in [(GOOD_MPH, str(GOOD_MPH)), (CAUTION_MPH, str(CAUTION_MPH))]:
        ang = _gauge_ang(mph_val)
        ca, sa = math.cos(ang), math.sin(ang)
        lx, ly = cx + (r - 28) * ca, cy + (r - 28) * sa
        d.text((int(lx), int(ly)), lbl, fill=(165, 165, 165), font=font_label, anchor="mm")

    # Dim "GUST" label in upper-center of gauge interior — context for the needle
    d.text((cx, cy - 50), "GUST", fill=(48, 48, 48), font=font_label, anchor="mm")

    # Peak gust marker — bright tick just outside the arc at session-max position
    if history and len(history) >= 2 and not stale:
        peak = max(history)
        if peak > actual_gust + 0.3:
            p_ang  = _gauge_ang(min(peak, GAUGE_MAX))
            p_ca, p_sa = math.cos(p_ang), math.sin(p_ang)
            po = (cx + (r + 4) * p_ca, cy + (r + 4) * p_sa)
            pi = (cx + (r - 4) * p_ca, cy + (r - 4) * p_sa)
            d.line([(int(po[0]), int(po[1])), (int(pi[0]), int(pi[1]))],
                   fill=(220, 200, 55), width=2)

    if stale:
        d.text((cx, cy), "STALE", fill=(55, 55, 55), font=font_label, anchor="mm")

    # Sustained avg wind: diamond marker on the arc at r-10, plus tiny readout below hub
    if wind is not None and not stale:
        w_ang  = _gauge_ang(min(wind, GAUGE_MAX))
        w_ca, w_sa = math.cos(w_ang), math.sin(w_ang)
        wx = int(cx + (r - 10) * w_ca)
        wy = int(cy + (r - 10) * w_sa)
        ds = 4
        d.polygon([(wx, wy - ds), (wx + ds, wy), (wx, wy + ds), (wx - ds, wy)],
                  fill=(45, 45, 45), outline=(145, 145, 145))
        d.text((cx, cy + 24), f"avg {wind:.0f}", fill=(72, 72, 72), font=font_label, anchor="mm")

    # Kite-shaped needle — soft glow arc at its angle for a back-lit instrument feel
    pct = min(max(needle_gust / GAUGE_MAX, 0), 1)
    ang = _gauge_ang(pct * GAUGE_MAX)
    if not stale and needle_gust > 0.1:
        glow_ang = _GAUGE_ARC_START + (needle_gust / GAUGE_MAX) * _GAUGE_ARC_SWEEP
        glow_c   = ((0, 28, 14) if needle_gust < GOOD_MPH
                    else ((32, 24, 0) if needle_gust <= CAUTION_MPH
                          else (32, 6, 6)))
        d.arc(box, glow_ang - 7, glow_ang + 7, fill=glow_c, width=22)
    ca, sa = math.cos(ang), math.sin(ang)
    perp = ang - math.pi / 2
    cp, sp = math.cos(perp), math.sin(perp)
    tip   = (cx + r  * ca,  cy + r  * sa)
    wide  = (cx + 8  * ca,  cy + 8  * sa)
    tail  = (cx - 14 * ca,  cy - 14 * sa)
    hw = 5.5
    left  = (wide[0] + hw * cp, wide[1] + hw * sp)
    right = (wide[0] - hw * cp, wide[1] - hw * sp)
    needle_fill = (90, 90, 90) if stale else (240, 240, 240)
    # Shadow polygon offset (+2,+2) for depth
    d.polygon(
        [(int(tip[0])   + 2, int(tip[1])   + 2),
         (int(left[0])  + 2, int(left[1])  + 2),
         (int(tail[0])  + 2, int(tail[1])  + 2),
         (int(right[0]) + 2, int(right[1]) + 2)],
        fill=(10, 10, 10),
    )
    d.polygon(
        [(int(tip[0]),   int(tip[1])),
         (int(left[0]),  int(left[1])),
         (int(tail[0]),  int(tail[1])),
         (int(right[0]), int(right[1]))],
        fill=needle_fill,
    )

    # Pulsing colored dot at needle tip — breathes to draw the eye
    if not stale:
        tc = ((0, 200, 90) if needle_gust < GOOD_MPH
              else ((240, 190, 0) if needle_gust <= CAUTION_MPH
                    else (240, 60, 60)))
        tp = 0.65 + 0.35 * math.sin(frame * math.pi / (FRAME_RATE * 0.7))
        tr = max(2, int(5 * tp))
        d.ellipse((int(tip[0]) - tr, int(tip[1]) - tr,
                   int(tip[0]) + tr, int(tip[1]) + tr), fill=tc)

    # Pivot hub — three concentric rings; center dot colored by current zone
    d.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill=(28, 28, 28), outline=(90, 90, 90), width=1)
    d.ellipse((cx -  8, cy -  8, cx +  8, cy +  8), fill=(18, 18, 18), outline=(55, 55, 55), width=1)
    if stale:
        hub_c = (90, 90, 90)
    elif actual_gust > CAUTION_MPH:
        hub_c = (220, 55, 55)
    elif actual_gust > GOOD_MPH:
        hub_c = (210, 170, 0)
    else:
        hub_c = (0, 180, 80)
    d.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=hub_c)


def render_display(state, frame, needle_gust):
    wind    = state["wind"]
    gust    = state["gust"]
    wdir    = state["wdir"]
    age     = state["age"]
    wtmp    = state["wtmp"]
    wvht    = state["wvht"]
    atmp    = state["atmp"]
    alerts  = state["alerts"]
    history = state.get("gust_history", [])

    msg    = ("GOOD" if gust < GOOD_MPH
              else "CAUTION" if gust <= CAUTION_MPH
              else "TOO WINDY")
    accent = _STATUS_CONFIG[msg][0]

    trend = _trend(history)

    img, d = make_image()
    r  = 80
    # Advisory strip occupies y=0-14; gauge starts 14px below that
    cy = 14 + 14 + (r + 11)
    cx = device.width // 2

    # Pre-compute info_y so the background draws before gauge text
    info_y = cy + int(r * 0.707) + 8

    # Draw animated info-section background first (behind all text)
    _draw_info_bg(d, info_y, device.height - 1, msg, frame)

    stale = age is not None and age >= STALE_MINUTES
    _draw_gauge(d, cx, cy, r, needle_gust, gust, frame, stale=stale, wind=wind, history=history)

    # Compass rose inside the arc — upper-left to clear the "18" label at upper-right
    _draw_compass(d, cx - 38, cy - 22, 14, wdir)

    # Data freshness bar — centered horizontal line in the gauge mouth gap
    if age is not None:
        freshness = max(0.0, 1.0 - age / STALE_MINUTES)
        bar_hw = int(42 * freshness)
        if bar_hw > 0:
            bright = max(14, int(40 * freshness))
            by = info_y - 7
            d.line([(cx - bar_hw, by), (cx + bar_hw, by)], fill=(bright, bright, bright), width=1)

    # Pulsing separator — slowly breathes in the accent color
    sep_p   = 0.25 + 0.75 * abs(math.sin(frame * math.pi / (FRAME_RATE * 3)))
    sep_col = tuple(int(c * sep_p * 0.22) for c in accent)
    d.line([0, info_y - 4, device.width, info_y - 4], fill=sep_col)

    # Row 1 — status word, pulsing fill for urgent states; grey when stale
    status_font = font_wide if msg == "TOO WINDY" else font_big
    if stale:
        text_fill = (55, 55, 55)
    else:
        if msg == "TOO WINDY":
            pv = int(55 * math.sin(frame * math.pi / (FRAME_RATE * 0.8)))
        elif msg == "CAUTION":
            pv = int(25 * math.sin(frame * math.pi / (FRAME_RATE * 1.5)))
        else:
            pv = 0
        text_fill = tuple(min(255, max(0, c + pv)) for c in accent)
    d.text((cx, info_y + 28), msg, fill=text_fill, font=status_font, anchor="mm")

    # Row 2 — big gust number + "mph" unit; grey when stale
    if stale:
        gust_fill = (50, 50, 50)
        mph_fill  = (40, 40, 40)
    elif gust >= CAUTION_MPH:
        gp = 0.55 + 0.45 * math.sin(frame * math.pi / (FRAME_RATE * 1.0))
        gust_fill = tuple(min(255, int(c * gp)) for c in accent)
        mph_fill  = (140, 140, 140)
    else:
        gust_fill = (220, 220, 220)
        mph_fill  = (140, 140, 140)
    num_str = f"{gust:.1f}"
    num_w   = int(d.textlength(num_str, font=font_status))
    unit_w  = int(d.textlength("mph",   font=font_unit))
    grp_x   = (device.width - num_w - 8 - unit_w) // 2
    d.text((grp_x,              info_y + 93),      num_str, fill=gust_fill, font=font_status, anchor="lm")
    d.text((grp_x + num_w + 8,  info_y + 93 + 18), "mph",   fill=mph_fill,  font=font_unit,   anchor="lm")

    # Trend arrow at right margin, vertically centered on the gust row
    if trend is not None:
        _draw_trend(d, device.width - 16, info_y + 87, trend)

    # Wind + trend shown in the advisory strip (cycling with marine / clock)
    dir_tag  = f"  {wdir}" if wdir else ""
    wind_str = f"Wind {wind:.1f} mph{dir_tag}"
    if trend is not None:
        arrow = "↑" if trend == "up" else ("↓" if trend == "down" else "→")
        wind_str += f" {arrow}"

    # Build marine string for the advisory strip cycle
    marine_parts = []
    if wtmp is not None:
        marine_parts.append(f"Water {wtmp:.0f}°")
    if atmp is not None:
        marine_parts.append(f"Air {atmp:.0f}°")
    if wvht is not None:
        marine_parts.append(f"{wvht:.1f}ft waves")
    marine_str = "  ".join(marine_parts) if marine_parts else None

    # Pulsing accent strips framing the left/right screen edges
    _draw_edge_accents(d, accent, frame)

    # Animated wave at screen bottom — completes the edge framing
    accent_dim  = tuple(c // 6 for c in accent)
    bwave_off   = (frame * 3) % 22
    for bx in range(device.width):
        by = device.height - 1 - int(1.5 * math.sin(2 * math.pi * (bx + bwave_off) / 22))
        d.point((bx, by), fill=accent_dim)

    # Top strip: NOAA advisories → wind → marine → clock/wave
    _draw_alert_strip(d, alerts, frame, accent, y0=0,
                      marine_str=marine_str, wind_str=wind_str, age_minutes=age)

    device.display(img)


def fetch_alerts():
    req = Request(ALERTS_URL,
                  headers={"User-Agent": "pontoon-wind-meter/1.0",
                            "Accept": "application/geo+json"})
    last_exc = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            seen, result = set(), []
            for feat in data.get("features", []):
                props = feat.get("properties", {})
                event = props.get("event", "")
                sev   = props.get("severity", "Unknown")
                if event and event not in seen:
                    seen.add(event)
                    result.append((event.upper(), sev))
            return result
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                logging.warning("Alert fetch attempt %d failed: %s", attempt + 1, exc)
                time.sleep(5)
    raise last_exc


def handle_exit(sig, _frame):
    logging.info("Shutting down (signal %d)", sig)
    try:
        img, d = make_image()
        draw_centered(d, 125, "Offline", (80, 80, 80), font_big)
        device.display(img)
    except Exception:
        pass
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_exit)
signal.signal(signal.SIGINT,  handle_exit)


def fetch_ndbc():
    req = Request(URL, headers={"User-Agent": "pontoon-wind-meter/1.0"})
    last_exc = None
    for attempt in range(3):
        try:
            with urlopen(req, timeout=10) as resp:
                return resp.read().decode()
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                logging.warning("Fetch attempt %d failed: %s", attempt + 1, exc)
                time.sleep(5)
    raise last_exc


def _data_loop():
    """Background thread: refresh NDBC wind data and NOAA alerts on independent timers."""
    last_alert_fetch = 0.0
    while True:
        t0 = time.monotonic()

        try:
            row = parse_ndbc(fetch_ndbc())
            if row.get("WSPD", "MM") == "MM":
                raise ValueError("WSPD reading unavailable")
            wind     = ms_to_mph(float(row["WSPD"]))
            gust_raw = row.get("GST", "MM")
            gust     = ms_to_mph(float(gust_raw)) if gust_raw != "MM" else wind
            wdir     = wind_direction(row.get("WDIR", "MM"))
            age      = obs_age_minutes(row)
            wtmp_raw = row.get("WTMP", "MM")
            wvht_raw = row.get("WVHT", "MM")
            atmp_raw = row.get("ATMP", "MM")
            wtmp = celsius_to_f(float(wtmp_raw)) if wtmp_raw != "MM" else None
            wvht = m_to_ft(float(wvht_raw))      if wvht_raw != "MM" else None
            atmp = celsius_to_f(float(atmp_raw)) if atmp_raw != "MM" else None
            with _lock:
                new_history = [gust] + _state["gust_history"][:5]
                _state.update(wind=wind, gust=gust, wdir=wdir, age=age,
                              wtmp=wtmp, wvht=wvht, atmp=atmp,
                              gust_history=new_history, error=None)
            logging.info("wind=%.1f mph gust=%.1f mph dir=%s age=%sm wtmp=%s wvht=%s",
                         wind, gust, wdir, age,
                         f"{wtmp:.1f}°F" if wtmp is not None else "MM",
                         f"{wvht:.1f}ft" if wvht is not None else "MM")
        except Exception as e:
            logging.error("NDBC fetch failed: %s", e)
            with _lock:
                _state["error"] = str(e)

        if time.monotonic() - last_alert_fetch >= ALERTS_INTERVAL:
            try:
                alerts = fetch_alerts()
                with _lock:
                    _state["alerts"] = alerts
                if alerts:
                    logging.info("Active alerts: %s", [a[0] for a in alerts])
                else:
                    logging.info("No active alerts")
            except Exception as e:
                logging.warning("Alert fetch failed: %s", e)
            last_alert_fetch = time.monotonic()

        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, POLL_INTERVAL - elapsed))


def main():
    global _needle_gust, _needle_vel

    threading.Thread(target=_data_loop, daemon=True).start()

    frame = 0
    while True:
        t0 = time.monotonic()

        with _lock:
            snap                  = dict(_state)
            snap["alerts"]        = list(_state["alerts"])
            snap["gust_history"]  = list(_state["gust_history"])

        if snap["wind"] is not None:
            # Spring-damper: natural overshoot and settle like a real needle
            diff = snap["gust"] - _needle_gust
            _needle_vel  = max(-6.0, min(6.0, _needle_vel * 0.50 + diff * 0.28))
            _needle_gust = max(0.0, min(GAUGE_MAX + 3, _needle_gust + _needle_vel))
            try:
                render_display(snap, frame, _needle_gust)
            except Exception:
                logging.exception("Render failed")
        elif snap["error"] is not None:
            try:
                img, d = make_image()
                ep  = 0.45 + 0.55 * math.sin(frame * math.pi / (FRAME_RATE * 0.7))
                ec  = tuple(int(c * (0.35 + 0.65 * ep)) for c in _RED)
                ec2 = tuple(c // 2 for c in ec)
                # Layered pulsing corner triangles
                for sz, col in ((30, ec2), (16, ec)):
                    d.polygon([(0, 0), (sz, 0), (0, sz)], fill=col)
                    d.polygon([(device.width - 1, 0),
                               (device.width - 1 - sz, 0),
                               (device.width - 1, sz)], fill=col)
                draw_centered(d, 78, "ERROR", ec, font_big)
                err_text = textwrap.shorten(snap["error"], width=34, placeholder="…")
                d.text((12, 150), err_text, fill=(160, 160, 160), font=font_data)
                device.display(img)
            except Exception:
                logging.exception("Error screen render failed")
        else:
            # Animated connecting screen — spinning arc + pulsing text
            try:
                dots   = "." * ((frame // FRAME_RATE) % 4)
                bright = int(60 + 30 * math.sin(frame * math.pi / (FRAME_RATE * 2)))
                img, d = make_image()
                cx_s  = device.width // 2
                spin  = (frame * 14) % 360
                bc    = (bright, bright, bright)
                d.arc((cx_s - 30, 110, cx_s + 30, 170), spin, spin + 115, fill=bc, width=3)
                d.arc((cx_s - 30, 110, cx_s + 30, 170), spin + 115, spin + 230,
                      fill=(bright // 4, bright // 4, bright // 4), width=2)
                draw_centered(d, 185, "PONTOON WIND",
                              (bright, bright, bright), font_title)
                draw_centered(d, 204, f"Connecting{dots}",
                              (bright - 20, bright - 20, bright - 20), font_data)
                device.display(img)
            except Exception:
                logging.exception("Connecting screen render failed")

        frame += 1
        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, 1 / FRAME_RATE - elapsed))


if __name__ == "__main__":
    main()
