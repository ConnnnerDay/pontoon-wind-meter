from luma.core.interface.serial import spi
from luma.lcd.device import ili9341
from PIL import Image, ImageDraw, ImageFont
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
import io
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
FRAME_RATE      = 30    # display frames per second
WEB_PORT        = 8080  # iframe dashboard HTTP port
GAUGE_MAX       = 25    # mph, full-scale
GOOD_MPH        = 12
CAUTION_MPH     = 18
STALE_MINUTES   = 90
_SS             = 2     # supersampling scale — render at 2× then LANCZOS to device

# Gauge arc geometry — horseshoe opening at the bottom (PIL degrees)
_GAUGE_ARC_START = 135   # lower-left (0 mph)
_GAUGE_ARC_SWEEP = 270   # clockwise to lower-right (GAUGE_MAX mph)

# Arc boundary angles (PIL degrees, precomputed from thresholds)
GOOD_ARC_END    = round(_GAUGE_ARC_START + (GOOD_MPH    / GAUGE_MAX) * _GAUGE_ARC_SWEEP)
CAUTION_ARC_END = round(_GAUGE_ARC_START + (CAUTION_MPH / GAUGE_MAX) * _GAUGE_ARC_SWEEP)

# Colors
_GREEN  = (0, 210, 85)
_YELLOW = (235, 190, 0)
_RED    = (235, 65, 55)

_STATUS_CONFIG = {
    "GOOD":      (_GREEN,  (0, 80, 32)),
    "CAUTION":   (_YELLOW, (90, 72, 0)),
    "TOO WINDY": (_RED,    (95, 20, 16)),
}

_ALERT_COLORS = {
    "Extreme":  _RED,
    "Severe":   _RED,
    "Moderate": (220, 110, 0),
    "Minor":    _YELLOW,
    "Unknown":  _YELLOW,
}

