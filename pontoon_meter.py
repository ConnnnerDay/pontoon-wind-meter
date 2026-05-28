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

# Arc boundary angles (PIL degrees, precomputed from thresholds)
GOOD_ARC_END    = round(180 + (GOOD_MPH    / GAUGE_MAX) * 180)
CAUTION_ARC_END = round(180 + (CAUTION_MPH / GAUGE_MAX) * 180)

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
    device = ili9341(_serial, width=240, height=320, rotate=0)
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


def _draw_status_badge(d, y, msg, frame):
    """Rounded-rectangle badge with a sinusoidally pulsing border."""
    accent_base, bg = _STATUS_CONFIG[msg]
    amp = 50 if msg == "TOO WINDY" else 20
    pulse = int(amp * math.sin(frame * math.pi / (FRAME_RATE * 1.2)))
    accent = tuple(min(255, max(0, c + pulse)) for c in accent_base)
    text_w = d.textlength(msg, font=font_status)
    bw = max(int(text_w) + 32, 150)
    bx = int((device.width - bw) / 2)
    bh = 30
    d.rounded_rectangle([bx, y, bx + bw, y + bh], radius=6, fill=bg, outline=accent, width=2)
    d.text((device.width // 2, y + bh // 2), msg, fill="white", font=font_status, anchor="mm")


def _trend(history):
    """Return 'up', 'down', or 'steady' from a most-recent-first gust list, or None."""
    if len(history) < 4:
        return None
    delta = history[0] - history[3]   # newest vs 15 min ago
    if delta >  1.5: return "up"
    if delta < -1.5: return "down"
    return "steady"


def _draw_trend(d, cx, y, trend):
    """9 px tall directional indicator centred at (cx, y)."""
    if trend == "up":
        # Upward triangle — wind increasing (warm amber warning)
        d.polygon([(cx, y), (cx - 4, y + 8), (cx + 4, y + 8)], fill=(240, 130, 0))
    elif trend == "down":
        # Downward triangle — wind easing (cool blue)
        d.polygon([(cx, y + 8), (cx - 4, y), (cx + 4, y)], fill=(0, 145, 200))
    else:
        # Steady — horizontal dash
        d.line([(cx - 5, y + 4), (cx + 5, y + 4)], fill=(85, 85, 85), width=2)


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

    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(48, 48, 48), width=1)
    # North tick — tiny mark at the top of the circle
    d.line([(cx, cy - r + 1), (cx, cy - r + 4)], fill=(62, 62, 62), width=1)

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
               (int(rr_x), int(rr_y))], fill=(185, 185, 185))
    d.line([(int(base_x), int(base_y)), (int(tail_x), int(tail_y))],
           fill=(105, 105, 105), width=1)
    # Abbreviation in the lower half of the circle, dim so arrow reads over it
    d.text((cx, cy + 3), wdir_str, fill=(58, 58, 58), font=font_label, anchor="mt")


