from luma.core.interface.serial import spi
from luma.lcd.device import ili9341
from PIL import Image, ImageDraw, ImageFont
from urllib.request import urlopen, Request
import logging
import math
import os
import signal
import sys
import textwrap
import time

from ndbc import ms_to_mph, wind_direction, parse_ndbc, obs_age_minutes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

URL           = "https://www.ndbc.noaa.gov/data/realtime2/41038.txt"
POLL_INTERVAL = 300
GAUGE_MAX     = 25    # mph, full-scale
GOOD_MPH      = 12
CAUTION_MPH   = 18
STALE_MINUTES = 90

# Arc boundary angles (PIL degrees, precomputed from thresholds)
GOOD_ARC_END    = round(180 + (GOOD_MPH    / GAUGE_MAX) * 180)
CAUTION_ARC_END = round(180 + (CAUTION_MPH / GAUGE_MAX) * 180)

# Colors
_GREEN  = (0, 160, 70)
_YELLOW = (210, 165, 0)
_RED    = (210, 40, 40)

_STATUS_CONFIG = {
    "GOOD":      (_GREEN,  (0, 55, 22)),    # (accent, badge_bg)
    "CAUTION":   (_YELLOW, (65, 52, 0)),
    "TOO WINDY": (_RED,    (70, 12, 12)),
}

# Font loading — find the first usable TrueType font once
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
_FONT_PATH = next((p for p in _FONT_CANDIDATES if os.path.exists(p)), None)

def _load_font(size):
    if _FONT_PATH:
        return ImageFont.truetype(_FONT_PATH, size)
    return ImageFont.load_default()

font_title  = _load_font(14)
font_status = _load_font(22)
font_data   = _load_font(13)
font_label  = _load_font(11)

try:
    _serial = spi(port=0, device=0, gpio_DC=24, gpio_RST=25)
    device = ili9341(_serial, width=320, height=240, rotate=1)
except Exception as e:
    logging.critical("Display init failed: %s", e)
    sys.exit(1)


def make_image():
    img = Image.new("RGB", device.size, "black")
    return img, ImageDraw.Draw(img)


def draw_centered(d, y, text, fill, font):
    w = d.textlength(text, font=font)
    d.text((int((device.width - w) / 2), y), text, fill=fill, font=font)


