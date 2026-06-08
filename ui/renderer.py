"""
Display rendering for Pontoon Wind Meter.

Call :func:`init` once at startup to attach the hardware device and
configuration.  After that, :func:`render_display` produces one frame.

All drawing is done on a 2× supersampled PIL canvas that is BILINEAR-
downscaled to the device resolution before pushing to the ILI9341.
"""

from __future__ import annotations

import io
import math
import os
import threading
import time
from typing import Any

from PIL import Image, ImageDraw, ImageFont

import logic

# ---------------------------------------------------------------------------
# Module-level mutable state — populated by init()
# ---------------------------------------------------------------------------
_cfg: dict[str, Any] = {}
_device = None

_SS: int     = 2
_W:  int     = 640
_H:  int     = 480

# Gauge constants (set by init)
_GAUGE_R:  int = 140
_GAUGE_CX: int = 320
_GAUGE_CY: int = 230

_GAUGE_ARC_START: int = 135
_GAUGE_ARC_SWEEP: int = 270

_GREEN  = (0, 210, 85)
_YELLOW = (235, 190, 0)
_RED    = (235, 65, 55)

_STATUS_CONFIG: dict[str, tuple] = {
    "GO":      (_GREEN,  (0, 80, 32)),
    "CAUTION": (_YELLOW, (90, 72, 0)),
    "NO-GO":   (_RED,    (95, 20, 16)),
}
_ALERT_COLORS: dict[str, tuple] = {
    "Extreme":  _RED,
    "Severe":   _RED,
    "Moderate": (220, 110, 0),
    "Minor":    _YELLOW,
    "Unknown":  _YELLOW,
}
_COMPASS_DEGREES = {
    "N": 0, "NE": 45, "E": 90, "SE": 135,
    "S": 180, "SW": 225, "W": 270, "NW": 315,
}

# Derived threshold angles (set by init)
_GOOD_ARC_END:    int = 162
_CAUTION_ARC_END: int = 240

# Fonts (set by init)
font_title = font_gust = font_data = font_label = font_strip = font_unit = font_big = None

# Pre-measured constant text widths (set by init)
_MPH_UNIT_W: int = 0
_GUST_WIDTHS: dict[int, int] = {}

# Animation geometry (set by init)
_INFO_Y  = 0
_BAND_H  = 0
_BAND_Y0 = 0
_BAND_Y1 = 0
_ROW_Y   = 0
_SEP_SEGS: list = []
_SEP_Y1  = 0
_SEP_Y2  = 0

_BWAVE_PERIOD: int = 44
_BWAVE_TABLE:  list = []
_BWAVE_FREQ:   float = 0.0

_MARINE_PERIOD:     int  = 110
_MARINE_WAVE_TABLE: list = []
_MARINE_WAVE_FREQ:  float = 0.0

_GOOD_WAVE_PARAMS:   list = []
_GOOD_WAVE_FREQS:    list = []
_GOOD_WAVE_PERIODS:  list = []
_GOOD_WAVE_TABLES:   list = []

_GOOD_PARTICLE_X:  list = []
_CAUTION_DROP_BX:  list = []
_TICK_DATA:        list = []
_TICK_OUTER: int = 0
_TICK_INNER: int = 0

_SUNBURST_C: list = []
_SUNBURST_S: list = []

_EDGE_FADE_BASE: list = []

_NEEDLE_DAMPING: float = 0.89
_NEEDLE_SPRING:  float = 0.047

# Windy background streak geometry (constant)
_WINDY_BXS = [8,  52,  96, 140, 184, 228,  30,  74]
_WINDY_LNS = [22, 28,  20,  26,  24,  18,  32,  16]
_WINDY_SPD = [5,   7,   4,   6,   5,   8,   6,   3]

# PNG frame store for the web dashboard
_frame_lock  = threading.Lock()
_frame_store = [b""]
_show_counter = [0]

try:
    _RESAMPLE_DOWN = Image.Resampling.BILINEAR
except AttributeError:
    _RESAMPLE_DOWN = Image.BILINEAR  # Pillow < 9.1

# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
_FONT_CANDIDATES = [
    os.path.join(_ASSETS_DIR, "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
_BOLD_CANDIDATES = [
    os.path.join(_ASSETS_DIR, "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]
_font_path = next((p for p in _FONT_CANDIDATES if os.path.exists(p)), None)
_bold_path = next((p for p in _BOLD_CANDIDATES if os.path.exists(p)), _font_path)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    if _font_path:
        return ImageFont.truetype(_font_path, size)
    return ImageFont.load_default()


def _load_bold(size: int) -> ImageFont.FreeTypeFont:
    if _bold_path:
        return ImageFont.truetype(_bold_path, size)
    return _load_font(size)


# ---------------------------------------------------------------------------
# Public initialisation
# ---------------------------------------------------------------------------

def init(device, cfg: dict) -> None:
    """Initialise the renderer.

    Must be called once before :func:`render_display`.

    Parameters
    ----------
    device:
        A ``luma.lcd`` device object with ``.width``, ``.height``, and
        ``.display()`` methods.
    cfg:
        Configuration dict from :func:`config.load_config`.
    """
    global _cfg, _device, _SS, _W, _H
    global _GAUGE_R, _GAUGE_CX, _GAUGE_CY
    global _GAUGE_ARC_START, _GAUGE_ARC_SWEEP, _GOOD_ARC_END, _CAUTION_ARC_END
    global font_title, font_gust, font_data, font_label, font_strip, font_unit, font_big
    global _MPH_UNIT_W, _GUST_WIDTHS
    global _INFO_Y, _BAND_H, _BAND_Y0, _BAND_Y1, _ROW_Y, _SEP_SEGS, _SEP_Y1, _SEP_Y2
    global _BWAVE_PERIOD, _BWAVE_TABLE, _BWAVE_FREQ
    global _MARINE_PERIOD, _MARINE_WAVE_TABLE, _MARINE_WAVE_FREQ
    global _GOOD_WAVE_PARAMS, _GOOD_WAVE_FREQS, _GOOD_WAVE_PERIODS, _GOOD_WAVE_TABLES
    global _GOOD_PARTICLE_X, _CAUTION_DROP_BX
    global _TICK_DATA, _TICK_OUTER, _TICK_INNER
    global _SUNBURST_C, _SUNBURST_S
    global _EDGE_FADE_BASE
    global _NEEDLE_DAMPING, _NEEDLE_SPRING

    import logging
    _cfg    = cfg
    _device = device
    _SS     = 2

    _W = device.width  * _SS
    _H = device.height * _SS

    _GAUGE_ARC_START = 135
    _GAUGE_ARC_SWEEP = 270

    good_mph    = cfg["good_mph"]
    caution_mph = cfg["caution_mph"]
    gauge_max   = cfg["gauge_max"]
    frame_rate  = cfg["frame_rate"]

    _GOOD_ARC_END    = round(_GAUGE_ARC_START + (good_mph    / gauge_max) * _GAUGE_ARC_SWEEP)
    _CAUTION_ARC_END = round(_GAUGE_ARC_START + (caution_mph / gauge_max) * _GAUGE_ARC_SWEEP)

    # Fonts
    if _font_path:
        logging.info("Using font: %s", _font_path)
    else:
        logging.warning("No TrueType font found — text will render as tiny bitmap fallback")

    font_title = _load_font(15 * _SS)
    font_gust  = _load_bold(72 * _SS)
    font_data  = _load_font(22 * _SS)
    font_label = _load_font(14 * _SS)
    font_strip = _load_font(16 * _SS)
    font_unit  = _load_bold(36 * _SS)
    font_big   = _load_bold(64 * _SS)

    _dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    _MPH_UNIT_W = int(_dummy.textlength("mph", font=font_unit))
    _GUST_WIDTHS.clear()
    for i in range(56):
        _GUST_WIDTHS[i] = int(_dummy.textlength(str(i), font=font_gust))

    # Gauge layout
    _GAUGE_R  = 70 * _SS
    _GAUGE_CX = _W // 2
    _GAUGE_CY = (18 + 14) * _SS + _GAUGE_R + 11 * _SS

    # Info / band layout
    _INFO_Y  = _GAUGE_CY + int(_GAUGE_R * 0.707) + 8 * _SS
    _BAND_H  = 36 * _SS
    _BAND_Y0 = _INFO_Y + 4 * _SS
    _BAND_Y1 = _BAND_Y0 + _BAND_H
    _ROW_Y   = _BAND_Y1 + 10 * _SS
    _SEP_SEGS[:] = [(int(_GAUGE_CX * f), b) for f, b in ((0.10, 0.22), (0.40, 0.42), (0.70, 0.62))]
    _SEP_Y1  = _INFO_Y - 4 * _SS
    _SEP_Y2  = _INFO_Y - 5 * _SS

    # Tick marks
    _TICK_OUTER = _GAUGE_R - 8  * _SS
    _TICK_INNER = _GAUGE_R - 18 * _SS
    _TICK_DATA.clear()
    for t in range(0, int(gauge_max) + 1, 5):
        ta = math.radians(_GAUGE_ARC_START + (t / gauge_max) * _GAUGE_ARC_SWEEP)
        tc, ts = math.cos(ta), math.sin(ta)
        _TICK_DATA.append((
            t,
            _GAUGE_ARC_START + (t / gauge_max) * _GAUGE_ARC_SWEEP,
            int(_GAUGE_CX + _TICK_OUTER * tc),
            int(_GAUGE_CY + _TICK_OUTER * ts),
            int(_GAUGE_CX + _TICK_INNER * tc),
            int(_GAUGE_CY + _TICK_INNER * ts),
            (t % 10 == 0) or t == gauge_max,
        ))

    # Pre-computed particle / drop x-positions
    _GOOD_PARTICLE_X[:] = [(i * 17 * _SS + 11 * _SS) % _W for i in range(14)]
    _CAUTION_DROP_BX[:] = [(i * 12 * _SS) % _W for i in range(20)]

    # Sine tables
    _BWAVE_FREQ   = 2 * math.pi / (22 * _SS)
    _MARINE_WAVE_FREQ = 2 * math.pi / (55 * _SS)
    _BWAVE_PERIOD  = 22 * _SS
    _MARINE_PERIOD = 55 * _SS
    _BWAVE_TABLE[:]       = [int(3 * _SS * math.sin(_BWAVE_FREQ   * x)) for x in range(_BWAVE_PERIOD)]
    _MARINE_WAVE_TABLE[:] = [int(3 * _SS * math.sin(_MARINE_WAVE_FREQ * x)) for x in range(_MARINE_PERIOD)]

    _GOOD_WAVE_PARAMS[:] = [
        (10,  (0, 122, 79), 70, 3),
        (30,  (0, 105, 68), 90, 5),
        (52,  (0,  90, 58), 80, 4),
        (74,  (0,  75, 48), 65, 3),
        (96,  (0,  59, 39), 80, 4),
        (118, (0,  43, 30), 95, 5),
    ]
    _GOOD_WAVE_FREQS[:]   = [2 * math.pi / (wl * _SS) for (_, _, wl, _) in _GOOD_WAVE_PARAMS]
    _GOOD_WAVE_PERIODS[:] = [wl * _SS for (_, _, wl, _) in _GOOD_WAVE_PARAMS]
    _GOOD_WAVE_TABLES[:]  = [
        [int(amp * _SS * math.sin(_GOOD_WAVE_FREQS[i] * x)) for x in range(_GOOD_WAVE_PERIODS[i])]
        for i, (_, _, wl, amp) in enumerate(_GOOD_WAVE_PARAMS)
    ]

    _SUNBURST_C[:] = [math.cos(math.radians(i * 30)) for i in range(12)]
    _SUNBURST_S[:] = [math.sin(math.radians(i * 30)) for i in range(12)]

    _EDGE_FADE_BASE[:] = [(3 * _SS - x) / (3.0 * _SS) for x in range(3 * _SS)]

    _NEEDLE_DAMPING = 0.50 ** (1.0 / (frame_rate * 0.20))
    _NEEDLE_SPRING  = 0.28  / (frame_rate * 0.20)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dim(color: tuple, factor: float) -> tuple:
    return tuple(max(0, min(255, int(c * factor))) for c in color)


def _gauge_ang(mph_val: float) -> float:
    gauge_max = _cfg["gauge_max"]
    return math.radians(_GAUGE_ARC_START + (mph_val / gauge_max) * _GAUGE_ARC_SWEEP)


def make_image() -> tuple:
    img = Image.new("RGB", (_W, _H), "black")
    return img, ImageDraw.Draw(img)


def draw_centered(d, y: int, text: str, fill, font) -> None:
    w = d.textlength(text, font=font)
    d.text((int((_W - w) / 2), y), text, fill=fill, font=font)


def _fit_text(d, text: str, font, max_w: int) -> str:
    if d.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 1 and d.textlength(text[:-1] + "…", font=font) > max_w:
        text = text[:-1]
    return text[:-1] + "…"


def _show(img: Image.Image) -> None:
    out = img.resize(_device.size, _RESAMPLE_DOWN)
    _show_counter[0] += 1
    if _show_counter[0] % 2 == 0:
        buf = io.BytesIO()
        out.save(buf, format="PNG", compress_level=1)
        with _frame_lock:
            _frame_store[0] = buf.getvalue()
    _device.display(out)


def get_latest_frame() -> bytes:
    with _frame_lock:
        return _frame_store[0]


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _draw_trend(d, cx: int, y: int, tr: str) -> None:
    h = 24 * _SS
    w = 10 * _SS
    if tr == "up":
        d.polygon([(cx, y), (cx - w, y + h), (cx + w, y + h)], fill=(255, 150, 0))
    elif tr == "down":
        d.polygon([(cx, y + h), (cx - w, y), (cx + w, y)], fill=(30, 165, 225))
    else:
        d.line([(cx - 11 * _SS, y + h // 2), (cx + 11 * _SS, y + h // 2)],
               fill=(85, 85, 85), width=3 * _SS)


def _draw_wind_streaks(d, cx: int, cy: int, r: int, gust: float, frame: int) -> None:
    speed = max(2, round(20 - gust * 0.4))
    inner = r - 24 * _SS
    n     = 7
    for i in range(n):
        phase = ((frame // speed + i * (100 // n)) % 100) / 100.0
        ang   = math.pi * (1 - phase)
        ca, sa = math.cos(ang), math.sin(ang)
        bright = int(70 + 170 * math.sin(phase * math.pi))
        perp   = ang + math.pi / 2
        cp, sp = math.cos(perp), math.sin(perp)
        hw     = 5 * _SS
        x0 = cx + inner * ca
        y0 = cy - inner * sa
        x1 = int(x0 + hw * cp);  y1 = int(y0 - hw * sp)
        x2 = int(x0 - hw * cp);  y2 = int(y0 + hw * sp)
        d.line([(x1, y1), (x2, y2)],
               fill=(max(0, bright - 20), max(0, bright - 10), bright),
               width=2 * _SS)


def _draw_info_bg(d, y_top: int, y_bot: int, status: str, frame: int) -> None:
    frame_rate = _cfg["frame_rate"]
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

        span = y_bot - y_top
        for i in range(14):
            px_pos  = _GOOD_PARTICLE_X[i]
            speed   = 1 + (i % 3)
            py_off  = span - (frame * speed + i * (span // 14)) % span
            py_pos  = y_top + int(py_off)
            bright  = int(78 + 65 * math.sin(frame * math.pi / (frame_rate * 2.2) + i * 0.8))
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
                frame * math.pi / (frame_rate * 1.5) + i * math.pi / 5))
            rc = (int(bright * 0.18), int(bright * 0.32), int(bright * 0.90))
            d.line([(x0, y0c), (x1, y1c)], fill=rc, width=2 * _SS)
            if y1 >= y_bot - 5 * _SS:
                sp  = min(1.0, (y1 - (y_bot - 5 * _SS)) / (5 * _SS))
                sw  = max(1, int(4 * _SS * sp))
                sy_s = min(y_bot - 1, y0 + 10 * _SS)
                sc_s = (int(rc[0] * 0.5), int(rc[1] * 0.5), int(rc[2] * 0.5))
                d.line([(x0 - sw, sy_s), (x0, sy_s - 2 * _SS)], fill=sc_s, width=_SS)
                d.line([(x0, sy_s - 2 * _SS), (x0 + sw, sy_s)], fill=sc_s, width=_SS)

        if (frame % (frame_rate * 9)) < 2:
            bolt_f = frame % (frame_rate * 9)
            lb  = int(160 * (1 - bolt_f / 2))
            lc  = (int(lb * 0.55), int(lb * 0.70), lb)
            lbx = _W // 3
            d.line([(lbx,               y_top +  8 * _SS), (lbx - 10 * _SS, y_top + 42 * _SS)], fill=lc, width=2 * _SS)
            d.line([(lbx - 10 * _SS,    y_top + 42 * _SS), (lbx +  8 * _SS, y_top + 80 * _SS)], fill=lc, width=2 * _SS)

    else:  # NO-GO — wind streaks
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
                    frame * math.pi / (frame_rate * 0.8) + j * math.pi / 4))
                sc = (bright, bright // 2, bright // 4)
                if ex <= _W:
                    d.line([(sx, sy), (ex, sy)], fill=sc, width=2 * _SS)
                else:
                    d.line([(sx, sy), (_W - 1, sy)], fill=sc, width=2 * _SS)
                    d.line([(0,  sy), (ex % _W, sy)], fill=sc, width=2 * _SS)


def _draw_marine_wave(d, frame: int, color: tuple, y_mid: int) -> None:
    offset = (frame * 2) % _MARINE_PERIOD
    pts = [(x, y_mid + _MARINE_WAVE_TABLE[(x + offset) % _MARINE_PERIOD])
           for x in range(_W + 1)]
    d.line(pts, fill=color, width=_SS)


def _draw_edge_accents(d, accent: tuple, frame: int) -> None:
    frame_rate = _cfg["frame_rate"]
    pulse = 0.2 + 0.8 * abs(math.sin(frame * math.pi / (frame_rate * 2.5)))
    scale = pulse * 0.35
    fades = [f * scale for f in _EDGE_FADE_BASE]
    for x in range(3 * _SS):
        col = tuple(int(c * fades[x]) for c in accent)
        d.line([(x, 18 * _SS), (x, _H - 1)], fill=col)
        d.line([(_W - 1 - x, 18 * _SS), (_W - 1 - x, _H - 1)], fill=col)


def _draw_alert_strip(d, alerts, frame, status_color, y0, marine_str=None, wind_str=None, age_minutes=None):
    frame_rate = _cfg["frame_rate"]
    strip_h = 18 * _SS
    y_mid   = y0 + strip_h // 2
    cx      = _W // 2
    d.rectangle([0, y0, _W - 1, y0 + strip_h], fill=(18, 18, 18))
    sep_b = int(45 + 55 * abs(math.sin(frame * math.pi / (frame_rate * 2.0))))
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
        idx  = (frame // (frame_rate * 5)) % len(slots)
        kind, text = slots[idx]
        r3, r9 = 3 * _SS, 9 * _SS
        strip_max = _W - 24 * _SS
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
        n_slots = len(slots)
        for si in range(n_slots):
            dx = _W - 4 * _SS - (n_slots - 1 - si) * 5 * _SS
            dc = (tuple(min(255, int(c * 0.65)) for c in status_color) if si == idx
                  else (50, 50, 50))
            pr = 2 * _SS
            d.ellipse((dx - pr, y_mid - pr, dx + pr, y_mid + pr), fill=dc)
        return

    idx       = (frame // (frame_rate * 4)) % len(alerts)
    name, sev = alerts[idx]
    color     = _ALERT_COLORS.get(sev, _YELLOW)

    pulse     = 0.38 + 0.62 * abs(math.sin(frame * math.pi / (frame_rate * 0.7)))
    dot_color = tuple(min(255, int(c * pulse)) for c in color)
    d.ellipse([7 * _SS, y_mid - 5 * _SS, 15 * _SS, y_mid + 5 * _SS], fill=dot_color)

    text = _fit_text(d, name, font_strip, _W - 26 * _SS)
    d.text((21 * _SS, y_mid), text, fill=color, font=font_strip, anchor="lm")

    if len(alerts) > 1:
        count_str = f"{idx + 1}/{len(alerts)}"
        cw = int(d.textlength(count_str, font=font_strip))
        d.text((_W - cw - 4 * _SS, y_mid), count_str,
               fill=(82, 82, 82), font=font_strip, anchor="lm")


def _draw_gauge(d, cx, cy, r, needle_gust, actual_gust, frame, stale=False, wind=None, history=None, raw_gust=None):
    gauge_max   = _cfg["gauge_max"]
    good_mph    = _cfg["good_mph"]
    caution_mph = _cfg["caution_mph"]
    frame_rate  = _cfg["frame_rate"]

    box     = (cx - r, cy - r, cx + r, cy + r)
    arc_end = _GAUGE_ARC_START + _GAUGE_ARC_SWEEP

    ob = r + 10 * _SS
    if stale:
        ob_col = (42, 42, 42)
    else:
        zone_c = (_GREEN if actual_gust < good_mph
                  else (_YELLOW if actual_gust <= caution_mph else _RED))
        ob_p   = 0.3 + 0.7 * abs(math.sin(frame * math.pi / (frame_rate * 2.5)))
        ob_col = tuple(max(16, int(c * ob_p * 0.25)) for c in zone_c)
    d.arc((cx - ob, cy - ob, cx + ob, cy + ob),
          _GAUGE_ARC_START - 4, arc_end + 4, fill=ob_col, width=2 * _SS)

    d.arc(box, _GAUGE_ARC_START - 2, arc_end + 2, fill=(20, 20, 20), width=22 * _SS)
    d.arc(box, _GAUGE_ARC_START - 2, arc_end + 2, fill=(36, 36, 36), width=14 * _SS)
    d.arc(box, _GAUGE_ARC_START - 2, arc_end + 2, fill=(24, 24, 24), width= 6 * _SS)

    if not stale and actual_gust < good_mph:
        glow_r = 55 * _SS
        glow_p = 0.10 + 0.06 * abs(math.sin(frame * math.pi / (frame_rate * 3.0)))
        d.ellipse((cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r),
                  fill=(0, int(185 * glow_p), int(80 * glow_p)))
        ray_r = math.radians((frame * 2.5) % 360)
        bc, bs = math.cos(ray_r), math.sin(ray_r)
        for i in range(12):
            ca_r = bc * _SUNBURST_C[i] - bs * _SUNBURST_S[i]
            sa_r = bs * _SUNBURST_C[i] + bc * _SUNBURST_S[i]
            b = int(12 + 8 * math.sin(frame * math.pi / (frame_rate * 2.5) + i * math.pi / 6))
            d.line([
                (int(cx + 20 * _SS * ca_r), int(cy + 20 * _SS * sa_r)),
                (int(cx + 48 * _SS * ca_r), int(cy + 48 * _SS * sa_r))
            ], fill=(0, b, b // 3), width=_SS)

    dim = 0.30 if stale else 1.0
    if stale:
        gd = yd = rd = 0.30
    elif needle_gust < good_mph:
        gd, yd, rd = dim, dim * 0.35, dim * 0.35
    elif needle_gust <= caution_mph:
        gd, yd, rd = dim * 0.35, dim, dim * 0.35
    else:
        gd, yd, rd = dim * 0.35, dim * 0.35, dim

    d.arc(box, _GAUGE_ARC_START, _GOOD_ARC_END,    fill=_dim(_GREEN,  gd * 0.26), width=26 * _SS)
    d.arc(box, _GOOD_ARC_END,    _CAUTION_ARC_END, fill=_dim(_YELLOW, yd * 0.26), width=26 * _SS)
    d.arc(box, _CAUTION_ARC_END, arc_end,          fill=_dim(_RED,    rd * 0.26), width=26 * _SS)

    d.arc(box, _GAUGE_ARC_START, _GOOD_ARC_END,    fill=_dim(_GREEN,  gd), width=16 * _SS)
    d.arc(box, _GOOD_ARC_END,    _CAUTION_ARC_END, fill=_dim(_YELLOW, yd), width=16 * _SS)
    d.arc(box, _CAUTION_ARC_END, arc_end,          fill=_dim(_RED,    rd), width=16 * _SS)

    ih   = r - 8 * _SS
    ibox = (cx - ih, cy - ih, cx + ih, cy + ih)
    d.arc(ibox, _GAUGE_ARC_START, _GOOD_ARC_END,    fill=_dim(_GREEN,  gd * 0.55), width=2 * _SS)
    d.arc(ibox, _GOOD_ARC_END,    _CAUTION_ARC_END, fill=_dim(_YELLOW, yd * 0.55), width=2 * _SS)
    d.arc(ibox, _CAUTION_ARC_END, arc_end,          fill=_dim(_RED,    rd * 0.55), width=2 * _SS)

    if not stale:
        zp = 0.28 + 0.26 * abs(math.sin(frame * math.pi / (frame_rate * 1.8)))
        if needle_gust < good_mph:
            za_s, za_e, zc = _GAUGE_ARC_START, _GOOD_ARC_END,    _GREEN
        elif needle_gust <= caution_mph:
            za_s, za_e, zc = _GOOD_ARC_END,    _CAUTION_ARC_END, _YELLOW
        else:
            za_s, za_e, zc = _CAUTION_ARC_END, arc_end,          _RED
        d.arc(box, za_s, za_e, fill=_dim(zc, zp), width=26 * _SS)

    if needle_gust > 0.1 and not stale:
        na_end = _GAUGE_ARC_START + (needle_gust / gauge_max) * _GAUGE_ARC_SWEEP
        if na_end > _GAUGE_ARC_START:
            d.arc(box, _GAUGE_ARC_START, min(na_end, _GOOD_ARC_END), fill=(0, 220, 100), width=4 * _SS)
        if na_end > _GOOD_ARC_END:
            d.arc(box, _GOOD_ARC_END, min(na_end, _CAUTION_ARC_END), fill=(255, 210, 0), width=4 * _SS)
        if na_end > _CAUTION_ARC_END:
            d.arc(box, _CAUTION_ARC_END, na_end, fill=(255, 80, 80), width=4 * _SS)

    _draw_wind_streaks(d, cx, cy, r, raw_gust if raw_gust is not None else actual_gust, frame)

    needle_deg = _GAUGE_ARC_START + (needle_gust / gauge_max) * _GAUGE_ARC_SWEEP
    for mph_val, tick_deg, x1, y1, x2, y2, is_major in _TICK_DATA:
        t_dim = dim * (0.7 if is_major else 0.45)
        if mph_val <= good_mph:
            tick_c = _dim(_GREEN, t_dim)
        elif mph_val <= caution_mph:
            tick_c = _dim(_YELLOW, t_dim)
        else:
            tick_c = _dim(_RED, t_dim)
        if not stale and needle_gust > 0:
            prox = max(0.0, 1.0 - abs(tick_deg - needle_deg) / 14.0)
            if prox > 0:
                tick_c = tuple(min(255, int(c + 110 * prox)) for c in tick_c)
        d.line([(x1, y1), (x2, y2)], fill=tick_c, width=2 * _SS if is_major else _SS)

    if history and len(history) >= 2 and not stale:
        peak = max(history)
        cur  = raw_gust if raw_gust is not None else actual_gust
        if peak > cur + 0.3:
            p_ang  = _gauge_ang(min(peak, gauge_max))
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

    if wind is not None and not stale:
        w_ang  = _gauge_ang(min(wind, gauge_max))
        w_ca, w_sa = math.cos(w_ang), math.sin(w_ang)
        wx = int(cx + (r - 10 * _SS) * w_ca)
        wy = int(cy + (r - 10 * _SS) * w_sa)
        ds = 4 * _SS
        d.ellipse((wx - 7 * _SS, wy - 7 * _SS, wx + 7 * _SS, wy + 7 * _SS), fill=(10, 10, 10))
        d.polygon([(wx, wy - ds), (wx + ds, wy), (wx, wy + ds), (wx - ds, wy)],
                  fill=(45, 45, 45), outline=(178, 178, 178))

    pct = min(max(needle_gust / gauge_max, 0), 1)
    ang = _gauge_ang(pct * gauge_max)
    if not stale and actual_gust > caution_mph:
        ang += math.radians(2.5 * math.sin(frame * math.pi / (frame_rate * 0.10)))
    if not stale and needle_gust > 0.1:
        glow_ang = _GAUGE_ARC_START + (needle_gust / gauge_max) * _GAUGE_ARC_SWEEP
        glow_c   = ((0, 36, 18) if needle_gust < good_mph
                    else ((42, 32, 0) if needle_gust <= caution_mph
                          else (42, 8, 8)))
        glow_w   = int((17 + 7 * abs(math.sin(frame * math.pi / (frame_rate * 0.7)))) * _SS)
        d.arc(box, glow_ang - 7, glow_ang + 7, fill=glow_c, width=glow_w)
    ca, sa = math.cos(ang), math.sin(ang)
    perp   = ang - math.pi / 2
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

    if not stale:
        tc = ((0, 200, 90) if needle_gust < good_mph
              else ((240, 190, 0) if needle_gust <= caution_mph
                    else (240, 60, 60)))
        tp = 0.65 + 0.35 * math.sin(frame * math.pi / (frame_rate * 0.7))
        tr = max(2 * _SS, int(6 * _SS * tp))
        d.ellipse((int(tip[0]) - tr, int(tip[1]) - tr,
                   int(tip[0]) + tr, int(tip[1]) + tr), fill=tc)

    h1, h2, h3 = 11 * _SS, 8 * _SS, 5 * _SS
    d.ellipse((cx - h1, cy - h1, cx + h1, cy + h1), fill=(28, 28, 28), outline=(108, 108, 108), width=_SS)
    d.ellipse((cx - h2, cy - h2, cx + h2, cy + h2), fill=(18, 18, 18), outline=(68, 68, 68), width=_SS)
    d.ellipse((cx - 5 * _SS, cy - 7 * _SS, cx - 1 * _SS, cy - 3 * _SS), fill=(95, 95, 95))
    if stale:
        hub_c = (90, 90, 90)
    elif actual_gust > caution_mph:
        hub_c = (235, 60, 60)
    elif actual_gust > good_mph:
        hub_c = (225, 185, 0)
    else:
        hub_c = (0, 200, 90)
    d.ellipse((cx - h3, cy - h3, cx + h3, cy + h3), fill=hub_c)


# ---------------------------------------------------------------------------
# Main render entry point
# ---------------------------------------------------------------------------

def render_display(state: dict, frame: int, needle_gust: float, composite: float) -> None:
    """Render one animation frame and push it to the display."""
    wind         = state["wind"]
    gust         = state["gust"]
    wdir         = state["wdir"]
    age          = state["age"]
    wtmp         = state["wtmp"]
    wvht         = state["wvht"]
    atmp         = state["atmp"]
    dpd          = state["dpd"]
    dewp         = state["dewp"]
    pres         = state["pres"]
    pres_history = state.get("pres_history") or []
    alerts       = state["alerts"]
    history      = state.get("gust_history", [])

    cfg        = _cfg
    good_mph   = cfg["good_mph"]
    caution_mph = cfg["caution_mph"]
    stale_minutes = cfg["stale_minutes"]
    frame_rate = cfg["frame_rate"]
    fog_spread_f = cfg["fog_spread_f"]
    pres_fall_caution = cfg["pres_fall_caution"]
    atmp_chilly_f = cfg["atmp_chilly_f"]

    feels_hi = None
    feels_wc = None
    if atmp is not None and dewp is not None and atmp >= 80:
        rh = logic.relative_humidity(atmp, dewp)
        if rh >= 40:
            hi = logic.heat_index_f(atmp, rh)
            if hi >= atmp + 3:
                feels_hi = hi
    if atmp is not None and wind is not None and atmp <= 50 and wind >= 3:
        wc = logic.wind_chill_f(atmp, wind)
        if wc <= atmp - 3:
            feels_wc = wc
    extreme_heat = feels_hi is not None and feels_hi >= 103

    cond_wind, cond_wave, cond_temp = logic.condition_statuses(state, cfg)
    msg    = logic.status_label(composite, cfg)
    accent = _STATUS_CONFIG[msg][0]
    tr     = logic.trend(history)

    img, d = make_image()
    r  = _GAUGE_R
    cx = _GAUGE_CX
    cy = _GAUGE_CY

    _draw_info_bg(d, _INFO_Y, _H - 1, msg, frame)

    stale = age is not None and age >= stale_minutes

    if not stale:
        halo_p   = 0.3 + 0.7 * abs(math.sin(frame * math.pi / (frame_rate * 3.0)))
        halo_end = _GAUGE_ARC_START + _GAUGE_ARC_SWEEP
        for h_off, h_fac in ((r + 17 * _SS, 0.17), (r + 24 * _SS, 0.10)):
            hc = tuple(max(0, int(c * h_fac * halo_p)) for c in accent)
            if any(v > 0 for v in hc):
                d.arc((cx - h_off, cy - h_off, cx + h_off, cy + h_off),
                      _GAUGE_ARC_START - 8, halo_end + 8, fill=hc, width=2 * _SS)

    _draw_gauge(d, cx, cy, r, needle_gust, composite, frame,
                stale=stale, wind=wind, history=history, raw_gust=gust)

    if age is not None:
        freshness = max(0.0, 1.0 - age / stale_minutes)
        bar_hw = int(42 * _SS * freshness)
        if bar_hw > 0:
            bright = max(28, int(85 * freshness))
            by = _INFO_Y - 7 * _SS
            d.line([(cx - bar_hw, by), (cx + bar_hw, by)], fill=(bright, bright, bright), width=_SS)

    sep_p = 0.3 + 0.7 * abs(math.sin(frame * math.pi / (frame_rate * 3)))
    for sx0, bfac in _SEP_SEGS:
        sc_ = tuple(int(c * sep_p * bfac) for c in accent)
        if any(v > 0 for v in sc_):
            d.line([(sx0, _SEP_Y1), (_W - sx0, _SEP_Y1)], fill=sc_, width=_SS)
            d.line([(sx0, _SEP_Y2), (_W - sx0, _SEP_Y2)], fill=tuple(v // 2 for v in sc_), width=_SS)

    _, badge_bg = _STATUS_CONFIG[msg]
    band_bg = badge_bg if stale else tuple(min(255, int(c * 0.65)) for c in accent)
    d.rectangle([0, _BAND_Y0, _W - 1, _BAND_Y1], fill=band_bg)
    edge_p  = 0.5 + 0.5 * abs(math.sin(frame * math.pi / (frame_rate * 2.0)))
    edge_c  = tuple(min(255, int(c * edge_p)) for c in accent) if not stale else (55, 55, 55)
    d.line([(0, _BAND_Y0), (_W - 1, _BAND_Y0)], fill=edge_c, width=2 * _SS)
    d.line([(0, _BAND_Y1), (_W - 1, _BAND_Y1)], fill=tuple(c // 2 for c in edge_c), width=_SS)
    vib_x  = [-1 * _SS, 0, 1 * _SS, 0][frame % 4] if msg == "NO-GO" and not stale else 0
    band_cy = _BAND_Y0 + _BAND_H // 2
    if not stale:
        d.text((cx + vib_x + _SS, band_cy + _SS), msg, fill=(0, 0, 0), font=font_unit, anchor="mm")
    d.text((cx + vib_x, band_cy), msg,
           fill=(60, 60, 60) if stale else (255, 255, 255),
           font=font_unit, anchor="mm")

    # CACHED badge — amber pill on the left of the status band when showing offline data
    if state.get("cached"):
        badge_text = "CACHED"
        bx = 6 * _SS
        d.text((bx + _SS, band_cy + _SS), badge_text, fill=(0, 0, 0),       font=font_label, anchor="lm")
        d.text((bx,       band_cy),       badge_text, fill=(200, 140, 0),   font=font_label, anchor="lm")

    _DOT_C = {"GO": _GREEN, "CAUTION": _YELLOW, "NO-GO": _RED}
    fog_risk     = dewp is not None and atmp is not None and (atmp - dewp) < fog_spread_f
    pres_falling = False
    if pres is not None and len(pres_history) >= 3:
        oldest_p = next((p for p in reversed(pres_history) if p is not None), None)
        pres_falling = oldest_p is not None and (oldest_p - pres) >= pres_fall_caution
    cold_air     = atmp is not None and atmp < atmp_chilly_f
    cond_weather = "CAUTION" if (fog_risk or pres_falling or cold_air or extreme_heat) else "GO"
    weather_known = dewp is not None or pres is not None or atmp is not None
    dot_r = 3 * _SS
    dot_y = band_cy
    for j, (cond, has_data) in enumerate((
            (cond_wind,    True),
            (cond_wave,    wvht is not None),
            (cond_temp,    wtmp is not None),
            (cond_weather, weather_known),
    )):
        dx = _W - (9 + j * 8) * _SS
        dc = _DOT_C[cond] if (has_data and not stale) else (50, 50, 50)
        d.ellipse((dx - dot_r, dot_y - dot_r, dx + dot_r, dot_y + dot_r), fill=dc)

    row_y = _ROW_Y
    if not stale and (wtmp is not None or wvht is not None
                      or fog_risk or pres_falling or cold_air or extreme_heat):
        d.rectangle([0, row_y - 9 * _SS, _W - 1, row_y + 9 * _SS],
                    fill=tuple(max(0, c // 4) for c in accent))
        sh = _SS
        if wtmp is not None:
            wt_str  = f"{wtmp:.0f}° water"
            wt_fill = _DOT_C[cond_temp]
            d.text((8 * _SS + sh, row_y + sh), wt_str, fill=(0, 0, 0), font=font_label, anchor="lm")
            d.text((8 * _SS,      row_y),       wt_str, fill=wt_fill,   font=font_label, anchor="lm")
        if fog_risk or pres_falling or cold_air or extreme_heat:
            wx_parts = []
            if fog_risk:     wx_parts.append("~Fog")
            if pres_falling: wx_parts.append("↓P")
            if cold_air:     wx_parts.append(f"Chilly {atmp:.0f}°")
            if extreme_heat: wx_parts.append(f"HI {feels_hi:.0f}°!")
            wx_str = "  ".join(wx_parts)
            d.text((cx + sh, row_y + sh), wx_str, fill=(0, 0, 0), font=font_label, anchor="mm")
            d.text((cx,      row_y),       wx_str, fill=_YELLOW,    font=font_label, anchor="mm")
        if wvht is not None:
            wv_str  = (f"{wvht:.1f}ft/{dpd:.0f}s" if dpd is not None else f"{wvht:.1f}ft waves")
            wv_fill = _DOT_C[cond_wave]
            d.text((_W - 8 * _SS + sh, row_y + sh), wv_str, fill=(0, 0, 0), font=font_label, anchor="rm")
            d.text((_W - 8 * _SS,      row_y),       wv_str, fill=wv_fill,   font=font_label, anchor="rm")

    if stale:
        gust_fill = (50, 50, 50);  mph_fill = (40, 40, 40)
    elif needle_gust > caution_mph:
        gust_fill = (255, 110, 90); mph_fill = (210, 85, 70)
    elif needle_gust >= good_mph:
        gust_fill = (255, 215, 50); mph_fill = (205, 170, 35)
    else:
        gust_fill = (240, 240, 240); mph_fill = (180, 180, 180)
    gust_int = min(55, max(0, round(gust)))
    num_str  = str(gust_int)
    num_w    = _GUST_WIDTHS[gust_int]
    unit_w   = _MPH_UNIT_W
    grp_x    = (_W - num_w - 8 * _SS - unit_w) // 2
    num_top  = row_y + 20 * _SS
    num_y    = num_top + (_H - num_top) // 2
    d.text((grp_x + 2 * _SS,            num_y + 2 * _SS),             num_str, fill=(0, 0, 0),  font=font_gust, anchor="lm")
    d.text((grp_x,                       num_y),                       num_str, fill=gust_fill,  font=font_gust, anchor="lm")
    d.text((grp_x + num_w + 10 * _SS,   num_y + 16 * _SS + 2 * _SS), "mph",   fill=(0, 0, 0),  font=font_unit, anchor="lm")
    d.text((grp_x + num_w +  8 * _SS,   num_y + 16 * _SS),            "mph",   fill=mph_fill,   font=font_unit, anchor="lm")

    if tr is not None:
        _draw_trend(d, 18 * _SS, num_y - 12 * _SS, tr)

    dir_tag  = f"  {wdir}" if wdir else ""
    if gust is not None and wind is not None and abs(gust - wind) > 1.0:
        wind_str = f"Gust {gust:.0f}  Wind {wind:.0f} mph{dir_tag}"
    else:
        wind_str = f"Wind {wind:.0f} mph{dir_tag}"
    if tr is not None:
        arrow = "↑" if tr == "up" else ("↓" if tr == "down" else "→")
        wind_str += f" {arrow}"

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
            pfall   = oldest_p - pres
            p_arrow = " ↓" if pfall >= 0.5 else (" ↑" if pfall <= -0.5 else "")
        else:
            p_arrow = ""
        marine_parts.append(f"{pres:.0f}hPa{p_arrow}")
    if dewp is not None and atmp is not None and (atmp - dewp) < fog_spread_f:
        marine_parts.append("~Fog")
    marine_str = "  ".join(marine_parts) if marine_parts else None

    _draw_edge_accents(d, accent, frame)

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

    _draw_alert_strip(d, alerts, frame, accent, y0=0,
                      marine_str=marine_str, wind_str=wind_str, age_minutes=age)

    _show(img)