def _draw_wind_streaks(d, cx, cy, r, gust, frame):
    """Animated short dashes flowing along the inner gauge face, speed ∝ wind."""
    speed  = max(2, round(20 - gust * 0.4))   # frames per full sweep
    inner  = r - 24                             # radius just inside the tick zone
    n      = 5
    for i in range(n):
        phase = ((frame // speed + i * (100 // n)) % 100) / 100.0
        ang   = math.pi * (1 - phase)           # sweeps left→right with gauge
        ca, sa = math.cos(ang), math.sin(ang)
        bright = int(35 + 80 * math.sin(phase * math.pi))  # bell-curve fade
        perp = ang + math.pi / 2
        cp, sp = math.cos(perp), math.sin(perp)
        hw = 4
        x0 = cx + inner * ca
        y0 = cy - inner * sa
        x1 = int(x0 + hw * cp);  y1 = int(y0 - hw * sp)
        x2 = int(x0 - hw * cp);  y2 = int(y0 + hw * sp)
        d.line([(x1, y1), (x2, y2)], fill=(bright, bright, bright), width=1)


def _draw_marine_wave(d, frame, color):
    """Scrolling sine wave shown in the bottom strip when no alerts are active."""
    y_mid      = device.height - 9
    amplitude  = 3
    wavelength = 55
    offset     = (frame * 2) % wavelength
    prev = None
    for x in range(device.width + 1):
        y = y_mid + int(amplitude * math.sin(2 * math.pi * (x + offset) / wavelength))
        if prev is not None:
            d.line([prev, (x, y)], fill=color, width=1)
        prev = (x, y)


def _draw_marine_data(d, wtmp, wvht, atmp):
    """Water temp (left), air temp (center), wave height (right) at y=88."""
    if wtmp is not None:
        d.text((12, 88), f"Water {wtmp:.0f}°F", fill=(90, 90, 90), font=font_label)
    if atmp is not None:
        d.text((device.width // 2, 88), f"Air {atmp:.0f}°F",
               fill=(80, 80, 80), font=font_label, anchor="mt")
    if wvht is not None:
        d.text((device.width - 12, 88), f"Waves {wvht:.1f}ft",
               fill=(90, 90, 90), font=font_label, anchor="ra")


def _draw_alert_strip(d, alerts, frame, status_color):
    """Bottom strip: cycles through active NOAA alerts, or shows a marine wave."""
    y0 = device.height - 18
    d.rectangle([0, y0, device.width - 1, device.height - 1], fill=(12, 12, 12))
    d.line([0, y0, device.width, y0], fill=(40, 40, 40))

    if not alerts:
        wave_color = tuple(max(0, c // 4) for c in status_color)
        _draw_marine_wave(d, frame, wave_color)
        # Overlay local time dimly on the wave
        time_str = time.strftime("%H:%M")
        d.text((device.width // 2, device.height - 9), time_str,
               fill=(62, 62, 62), font=font_label, anchor="mm")
        return

    idx = (frame // (FRAME_RATE * 4)) % len(alerts)   # new alert every 4 s
    name, severity = alerts[idx]
    color = _ALERT_COLORS.get(severity, _YELLOW)

    # Pulsing warning dot
    pulse = 0.55 + 0.45 * math.sin(frame * math.pi / (FRAME_RATE * 0.7))
    dot_color = tuple(min(255, int(c * pulse)) for c in color)
    d.ellipse([7, y0 + 4, 15, y0 + 12], fill=dot_color)

    # Alert name, truncated to available width
    text = _fit_text(d, name, font_label, device.width - 26)
    d.text((21, y0 + 8), text, fill=color, font=font_label, anchor="lm")

    # Page indicator when there are multiple alerts
    if len(alerts) > 1:
        count_str = f"{idx + 1}/{len(alerts)}"
        cw = int(d.textlength(count_str, font=font_label))
        d.text((device.width - cw - 4, y0 + 8), count_str,
               fill=(65, 65, 65), font=font_label, anchor="lm")


def _dim(color, factor):
    """Multiply each channel of a (R,G,B) tuple by factor (0–1)."""
    return tuple(max(0, min(255, int(c * factor))) for c in color)


def _draw_gauge(d, cx, cy, r, needle_gust, actual_gust, frame, stale=False):
    """Draw the backing arc, colored zones, tick marks, scale labels, wind streaks, and needle.

    When stale=True the arc zones are dimmed and animated streaks are suppressed
    to communicate that the underlying data is old.
    """
    box = (cx - r, cy - r, cx + r, cy + r)

    _draw_speed_lines(d, cx, cy, r)

    # Dark channel arc (slightly wider → thin dark border around the zones)
    d.arc(box, 178, 362, fill=(30, 30, 30), width=22)

    # Colored zone arcs — dimmed to ~30 % when data is stale
    dim = 0.30 if stale else 1.0
    d.arc(box, 180,             GOOD_ARC_END,    fill=_dim(_GREEN,  dim), width=16)
    d.arc(box, GOOD_ARC_END,    CAUTION_ARC_END, fill=_dim(_YELLOW, dim), width=16)
    d.arc(box, CAUTION_ARC_END, 360,             fill=_dim(_RED,    dim), width=16)

    if not stale:
        _draw_wind_streaks(d, cx, cy, r, actual_gust, frame)

    # Tick marks at every 5 mph, drawn just inside the arc inner edge
    tick_outer = r - 8
    tick_inner = r - 18
    for mph_val in range(0, GAUGE_MAX + 1, 5):
        ang = math.pi * (1 - mph_val / GAUGE_MAX)
        ca, sa = math.cos(ang), math.sin(ang)
        x1, y1 = cx + tick_outer * ca, cy - tick_outer * sa
        x2, y2 = cx + tick_inner * ca, cy - tick_inner * sa
        is_major = (mph_val % 10 == 0) or mph_val == GAUGE_MAX
        d.line([(int(x1), int(y1)), (int(x2), int(y2))],
               fill=(200, 200, 200) if is_major else (110, 110, 110),
               width=2 if is_major else 1)

    # Scale labels at zone boundaries — positions computed from gauge geometry
    label_r = r + 16
    for mph_val, label in [(0, "0"), (GOOD_MPH, str(GOOD_MPH)),
                            (CAUTION_MPH, str(CAUTION_MPH)), (GAUGE_MAX, str(GAUGE_MAX))]:
        ang = math.pi * (1 - mph_val / GAUGE_MAX)
        lx = int(cx + label_r * math.cos(ang))
        ly = int(cy - label_r * math.sin(ang))
        d.text((lx, ly), label, fill=(150, 150, 150), font=font_label, anchor="mm")

    # Kite-shaped needle — position driven by smoothed needle_gust; greyed out when stale
    pct  = min(max(needle_gust / GAUGE_MAX, 0), 1)
    ang  = math.pi * (1 - pct)
    perp = ang + math.pi / 2
    ca, sa = math.cos(ang), math.sin(ang)
    cp, sp = math.cos(perp), math.sin(perp)
    tip   = (cx + r  * ca,  cy - r  * sa)
    wide  = (cx + 8  * ca,  cy - 8  * sa)
    tail  = (cx - 14 * ca,  cy + 14 * sa)
    hw = 5.5
    left  = (wide[0] + hw * cp, wide[1] - hw * sp)
    right = (wide[0] - hw * cp, wide[1] + hw * sp)
    needle_fill = (90, 90, 90) if stale else (240, 240, 240)
    d.polygon(
        [(int(tip[0]),   int(tip[1])),
         (int(left[0]),  int(left[1])),
         (int(tail[0]),  int(tail[1])),
         (int(right[0]), int(right[1]))],
        fill=needle_fill,
    )

    # Stale data watermark — centered in the arc face
    if stale:
        d.text((cx, cy - r // 2), "STALE", fill=(52, 52, 52), font=font_status, anchor="mm")

    # Pivot hub: dark ring with subtle outline, bright centre dot
    d.ellipse((cx - 11, cy - 11, cx + 11, cy + 11), fill=(28, 28, 28), outline=(90, 90, 90), width=1)
    d.ellipse((cx -  5, cy -  5, cx +  5, cy +  5), fill=(210, 210, 210))


def render_display(state, frame, needle_gust):
    wind    = state["wind"]
    gust    = state["gust"]
    wdir    = state["wdir"]
    age     = state["age"]
    wtmp    = state["wtmp"]
    wvht    = state["wvht"]
    atmp    = state.get("atmp")
    alerts  = state["alerts"]
    history = state.get("gust_history", [])

    msg    = ("GOOD" if gust < GOOD_MPH
              else "CAUTION" if gust <= CAUTION_MPH
              else "TOO WINDY")
    accent = _STATUS_CONFIG[msg][0]

    img, d = make_image()
    cx, cy, r = 120, 255, 100

    draw_centered(d, 5,  "PONTOON WIND",         (160, 160, 160), font_title)
    _draw_status_badge(d, 23, msg, frame)

    # Gust: colored in status accent, with trend indicator to its right
    gust_str = f"Gust  {gust:.1f} mph"
    gtw = int(d.textlength(gust_str, font=font_data))
    gx  = (device.width - gtw) // 2
    d.text((gx, 58), gust_str, fill=accent, font=font_data)
    trend = _trend(history)
    if trend is not None:
        _draw_trend(d, gx + gtw + 9, 59, trend)

    draw_centered(d, 75, f"Wind  {wind:.1f} mph", "white", font_data)
    _draw_compass(d, device.width - 15, 75, 11, wdir)
    _draw_marine_data(d, wtmp, wvht, atmp)

    if age is not None:
        age_color = _YELLOW if age >= STALE_MINUTES else (100, 100, 100)
        age_str = f"{age}m"
        w = d.textlength(age_str, font=font_label)
        d.text((int(device.width - w - 5), 5), age_str, fill=age_color, font=font_label)

    stale = age is not None and age >= STALE_MINUTES
    _draw_gauge(d, cx, cy, r, needle_gust, gust, frame, stale=stale)
    _draw_alert_strip(d, alerts, frame, accent)

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
    global _needle_gust

    threading.Thread(target=_data_loop, daemon=True).start()

    frame = 0
    while True:
        t0 = time.monotonic()

        with _lock:
            snap                  = dict(_state)
            snap["alerts"]        = list(_state["alerts"])
            snap["gust_history"]  = list(_state["gust_history"])

        if snap["wind"] is not None:
            # Exponential approach: close 35 % of remaining gap each frame
            _needle_gust += (snap["gust"] - _needle_gust) * 0.35
            try:
                render_display(snap, frame, _needle_gust)
            except Exception:
                logging.exception("Render failed")
        elif snap["error"] is not None:
            try:
                img, d = make_image()
                draw_centered(d, 20, "ERROR", _RED, font_status)
                d.text((12, 65), textwrap.shorten(snap["error"], width=40, placeholder="…"),
                       fill=(180, 180, 180), font=font_data)
                device.display(img)
            except Exception:
                logging.exception("Error screen render failed")
        else:
            # Animated connecting screen — pulsing dots while waiting for first data
            try:
                dots = "." * ((frame // FRAME_RATE) % 4)
                bright = int(60 + 30 * math.sin(frame * math.pi / (FRAME_RATE * 2)))
                img, d = make_image()
                draw_centered(d, 98,  "PONTOON WIND",      (bright, bright, bright),       font_title)
                draw_centered(d, 116, f"Connecting{dots}", (bright - 20, bright - 20, bright - 20), font_status)
                device.display(img)
            except Exception:
                logging.exception("Connecting screen render failed")

        frame += 1
        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, 1 / FRAME_RATE - elapsed))


if __name__ == "__main__":
    main()
