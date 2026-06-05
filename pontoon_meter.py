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
GAUGE_MAX       = 30    # mph, full-scale
GOOD_MPH        = 15
CAUTION_MPH     = 23
STALE_MINUTES   = 90
# Composite go/no-go thresholds — wave and temp map onto the same 0–GAUGE_MAX scale as wind
WAVE_GOOD_FT    = 2.0   # ft — below this is a GO for waves
WAVE_CAUTION_FT = 3.0   # ft — above this is a NO-GO for waves
WAVE_CHOP_DPD   = 5.0   # seconds — period below this = short choppy wind chop
WAVE_SWELL_DPD  = 9.0   # seconds — period above this = long gentle swell
TEMP_COOL_F       = 65    # °F water — below this starts adding a caution penalty
TEMP_COLD_F       = 50    # °F water — below this adds a significant penalty
PRES_FALL_CAUTION = 1.5   # hPa drop over polling window (>=3 samples) -> CAUTION floor
FOG_SPREAD_F      = 5.0   # °F air-to-dewpoint spread below this = fog/mist likely
# Air temperature comfort — separate from water safety thresholds above
# Hot air: open pontoon handles more wind; cold air: wind chill makes any breeze worse
ATMP_WARM_F   = 80.0  # °F — above this, warm-day wind relief applies (up to 5 mph equivalent)
ATMP_CHILLY_F = 62.0  # °F — below this, cold-air comfort penalty applies on open deck
_SS             = 2     # supersampling scale — render at 2× then BILINEAR downsample to device

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
    "GO":      (_GREEN,  (0, 80, 32)),
    "CAUTION": (_YELLOW, (90, 72, 0)),
    "NO-GO":   (_RED,    (95, 20, 16)),
}

_ALERT_COLORS = {
    "Extreme":  _RED,
    "Severe":   _RED,
    "Moderate": (220, 110, 0),
    "Minor":    _YELLOW,
    "Unknown":  _YELLOW,
}

# Compass direction → degrees (meteorological: direction wind comes FROM)
_COMPASS_DEGREES = {
    "N": 0, "NE": 45, "E": 90, "SE": 135,
    "S": 180, "SW": 225, "W": 270, "NW": 315,
}

# TOO WINDY info-section streak geometry — constant per streak, no per-frame allocation
_WINDY_BXS = [8,  52,  96, 140, 184, 228,  30,  74]
_WINDY_LNS = [22, 28,  20,  26,  24,  18,  32,  16]
_WINDY_SPD = [5,   7,   4,   6,   5,   8,   6,   3]

# GOOD-state info-section wave parameters — static, avoids list re-creation every frame
_GOOD_WAVE_PARAMS = [
    (10,  (0, 122, 79), 70, 3),
    (30,  (0, 105, 68), 90, 5),
    (52,  (0,  90, 58), 80, 4),
    (74,  (0,  75, 48), 65, 3),
    (96,  (0,  59, 39), 80, 4),
    (118, (0,  43, 30), 95, 5),
]
_GOOD_WAVE_FREQS = [2 * math.pi / (wl_i * _SS) for (_, _, wl_i, _) in _GOOD_WAVE_PARAMS]

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
font_gust   = _load_bold(72  * _SS)   # main speed number — sized to fit below the status band
font_data   = _load_font(22  * _SS)
font_label  = _load_font(14  * _SS)
font_strip  = _load_font(16  * _SS)
font_unit   = _load_bold(36  * _SS)
font_big    = _load_bold(64  * _SS)

# Pre-measure constant text widths — avoids one textlength() call per frame
_MPH_UNIT_W = int(ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength("mph", font=font_unit))
# Pre-measure every integer gust label 0–55 mph so render_display never calls textlength()
_GUST_WIDTHS = {i: int(ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(str(i), font=font_gust))
                for i in range(56)}

try:
    _serial = spi(port=0, device=0, gpio_DC=24, gpio_RST=25)
    device = ili9341(_serial, width=320, height=240, rotate=3)
except Exception as e:
    logging.critical("Display init failed: %s", e)
    sys.exit(1)

# Supersampled canvas dimensions — all drawing happens here, then BILINEAR downsample to device
_W = device.width  * _SS
_H = device.height * _SS

# Per-frame constants derived from _W / _SS — precomputed once to avoid per-frame work
_GOOD_PARTICLE_X  = [(i * 17 * _SS + 11 * _SS) % _W for i in range(14)]
_CAUTION_DROP_BX  = [(i * 12 * _SS) % _W for i in range(20)]
_MARINE_WAVE_FREQ = 2 * math.pi / (55 * _SS)   # reused by _draw_marine_wave
_BWAVE_FREQ       = 2 * math.pi / (22 * _SS)   # bottom accent wave frequency
_EDGE_FADE_BASE   = [(3 * _SS - x) / (3.0 * _SS) for x in range(3 * _SS)]

# Sine lookup tables — replace per-pixel trig with integer indexing each frame
# Bottom wave: 22×SS-pixel period → 44-entry table
_BWAVE_PERIOD      = 22 * _SS
_BWAVE_TABLE       = [int(3 * _SS * math.sin(_BWAVE_FREQ * x)) for x in range(_BWAVE_PERIOD)]
# Alert-strip marine wave: 55×SS-pixel period → 110-entry table
_MARINE_PERIOD     = 55 * _SS
_MARINE_WAVE_TABLE = [int(3 * _SS * math.sin(_MARINE_WAVE_FREQ * x)) for x in range(_MARINE_PERIOD)]
# GOOD-state info waves: one table per wavelength
_GOOD_WAVE_PERIODS = [wl_i * _SS for (_, _, wl_i, _) in _GOOD_WAVE_PARAMS]
_GOOD_WAVE_TABLES  = [
    [int(amp_i * _SS * math.sin(_GOOD_WAVE_FREQS[i] * x)) for x in range(_GOOD_WAVE_PERIODS[i])]
    for i, (_, _, wl_i, amp_i) in enumerate(_GOOD_WAVE_PARAMS)
]
# Sunburst: precomputed (cos, sin) at 30° intervals — rotation formula reduces 12 trig
# calls per GO frame to 1 base call + 12 multiply-add pairs
_SUNBURST_C = [math.cos(math.radians(i * 30)) for i in range(12)]
_SUNBURST_S = [math.sin(math.radians(i * 30)) for i in range(12)]

# Gauge layout — cx/cy/r are the same every frame; centralise here so _TICK_DATA stays in sync
_GAUGE_R  = 70 * _SS
_GAUGE_CX = _W // 2
_GAUGE_CY = (18 + 14) * _SS + _GAUGE_R + 11 * _SS