def _draw_status_badge(d, y, msg):
    """Rounded-rectangle badge with colored border and dim fill."""
    accent, bg = _STATUS_CONFIG[msg]
    text_w = d.textlength(msg, font=font_status)
    bw = max(int(text_w) + 32, 150)
    bx = int((device.width - bw) / 2)
    bh = 30
    d.rounded_rectangle([bx, y, bx + bw, y + bh], radius=6, fill=bg, outline=accent, width=2)
    # anchor="mm" centers the text on the badge mid-point exactly
    d.text((device.width // 2, y + bh // 2), msg, fill="white", font=font_status, anchor="mm")


def _draw_gauge(d, cx, cy, r, gust):
    """Draw the backing arc, colored zones, tick marks, scale labels, and needle."""
    box = (cx - r, cy - r, cx + r, cy + r)

    # Dark channel arc (slightly wider → creates a thin dark border around the zones)
    d.arc(box, 178, 362, fill=(30, 30, 30), width=22)

    # Colored zone arcs
    d.arc(box, 180,           GOOD_ARC_END,    fill=_GREEN,  width=16)
    d.arc(box, GOOD_ARC_END,  CAUTION_ARC_END, fill=_YELLOW, width=16)
    d.arc(box, CAUTION_ARC_END, 360,           fill=_RED,    width=16)

    # Tick marks at every 5 mph, drawn just inside the arc inner edge
    tick_outer = r - 8   # inner edge of the colored arc
    tick_inner = r - 18  # 10 px inward
    for mph_val in range(0, GAUGE_MAX + 1, 5):
        ang = math.pi * (1 - mph_val / GAUGE_MAX)
        ca, sa = math.cos(ang), math.sin(ang)
        x1, y1 = cx + tick_outer * ca, cy - tick_outer * sa
        x2, y2 = cx + tick_inner * ca, cy - tick_inner * sa
        is_major = (mph_val % 10 == 0) or mph_val == GAUGE_MAX
        tick_color = (200, 200, 200) if is_major else (110, 110, 110)
        d.line([(int(x1), int(y1)), (int(x2), int(y2))],
               fill=tick_color, width=2 if is_major else 1)

    # Scale labels at zone boundaries — positions computed from gauge geometry
    label_r = r + 16
    for mph_val, label in [(0, "0"), (GOOD_MPH, str(GOOD_MPH)),
                            (CAUTION_MPH, str(CAUTION_MPH)), (GAUGE_MAX, str(GAUGE_MAX))]:
        ang = math.pi * (1 - mph_val / GAUGE_MAX)
        lx = int(cx + label_r * math.cos(ang))
        ly = int(cy - label_r * math.sin(ang))
        d.text((lx, ly), label, fill=(150, 150, 150), font=font_label, anchor="mm")

    # Kite-shaped needle: tip at arc, widest ~8 px from pivot, short tail behind
    pct  = min(max(gust / GAUGE_MAX, 0), 1)
    ang  = math.pi * (1 - pct)
    perp = ang + math.pi / 2
    ca, sa = math.cos(ang), math.sin(ang)
    cp, sp = math.cos(perp), math.sin(perp)

    tip  = (cx + r  * ca,  cy - r  * sa)   # tip at arc face
    wide = (cx + 8  * ca,  cy - 8  * sa)   # widest point, 8 px from pivot
    tail = (cx - 14 * ca,  cy + 14 * sa)   # short tail behind pivot
    hw   = 5.5                              # half-width at the wide point
    left  = (wide[0] + hw * cp,  wide[1] - hw * sp)
    right = (wide[0] - hw * cp,  wide[1] + hw * sp)

    d.polygon(
        [(int(tip[0]), int(tip[1])),
         (int(left[0]), int(left[1])),
         (int(tail[0]), int(tail[1])),
         (int(right[0]), int(right[1]))],
        fill=(240, 240, 240),
    )

    # Pivot hub: dark ring with subtle outline, bright centre dot
    d.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill=(28, 28, 28), outline=(90, 90, 90), width=1)
    d.ellipse((cx -  5, cy -  5, cx +  5, cy +  5), fill=(210, 210, 210))


def render_display(wind, gust, wdir, age):
    msg = ("GOOD" if gust < GOOD_MPH
           else "CAUTION" if gust <= CAUTION_MPH
           else "TOO WINDY")

    img, d = make_image()
    cx, cy, r = 160, 205, 112

    # ── header ──────────────────────────────────────────────────────────
    draw_centered(d, 5, "PONTOON WIND", (160, 160, 160), font_title)
    _draw_status_badge(d, 23, msg)
    draw_centered(d, 58, f"Gust  {gust:.1f} mph",       "white", font_data)
    draw_centered(d, 75, f"Wind  {wind:.1f} mph   {wdir}", "white", font_data)

    # Data-age indicator (top-right, dims to gray, turns yellow when stale)
    if age is not None:
        age_color = _YELLOW if age >= STALE_MINUTES else (100, 100, 100)
        age_str = f"{age}m"
        w = d.textlength(age_str, font=font_label)
        d.text((int(device.width - w - 5), 5), age_str, fill=age_color, font=font_label)

    # ── gauge ────────────────────────────────────────────────────────────
    _draw_gauge(d, cx, cy, r, gust)

    device.display(img)


def handle_exit(sig, _frame):
    logging.info("Shutting down (signal %d)", sig)
    try:
        img, d = make_image()
        draw_centered(d, 105, "Offline", (80, 80, 80), font_status)
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


def main():
    img, d = make_image()
    draw_centered(d, 98,  "PONTOON WIND", (100, 100, 100), font_title)
    draw_centered(d, 116, "Connecting…",  (60,  60,  60),  font_status)
    device.display(img)

    while True:
        cycle_start = time.monotonic()

        try:
            row = parse_ndbc(fetch_ndbc())

            if row.get("WSPD", "MM") == "MM":
                raise ValueError("WSPD reading unavailable")
            wind = ms_to_mph(float(row["WSPD"]))
            gust_raw = row.get("GST", "MM")
            gust = ms_to_mph(float(gust_raw)) if gust_raw != "MM" else wind
            wdir = wind_direction(row.get("WDIR", "MM"))
            age  = obs_age_minutes(row)

            render_display(wind, gust, wdir, age)
            logging.info("wind=%.1f mph gust=%.1f mph dir=%s age=%sm", wind, gust, wdir, age)

        except Exception as e:
            logging.error("Update failed: %s", e)
            try:
                img, d = make_image()
                draw_centered(d, 20, "ERROR", _RED, font_status)
                d.text((12, 65), textwrap.shorten(str(e), width=40, placeholder="…"),
                       fill=(180, 180, 180), font=font_data)
                device.display(img)
            except Exception:
                logging.exception("Error screen render failed")

        elapsed = time.monotonic() - cycle_start
        time.sleep(max(0.0, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
