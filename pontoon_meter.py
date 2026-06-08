from luma.core.interface.serial import spi
from luma.lcd.device import ili9341
import io
import json
import logging
import math
import signal
import sys
import textwrap
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

from config import build_arg_parser, load_config
import logic
from data.fetcher import data_loop as _data_loop_fn
import ui.renderer as renderer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Parse CLI arguments and load configuration
_args = build_arg_parser().parse_args()
cfg   = load_config(_args)

# Build NOAA URLs from config
URL        = f"https://www.ndbc.noaa.gov/data/realtime2/{cfg['ndbc_station']}.txt"
ALERTS_URL = f"https://api.weather.gov/alerts/active?point={cfg['lat']},{cfg['lon']}"

# Convenience aliases used by the web handler
GOOD_MPH    = cfg["good_mph"]
CAUTION_MPH = cfg["caution_mph"]
STALE_MINUTES = cfg["stale_minutes"]
FRAME_RATE  = cfg["frame_rate"]
WEB_PORT    = cfg["web_port"]

try:
    _serial = spi(port=0, device=0, gpio_DC=24, gpio_RST=25)
    device  = ili9341(_serial, width=320, height=240, rotate=3)
except Exception as e:
    logging.critical("Display init failed: %s", e)
    sys.exit(1)

renderer.init(device, cfg)

# Thread-safe state shared between the data thread and the animation loop
_lock  = threading.Lock()
_state = {
    "wind": None, "gust": None, "wdir": "---",
    "age": None, "wtmp": None, "wvht": None, "atmp": None, "dpd": None,
    "dewp": None,
    "pres": None, "pres_history": [],
    "gust_history": [],
    "alerts": [], "error": None,
    "cached": False,   # True when the display is showing disk-cached data
}

# Animation-loop-only needle state (no lock needed)
_needle_gust = 0.0
_needle_vel  = 0.0


# ---------------------------------------------------------------------------
# Web dashboard
# ---------------------------------------------------------------------------

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
            data = renderer.get_latest_frame()
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
                score     = logic.composite_score(s, cfg)
                s["status"] = logic.status_label(score, cfg)
            else:
                s["status"] = "OFFLINE"
            s["stale"]  = s.get("age") is not None and s["age"] >= STALE_MINUTES
            s["trend"]  = logic.trend(s["gust_history"])
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
        pass