# Precomputed tick-mark geometry — saves 6 trig pairs + 12 multiplies per frame
_TICK_OUTER = _GAUGE_R - 8  * _SS
_TICK_INNER = _GAUGE_R - 18 * _SS
_TICK_DATA  = []
for _t in range(0, GAUGE_MAX + 1, 5):
    _ta = math.radians(_GAUGE_ARC_START + (_t / GAUGE_MAX) * _GAUGE_ARC_SWEEP)
    _tc, _ts = math.cos(_ta), math.sin(_ta)
    _TICK_DATA.append((
        _t,
        _GAUGE_ARC_START + (_t / GAUGE_MAX) * _GAUGE_ARC_SWEEP,   # degrees for proximity calc
        int(_GAUGE_CX + _TICK_OUTER * _tc),
        int(_GAUGE_CY + _TICK_OUTER * _ts),
        int(_GAUGE_CX + _TICK_INNER * _tc),
        int(_GAUGE_CY + _TICK_INNER * _ts),
        (_t % 10 == 0) or _t == GAUGE_MAX,                        # is_major
    ))
del _t, _ta, _tc, _ts

# Info / status-band layout — all depend only on the constants above, so precompute once
_INFO_Y  = _GAUGE_CY + int(_GAUGE_R * 0.707) + 8 * _SS
_BAND_H  = 36 * _SS
_BAND_Y0 = _INFO_Y + 4 * _SS
_BAND_Y1 = _BAND_Y0 + _BAND_H
_ROW_Y   = _BAND_Y1 + 10 * _SS
# Separator line segment x-ranges and y positions — avoids per-frame float multiply + int()
_SEP_SEGS = [(int(_GAUGE_CX * f), b) for f, b in ((0.10, 0.22), (0.40, 0.42), (0.70, 0.62))]
_SEP_Y1   = _INFO_Y - 4 * _SS
_SEP_Y2   = _INFO_Y - 5 * _SS

