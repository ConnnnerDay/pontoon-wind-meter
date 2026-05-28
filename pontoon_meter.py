from luma.core.interface.serial import spi
from luma.lcd.device import ili9341
from PIL import Image, ImageDraw
from urllib.request import urlopen
import logging
import math
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

URL = "https://www.ndbc.noaa.gov/data/realtime2/41038.txt"

GAUGE_MAX = 25      # mph
GOOD_MPH = 12
CAUTION_MPH = 18

COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

try:
    _serial = spi(port=0, device=0, gpio_DC=24, gpio_RST=25)
    device = ili9341(_serial, width=320, height=240, rotate=1)
except Exception as e:
    logging.critical("Display init failed: %s", e)
    sys.exit(1)


def ms_to_mph(ms):
    return ms * 2.23694


def make_image():
    img = Image.new("RGB", device.size, "black")
    return img, ImageDraw.Draw(img)


def draw_needle(draw, cx, cy, r, val):
    pct = min(max(val / GAUGE_MAX, 0), 1)
    ang = math.pi * (1 - pct)
    x = cx + r * math.cos(ang)
    y = cy - r * math.sin(ang)
    draw.line((cx, cy, x, y), fill="white", width=4)
    draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill="white")


def wind_direction(deg_str):
    """Convert NDBC WDIR string (degrees or 'MM'/'999') to compass label."""
    if deg_str in ("MM", "999"):
        return "---"
    return COMPASS[round(int(deg_str) / 45) % 8]


def fetch_ndbc():
    """Fetch NDBC text file with up to 3 attempts on transient errors."""
    last_exc = None
    for attempt in range(3):
        try:
            with urlopen(URL, timeout=10) as resp:
                return resp.read().decode()
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                logging.warning("Fetch attempt %d failed: %s", attempt + 1, exc)
                time.sleep(5)
    raise last_exc


def parse_ndbc(txt):
    """Return the most recent observation as a column-name → value dict."""
    lines = txt.splitlines()
    header_line = next((l for l in lines if l.startswith("#YY")), None)
    if header_line is None:
        raise ValueError("NDBC header row not found")
    cols = header_line.lstrip("#").split()
    data_line = next((l for l in lines if l.strip() and not l.startswith("#")), None)
    if data_line is None:
        raise ValueError("No NDBC data rows found")
    return dict(zip(cols, data_line.split()))


while True:
    try:
        row = parse_ndbc(fetch_ndbc())

        if row.get("WSPD", "MM") == "MM":
            raise ValueError("WSPD reading unavailable")
        wind = ms_to_mph(float(row["WSPD"]))
        gust_raw = row.get("GST", "MM")
        gust = ms_to_mph(float(gust_raw)) if gust_raw != "MM" else wind
        wdir = wind_direction(row.get("WDIR", "MM"))

        if gust < GOOD_MPH:
            msg, msg_color = "GOOD", "green"
        elif gust <= CAUTION_MPH:
            msg, msg_color = "CAUTION", "yellow"
        else:
            msg, msg_color = "TOO WINDY", "red"

        img, d = make_image()
        cx, cy, r = 160, 205, 112
        box = (cx - r, cy - r, cx + r, cy + r)

        d.text((78, 8), "PONTOON WIND", fill="white")
        d.text((105, 30), msg, fill=msg_color)
        d.text((80, 52), f"Gust {gust:.1f} mph", fill="white")
        d.text((85, 70), f"Wind {wind:.1f} mph  {wdir}", fill="white")

        good_arc_end = round(180 + (GOOD_MPH / GAUGE_MAX) * 180)
        caution_arc_end = round(180 + (CAUTION_MPH / GAUGE_MAX) * 180)
        d.arc(box, 180, good_arc_end, fill="green", width=16)
        d.arc(box, good_arc_end, caution_arc_end, fill="yellow", width=16)
        d.arc(box, caution_arc_end, 360, fill="red", width=16)

        draw_needle(d, cx, cy, r, gust)

        d.text((28, 200), "0", fill="white")
        d.text((147, 92), str(GOOD_MPH), fill="white")
        d.text((275, 200), str(GAUGE_MAX), fill="white")

        device.display(img)
        logging.info("wind=%.1f mph gust=%.1f mph dir=%s status=%s", wind, gust, wdir, msg)

    except Exception as e:
        logging.error("Update failed: %s", e)
        try:
            img, d = make_image()
            d.text((20, 20), "ERROR", fill="red")
            d.text((20, 60), str(e)[:30], fill="white")
            device.display(img)
        except Exception:
            logging.exception("Error screen render failed")

    time.sleep(300)