def _start_web_server():
    class _Server(HTTPServer):
        allow_reuse_address = True
    server = _Server(("", WEB_PORT), _WebHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Web dashboard on http://0.0.0.0:%d/", WEB_PORT)


# ---------------------------------------------------------------------------
# Shutdown handler
# ---------------------------------------------------------------------------

def handle_exit(sig, _frame):
    logging.info("Shutting down (signal %d)", sig)
    try:
        img, d = renderer.make_image()
        renderer.draw_centered(d, 125 * renderer._SS, "Offline", (80, 80, 80), renderer.font_big)
        renderer._show(img)
    except Exception:
        pass
    sys.exit(0)


signal.signal(signal.SIGTERM, handle_exit)
signal.signal(signal.SIGINT,  handle_exit)


# ---------------------------------------------------------------------------
# Startup diagnostics
# ---------------------------------------------------------------------------

def _log_startup_info() -> None:
    """Emit a structured startup banner so every run is easy to trace in journald."""
    logging.info("=" * 60)
    logging.info("Pontoon Wind Meter — starting up")
    logging.info("  Location   : %s", cfg["location_name"])
    logging.info("  NDBC       : station %s  → %s", cfg["ndbc_station"], URL)
    logging.info("  CO-OPS     : station %s (water-temp fallback)", cfg["coops_station"])
    logging.info("  Alerts     : %s", ALERTS_URL)
    logging.info("  Poll       : NDBC every %ds, alerts every %ds",
                 cfg["poll_interval"], cfg["alerts_interval"])
    logging.info("  Display    : %d fps, web on :%d", cfg["frame_rate"], cfg["web_port"])
    if cfg.get("cache_enabled"):
        logging.info("  Cache      : enabled — %s (max age %dm)",
                     cfg["cache_path"], cfg["cache_max_age_minutes"])
    else:
        logging.info("  Cache      : disabled")
    logging.info("=" * 60)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    global _needle_gust, _needle_vel

    _log_startup_info()
    _start_web_server()
    threading.Thread(
        target=_data_loop_fn,
        args=(_state, _lock, cfg),
        daemon=True,
    ).start()

    frame = 0
    while True:
        t0 = time.monotonic()

        with _lock:
            snap                 = dict(_state)
            snap["alerts"]       = list(_state["alerts"])
            snap["gust_history"] = list(_state["gust_history"])
            snap["pres_history"] = list(_state["pres_history"])

        if snap["wind"] is not None:
            snap_composite = logic.composite_score(snap, cfg)
            diff = snap_composite - _needle_gust
            _needle_vel  = max(-6.0, min(6.0,
                _needle_vel * renderer._NEEDLE_DAMPING + diff * renderer._NEEDLE_SPRING))
            _needle_gust = max(0.0, min(cfg["gauge_max"] + 3, _needle_gust + _needle_vel))
            try:
                renderer.render_display(snap, frame, _needle_gust, snap_composite)
            except Exception:
                logging.exception("Render failed")

        elif snap["error"] is not None:
            try:
                img, d = renderer.make_image()
                _SS = renderer._SS
                _RED = renderer._RED
                ep  = 0.45 + 0.55 * math.sin(frame * math.pi / (FRAME_RATE * 0.7))
                ec  = tuple(int(c * (0.35 + 0.65 * ep)) for c in _RED)
                ec2 = tuple(c // 2 for c in ec)
                for sz, col in ((30 * _SS, ec2), (16 * _SS, ec)):
                    d.polygon([(0, 0), (sz, 0), (0, sz)], fill=col)
                    d.polygon([(renderer._W - 1, 0),
                               (renderer._W - 1 - sz, 0),
                               (renderer._W - 1, sz)], fill=col)
                renderer.draw_centered(d, 78 * _SS, "ERROR", ec, renderer.font_big)
                err_text = textwrap.shorten(snap["error"], width=34, placeholder="…")
                d.text((12 * _SS, 150 * _SS), err_text,
                       fill=(160, 160, 160), font=renderer.font_data)
                renderer._show(img)
            except Exception:
                logging.exception("Error screen render failed")

        else:
            try:
                _SS  = renderer._SS
                _W   = renderer._W
                _H   = renderer._H
                cx_s = renderer._GAUGE_CX
                dots   = "." * ((frame // FRAME_RATE) % 4)
                bright = int(72 + 42 * math.sin(frame * math.pi / (FRAME_RATE * 2)))
                dim2   = int(bright * 0.62)
                dim3   = int(bright * 0.84)
                img, d = renderer.make_image()
                spin  = (frame * 14) % 360
                bc    = (bright, bright, bright)
                d.arc((cx_s - 30 * _SS, 110 * _SS, cx_s + 30 * _SS, 170 * _SS),
                      spin, spin + 115, fill=bc, width=3 * _SS)
                d.arc((cx_s - 30 * _SS, 110 * _SS, cx_s + 30 * _SS, 170 * _SS),
                      spin + 115, spin + 230,
                      fill=(bright // 4, bright // 4, bright // 4), width=2 * _SS)
                spin2 = (360 - frame * 9) % 360
                d.arc((cx_s - 18 * _SS, 122 * _SS, cx_s + 18 * _SS, 158 * _SS),
                      spin2, spin2 + 80,
                      fill=(bright // 3, bright // 3, bright // 3), width=2 * _SS)
                renderer.draw_centered(
                    d, 182 * _SS, "PONTOON WIND",
                    (int(bright * 0.60), bright, int(bright * 0.72)), renderer.font_title)
                renderer.draw_centered(
                    d, 198 * _SS,
                    f"NDBC {cfg['ndbc_station']} · {cfg['location_name']}",
                    (int(dim2 * 0.55), int(dim2 * 0.75), dim2), renderer.font_label)
                renderer.draw_centered(
                    d, 216 * _SS, f"Connecting{dots}",
                    (dim3 // 2, dim3 // 2, dim3 // 2), renderer.font_data)
                renderer._show(img)
            except Exception:
                logging.exception("Connecting screen render failed")

        frame += 1
        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, 1 / FRAME_RATE - elapsed))


if __name__ == "__main__":
    main()
