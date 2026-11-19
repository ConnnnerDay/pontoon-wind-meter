from luma.core.interface.serial import spi
from luma.lcd.device import ili9341
import json
import logging
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit

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

STALE_MINUTES = cfg["stale_minutes"]
FRAME_RATE    = cfg["frame_rate"]
WEB_PORT      = cfg["web_port"]

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
    "gust_history": [], "obs_time": None,
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
        path = urlsplit(self.path).path
        if path == "/frame":
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
        elif path == "/data":
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
            s["alerts"] = [[a[0], a[1]] for a in s["alerts"]]
            body = json.dumps(s).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/", "/index.html"):
            body = _WEB_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *_args):
        pass


def _start_web_server():
    # Threaded so one slow phone on bad Wi-Fi can't stall every other client.
    class _Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True
    try:
        server = _Server(("", WEB_PORT), _WebHandler)
    except OSError as exc:
        # Don't take the whole display down because the port is busy.
        logging.error("Web dashboard disabled: cannot bind port %d: %s", WEB_PORT, exc)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Web dashboard on http://0.0.0.0:%d/", WEB_PORT)


# ---------------------------------------------------------------------------
# Shutdown handler
# ---------------------------------------------------------------------------

def handle_exit(sig, _frame):
    logging.info("Shutting down (signal %d)", sig)
    try:
        # luma clears the panel in its atexit hook unless persist is set,
        # which would wipe the "Offline" frame straight away.
        device.persist = True
        renderer.render_offline()
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
                renderer.render_error(snap["error"], frame)
            except Exception:
                logging.exception("Error screen render failed")

        else:
            try:
                renderer.render_connecting(frame)
            except Exception:
                logging.exception("Connecting screen render failed")

        frame += 1
        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, 1 / FRAME_RATE - elapsed))


if __name__ == "__main__":
    main()
