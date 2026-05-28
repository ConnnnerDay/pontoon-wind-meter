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
POLL_INTERVAL = 300   # seconds between data refreshes
GAUGE_MAX     = 25    # mph, full-scale value for the needle
GOOD_MPH      = 12
CAUTION_MPH   = 18
STALE_MINUTES = 90    # age threshold (minutes) at which data age turns yellow

# Arc boundaries derived once from the threshold constants (PIL degrees, clockwise from right)
GOOD_ARC_END    = round(180 + (GOOD_MPH    / GAUGE_MAX) * 180)
CAUTION_ARC_END = round(180 + (CAUTION_MPH / GAUGE_MAX) * 180)

# Find a usable TrueType font once; fall back to PIL's built-in bitmap font
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",  # Arch / some Pis
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


def draw_needle(draw, cx, cy, r, val):
    pct = min(max(val / GAUGE_MAX, 0), 1)
    ang = math.pi * (1 - pct)
    x = cx + r * math.cos(ang)
    y = cy - r * math.sin(ang)
    draw.line((cx, cy, x, y), fill="white", width=4)
    draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill="white")


def render_display(wind, gust, wdir, age):
    if gust < GOOD_MPH:
        msg, msg_color = "GOOD", "green"
    elif gust <= CAUTION_MPH:
        msg, msg_color = "CAUTION", "yellow"
    else:
        msg, msg_color = "TOO WINDY", "red"

    img, d = make_image()
    cx, cy, r = 160, 205, 112
    box = (cx - r, cy - r, cx + r, cy + r)

    draw_centered(d, 5,  "PONTOON WIND",               "white",   font_title)
    draw_centered(d, 24, msg,                           msg_color, font_status)
    draw_centered(d, 54, f"Gust  {gust:.1f} mph",      "white",   font_data)
    draw_centered(d, 72, f"Wind  {wind:.1f} mph  {wdir}", "white", font_data)

    if age is not None:
        age_color = "yellow" if age >= STALE_MINUTES else (140, 140, 140)
        age_str = f"{age}m"
        w = d.textlength(age_str, font=font_label)
        d.text((int(device.width - w - 5), 5), age_str, fill=age_color, font=font_label)

    d.arc(box, 180,           GOOD_ARC_END,    fill="green",  width=16)
    d.arc(box, GOOD_ARC_END,  CAUTION_ARC_END, fill="yellow", width=16)
    d.arc(box, CAUTION_ARC_END, 360,           fill="red",    width=16)

    draw_needle(d, cx, cy, r, gust)

    d.text((22,  200), "0",            fill="white", font=font_label)
    d.text((147,  90), str(GOOD_MPH),  fill="white", font=font_label)
    d.text((268, 200), str(GAUGE_MAX), fill="white", font=font_label)

    device.display(img)


def handle_exit(sig, _frame):
    logging.info("Shutting down (signal %d)", sig)
    try:
        img, d = make_image()
        draw_centered(d, 105, "Offline", (100, 100, 100), font_status)
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
            draw_centered(d, 20, "ERROR", "red", font_status)
            d.text((12, 65), textwrap.shorten(str(e), width=40, placeholder="…"), fill="white", font=font_data)
            device.display(img)
        except Exception:
            logging.exception("Error screen render failed")

    elapsed = time.monotonic() - cycle_start
    time.sleep(max(0.0, POLL_INTERVAL - elapsed))