# Thread-safe state shared between the data thread and the animation loop
_lock  = threading.Lock()
_state = {
    "wind": None, "gust": None, "wdir": "---",
    "age": None, "wtmp": None, "wvht": None, "atmp": None, "dpd": None,
    "dewp": None,                       # dewpoint temperature (°F); None = missing
    "pres": None, "pres_history": [],   # most-recent-first barometric pressure (hPa)
    "gust_history": [],                 # most-recent-first list of up to 6 gust readings
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
        out.save(buf, format="PNG", compress_level=1)
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


def _relative_humidity(atmp_f, dewp_f):
    """Approx relative humidity (%) from air temp and dewpoint (°F), Magnus formula."""
    tc = (atmp_f - 32) * 5 / 9
    td = (dewp_f - 32) * 5 / 9
    return max(0.0, min(100.0,
        100.0 * math.exp(17.625 * td / (243.04 + td))
              / math.exp(17.625 * tc / (243.04 + tc))))


def _heat_index_f(t_f, rh):
    """Rothfusz heat index (°F). Call only when t_f >= 80 and rh >= 40."""
    return (-42.379 + 2.04901523*t_f + 10.14333127*rh
            - 0.22475541*t_f*rh - 0.00683783*t_f**2 - 0.05481717*rh**2
            + 0.00122874*t_f**2*rh + 0.00085282*t_f*rh**2
            - 0.00000199*t_f**2*rh**2)


def _wind_chill_f(t_f, wind_mph):
    """NOAA wind chill (°F). Call only when t_f <= 50 and wind_mph >= 3."""
    return (35.74 + 0.6215*t_f - 35.75*(wind_mph**0.16)
            + 0.4275*t_f*(wind_mph**0.16))


def _draw_trend(d, cx, y, trend):
    """24 px tall directional indicator: up = rising, down = easing, dash = steady."""
    h = 24 * _SS
    w = 10 * _SS
    if trend == "up":
        d.polygon([(cx, y), (cx - w, y + h), (cx + w, y + h)], fill=(255, 150, 0))
    elif trend == "down":
        d.polygon([(cx, y + h), (cx - w, y), (cx + w, y)], fill=(30, 165, 225))
    else:
        d.line([(cx - 11 * _SS, y + h // 2), (cx + 11 * _SS, y + h // 2)],
               fill=(85, 85, 85), width=3 * _SS)



def _draw_wind_streaks(d, cx, cy, r, gust, frame):
    """Animated short dashes flowing along the inner gauge face, speed ∝ wind."""
    speed  = max(2, round(20 - gust * 0.4))
    inner  = r - 24 * _SS
    n      = 7
    for i in range(n):
        phase = ((frame // speed + i * (100 // n)) % 100) / 100.0
        ang   = math.pi * (1 - phase)
        ca, sa = math.cos(ang), math.sin(ang)
        bright = int(70 + 170 * math.sin(phase * math.pi))   # brighter: 70–240
        perp = ang + math.pi / 2
        cp, sp = math.cos(perp), math.sin(perp)
        hw = 5 * _SS
        x0 = cx + inner * ca
        y0 = cy - inner * sa
        x1 = int(x0 + hw * cp);  y1 = int(y0 - hw * sp)
        x2 = int(x0 - hw * cp);  y2 = int(y0 + hw * sp)
        d.line([(x1, y1), (x2, y2)], fill=(max(0, bright - 20), max(0, bright - 10), bright), width=2 * _SS)

def _draw_info_bg(d, y_top, y_bot, status, frame):
    """Animated background texture drawn behind the info-section text."""
    if status == "GO":
        scroll = (frame * 2) % (80 * _SS)
        for i, (wy_off, wc, wl_i, amp_i) in enumerate(_GOOD_WAVE_PARAMS):
            wy = y_top + wy_off * _SS
            if wy >= y_bot:
                continue
            period = _GOOD_WAVE_PERIODS[i]
            table  = _GOOD_WAVE_TABLES[i]
            base   = (scroll + i * 17 * _SS) % period
            pts    = [(px, wy + table[(px + base) % period]) for px in range(_W + 1)]
            d.line(pts, fill=wc, width=_SS)

        # Floating upward particles — slow drift gives GOOD state a lively feel
        span = y_bot - y_top
        for i in range(14):
            px_pos  = _GOOD_PARTICLE_X[i]
            speed   = 1 + (i % 3)
            py_off  = span - (frame * speed + i * (span // 14)) % span
            py_pos  = y_top + int(py_off)
            bright  = int(78 + 65 * math.sin(frame * math.pi / (FRAME_RATE * 2.2) + i * 0.8))
            pc = (0, int(bright * 0.72), int(bright * 0.46))
            if y_top <= py_pos < y_bot:
                d.point((px_pos, py_pos), fill=pc)
                if py_pos + 1 < y_bot:
                    d.point((px_pos, py_pos + 1), fill=(0, bright // 4, bright // 6))

    elif status == "CAUTION":
        info_h = y_bot - y_top
        for i in range(20):
            bx  = _CAUTION_DROP_BX[i]
            t   = (frame * 2 + i * 7) % (info_h + 14 * _SS)
            x0, y0 = bx,              y_top + t - 14 * _SS
            x1, y1 = bx + 8 * _SS,   y0 + 10 * _SS
            y0c = max(y0, y_top);  y1c = min(y1, y_bot)
            if y1c <= y_top or y0c >= y_bot:
                continue
            bright = 100 + int(60 * math.sin(
                frame * math.pi / (FRAME_RATE * 1.5) + i * math.pi / 5))
            rc = (int(bright * 0.18), int(bright * 0.32), int(bright * 0.90))
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
            flash_r = int(80 * (1 - flash / 4))
            d.rectangle([0, y_top, _W - 1, y_bot], fill=(flash_r, 0, 0))

        for row_off in (14, 50, 86, 112):
            ry = y_top + row_off * _SS
            if ry >= y_bot:
                continue
            for j in range(8):
                sy = ry + (j % 3 - 1) * 6 * _SS
                if sy < y_top or sy >= y_bot:
                    continue
                sx = int((_WINDY_BXS[j] * _SS - frame * _WINDY_SPD[j]) % _W)
                ex = sx + _WINDY_LNS[j] * _SS
                bright = 62 + int(42 * math.sin(
                    frame * math.pi / (FRAME_RATE * 0.8) + j * math.pi / 4))
                sc = (bright, bright // 2, bright // 4)
                if ex <= _W:
                    d.line([(sx, sy), (ex, sy)], fill=sc, width=2 * _SS)
                else:
                    d.line([(sx, sy), (_W - 1, sy)], fill=sc, width=2 * _SS)
                    d.line([(0,  sy), (ex % _W, sy)], fill=sc, width=2 * _SS)


def _draw_marine_wave(d, frame, color, y_mid):
    """Scrolling sine wave shown in the bottom strip when no alerts are active."""
    offset = (frame * 2) % _MARINE_PERIOD
    pts = [(x, y_mid + _MARINE_WAVE_TABLE[(x + offset) % _MARINE_PERIOD])
           for x in range(_W + 1)]
    d.line(pts, fill=color, width=_SS)



def _draw_edge_accents(d, accent, frame):
    """Thin pulsing accent strips on the left and right screen edges."""
    pulse = 0.2 + 0.8 * abs(math.sin(frame * math.pi / (FRAME_RATE * 2.5)))
    scale = pulse * 0.35
    fades = [f * scale for f in _EDGE_FADE_BASE]
    for x in range(3 * _SS):
        col = tuple(int(c * fades[x]) for c in accent)
        d.line([(x, 18 * _SS), (x, _H - 1)], fill=col)
        d.line([(_W - 1 - x, 18 * _SS), (_W - 1 - x, _H - 1)], fill=col)


def _draw_alert_strip(d, alerts, frame, status_color, y0, marine_str=None, wind_str=None, age_minutes=None):
    """Top strip: cycles NOAA alerts; when quiet, rotates wind / marine / clock."""
    strip_h = 18 * _SS
    y_mid = y0 + strip_h // 2
    cx    = _W // 2
    d.rectangle([0, y0, _W - 1, y0 + strip_h], fill=(18, 18, 18))
    sep_b = int(45 + 55 * abs(math.sin(frame * math.pi / (FRAME_RATE * 2.0))))
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
                   fill=(195, 215, 195), font=font_strip, anchor="mm")
        elif kind == "marine":
            d.ellipse((r3, y_mid - r3, r9, y_mid + r3), fill=(35, 70, 115))
            d.text((cx, y_mid), _fit_text(d, text, font_strip, strip_max),
                   fill=(130, 158, 180), font=font_strip, anchor="mm")
        else:
            _draw_marine_wave(d, frame, wave_color, y_mid)
            d.ellipse((r3, y_mid - r3, r9, y_mid + r3), outline=(72, 72, 72), width=_SS)
            clk = 6 * _SS
            d.line([(clk, y_mid), (clk, y_mid - 2 * _SS)], fill=(72, 72, 72), width=_SS)
            d.line([(clk, y_mid), (clk + 2 * _SS, y_mid)], fill=(72, 72, 72), width=_SS)
            time_str = time.strftime("%a %H:%M")
            if age_minutes is not None:
                age_str = f"{int(age_minutes)}m"
                d.text((cx - 4 * _SS, y_mid), time_str, fill=(128, 128, 128), font=font_strip, anchor="rm")
                d.text((cx + 4 * _SS, y_mid), age_str,  fill=(90, 90, 90),   font=font_strip, anchor="lm")
            else:
                d.text((cx, y_mid), time_str, fill=(128, 128, 128), font=font_strip, anchor="mm")

        # Horizontal slot progress dots at right edge
        n_slots = len(slots)
        for si in range(n_slots):
            dx = _W - 4 * _SS - (n_slots - 1 - si) * 5 * _SS
            dc = (tuple(min(255, int(c * 0.65)) for c in status_color) if si == idx
                  else (50, 50, 50))
            pr = 2 * _SS
            d.ellipse((dx - pr, y_mid - pr, dx + pr, y_mid + pr), fill=dc)
        return

    idx = (frame // (FRAME_RATE * 4)) % len(alerts)   # new alert every 4 s
    name, severity = alerts[idx]
    color = _ALERT_COLORS.get(severity, _YELLOW)

    # Pulsing warning dot — abs(sin) keeps the dot visible at all times (min 0.38)
    pulse = 0.38 + 0.62 * abs(math.sin(frame * math.pi / (FRAME_RATE * 0.7)))
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
               fill=(82, 82, 82), font=font_strip, anchor="lm")


def _dim(color, factor):
    """Multiply each channel of a (R,G,B) tuple by factor (0–1)."""
    return tuple(max(0, min(255, int(c * factor))) for c in color)


def _gauge_ang(mph_val):
    """PIL angle (radians) for a given mph value on the horseshoe arc."""
    return math.radians(_GAUGE_ARC_START + (mph_val / GAUGE_MAX) * _GAUGE_ARC_SWEEP)


def _draw_gauge(d, cx, cy, r, needle_gust, actual_gust, frame, stale=False, wind=None, history=None, raw_gust=None):
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
        ob_col = tuple(max(16, int(c * ob_p * 0.25)) for c in zone_c)
    d.arc((cx - ob, cy - ob, cx + ob, cy + ob),
          _GAUGE_ARC_START - 4, arc_end + 4, fill=ob_col, width=2 * _SS)

    # Dark backing arc — layered to create a subtle cross-section depth
    d.arc(box, _GAUGE_ARC_START - 2, arc_end + 2, fill=(20, 20, 20), width=22 * _SS)
    d.arc(box, _GAUGE_ARC_START - 2, arc_end + 2, fill=(36, 36, 36), width=14 * _SS)
    d.arc(box, _GAUGE_ARC_START - 2, arc_end + 2, fill=(24, 24, 24), width= 6 * _SS)

    # GOOD-state inner glow + sunburst — drawn into the clear face of the horseshoe
    if not stale and actual_gust < GOOD_MPH:
        # Soft pulsing green disc in the gauge interior
        glow_r = 55 * _SS
        glow_p = 0.10 + 0.06 * abs(math.sin(frame * math.pi / (FRAME_RATE * 3.0)))
        d.ellipse((cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r),
                  fill=(0, int(185 * glow_p), int(80 * glow_p)))
        # Rotating rays — 1 trig call for base angle, then rotation matrix for each ray
        ray_r = math.radians((frame * 2.5) % 360)
        bc, bs = math.cos(ray_r), math.sin(ray_r)
        for i in range(12):
            ca_r = bc * _SUNBURST_C[i] - bs * _SUNBURST_S[i]
            sa_r = bs * _SUNBURST_C[i] + bc * _SUNBURST_S[i]
            b = int(12 + 8 * math.sin(frame * math.pi / (FRAME_RATE * 2.5) + i * math.pi / 6))
            d.line([
                (int(cx + 20 * _SS * ca_r), int(cy + 20 * _SS * sa_r)),
                (int(cx + 48 * _SS * ca_r), int(cy + 48 * _SS * sa_r))
            ], fill=(0, b, b // 3), width=_SS)

    dim = 0.30 if stale else 1.0

    # Per-zone brightness: active zone full, inactive zones dimmed to 35%
    if stale:
        gd = yd = rd = 0.30
    elif needle_gust < GOOD_MPH:
        gd, yd, rd = dim, dim * 0.35, dim * 0.35
    elif needle_gust <= CAUTION_MPH:
        gd, yd, rd = dim * 0.35, dim, dim * 0.35
    else:
        gd, yd, rd = dim * 0.35, dim * 0.35, dim

    # Soft ambient glow behind zone arcs — wider dim pre-pass for depth
    d.arc(box, _GAUGE_ARC_START, GOOD_ARC_END,   fill=_dim(_GREEN,  gd * 0.26), width=26 * _SS)
    d.arc(box, GOOD_ARC_END,    CAUTION_ARC_END, fill=_dim(_YELLOW, yd * 0.26), width=26 * _SS)
    d.arc(box, CAUTION_ARC_END, arc_end,         fill=_dim(_RED,    rd * 0.26), width=26 * _SS)

    # Colored zone arcs
    d.arc(box, _GAUGE_ARC_START, GOOD_ARC_END,   fill=_dim(_GREEN,  gd), width=16 * _SS)
    d.arc(box, GOOD_ARC_END,    CAUTION_ARC_END, fill=_dim(_YELLOW, yd), width=16 * _SS)
    d.arc(box, CAUTION_ARC_END, arc_end,         fill=_dim(_RED,    rd), width=16 * _SS)

    # Thin specular highlight at the inner edge of each arc band — adds depth
    ih   = r - 8 * _SS
    ibox = (cx - ih, cy - ih, cx + ih, cy + ih)
    d.arc(ibox, _GAUGE_ARC_START, GOOD_ARC_END,   fill=_dim(_GREEN,  gd * 0.55), width=2 * _SS)
    d.arc(ibox, GOOD_ARC_END,    CAUTION_ARC_END, fill=_dim(_YELLOW, yd * 0.55), width=2 * _SS)
    d.arc(ibox, CAUTION_ARC_END, arc_end,         fill=_dim(_RED,    rd * 0.55), width=2 * _SS)

    # Pulsing brightness boost on the active zone — breathes to confirm live data
    if not stale:
        zp = 0.28 + 0.26 * abs(math.sin(frame * math.pi / (FRAME_RATE * 1.8)))
        if needle_gust < GOOD_MPH:
            za_s, za_e, zc = _GAUGE_ARC_START, GOOD_ARC_END, _GREEN
        elif needle_gust <= CAUTION_MPH:
            za_s, za_e, zc = GOOD_ARC_END, CAUTION_ARC_END, _YELLOW
        else:
            za_s, za_e, zc = CAUTION_ARC_END, arc_end, _RED
        d.arc(box, za_s, za_e, fill=_dim(zc, zp), width=26 * _SS)

    # Bright narrow trace from 0 to current needle — each segment stays its zone color
    if needle_gust > 0.1 and not stale:
        na_end = _GAUGE_ARC_START + (needle_gust / GAUGE_MAX) * _GAUGE_ARC_SWEEP
        if na_end > _GAUGE_ARC_START:
            d.arc(box, _GAUGE_ARC_START, min(na_end, GOOD_ARC_END),
                  fill=(0, 220, 100), width=4 * _SS)
        if na_end > GOOD_ARC_END:
            d.arc(box, GOOD_ARC_END, min(na_end, CAUTION_ARC_END),
                  fill=(255, 210, 0), width=4 * _SS)
        if na_end > CAUTION_ARC_END:
            d.arc(box, CAUTION_ARC_END, na_end, fill=(255, 80, 80), width=4 * _SS)

    # Animated wind streaks flowing inside the arc face — speed tracks actual wind
    _draw_wind_streaks(d, cx, cy, r, raw_gust if raw_gust is not None else actual_gust, frame)

    # Tick marks — colored by zone; ticks near the needle glow brighter
    needle_deg = _GAUGE_ARC_START + (needle_gust / GAUGE_MAX) * _GAUGE_ARC_SWEEP
    for mph_val, tick_deg, x1, y1, x2, y2, is_major in _TICK_DATA:
        t_dim = dim * (0.7 if is_major else 0.45)
        if mph_val <= GOOD_MPH:
            tick_c = _dim(_GREEN, t_dim)
        elif mph_val <= CAUTION_MPH:
            tick_c = _dim(_YELLOW, t_dim)
        else:
            tick_c = _dim(_RED, t_dim)
        if not stale and needle_gust > 0:
            prox = max(0.0, 1.0 - abs(tick_deg - needle_deg) / 14.0)
            if prox > 0:
                tick_c = tuple(min(255, int(c + 110 * prox)) for c in tick_c)
        d.line([(x1, y1), (x2, y2)],
               fill=tick_c, width=2 * _SS if is_major else _SS)


    # Peak gust marker — bright tick just outside the arc at session-max wind position
    if history and len(history) >= 2 and not stale:
        peak = max(history)
        cur  = raw_gust if raw_gust is not None else actual_gust
        if peak > cur + 0.3:
            p_ang  = _gauge_ang(min(peak, GAUGE_MAX))
            p_ca, p_sa = math.cos(p_ang), math.sin(p_ang)
            po = (cx + (r + 4 * _SS) * p_ca, cy + (r + 4 * _SS) * p_sa)
            pi = (cx + (r - 4 * _SS) * p_ca, cy + (r - 4 * _SS) * p_sa)
            d.line([(int(po[0]), int(po[1])), (int(pi[0]), int(pi[1]))],
                   fill=(220, 200, 55), width=2 * _SS)
            lx = int(cx + (r + 15 * _SS) * p_ca)
            ly = int(cy + (r + 15 * _SS) * p_sa)
            d.text((lx, ly), f"{peak:.0f}", fill=(168, 148, 38), font=font_label, anchor="mm")

    if stale:
        sweep_pos = _GAUGE_ARC_START + (frame * 3) % _GAUGE_ARC_SWEEP
        trail_s   = max(_GAUGE_ARC_START, sweep_pos - 28)
        d.arc(box, trail_s, sweep_pos, fill=(0, 22, 11), width=14 * _SS)
        d.text((cx, cy), "STALE", fill=(72, 72, 72), font=font_label, anchor="mm")

    # Sustained avg wind: diamond marker on the arc, plus tiny readout below hub
    if wind is not None and not stale:
        w_ang  = _gauge_ang(min(wind, GAUGE_MAX))
        w_ca, w_sa = math.cos(w_ang), math.sin(w_ang)
        wx = int(cx + (r - 10 * _SS) * w_ca)
        wy = int(cy + (r - 10 * _SS) * w_sa)
        ds = 4 * _SS
        # Dark disc backdrop so the diamond reads cleanly over the colored arc
        d.ellipse((wx - 7 * _SS, wy - 7 * _SS, wx + 7 * _SS, wy + 7 * _SS), fill=(10, 10, 10))
        d.polygon([(wx, wy - ds), (wx + ds, wy), (wx, wy + ds), (wx - ds, wy)],
                  fill=(45, 45, 45), outline=(178, 178, 178))

    # Kite-shaped needle — soft glow arc at its angle for a back-lit instrument feel
    pct = min(max(needle_gust / GAUGE_MAX, 0), 1)
    ang = _gauge_ang(pct * GAUGE_MAX)
    if not stale and actual_gust > CAUTION_MPH:
        ang += math.radians(2.5 * math.sin(frame * math.pi / (FRAME_RATE * 0.10)))
    if not stale and needle_gust > 0.1:
        glow_ang = _GAUGE_ARC_START + (needle_gust / GAUGE_MAX) * _GAUGE_ARC_SWEEP
        glow_c   = ((0, 36, 18) if needle_gust < GOOD_MPH
                    else ((42, 32, 0) if needle_gust <= CAUTION_MPH
                          else (42, 8, 8)))
        glow_w = int((17 + 7 * abs(math.sin(frame * math.pi / (FRAME_RATE * 0.7)))) * _SS)
        d.arc(box, glow_ang - 7, glow_ang + 7, fill=glow_c, width=glow_w)
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
        tr = max(2 * _SS, int(6 * _SS * tp))
        d.ellipse((int(tip[0]) - tr, int(tip[1]) - tr,
                   int(tip[0]) + tr, int(tip[1]) + tr), fill=tc)

    # Pivot hub — three concentric rings; center dot colored by current zone
    h1, h2, h3 = 11 * _SS, 8 * _SS, 5 * _SS
    d.ellipse((cx - h1, cy - h1, cx + h1, cy + h1), fill=(28, 28, 28), outline=(108, 108, 108), width=_SS)
    d.ellipse((cx - h2, cy - h2, cx + h2, cy + h2), fill=(18, 18, 18), outline=(68, 68, 68), width=_SS)
    # Specular highlight — upper-left catch-light gives the hub a convex, 3-D instrument feel
    d.ellipse((cx - 5 * _SS, cy - 7 * _SS, cx - 1 * _SS, cy - 3 * _SS), fill=(95, 95, 95))
    if stale:
        hub_c = (90, 90, 90)
    elif actual_gust > CAUTION_MPH:
        hub_c = (235, 60, 60)
    elif actual_gust > GOOD_MPH:
        hub_c = (225, 185, 0)
    else:
        hub_c = (0, 200, 90)
    d.ellipse((cx - h3, cy - h3, cx + h3, cy + h3), fill=hub_c)


def _condition_statuses(state):
    """Return (wind_status, wave_status, temp_status) each as 'GO'/'CAUTION'/'NO-GO'."""
    gust = state.get("gust") or 0.0
    wvht = state.get("wvht")
    wtmp = state.get("wtmp")

    if gust < GOOD_MPH:
        ws = "GO"
    elif gust <= CAUTION_MPH:
        ws = "CAUTION"
    else:
        ws = "NO-GO"

    if wvht is None or wvht < WAVE_GOOD_FT:
        wvs = "GO"
    elif wvht <= WAVE_CAUTION_FT:
        wvs = "CAUTION"
    else:
        wvs = "NO-GO"

    # Short-period chop upgrades a borderline GO to CAUTION even at low wave heights
    dpd = state.get("dpd")
    if wvs == "GO" and dpd is not None and dpd < WAVE_CHOP_DPD and wvht is not None and wvht >= 1.0:
        wvs = "CAUTION"

    if wtmp is None or wtmp >= TEMP_COOL_F:
        ts = "GO"
    elif wtmp >= TEMP_COLD_F:
        ts = "CAUTION"
    else:
        ts = "NO-GO"

    return ws, wvs, ts


def _composite_score(state):
    """Weighted go/no-go score on the 0–GAUGE_MAX scale.

    Wind gust is primary (55%).  Wave height, water temperature, and active
    NOAA alerts each contribute the remainder.  Any active alert forces the
    result to at least the GOOD/CAUTION boundary.
    """
    gust   = state.get("gust") or 0.0
    wvht   = state.get("wvht")
    wtmp   = state.get("wtmp")
    atmp   = state.get("atmp")
    alerts = state.get("alerts") or []

    wind_eq = min(float(gust), float(GAUGE_MAX))

    # Hot-day comfort: warm air makes moderate wind feel refreshing, not threatening.
    # Max 5 mph equivalent relief — enough to shift a warm breezy day from CAUTION to GO
    # without letting dangerous winds disappear (30 mph is NO-GO regardless of heat).
    if atmp is not None and atmp > ATMP_WARM_F:
        warm_relief = min(5.0, (atmp - ATMP_WARM_F) / 20.0 * 5.0)
        wind_eq = max(0.0, wind_eq - warm_relief)

    # Map wave height: WAVE_GOOD_FT → GOOD_MPH, WAVE_CAUTION_FT → CAUTION_MPH
    if wvht is None or wvht <= 0:
        wave_eq = 0.0
    elif wvht <= WAVE_GOOD_FT:
        wave_eq = (wvht / WAVE_GOOD_FT) * GOOD_MPH
    elif wvht <= WAVE_CAUTION_FT:
        t = (wvht - WAVE_GOOD_FT) / (WAVE_CAUTION_FT - WAVE_GOOD_FT)
        wave_eq = GOOD_MPH + t * (CAUTION_MPH - GOOD_MPH)
    else:
        wave_eq = min(GAUGE_MAX, CAUTION_MPH + (wvht - WAVE_CAUTION_FT) * 3.5)

    # Cold water/air safety + comfort penalty
    temp_eq = 0.0
    if wtmp is not None and wtmp < TEMP_COOL_F:
        span = max(1, TEMP_COOL_F - TEMP_COLD_F)
        temp_eq = max(temp_eq, min(CAUTION_MPH,
                      (TEMP_COOL_F - wtmp) / span * CAUTION_MPH))
    if atmp is not None:
        if atmp < TEMP_COLD_F:
            # Safety: near-freezing air — hypothermia risk if anything goes wrong
            temp_eq = max(temp_eq, min(GOOD_MPH,
                          (TEMP_COLD_F - atmp) / 15.0 * GOOD_MPH))
        elif atmp < ATMP_CHILLY_F:
            # Comfort: open pontoon deck with chilly air (50–62 °F) is unpleasant,
            # especially once any wind chill is added. Scales up to 0.75×GOOD_MPH
            # so cold air alone won't force CAUTION but adds pressure when combined.
            chilly_frac = (ATMP_CHILLY_F - atmp) / (ATMP_CHILLY_F - TEMP_COLD_F)
            temp_eq = max(temp_eq, chilly_frac * GOOD_MPH * 0.75)

    # DPD modulates wave danger: choppy short-period seas are worse; long gentle swells forgiven
    dpd = state.get("dpd")
    if dpd is not None and wvht and wvht > 0:
        if dpd < WAVE_CHOP_DPD:
            wave_eq *= 1.20   # short-period chop is rougher on a pontoon
        elif dpd >= WAVE_SWELL_DPD:
            wave_eq *= 0.85   # long swell rolls under the boat more gently

    # Fog: tight air-dewpoint spread means near-saturated air (mist/fog likely on the water)
    fog_eq = 0.0
    dewp = state.get("dewp")
    if atmp is not None and dewp is not None and (atmp - dewp) < FOG_SPREAD_F:
        fog_eq = GOOD_MPH + 0.5   # fog alone always floors at CAUTION

    # Falling pressure signals incoming weather — adds a caution floor
    pres_eq = 0.0
    pres_history = state.get("pres_history") or []
    if len(pres_history) >= 3:
        oldest_p = next((p for p in reversed(pres_history) if p is not None), None)
        newest_p = pres_history[0] if pres_history[0] is not None else None
        if oldest_p is not None and newest_p is not None:
            pres_fall = oldest_p - newest_p   # positive = pressure dropping
            if pres_fall >= PRES_FALL_CAUTION:
                # At threshold: GOOD_MPH (caution entry); above: capped at GOOD_MPH+0.5
                pres_eq = min(GOOD_MPH + 0.5, GOOD_MPH * (pres_fall / PRES_FALL_CAUTION))

    # Each condition can independently push the verdict; waves/temp are slightly
    # discounted so a marginal reading alone doesn't trigger NO-GO on its own.
    composite = max(
        wind_eq,          # wind: full weight — 20 mph alone = NO-GO
        wave_eq * 0.75,   # waves: 4 ft -> 16 (CAUTION); >4.5 ft -> NO-GO
        temp_eq * 0.80,   # temp: 45F water -> CAUTION; warmer = smaller penalty
        pres_eq,          # falling pressure: floor near CAUTION for rapid drops
        fog_eq,           # tight dewpoint spread: fog/mist = always CAUTION
    )

    # Active alerts -> hard floor just inside caution zone
    if alerts:
        composite = max(composite, GOOD_MPH + 0.5)

    return min(composite, GAUGE_MAX)


def render_display(state, frame, needle_gust, composite):
    wind    = state["wind"]
    gust    = state["gust"]
    wdir    = state["wdir"]
    age     = state["age"]
    wtmp    = state["wtmp"]
    wvht    = state["wvht"]
    atmp    = state["atmp"]
    dpd          = state["dpd"]
    dewp         = state["dewp"]
    pres         = state["pres"]
    pres_history = state.get("pres_history") or []
    alerts       = state["alerts"]
    history      = state.get("gust_history", [])

    # Feels-like temperature — computed once, used by dots, secondary row, and marine strip
    feels_hi = None   # heat index (°F) when hot + humid
    feels_wc = None   # wind chill (°F) when cold + windy
    if atmp is not None and dewp is not None and atmp >= 80:
        rh = _relative_humidity(atmp, dewp)
        if rh >= 40:
            hi = _heat_index_f(atmp, rh)
            if hi >= atmp + 3:       # only meaningful when notably above air temp
                feels_hi = hi
    if atmp is not None and wind is not None and atmp <= 50 and wind >= 3:
        wc = _wind_chill_f(atmp, wind)
        if wc <= atmp - 3:           # only meaningful when notably below air temp
            feels_wc = wc
    extreme_heat = feels_hi is not None and feels_hi >= 103  # NOAA "Danger" threshold

    cond_wind, cond_wave, cond_temp = _condition_statuses(state)
    msg    = ("GO"      if composite < GOOD_MPH
              else "CAUTION" if composite <= CAUTION_MPH
              else "NO-GO")
    accent = _STATUS_CONFIG[msg][0]

    trend = _trend(history)

    img, d = make_image()
    r  = _GAUGE_R
    cx = _GAUGE_CX
    cy = _GAUGE_CY

    info_y = _INFO_Y

    # Draw animated info-section background first (behind all text)
    _draw_info_bg(d, info_y, _H - 1, msg, frame)

    stale = age is not None and age >= STALE_MINUTES

    # Pulsing halo arcs just outside the gauge — tinted by status, very dim
    if not stale:
        halo_p   = 0.3 + 0.7 * abs(math.sin(frame * math.pi / (FRAME_RATE * 3.0)))
        halo_end = _GAUGE_ARC_START + _GAUGE_ARC_SWEEP
        for h_off, h_fac in ((r + 17 * _SS, 0.17), (r + 24 * _SS, 0.10)):
            hc = tuple(max(0, int(c * h_fac * halo_p)) for c in accent)
            if any(v > 0 for v in hc):
                d.arc((cx - h_off, cy - h_off, cx + h_off, cy + h_off),
                      _GAUGE_ARC_START - 8, halo_end + 8, fill=hc, width=2 * _SS)

    _draw_gauge(d, cx, cy, r, needle_gust, composite, frame, stale=stale, wind=wind, history=history, raw_gust=gust)

    # Data freshness bar — centered horizontal line in the gauge mouth gap
    if age is not None:
        freshness = max(0.0, 1.0 - age / STALE_MINUTES)
        bar_hw = int(42 * _SS * freshness)
        if bar_hw > 0:
            bright = max(28, int(85 * freshness))
            by = info_y - 7 * _SS
            d.line([(cx - bar_hw, by), (cx + bar_hw, by)], fill=(bright, bright, bright), width=_SS)

    # Separator — 3-zone gradient line: bright center, dim edges; 6 draw calls total
    sep_p = 0.3 + 0.7 * abs(math.sin(frame * math.pi / (FRAME_RATE * 3)))
    for sx0, bfac in _SEP_SEGS:
        sc_ = tuple(int(c * sep_p * bfac) for c in accent)
        if any(v > 0 for v in sc_):
            d.line([(sx0, _SEP_Y1), (_W - sx0, _SEP_Y1)], fill=sc_, width=_SS)
            d.line([(sx0, _SEP_Y2), (_W - sx0, _SEP_Y2)], fill=tuple(v // 2 for v in sc_), width=_SS)

    # ── Info section ──────────────────────────────────────────────────────────
    # Full-width status band (replaces narrow centered badge)
    band_h  = _BAND_H
    band_y0 = _BAND_Y0
    band_y1 = _BAND_Y1
    _, badge_bg = _STATUS_CONFIG[msg]
    band_bg = badge_bg if stale else tuple(min(255, int(c * 0.65)) for c in accent)
    d.rectangle([0, band_y0, _W - 1, band_y1], fill=band_bg)
    # Bright top/bottom edges on the band
    edge_p  = 0.5 + 0.5 * abs(math.sin(frame * math.pi / (FRAME_RATE * 2.0)))
    edge_c  = tuple(min(255, int(c * edge_p)) for c in accent) if not stale else (55, 55, 55)
    d.line([(0, band_y0), (_W - 1, band_y0)], fill=edge_c, width=2 * _SS)
    d.line([(0, band_y1), (_W - 1, band_y1)], fill=tuple(c // 2 for c in edge_c), width=_SS)
    vib_x  = [-1 * _SS, 0, 1 * _SS, 0][frame % 4] if msg == "NO-GO" and not stale else 0
    band_cy = band_y0 + band_h // 2
    if not stale:
        d.text((cx + vib_x + _SS, band_cy + _SS), msg, fill=(0, 0, 0), font=font_unit, anchor="mm")
    d.text((cx + vib_x, band_cy), msg,
           fill=(60, 60, 60) if stale else (255, 255, 255),
           font=font_unit, anchor="mm")

    # Condition dots — right side of status band (right→left): wind | wave | temp | weather
    # "weather" dot covers fog, falling pressure, and chilly air — makes silent CAUTION visible
    _DOT_C = {"GO": _GREEN, "CAUTION": _YELLOW, "NO-GO": _RED}
    fog_risk     = dewp is not None and atmp is not None and (atmp - dewp) < FOG_SPREAD_F
    pres_falling = False
    if pres is not None and len(pres_history) >= 3:
        oldest_p = next((p for p in reversed(pres_history) if p is not None), None)
        pres_falling = oldest_p is not None and (oldest_p - pres) >= PRES_FALL_CAUTION
    cold_air     = atmp is not None and atmp < ATMP_CHILLY_F
    cond_weather  = "CAUTION" if (fog_risk or pres_falling or cold_air or extreme_heat) else "GO"
    weather_known = dewp is not None or pres is not None or atmp is not None
    dot_r  = 3 * _SS
    dot_y  = band_cy
    for j, (cond, has_data) in enumerate((
            (cond_wind,    True),
            (cond_wave,    wvht is not None),
            (cond_temp,    wtmp is not None),
            (cond_weather, weather_known),
    )):
        dx = _W - (9 + j * 8) * _SS
        dc = _DOT_C[cond] if (has_data and not stale) else (50, 50, 50)
        d.ellipse((dx - dot_r, dot_y - dot_r, dx + dot_r, dot_y + dot_r), fill=dc)

    # Secondary info row — water temp | weather alert | wave height
    row_y = _ROW_Y
    if not stale and (wtmp is not None or wvht is not None
                      or fog_risk or pres_falling or cold_air or extreme_heat):
        # Dark tinted backdrop so text reads over the animated info-section background
        d.rectangle([0, row_y - 9 * _SS, _W - 1, row_y + 9 * _SS],
                    fill=tuple(max(0, c // 4) for c in accent))
        sh = _SS  # 1 device shadow offset
        if wtmp is not None:
            wt_str  = f"{wtmp:.0f}° water"
            wt_fill = _DOT_C[cond_temp]
            d.text((8 * _SS + sh, row_y + sh), wt_str, fill=(0, 0, 0), font=font_label, anchor="lm")
            d.text((8 * _SS,      row_y),       wt_str, fill=wt_fill,   font=font_label, anchor="lm")
        # Centre slot: explain why the weather dot is yellow when it's the cause of CAUTION
        if fog_risk or pres_falling or cold_air or extreme_heat:
            wx_parts = []
            if fog_risk:      wx_parts.append("~Fog")
            if pres_falling:  wx_parts.append("↓P")
            if cold_air:      wx_parts.append(f"Chilly {atmp:.0f}°")
            if extreme_heat:  wx_parts.append(f"HI {feels_hi:.0f}°!")
            wx_str = "  ".join(wx_parts)
            d.text((cx + sh, row_y + sh), wx_str, fill=(0, 0, 0),   font=font_label, anchor="mm")
            d.text((cx,      row_y),       wx_str, fill=_YELLOW,     font=font_label, anchor="mm")
        if wvht is not None:
            wv_str  = (f"{wvht:.1f}ft/{dpd:.0f}s" if dpd is not None else f"{wvht:.1f}ft waves")
            wv_fill = _DOT_C[cond_wave]
            d.text((_W - 8 * _SS + sh, row_y + sh), wv_str, fill=(0, 0, 0), font=font_label, anchor="rm")
            d.text((_W - 8 * _SS,      row_y),       wv_str, fill=wv_fill,   font=font_label, anchor="rm")

    # Big gust number — vertically centered in remaining space below info row
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
    gust_int = min(55, max(0, round(gust)))
    num_str  = str(gust_int)
    num_w    = _GUST_WIDTHS[gust_int]
    unit_w  = _MPH_UNIT_W
    grp_x   = (_W - num_w - 8 * _SS - unit_w) // 2
    num_top = row_y + 20 * _SS   # start below the info row
    num_y   = num_top + (_H - num_top) // 2   # center in remaining space
    d.text((grp_x + 2 * _SS,             num_y + 2 * _SS),              num_str, fill=(0, 0, 0),  font=font_gust, anchor="lm")
    d.text((grp_x,                        num_y),                        num_str, fill=gust_fill,  font=font_gust, anchor="lm")
    d.text((grp_x + num_w + 10 * _SS,    num_y + 16 * _SS + 2 * _SS),  "mph",   fill=(0, 0, 0),  font=font_unit, anchor="lm")
    d.text((grp_x + num_w + 8  * _SS,    num_y + 16 * _SS),             "mph",   fill=mph_fill,   font=font_unit, anchor="lm")

    # Trend arrow — left margin, vertically aligned with number center
    if trend is not None:
        _draw_trend(d, 18 * _SS, num_y - 12 * _SS, trend)

    # Wind + trend shown in the advisory strip (cycling with marine / clock)
    dir_tag  = f"  {wdir}" if wdir else ""
    if gust is not None and wind is not None and abs(gust - wind) > 1.0:
        wind_str = f"Gust {gust:.0f}  Wind {wind:.0f} mph{dir_tag}"
    else:
        wind_str = f"Wind {wind:.0f} mph{dir_tag}"
    if trend is not None:
        arrow = "↑" if trend == "up" else ("↓" if trend == "down" else "→")
        wind_str += f" {arrow}"

    # Build marine string for the advisory strip cycle
    marine_parts = []
    if wtmp is not None:
        marine_parts.append(f"Water {wtmp:.0f}°")
    if atmp is not None:
        if feels_hi is not None:
            marine_parts.append(f"Air {atmp:.0f}° / HI {feels_hi:.0f}°")
        elif feels_wc is not None:
            marine_parts.append(f"Air {atmp:.0f}° / WC {feels_wc:.0f}°")
        else:
            marine_parts.append(f"Air {atmp:.0f}°")
    if wvht is not None:
        if dpd is not None:
            marine_parts.append(f"{wvht:.1f}ft {dpd:.0f}s waves")
        else:
            marine_parts.append(f"{wvht:.1f}ft waves")
    if pres is not None:
        oldest_p = next((p for p in reversed(pres_history) if p is not None), None) if len(pres_history) >= 3 else None
        if oldest_p is not None:
            pfall = oldest_p - pres
            p_arrow = " ↓" if pfall >= 0.5 else (" ↑" if pfall <= -0.5 else "")
        else:
            p_arrow = ""
        marine_parts.append(f"{pres:.0f}hPa{p_arrow}")
    if dewp is not None and atmp is not None and (atmp - dewp) < FOG_SPREAD_F:
        marine_parts.append("~Fog")
    marine_str = "  ".join(marine_parts) if marine_parts else None

    # Pulsing accent strips framing the left/right screen edges
    _draw_edge_accents(d, accent, frame)

    # Animated wave at screen bottom — table lookup replaces per-pixel sin(); 3-row depth
    accent_dim  = tuple(c // 4  for c in accent)
    accent_dim2 = tuple(c // 10 for c in accent)
    accent_dim3 = tuple(c // 22 for c in accent)
    bwave_off   = (frame * 3) % _BWAVE_PERIOD
    bwave_row1  = [(bx, _H - 1 - _BWAVE_TABLE[(bx + bwave_off) % _BWAVE_PERIOD]) for bx in range(_W)]
    bwave_row2  = [(bx, by - 1) for bx, by in bwave_row1 if by > 0]
    bwave_row3  = [(bx, by - 2) for bx, by in bwave_row1 if by > 1]
    d.line(bwave_row1, fill=accent_dim)
    if bwave_row2:
        d.line(bwave_row2, fill=accent_dim2)
    if bwave_row3:
        d.line(bwave_row3, fill=accent_dim3)


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
            dpd_raw  = row.get("DPD",  "MM")
            dewp_raw = row.get("DEWP", "MM")
            wtmp = celsius_to_f(float(wtmp_raw)) if wtmp_raw != "MM" else None
            wvht = m_to_ft(float(wvht_raw))      if wvht_raw != "MM" else None
            atmp = celsius_to_f(float(atmp_raw)) if atmp_raw != "MM" else None
            dewp = celsius_to_f(float(dewp_raw)) if dewp_raw != "MM" else None
            # DPD: NDBC uses "MM" and "99.00" as missing sentinels
            try:
                dpd = float(dpd_raw) if dpd_raw not in ("MM", "99.00") else None
                if dpd is not None and dpd <= 0:
                    dpd = None
            except ValueError:
                dpd = None
            pres_raw = row.get("PRES", "MM")
            try:
                pres = float(pres_raw) if pres_raw != "MM" else None
            except ValueError:
                pres = None
            with _lock:
                new_history = [gust] + _state["gust_history"][:5]
                new_pres_history = (
                    [pres] + _state["pres_history"][:5]
                    if pres is not None else list(_state["pres_history"])
                )
                _state.update(wind=wind, gust=gust, wdir=wdir, age=age,
                              wtmp=wtmp, wvht=wvht, atmp=atmp, dpd=dpd,
                              dewp=dewp, pres=pres, pres_history=new_pres_history,
                              gust_history=new_history, error=None)
            logging.info(
                "wind=%.1f mph gust=%.1f mph dir=%s age=%sm wtmp=%s wvht=%s pres=%s dewp=%s",
                wind, gust, wdir, age,
                f"{wtmp:.1f}°F" if wtmp is not None else "MM",
                f"{wvht:.1f}ft" if wvht is not None else "MM",
                f"{pres:.1f}hPa" if pres is not None else "MM",
                f"{dewp:.1f}°F" if dewp is not None else "MM",
            )
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
                s["pres_history"] = list(_state["pres_history"])
            if s.get("wind") is not None:
                c = _composite_score(s)
                s["status"] = ("GO" if c < GOOD_MPH
                               else "CAUTION" if c <= CAUTION_MPH
                               else "NO-GO")
            else:
                s["status"] = "OFFLINE"
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

    _start_web_server()
    threading.Thread(target=_data_loop, daemon=True).start()

    frame = 0
    while True:
        t0 = time.monotonic()

        with _lock:
            snap                  = dict(_state)
            snap["alerts"]        = list(_state["alerts"])
            snap["gust_history"]  = list(_state["gust_history"])
            snap["pres_history"]  = list(_state["pres_history"])

        if snap["wind"] is not None:
            # Compute composite once — used for spring target and rendering
            snap_composite = _composite_score(snap)
            diff = snap_composite - _needle_gust
            _needle_vel  = max(-6.0, min(6.0, _needle_vel * _NEEDLE_DAMPING + diff * _NEEDLE_SPRING))
            _needle_gust = max(0.0, min(GAUGE_MAX + 3, _needle_gust + _needle_vel))
            try:
                render_display(snap, frame, _needle_gust, snap_composite)
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
                bright = int(72 + 42 * math.sin(frame * math.pi / (FRAME_RATE * 2)))
                dim2   = int(bright * 0.62)
                dim3   = int(bright * 0.84)
                img, d = make_image()
                cx_s  = _GAUGE_CX
                spin  = (frame * 14) % 360
                bc    = (bright, bright, bright)
                d.arc((cx_s - 30 * _SS, 110 * _SS, cx_s + 30 * _SS, 170 * _SS), spin, spin + 115, fill=bc, width=3 * _SS)
                d.arc((cx_s - 30 * _SS, 110 * _SS, cx_s + 30 * _SS, 170 * _SS), spin + 115, spin + 230,
                      fill=(bright // 4, bright // 4, bright // 4), width=2 * _SS)
                spin2 = (360 - frame * 9) % 360
                d.arc((cx_s - 18 * _SS, 122 * _SS, cx_s + 18 * _SS, 158 * _SS), spin2, spin2 + 80,
                      fill=(bright // 3, bright // 3, bright // 3), width=2 * _SS)
                draw_centered(d, 182 * _SS, "PONTOON WIND",
                              (int(bright * 0.60), bright, int(bright * 0.72)), font_title)
                draw_centered(d, 198 * _SS, "NDBC 41038 · Cape Fear",
                              (int(dim2 * 0.55), int(dim2 * 0.75), dim2), font_label)
                draw_centered(d, 216 * _SS, f"Connecting{dots}",
                              (dim3 // 2, dim3 // 2, dim3 // 2), font_data)
                _show(img)
            except Exception:
                logging.exception("Connecting screen render failed")

        frame += 1
        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, 1 / FRAME_RATE - elapsed))


if __name__ == "__main__":
    main()