# Font loading — bundled fonts ship in assets/ so the Pi always has them
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_FONT_CANDIDATES = [
    os.path.join(_ASSETS_DIR, "DejaVuSans.ttf"),          # bundled — always first
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
_BOLD_CANDIDATES = [
    os.path.join(_ASSETS_DIR, "DejaVuSans-Bold.ttf"),     # bundled — always first
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]
_FONT_PATH = next((p for p in _FONT_CANDIDATES if os.path.exists(p)), None)
_BOLD_PATH = next((p for p in _BOLD_CANDIDATES if os.path.exists(p)), _FONT_PATH)
if _FONT_PATH:
    logging.info("Using font: %s", _FONT_PATH)
else:
    logging.warning("No TrueType font found — text will render as tiny bitmap fallback")

def _load_font(size):
    if _FONT_PATH:
        return ImageFont.truetype(_FONT_PATH, size)
    return ImageFont.load_default()

def _load_bold(size):
    if _BOLD_PATH:
        return ImageFont.truetype(_BOLD_PATH, size)
    return _load_font(size)

font_title  = _load_font(15  * _SS)
font_status = _load_bold(80  * _SS)
font_data   = _load_font(22  * _SS)
font_label  = _load_font(14  * _SS)
font_strip  = _load_font(16  * _SS)
font_unit   = _load_bold(36  * _SS)
font_big    = _load_bold(64  * _SS)
font_wide   = _load_bold(48  * _SS)
font_huge   = _load_bold(80  * _SS)

try:
    _serial = spi(port=0, device=0, gpio_DC=24, gpio_RST=25)
    device = ili9341(_serial, width=320, height=240, rotate=3)
except Exception as e:
    logging.critical("Display init failed: %s", e)
    sys.exit(1)

# Supersampled canvas dimensions — all drawing happens here, then LANCZOS to device
_W = device.width  * _SS
_H = device.height * _SS

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

# Needle spring-damper constants normalised to FRAME_RATE.
# Original tuning: half-life=200ms, spring=0.28 at 5fps (dt=200ms).
# Formula keeps the same real-time response at any frame rate.
_NEEDLE_DAMPING = 0.50 ** (1.0 / (FRAME_RATE * 0.20))  # 0.50 at 5fps, 0.89 at 30fps
_NEEDLE_SPRING  = 0.28  / (FRAME_RATE * 0.20)           # 0.28 at 5fps, 0.047 at 30fps

# Web dashboard frame store — mutable list so no global declaration needed
_frame_lock  = threading.Lock()
_frame_store = [b""]   # [0] = latest rendered frame as PNG bytes

# Pre-loaded GIF weather icon frames (48px, nearest-neighbor from 160×160 source)
_GIF_ICON_SIZE = 48
_GIF_ICONS: dict = {}   # state → list of RGBA PIL Images


_show_counter = [0]

try:
    _RESAMPLE_DOWN = Image.Resampling.BILINEAR
except AttributeError:
    _RESAMPLE_DOWN = Image.BILINEAR  # Pillow < 9.1


def _show(img):
    """Downscale 2× canvas to device resolution, push to display; encode PNG every 2nd call."""
    out = img.resize(device.size, _RESAMPLE_DOWN)
    _show_counter[0] += 1
    if _show_counter[0] % 2 == 0:
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        with _frame_lock:
            _frame_store[0] = buf.getvalue()
    device.display(out)


def make_image():
    img = Image.new("RGB", (_W, _H), "black")
    return img, ImageDraw.Draw(img)


def draw_centered(d, y, text, fill, font):
    w = d.textlength(text, font=font)
    d.text((int((_W - w) / 2), y), text, fill=fill, font=font)


def _fit_text(d, text, font, max_w):
    """Truncate text with ellipsis to fit within max_w pixels."""
    if d.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 1 and d.textlength(text[:-1] + "…", font=font) > max_w:
        text = text[:-1]
    return text[:-1] + "…"




def _trend(history):
    """Return 'up', 'down', or 'steady' from a most-recent-first gust list, or None."""
    if len(history) < 4:
        return None
    delta = history[0] - history[3]   # newest vs 15 min ago
    if delta >  1.5: return "up"
    if delta < -1.5: return "down"
    return "steady"


def _draw_trend(d, cx, y, trend):
    """24 px tall directional indicator: up = rising, down = easing, dash = steady."""
    h = 24 * _SS
    w = 10 * _SS
    if trend == "up":
        d.polygon([(cx, y), (cx - w, y + h), (cx + w, y + h)], fill=(240, 130, 0))
    elif trend == "down":
        d.polygon([(cx, y + h), (cx - w, y), (cx + w, y)], fill=(0, 145, 200))
    else:
        d.line([(cx - 11 * _SS, y + h // 2), (cx + 11 * _SS, y + h // 2)],
               fill=(85, 85, 85), width=4 * _SS)



def _draw_compass(d, cx, cy, r, wdir_str):
    """Compact compass rose: dim circle, N tick, and a filled directional arrow."""
    _dir_deg = {"N": 0, "NE": 45, "E": 90, "SE": 135,
                "S": 180, "SW": 225, "W": 270, "NW": 315}

    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(70, 70, 70), width=_SS)
    # North tick — tiny mark at the top of the circle
    d.line([(cx, cy - r + _SS), (cx, cy - r + 4 * _SS)], fill=(100, 100, 100), width=_SS)
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
    tip_x = cx + (r - 2 * _SS) * sin_r
    tip_y = cy - (r - 2 * _SS) * cos_r
    # Arrowhead base sits 40 % of the way from center toward tip
    base_x = cx + (r * 0.38) * sin_r
    base_y = cy - (r * 0.38) * cos_r
    # Perpendicular half-width for the triangular head
    hw   = 3.0 * _SS
    pr   = rad + math.pi / 2
    l_x  = base_x + hw * math.sin(pr);  l_y  = base_y - hw * math.cos(pr)
    rr_x = base_x - hw * math.sin(pr);  rr_y = base_y + hw * math.cos(pr)
    # Stem tail extends to the opposite side (shorter)
    tail_x = cx - (r - 5 * _SS) * sin_r
    tail_y = cy + (r - 5 * _SS) * cos_r

    d.polygon([(int(tip_x), int(tip_y)), (int(l_x), int(l_y)),
               (int(rr_x), int(rr_y))], fill=(225, 225, 225))
    d.line([(int(base_x), int(base_y)), (int(tail_x), int(tail_y))],
           fill=(130, 130, 130), width=_SS)
    # Abbreviation in the lower half of the circle
    d.text((cx, cy + 3 * _SS), wdir_str, fill=(130, 130, 130), font=font_label, anchor="mt")


def _draw_wind_streaks(d, cx, cy, r, gust, frame):
    """Animated short dashes flowing along the inner gauge face, speed ∝ wind."""
    speed  = max(2, round(20 - gust * 0.4))
    inner  = r - 24 * _SS
    n      = 7
    for i in range(n):
        phase = ((frame // speed + i * (100 // n)) % 100) / 100.0
        ang   = math.pi * (1 - phase)
        ca, sa = math.cos(ang), math.sin(ang)
        bright = int(60 + 160 * math.sin(phase * math.pi))   # brighter: 60–220
        perp = ang + math.pi / 2
        cp, sp = math.cos(perp), math.sin(perp)
        hw = 5 * _SS
        x0 = cx + inner * ca
        y0 = cy - inner * sa
        x1 = int(x0 + hw * cp);  y1 = int(y0 - hw * sp)
        x2 = int(x0 - hw * cp);  y2 = int(y0 + hw * sp)
        d.line([(x1, y1), (x2, y2)], fill=(bright, bright, bright), width=2 * _SS)


def _draw_weather_icon(d, x, y, status, frame, r=18):
    """Animated weather icon: breathing sun (GOOD), cloud+rain (CAUTION), double bolt (TOO WINDY)."""
    if status == "GOOD":
        breathe = 1 + 0.15 * math.sin(frame * math.pi / FRAME_RATE)
        disc_r  = max(2 * _SS, int((r - 5 * _SS) * breathe))
        rot     = (frame * 3) % 360
        for i in range(8):
            ang    = math.radians(rot + i * 45)
            ca, sa = math.cos(ang), math.sin(ang)
            ray_r  = r if i % 2 == 0 else r - 4 * _SS
            bright = int(200 + 55 * math.sin(frame * math.pi / FRAME_RATE + i * math.pi / 4))
            bright = max(140, min(255, bright))
            x1 = int(x + (disc_r + 2 * _SS) * ca);  y1 = int(y + (disc_r + 2 * _SS) * sa)
            x2 = int(x + ray_r * ca);                 y2 = int(y + ray_r * sa)
            d.line([(x1, y1), (x2, y2)], fill=(bright, int(bright * 0.78), 0), width=2 * _SS)
        d.ellipse((x - disc_r, y - disc_r, x + disc_r, y + disc_r), fill=(255, 200, 20))

    elif status == "CAUTION":
        pulse = 0.75 + 0.25 * math.sin(frame * math.pi / (FRAME_RATE * 1.5))
        cc    = tuple(int(c * pulse) for c in (155, 170, 185))
        for bx, by, br in [(-5*_SS, 2*_SS, 5*_SS), (5*_SS, 2*_SS, 5*_SS), (0, -3*_SS, 7*_SS)]:
            d.ellipse((x+bx-br, y+by-br, x+bx+br, y+by+br), fill=cc)
        cloud_base = y + 7 * _SS
        for i in range(4):
            dx     = x - 6 * _SS + i * 4 * _SS
            drop_y = cloud_base + (frame * 2 + i * 3) % (12 * _SS)
            if drop_y <= y + r - 2 * _SS:
                alpha = int(180 + 60 * math.sin(frame * math.pi / FRAME_RATE + i * math.pi / 2))
                d.line([(dx, drop_y), (dx, drop_y + 3 * _SS)],
                       fill=(70, 130, min(255, alpha)), width=2 * _SS)

    else:   # TOO WINDY — double lightning bolt with speed lines
        pulse = 0.5 + 0.5 * math.sin(frame * math.pi / (FRAME_RATE * 0.5))
        lc    = (min(255, int(230 * pulse + 40)), min(255, int(100 * pulse)), 0)
        dim   = tuple(max(0, int(c * 0.6)) for c in lc)
        s = _SS
        d.polygon([(x-s, y-r+2*s), (x+2*s, y-s), (x-3*s, y-s)], fill=dim)
        d.polygon([(x-5*s, y+r-2*s), (x-2*s, y+s), (x-6*s, y+s)], fill=dim)
        d.polygon([(x+4*s, y-r+2*s), (x+7*s, y-s), (x+s, y-s)], fill=lc)
        d.polygon([(x, y+r-2*s), (x+3*s, y+s), (x-3*s, y+s)], fill=lc)
        for j, (y_off, x_len) in enumerate([(-6, 12), (0, 16), (6, 10)]):
            lc_j = tuple(int(c * pulse * (1 - j * 0.15)) for c in (180, 180, 180))
            d.line([(x - r, y + y_off * s), (x - r + x_len * s, y + y_off * s)], fill=lc_j, width=2 * _SS)


def _load_gif_icons():
    """Load and resize weather GIF frames once at startup."""
    try:
        _resample = Image.Resampling.LANCZOS
    except AttributeError:
        _resample = Image.LANCZOS  # Pillow < 9.1
    assets = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    files = {"GOOD": "clear-day.gif", "CAUTION": "rain.gif", "TOO WINDY": "wind.gif"}
    for state, fname in files.items():
        path = os.path.join(assets, fname)
        if not os.path.exists(path):
            logging.warning("Weather icon missing: %s", path)
            continue
        frames, i = [], 0
        try:
            gif = Image.open(path)
            while True:
                try:
                    gif.seek(i)
                    f = gif.convert("RGBA").resize(
                        (_GIF_ICON_SIZE * _SS, _GIF_ICON_SIZE * _SS), _resample)
                    # Key out black background; threshold 30 handles LANCZOS anti-aliased edges
                    px = [(r, g, b, 0) if r + g + b < 30 else (r, g, b, a)
                          for r, g, b, a in f.getdata()]
                    f.putdata(px)
                    frames.append(f)
                    i += 1
                except EOFError:
                    break
        except Exception as exc:
            logging.warning("Cannot load %s: %s", fname, exc)
            continue
        if frames:
            _GIF_ICONS[state] = frames
            logging.info("GIF icon %s: %d frames at %dpx", state, len(frames), _GIF_ICON_SIZE)


def _draw_info_bg(d, y_top, y_bot, status, frame):
    """Animated background texture drawn behind the info-section text."""
    if status == "GOOD":
        scroll = (frame * 2) % (80 * _SS)
        for i, (wy_off, wc, wl_i, amp_i) in enumerate([
            (10,  (0,  99, 64), 70, 3),
            (30,  (0,  86, 56), 90, 5),
            (52,  (0,  74, 48), 80, 4),
            (74,  (0,  61, 40), 65, 3),
            (96,  (0,  48, 32), 80, 4),
            (118, (0,  35, 24), 95, 5),
        ]):
            wy = y_top + wy_off * _SS
            if wy >= y_bot:
                continue
            ph   = i * 17 * _SS
            pts  = [(px, wy + int(amp_i * _SS * math.sin(2 * math.pi * (px + scroll + ph) / (wl_i * _SS))))
                    for px in range(_W + 1)]
            d.line(pts, fill=wc, width=_SS)

        # Floating upward particles — slow drift gives GOOD state a lively feel
        span = y_bot - y_top
        for i in range(14):
            px_pos  = (i * 17 * _SS + 11 * _SS) % _W
            speed   = 1 + (i % 3)
            py_off  = span - (frame * speed + i * (span // 14)) % span
            py_pos  = y_top + int(py_off)
            bright  = int(65 + 55 * math.sin(frame * math.pi / (FRAME_RATE * 2.2) + i * 0.8))
            pc = (0, int(bright * 0.70), int(bright * 0.45))
            if y_top <= py_pos < y_bot:
                d.point((px_pos, py_pos), fill=pc)
                if py_pos + 1 < y_bot:
                    d.point((px_pos, py_pos + 1), fill=(0, bright // 4, bright // 6))

    elif status == "CAUTION":
        info_h = y_bot - y_top
        for i in range(20):
            bx  = (i * 12 * _SS) % _W
            t   = (frame * 2 + i * 7) % (info_h + 14 * _SS)
            x0, y0 = bx,              y_top + t - 14 * _SS
            x1, y1 = bx + 8 * _SS,   y0 + 10 * _SS
            y0c = max(y0, y_top);  y1c = min(y1, y_bot)
            if y1c <= y_top or y0c >= y_bot:
                continue
            bright = 90 + int(50 * math.sin(
                frame * math.pi / (FRAME_RATE * 1.5) + i * math.pi / 5))
            rc = (int(bright * 0.28), int(bright * 0.40), int(bright * 0.65))
            d.line([(x0, y0c), (x1, y1c)], fill=rc, width=2 * _SS)
            # Splash V-mark when drop reaches the bottom of the info section
            if y1 >= y_bot - 5 * _SS:
                sp = min(1.0, (y1 - (y_bot - 5 * _SS)) / (5 * _SS))
                sw = max(1, int(4 * _SS * sp))
                sy_s = min(y_bot - 1, y0 + 10 * _SS)
                sc_s = (int(rc[0] * 0.5), int(rc[1] * 0.5), int(rc[2] * 0.5))
                d.line([(x0 - sw, sy_s), (x0, sy_s - 2 * _SS)], fill=sc_s, width=_SS)
                d.line([(x0, sy_s - 2 * _SS), (x0 + sw, sy_s)], fill=sc_s, width=_SS)

        # Occasional lightning bolt — 2-frame flash every ~9 s
        if (frame % (FRAME_RATE * 9)) < 2:
            bolt_f = frame % (FRAME_RATE * 9)
            lb  = int(160 * (1 - bolt_f / 2))
            lc  = (int(lb * 0.55), int(lb * 0.70), lb)
            lbx = _W // 3
            d.line([(lbx,               y_top +  8 * _SS), (lbx - 10 * _SS, y_top + 42 * _SS)], fill=lc, width=2 * _SS)
            d.line([(lbx - 10 * _SS,    y_top + 42 * _SS), (lbx +  8 * _SS, y_top + 80 * _SS)], fill=lc, width=2 * _SS)

    else:   # TOO WINDY — horizontal wind streaks + periodic red alarm flash
        # Brief alarm flash every ~10 s — red wash fades in/out over 4 frames
        flash = (frame * 2) % 100
        if flash < 4:
            flash_r = int(65 * (1 - flash / 4))
            d.rectangle([0, y_top, _W - 1, y_bot], fill=(flash_r, 0, 0))

        bxs = [8,  52,  96, 140, 184, 228,  30,  74]
        lns = [22, 28,  20,  26,  24,  18,  32,  16]
        spd = [5,   7,   4,   6,   5,   8,   6,   3]
        for row_off in (14, 50, 86, 112):
            ry = y_top + row_off * _SS
            if ry >= y_bot:
                continue
            for j in range(8):
                sy = ry + (j % 3 - 1) * 6 * _SS
                if sy < y_top or sy >= y_bot:
                    continue
                sx = int((bxs[j] * _SS - frame * spd[j]) % _W)
                ex = sx + lns[j] * _SS
                bright = 55 + int(35 * math.sin(
                    frame * math.pi / (FRAME_RATE * 0.8) + j * math.pi / 4))
                sc = (bright, bright // 2, bright // 4)
                if ex <= _W:
                    d.line([(sx, sy), (ex, sy)], fill=sc, width=2 * _SS)
                else:
                    d.line([(sx, sy), (_W - 1, sy)], fill=sc, width=2 * _SS)
                    d.line([(0,  sy), (ex % _W, sy)], fill=sc, width=2 * _SS)


def _draw_marine_wave(d, frame, color, y_mid):
    """Scrolling sine wave shown in the bottom strip when no alerts are active."""
    amplitude  = 3  * _SS
    wavelength = 55 * _SS
    offset     = (frame * 2) % wavelength
    pts = [(x, y_mid + int(amplitude * math.sin(2 * math.pi * (x + offset) / wavelength)))
           for x in range(_W + 1)]
    d.line(pts, fill=color, width=_SS)



def _draw_edge_accents(d, accent, frame):
    """Thin pulsing accent strips on the left and right screen edges."""
    pulse = 0.2 + 0.8 * abs(math.sin(frame * math.pi / (FRAME_RATE * 2.5)))
    for x in range(3 * _SS):
        fade = (3 * _SS - x) / (3.0 * _SS)
        col  = tuple(int(c * pulse * fade * 0.28) for c in accent)
        d.line([(x, 18 * _SS), (x, _H - 1)], fill=col)
        d.line([(_W - 1 - x, 18 * _SS), (_W - 1 - x, _H - 1)], fill=col)


def _draw_alert_strip(d, alerts, frame, status_color, y0, marine_str=None, wind_str=None, age_minutes=None):
    """Top strip: cycles NOAA alerts; when quiet, rotates wind / marine / clock."""
    strip_h = 18 * _SS
    y_mid = y0 + strip_h // 2
    cx    = _W // 2
    d.rectangle([0, y0, _W - 1, y0 + strip_h], fill=(18, 18, 18))
    sep_b = int(38 + 42 * abs(math.sin(frame * math.pi / (FRAME_RATE * 2.0))))
    sep_c = tuple(min(255, int(c * sep_b / 255)) for c in status_color)
    d.line([0, y0 + strip_h, _W, y0 + strip_h], fill=sep_c, width=_SS)

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
        r3, r9 = 3 * _SS, 9 * _SS
        strip_max = _W - 24 * _SS   # leave room for the dot indicator on the right
        if kind == "wind":
            d.ellipse((r3, y_mid - r3, r9, y_mid + r3), fill=(55, 115, 55))
            d.text((cx, y_mid), _fit_text(d, text, font_strip, strip_max),
                   fill=(175, 195, 175), font=font_strip, anchor="mm")
        elif kind == "marine":
            d.ellipse((r3, y_mid - r3, r9, y_mid + r3), fill=(35, 70, 115))
            d.text((cx, y_mid), _fit_text(d, text, font_strip, strip_max),
                   fill=(110, 140, 160), font=font_strip, anchor="mm")
        else:
            _draw_marine_wave(d, frame, wave_color, y_mid)
            d.ellipse((r3, y_mid - r3, r9, y_mid + r3), outline=(72, 72, 72), width=_SS)
            clk = 6 * _SS
            d.line([(clk, y_mid), (clk, y_mid - 2 * _SS)], fill=(72, 72, 72), width=_SS)
            d.line([(clk, y_mid), (clk + 2 * _SS, y_mid)], fill=(72, 72, 72), width=_SS)
            time_str = time.strftime("%H:%M")
            if age_minutes is not None:
                age_str = f"{int(age_minutes)}m"
                d.text((cx - 4 * _SS, y_mid), time_str, fill=(110, 110, 110), font=font_strip, anchor="rm")
                d.text((cx + 4 * _SS, y_mid), age_str,  fill=(72, 72, 72),   font=font_strip, anchor="lm")
            else:
                d.text((cx, y_mid), time_str, fill=(110, 110, 110), font=font_strip, anchor="mm")

        # Horizontal slot progress dots at right edge
        n_slots = len(slots)
        for si in range(n_slots):
            dx = _W - 4 * _SS - (n_slots - 1 - si) * 5 * _SS
            dc = (tuple(min(255, int(c * 0.65)) for c in status_color) if si == idx
                  else (36, 36, 36))
            pr = 2 * _SS
            d.ellipse((dx - pr, y_mid - pr, dx + pr, y_mid + pr), fill=dc)
        return

    idx = (frame // (FRAME_RATE * 4)) % len(alerts)   # new alert every 4 s
    name, severity = alerts[idx]
    color = _ALERT_COLORS.get(severity, _YELLOW)

    # Pulsing warning dot
    pulse = 0.55 + 0.45 * math.sin(frame * math.pi / (FRAME_RATE * 0.7))
    dot_color = tuple(min(255, int(c * pulse)) for c in color)
    d.ellipse([7 * _SS, y_mid - 5 * _SS, 15 * _SS, y_mid + 5 * _SS], fill=dot_color)

    # Alert name, truncated to available width
    text = _fit_text(d, name, font_strip, _W - 26 * _SS)
    d.text((21 * _SS, y_mid), text, fill=color, font=font_strip, anchor="lm")

    # Page indicator when there are multiple alerts
    if len(alerts) > 1:
        count_str = f"{idx + 1}/{len(alerts)}"
        cw = int(d.textlength(count_str, font=font_strip))
        d.text((_W - cw - 4 * _SS, y_mid), count_str,
               fill=(65, 65, 65), font=font_strip, anchor="lm")


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

    # Outer border ring — pulses in the zone's accent color when live
    ob  = r + 10 * _SS
    if stale:
        ob_col = (42, 42, 42)
    else:
        zone_c = (_GREEN if actual_gust < GOOD_MPH
                  else (_YELLOW if actual_gust <= CAUTION_MPH else _RED))
        ob_p   = 0.3 + 0.7 * abs(math.sin(frame * math.pi / (FRAME_RATE * 2.5)))
        ob_col = tuple(max(16, int(c * ob_p * 0.20)) for c in zone_c)
    d.arc((cx - ob, cy - ob, cx + ob, cy + ob),
          _GAUGE_ARC_START - 4, arc_end + 4, fill=ob_col, width=2 * _SS)

    # Dark backing arc — layered to create a subtle cross-section depth
    d.arc(box, _GAUGE_ARC_START - 2, arc_end + 2, fill=(20, 20, 20), width=22 * _SS)
    d.arc(box, _GAUGE_ARC_START - 2, arc_end + 2, fill=(36, 36, 36), width=14 * _SS)
    d.arc(box, _GAUGE_ARC_START - 2, arc_end + 2, fill=(24, 24, 24), width= 6 * _SS)

    # GOOD-state inner sunburst — dim rotating rays in the clear interior of the gauge face
    if not stale and actual_gust < GOOD_MPH:
        ray_rot = (frame * 1.5) % 360
        for i in range(12):
            ang_r = math.radians(ray_rot + i * 30)
            ca_r, sa_r = math.cos(ang_r), math.sin(ang_r)
            b = int(9 + 5 * math.sin(frame * math.pi / (FRAME_RATE * 2.5) + i * math.pi / 6))
            d.line([
                (int(cx + 20 * _SS * ca_r), int(cy + 20 * _SS * sa_r)),
                (int(cx + 48 * _SS * ca_r), int(cy + 48 * _SS * sa_r))
            ], fill=(0, b, b // 3), width=_SS)

    dim = 0.30 if stale else 1.0

    # Soft ambient glow behind zone arcs — wider dim pre-pass for depth
    glow_dim = dim * 0.22
    d.arc(box, _GAUGE_ARC_START, GOOD_ARC_END,    fill=_dim(_GREEN,  glow_dim), width=26 * _SS)
    d.arc(box, GOOD_ARC_END,    CAUTION_ARC_END,  fill=_dim(_YELLOW, glow_dim), width=26 * _SS)
    d.arc(box, CAUTION_ARC_END, arc_end,           fill=_dim(_RED,    glow_dim), width=26 * _SS)

    # Colored zone arcs
    d.arc(box, _GAUGE_ARC_START, GOOD_ARC_END,    fill=_dim(_GREEN,  dim), width=16 * _SS)
    d.arc(box, GOOD_ARC_END,    CAUTION_ARC_END,  fill=_dim(_YELLOW, dim), width=16 * _SS)
    d.arc(box, CAUTION_ARC_END, arc_end,          fill=_dim(_RED,    dim), width=16 * _SS)

    # Thin specular highlight at the inner edge of each arc band — adds depth
    ih   = r - 8 * _SS
    ibox = (cx - ih, cy - ih, cx + ih, cy + ih)
    d.arc(ibox, _GAUGE_ARC_START, GOOD_ARC_END,   fill=_dim(_GREEN,  dim * 0.55), width=2 * _SS)
    d.arc(ibox, GOOD_ARC_END,    CAUTION_ARC_END, fill=_dim(_YELLOW, dim * 0.55), width=2 * _SS)
    d.arc(ibox, CAUTION_ARC_END, arc_end,         fill=_dim(_RED,    dim * 0.55), width=2 * _SS)

    # Pulsing overlay on the currently-active zone arc — breathes to indicate live zone
    if not stale:
        zp = 0.12 + 0.10 * math.sin(frame * math.pi / (FRAME_RATE * 1.8))
        if needle_gust < GOOD_MPH:
            za_s, za_e, zc = _GAUGE_ARC_START, GOOD_ARC_END, _GREEN
        elif needle_gust <= CAUTION_MPH:
            za_s, za_e, zc = GOOD_ARC_END, CAUTION_ARC_END, _YELLOW
        else:
            za_s, za_e, zc = CAUTION_ARC_END, arc_end, _RED
        d.arc(box, za_s, za_e, fill=_dim(zc, zp), width=26 * _SS)

    # Bright narrow trace from 0 to current needle — highlights the swept arc
    if needle_gust > 0.1 and not stale:
        na_end  = _GAUGE_ARC_START + (needle_gust / GAUGE_MAX) * _GAUGE_ARC_SWEEP
        trace_c = ((0, 220, 100) if needle_gust < GOOD_MPH
                   else ((255, 210, 0) if needle_gust <= CAUTION_MPH
                         else (255, 80, 80)))
        d.arc(box, _GAUGE_ARC_START, na_end, fill=trace_c, width=4 * _SS)

    # Animated wind streaks flowing inside the arc face
    _draw_wind_streaks(d, cx, cy, r, actual_gust, frame)

    # Tick marks — colored by zone; ticks near the needle glow brighter
    tick_outer  = r - 8  * _SS
    tick_inner  = r - 18 * _SS
    needle_deg  = _GAUGE_ARC_START + (needle_gust / GAUGE_MAX) * _GAUGE_ARC_SWEEP
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
        if not stale and needle_gust > 0:
            tick_deg = _GAUGE_ARC_START + (mph_val / GAUGE_MAX) * _GAUGE_ARC_SWEEP
            prox = max(0.0, 1.0 - abs(tick_deg - needle_deg) / 14.0)
            if prox > 0:
                tick_c = tuple(min(255, int(c + 110 * prox)) for c in tick_c)
        d.line([(int(x1), int(y1)), (int(x2), int(y2))],
               fill=tick_c, width=2 * _SS if is_major else _SS)

    # Zone-boundary labels — tinted to match the zone they mark
    for mph_val, lbl, lbl_c in [
        (GOOD_MPH,    str(GOOD_MPH),    _dim(_GREEN,  dim * 0.75)),
        (CAUTION_MPH, str(CAUTION_MPH), _dim(_YELLOW, dim * 0.75)),
    ]:
        ang = _gauge_ang(mph_val)
        ca, sa = math.cos(ang), math.sin(ang)
        lx, ly = cx + (r - 28 * _SS) * ca, cy + (r - 28 * _SS) * sa
        d.text((int(lx), int(ly)), lbl, fill=lbl_c, font=font_data, anchor="mm")

    # Dim "GUST" label in upper-center of gauge interior — context for the needle
    d.text((cx, cy - 50 * _SS), "GUST", fill=(48, 48, 48), font=font_label, anchor="mm")

    # Peak gust marker — bright tick just outside the arc at session-max position
    if history and len(history) >= 2 and not stale:
        peak = max(history)
        if peak > actual_gust + 0.3:
            p_ang  = _gauge_ang(min(peak, GAUGE_MAX))
            p_ca, p_sa = math.cos(p_ang), math.sin(p_ang)
            po = (cx + (r + 4 * _SS) * p_ca, cy + (r + 4 * _SS) * p_sa)
            pi = (cx + (r - 4 * _SS) * p_ca, cy + (r - 4 * _SS) * p_sa)
            d.line([(int(po[0]), int(po[1])), (int(pi[0]), int(pi[1]))],
                   fill=(220, 200, 55), width=2 * _SS)
            lx = int(cx + (r + 15 * _SS) * p_ca)
            ly = int(cy + (r + 15 * _SS) * p_sa)
            d.text((lx, ly), f"{peak:.0f}", fill=(140, 120, 28), font=font_label, anchor="mm")

    if stale:
        sweep_pos = _GAUGE_ARC_START + (frame * 3) % _GAUGE_ARC_SWEEP
        trail_s   = max(_GAUGE_ARC_START, sweep_pos - 28)
        d.arc(box, trail_s, sweep_pos, fill=(0, 22, 11), width=14 * _SS)
        d.text((cx, cy), "STALE", fill=(55, 55, 55), font=font_label, anchor="mm")

    # Sustained avg wind: diamond marker on the arc, plus tiny readout below hub
    if wind is not None and not stale:
        w_ang  = _gauge_ang(min(wind, GAUGE_MAX))
        w_ca, w_sa = math.cos(w_ang), math.sin(w_ang)
        wx = int(cx + (r - 10 * _SS) * w_ca)
        wy = int(cy + (r - 10 * _SS) * w_sa)
        ds = 4 * _SS
        d.polygon([(wx, wy - ds), (wx + ds, wy), (wx, wy + ds), (wx - ds, wy)],
                  fill=(45, 45, 45), outline=(145, 145, 145))
        d.text((cx, cy + 26 * _SS), f"avg {wind:.0f}", fill=(155, 155, 155), font=font_data, anchor="mm")

    # Tiny gust history sparkline below avg text — oldest left, newest right
    if history and len(history) >= 2 and not stale:
        pts  = list(reversed(history[:6]))
        n    = len(pts)
        sx0, sx1 = cx - 34 * _SS, cx + 34 * _SS
        syc  = cy + 48 * _SS
        h_sp = 6 * _SS
        maxv = max(max(pts) * 1.05, GOOD_MPH + 1)
        for j in range(n - 1):
            px0 = sx0 + j * (sx1 - sx0) // (n - 1)
            px1 = sx0 + (j + 1) * (sx1 - sx0) // (n - 1)
            py0 = syc - int(pts[j]     / maxv * h_sp)
            py1 = syc - int(pts[j + 1] / maxv * h_sp)
            v   = pts[j]
            sc  = ((0, 95, 45) if v < GOOD_MPH
                   else ((130, 100, 0) if v <= CAUTION_MPH else (130, 28, 28)))
            d.line([(px0, py0), (px1, py1)], fill=sc, width=_SS)
        last_y = syc - int(pts[-1] / maxv * h_sp)
        dot_c  = ((0, 175, 80) if pts[-1] < GOOD_MPH
                  else ((195, 152, 0) if pts[-1] <= CAUTION_MPH else (195, 45, 45)))
        dr = 2 * _SS
        d.ellipse((sx1 - dr, last_y - dr, sx1 + dr, last_y + dr), fill=dot_c)

    # Kite-shaped needle — soft glow arc at its angle for a back-lit instrument feel
    pct = min(max(needle_gust / GAUGE_MAX, 0), 1)
    ang = _gauge_ang(pct * GAUGE_MAX)
    if not stale and actual_gust > CAUTION_MPH:
        ang += math.radians(1.5 * math.sin(frame * math.pi / (FRAME_RATE * 0.10)))
    if not stale and needle_gust > 0.1:
        glow_ang = _GAUGE_ARC_START + (needle_gust / GAUGE_MAX) * _GAUGE_ARC_SWEEP
        glow_c   = ((0, 28, 14) if needle_gust < GOOD_MPH
                    else ((32, 24, 0) if needle_gust <= CAUTION_MPH
                          else (32, 6, 6)))
        d.arc(box, glow_ang - 7, glow_ang + 7, fill=glow_c, width=22 * _SS)
    ca, sa = math.cos(ang), math.sin(ang)
    perp = ang - math.pi / 2
    cp, sp = math.cos(perp), math.sin(perp)
    tip   = (cx + r         * ca, cy + r         * sa)
    wide  = (cx + 8  * _SS  * ca, cy + 8  * _SS  * sa)
    tail  = (cx - 14 * _SS  * ca, cy - 14 * _SS  * sa)
    hw = 5.5 * _SS
    left  = (wide[0] + hw * cp, wide[1] + hw * sp)
    right = (wide[0] - hw * cp, wide[1] - hw * sp)
    needle_fill = (90, 90, 90) if stale else (240, 240, 240)
    sh = 2 * _SS
    d.polygon(
        [(int(tip[0])   + sh, int(tip[1])   + sh),
         (int(left[0])  + sh, int(left[1])  + sh),
         (int(tail[0])  + sh, int(tail[1])  + sh),
         (int(right[0]) + sh, int(right[1]) + sh)],
        fill=(10, 10, 10),
    )
    d.polygon(
        [(int(tip[0]),   int(tip[1])),
         (int(left[0]),  int(left[1])),
         (int(tail[0]),  int(tail[1])),
         (int(right[0]), int(right[1]))],
        fill=needle_fill,
    )

    # Pulsing colored dot at needle tip
    if not stale:
        tc = ((0, 200, 90) if needle_gust < GOOD_MPH
              else ((240, 190, 0) if needle_gust <= CAUTION_MPH
                    else (240, 60, 60)))
        tp = 0.65 + 0.35 * math.sin(frame * math.pi / (FRAME_RATE * 0.7))
        tr = max(2 * _SS, int(5 * _SS * tp))
        d.ellipse((int(tip[0]) - tr, int(tip[1]) - tr,
                   int(tip[0]) + tr, int(tip[1]) + tr), fill=tc)

    # Pivot hub — three concentric rings; center dot colored by current zone
    h1, h2, h3 = 11 * _SS, 8 * _SS, 5 * _SS
    d.ellipse((cx - h1, cy - h1, cx + h1, cy + h1), fill=(28, 28, 28), outline=(90, 90, 90), width=_SS)
    d.ellipse((cx - h2, cy - h2, cx + h2, cy + h2), fill=(18, 18, 18), outline=(55, 55, 55), width=_SS)
    if stale:
        hub_c = (90, 90, 90)
    elif actual_gust > CAUTION_MPH:
        hub_c = (220, 55, 55)
    elif actual_gust > GOOD_MPH:
        hub_c = (210, 170, 0)
    else:
        hub_c = (0, 180, 80)
    d.ellipse((cx - h3, cy - h3, cx + h3, cy + h3), fill=hub_c)


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
    r  = 80 * _SS
    # Advisory strip occupies y=0-18; gauge starts 14px below that
    cy = (18 + 14) * _SS + r + 11 * _SS
    cx = _W // 2

    # Pre-compute info_y so the background draws before gauge text
    info_y = cy + int(r * 0.707) + 8 * _SS

    # Draw animated info-section background first (behind all text)
    _draw_info_bg(d, info_y, _H - 1, msg, frame)

    stale = age is not None and age >= STALE_MINUTES

    # Pulsing halo arcs just outside the gauge — tinted by status, very dim
    if not stale:
        halo_p   = 0.3 + 0.7 * abs(math.sin(frame * math.pi / (FRAME_RATE * 3.0)))
        halo_end = _GAUGE_ARC_START + _GAUGE_ARC_SWEEP
        for h_off, h_fac in ((r + 17 * _SS, 0.13), (r + 24 * _SS, 0.07)):
            hc = tuple(max(0, int(c * h_fac * halo_p)) for c in accent)
            if any(v > 0 for v in hc):
                d.arc((cx - h_off, cy - h_off, cx + h_off, cy + h_off),
                      _GAUGE_ARC_START - 8, halo_end + 8, fill=hc, width=2 * _SS)

    _draw_gauge(d, cx, cy, r, needle_gust, gust, frame, stale=stale, wind=wind, history=history)

    # Compass rose inside the arc — upper-left to clear the "18" label at upper-right
    _draw_compass(d, cx - 40 * _SS, cy - 24 * _SS, 20 * _SS, wdir)

    # Data freshness bar — centered horizontal line in the gauge mouth gap
    if age is not None:
        freshness = max(0.0, 1.0 - age / STALE_MINUTES)
        bar_hw = int(42 * _SS * freshness)
        if bar_hw > 0:
            bright = max(24, int(65 * freshness))
            by = info_y - 7 * _SS
            d.line([(cx - bar_hw, by), (cx + bar_hw, by)], fill=(bright, bright, bright), width=_SS)

    # Separator — 3-zone gradient line: bright center, dim edges; 6 draw calls total
    sep_p = 0.3 + 0.7 * abs(math.sin(frame * math.pi / (FRAME_RATE * 3)))
    for sx0, bfac in ((int(cx * 0.10), 0.18), (int(cx * 0.40), 0.34), (int(cx * 0.70), 0.52)):
        sc_ = tuple(int(c * sep_p * bfac) for c in accent)
        if any(v > 0 for v in sc_):
            d.line([(_W // 2 - cx + sx0, info_y - 4 * _SS), (_W // 2 + cx - sx0, info_y - 4 * _SS)],
                   fill=sc_, width=_SS)
            d.line([(_W // 2 - cx + sx0, info_y - 5 * _SS), (_W // 2 + cx - sx0, info_y - 5 * _SS)],
                   fill=tuple(v // 2 for v in sc_), width=_SS)

    # ── Info section ──────────────────────────────────────────────────────────
    # Full-width status band (replaces narrow centered badge)
    band_h  = 44 * _SS   # 22 px device
    band_y0 = info_y + 4 * _SS
    band_y1 = band_y0 + band_h
    _, badge_bg = _STATUS_CONFIG[msg]
    band_bg = badge_bg if stale else tuple(min(255, int(c * 0.55)) for c in accent)
    d.rectangle([0, band_y0, _W - 1, band_y1], fill=band_bg)
    # Bright top/bottom edges on the band
    edge_p  = 0.5 + 0.5 * abs(math.sin(frame * math.pi / (FRAME_RATE * 2.0)))
    edge_c  = tuple(min(255, int(c * edge_p)) for c in accent) if not stale else (55, 55, 55)
    d.line([(0, band_y0), (_W - 1, band_y0)], fill=edge_c, width=2 * _SS)
    d.line([(0, band_y1), (_W - 1, band_y1)], fill=tuple(c // 2 for c in edge_c), width=_SS)
    vib_x = [-1 * _SS, 0, 1 * _SS, 0][frame % 4] if msg == "TOO WINDY" and not stale else 0
    d.text((cx + vib_x, band_y0 + band_h // 2), msg,
           fill=(60, 60, 60) if stale else (255, 255, 255),
           font=font_unit, anchor="mm")

    # Big gust number — centered in the space below the band
    if stale:
        gust_fill = (50, 50, 50)
        mph_fill  = (40, 40, 40)
    elif needle_gust > CAUTION_MPH:
        gust_fill = (255, 110, 90)
        mph_fill  = (210, 85, 70)
    elif needle_gust >= GOOD_MPH:
        gust_fill = (255, 215, 50)
        mph_fill  = (205, 170, 35)
    else:
        gust_fill = (240, 240, 240)
        mph_fill  = (180, 180, 180)
    num_str = f"{needle_gust:.0f}"
    num_w   = int(d.textlength(num_str, font=font_status))
    unit_w  = int(d.textlength("mph",   font=font_unit))
    grp_x   = (_W - num_w - 8 * _SS - unit_w) // 2
    num_y   = band_y1 + (_H - band_y1) // 2   # vertically center in remaining space
    d.text((grp_x + 2 * _SS,             num_y + 2 * _SS),              num_str, fill=(0, 0, 0),  font=font_status, anchor="mm")
    d.text((grp_x,                        num_y),                        num_str, fill=gust_fill,  font=font_status, anchor="mm")
    d.text((grp_x + num_w + 10 * _SS,    num_y + 20 * _SS + 2 * _SS),  "mph",   fill=(0, 0, 0),  font=font_unit,   anchor="mm")
    d.text((grp_x + num_w + 8  * _SS,    num_y + 20 * _SS),             "mph",   fill=mph_fill,   font=font_unit,   anchor="mm")

    # Trend arrow — left of number group
    if trend is not None:
        _draw_trend(d, 18 * _SS, num_y - 12 * _SS, trend)

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

    # Animated wave at screen bottom — completes the edge framing (2px tall)
    accent_dim  = tuple(c // 5 for c in accent)
    accent_dim2 = tuple(c // 10 for c in accent)
    bwave_off   = (frame * 3) % (22 * _SS)
    bwave_row1, bwave_row2 = [], []
    for bx in range(_W):
        by = _H - 1 - int(2 * _SS * math.sin(2 * math.pi * (bx + bwave_off) / (22 * _SS)))
        bwave_row1.append((bx, by))
        if by > 0:
            bwave_row2.append((bx, by - 1))
    d.line(bwave_row1, fill=accent_dim)
    if bwave_row2:
        d.line(bwave_row2, fill=accent_dim2)

    # Dim accent-tinted corner data: water temp (bottom-left), wave height (bottom-right)
    if not stale:
        ctc = tuple(max(0, int(c * 0.45)) for c in accent)
        if wtmp is not None:
            d.text((6 * _SS, _H - 16 * _SS), f"{wtmp:.0f}°", fill=ctc, font=font_data, anchor="lm")
        if wvht is not None:
            d.text((_W - 6 * _SS, _H - 16 * _SS), f"{wvht:.1f}ft", fill=ctc, font=font_data, anchor="rm")

    # Top strip: NOAA advisories → wind → marine → clock/wave
    _draw_alert_strip(d, alerts, frame, accent, y0=0,
                      marine_str=marine_str, wind_str=wind_str, age_minutes=age)

    _show(img)


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
        draw_centered(d, 125 * _SS, "Offline", (80, 80, 80), font_big)
        _show(img)
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


_WEB_PAGE = (
    "<!DOCTYPE html><html lang='en'><head>"
    "<meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>Pontoon Wind</title>"
    "<style>"
    "*{margin:0;padding:0}"
    "html,body{width:100%;height:100%;background:#000;overflow:hidden}"
    "img{display:block;width:100%;height:100%;object-fit:contain}"
    "</style></head><body>"
    "<img id='f' src='/frame'>"
    "<script>"
    "(function(){"
    "var el=document.getElementById(\'f\');"
    "function next(){"
    "var t=new Image();"
    "t.onload=function(){el.src=t.src;setTimeout(next,150)};"
    "t.onerror=function(){setTimeout(next,1000)};"
    "t.src=\'/frame?\'+Date.now();"
    "}"
    "setTimeout(next,150);"
    "})();"
    "</script></body></html>"
)

class _WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/frame"):
            with _frame_lock:
                data = _frame_store[0]
            if not data:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/data":
            with _lock:
                s = dict(_state)
                s["alerts"]       = list(_state["alerts"])
                s["gust_history"] = list(_state["gust_history"])
            g = s.get("gust")
            s["status"] = ("GOOD"      if g is not None and g < GOOD_MPH
                           else "CAUTION"   if g is not None and g <= CAUTION_MPH
                           else "TOO WINDY" if g is not None
                           else "OFFLINE")
            s["stale"]  = s.get("age") is not None and s["age"] >= STALE_MINUTES
            s["trend"]  = _trend(s["gust_history"])
            s["alerts"] = [[a, b] for a, b in s["alerts"]]
            body = json.dumps(s).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = _WEB_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *_args):
        pass  # suppress per-request log noise


def _start_web_server():
    class _Server(HTTPServer):
        allow_reuse_address = True
    server = _Server(("", WEB_PORT), _WebHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Web dashboard on http://0.0.0.0:%d/", WEB_PORT)


def main():
    global _needle_gust, _needle_vel

    _load_gif_icons()
    _start_web_server()
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
            _needle_vel  = max(-6.0, min(6.0, _needle_vel * _NEEDLE_DAMPING + diff * _NEEDLE_SPRING))
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
                for sz, col in ((30 * _SS, ec2), (16 * _SS, ec)):
                    d.polygon([(0, 0), (sz, 0), (0, sz)], fill=col)
                    d.polygon([(_W - 1, 0),
                               (_W - 1 - sz, 0),
                               (_W - 1, sz)], fill=col)
                draw_centered(d, 78 * _SS, "ERROR", ec, font_big)
                err_text = textwrap.shorten(snap["error"], width=34, placeholder="…")
                d.text((12 * _SS, 150 * _SS), err_text, fill=(160, 160, 160), font=font_data)
                _show(img)
            except Exception:
                logging.exception("Error screen render failed")
        else:
            # Animated connecting screen — spinning arc + pulsing text
            try:
                dots   = "." * ((frame // FRAME_RATE) % 4)
                bright = int(60 + 30 * math.sin(frame * math.pi / (FRAME_RATE * 2)))
                img, d = make_image()
                cx_s  = _W // 2
                spin  = (frame * 14) % 360
                bc    = (bright, bright, bright)
                d.arc((cx_s - 30 * _SS, 110 * _SS, cx_s + 30 * _SS, 170 * _SS), spin, spin + 115, fill=bc, width=3 * _SS)
                d.arc((cx_s - 30 * _SS, 110 * _SS, cx_s + 30 * _SS, 170 * _SS), spin + 115, spin + 230,
                      fill=(bright // 4, bright // 4, bright // 4), width=2 * _SS)
                spin2 = (360 - frame * 9) % 360
                d.arc((cx_s - 18 * _SS, 122 * _SS, cx_s + 18 * _SS, 158 * _SS), spin2, spin2 + 80,
                      fill=(bright // 3, bright // 3, bright // 3), width=2 * _SS)
                draw_centered(d, 182 * _SS, "PONTOON WIND",
                              (bright, bright, bright), font_title)
                draw_centered(d, 198 * _SS, "NDBC 41038 · Cape Fear",
                              (bright // 2, bright // 2, bright // 2), font_label)
                draw_centered(d, 216 * _SS, f"Connecting{dots}",
                              (bright - 20, bright - 20, bright - 20), font_data)
                _show(img)
            except Exception:
                logging.exception("Connecting screen render failed")

        frame += 1
        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, 1 / FRAME_RATE - elapsed))


if __name__ == "__main__":
    main()
