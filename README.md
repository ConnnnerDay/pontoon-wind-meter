# Pontoon Wind Meter

A Raspberry Pi Zero 2 W marine conditions display that pulls live NOAA data and shows whether conditions are suitable for taking a pontoon boat out on the Intracoastal Waterway near Wilmington, North Carolina.

A 2.4" SPI TFT display shows a color-zoned speedometer gauge, current wind and gust speeds, water temperature, wave height, wind direction compass, gust trend arrow, and a scrolling strip for active NOAA weather alerts — all updated live every 5 minutes.

---

## Features

**Wind & conditions**
* Live NDBC buoy data — Station 41038 (Wrightsville Beach Nearshore, NC), polled every 5 minutes
* Wind speed and gust speed (m/s → mph), wind direction compass label (N/NE/E/…)
* Water temperature sourced from NDBC buoy; falls back automatically to NOAA CO-OPS Station 8658120 (Wilmington, NC) when the buoy reading is unavailable
* Wave height (m → ft) and dominant wave period for chop vs. swell detection
* Observation age indicator — turns yellow when the reading is older than 90 minutes

**Display**
* Animated speedometer gauge with green / yellow / red arc zones matching GO / CAUTION / NO-GO thresholds
* Smooth spring-damper needle animation
* Wind streak animation along the inner gauge face — speed proportional to gust
* Gust trend indicator (↑ amber / ↓ blue / — grey) based on the 15-minute delta
* Compact compass rose showing current wind direction
* Pulsing status badge (GO / CAUTION / NO-GO)
* Web dashboard on port 8080 (`/frame` for PNG snapshot, `/data` for JSON state)

**Weather alerts**
* Active NOAA weather alerts fetched every 10 minutes from the Weather.gov API (no API key required)
* Alert names shown in the top strip with a pulsing dot colored by severity
* Cycles through multiple simultaneous alerts with a page indicator

**Offline / cache**
* Every successful NOAA fetch is saved atomically to a JSON file on disk
* When the network is unavailable the last cached snapshot is loaded automatically — the display keeps showing the last known conditions with a small amber **CACHED** badge in the status band
* Cached data older than 3 hours (configurable) is considered too stale to use and is ignored

**Architecture**
* Background data thread refreshes NDBC and NOAA alerts on independent timers; animation loop runs at 30 fps and never blocks on network I/O
* Thread-safe state snapshot with `threading.Lock()`
* Auto-retries failed fetches (up to 3 attempts with 5-second back-off)
* Graceful shutdown — clears display on SIGTERM/SIGINT

---

## Configuration

All thresholds, location details, and polling intervals live in **`config.yaml`** next to the scripts.  Edit that file and restart the service — no code changes needed.

```yaml
location:
  ndbc_station: "41038"   # change to switch buoys
  lat: 34.2108
  lon: -77.5986

thresholds:
  good_mph:    22   # below this → GO (green)
  caution_mph: 30   # 22–30 mph  → CAUTION (yellow); above → NO-GO (red)
```

### Environment variable overrides

Every threshold can also be set via a `PONTOON_` env var — useful for quick experiments or remote management without touching the file:

```bash
PONTOON_GOOD_MPH=12 PONTOON_CAUTION_MPH=20 python pontoon_meter.py
```

### Command-line overrides

```bash
python pontoon_meter.py --good-mph 12 --caution-mph 20 --config myconfig.yaml
```

Run `python pontoon_meter.py --help` for the full list.

### Cache configuration

```yaml
cache:
  enabled:         true
  path:            "/home/pizero/.cache/pontoon-meter/latest_snapshot.json"
  max_age_minutes: 180   # ignore snapshots older than this
```

Environment variable overrides: `PONTOON_CACHE_ENABLED`, `PONTOON_CACHE_PATH`, `PONTOON_CACHE_MAX_AGE_MINUTES`.

Disable the cache entirely for a single run: `python pontoon_meter.py --no-cache`

---

## Go / No-Go Logic

| Status   | Gust speed   | Arc color |
|----------|--------------|-----------|
| GO       | Under 22 mph | Green     |
| CAUTION  | 22 – 30 mph  | Yellow    |
| NO-GO    | Over 30 mph  | Red       |

Wind is the primary factor. The composite score also factors in wave height, water temperature, barometric pressure trend, fog risk, heat index, and wind chill. Adjust thresholds in `config.yaml`.

---

## Data Sources

All data comes from public NOAA APIs — **no API key is required**.

| Source | URL | Refresh |
|--------|-----|---------|
| NDBC buoy 41038 (wind, waves, water temp) | `https://www.ndbc.noaa.gov/data/realtime2/41038.txt` | Every 5 min |
| NOAA CO-OPS station 8658120 (water temp fallback) | `https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?station=8658120&product=water_temperature&date=latest&units=english&time_zone=lst&format=json` | Cached 30 min |
| Weather.gov alerts | `https://api.weather.gov/alerts/active?point=34.2108,-77.5986` | Every 10 min |

To change the location, edit `ACTIVE_LOCATION` in `locations.py`. Look up CO-OPS station IDs at [tidesandcurrents.noaa.gov](https://tidesandcurrents.noaa.gov) and NDBC station IDs at [ndbc.noaa.gov](https://www.ndbc.noaa.gov).

---

## Species Reference

`data/species.json` lists common sport fish for the Wrightsville Beach / Wilmington area with habitat type (`surf`, `pier`, `inshore`, `nearshore`, `offshore`), size range, typical rigs, best baits, season, and a note on which wind/sea conditions suit each species. The file is standalone — the display app does not load it at runtime.

---

## Hardware

* Raspberry Pi Zero 2 W
* 2.4" SPI TFT display (ILI9341 controller, 320×240)
* MicroSD card
* 5V USB power supply

---

## Setup

### Raspberry Pi OS

Raspberry Pi OS Lite (64-bit) is recommended. The project runs headless over SSH.

### Enable SPI

```bash
sudo raspi-config
# Interface Options → SPI → Enable
# Reboot afterward
```

### Install System Dependencies

```bash
sudo apt update
sudo apt install -y python3-pip python3-pil python3-numpy python3-rpi.gpio python3-spidev python3-lgpio
```

### Create Virtual Environment

```bash
python3 -m venv --system-site-packages ~/tftenv
source ~/tftenv/bin/activate
pip install luma.core luma.lcd "Pillow>=8.2"
```

### Deploy the Scripts (manual)

```bash
cp -r . /home/pizero/pontoon-wind-meter/
```

### Deploy with the install script (recommended)

The `scripts/install_service.sh` script handles everything in one step:

```bash
sudo bash scripts/install_service.sh
```

It will:
1. Stop the existing service (if running)
2. Copy all project files to `/home/pizero/pontoon-wind-meter`
3. Install/update Python packages into `/home/pizero/tftenv`
4. Install the systemd unit and reload the daemon
5. Enable and restart the service
6. Print the final service status

---

## Running Manually

```bash
/home/pizero/tftenv/bin/python /home/pizero/pontoon-wind-meter/pontoon_meter.py
```

---

## systemd Service

```bash
sudo cp systemd/pontoon-meter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pontoon-meter.service
sudo systemctl start pontoon-meter.service
```

```bash
systemctl status pontoon-meter.service
sudo systemctl restart pontoon-meter.service
journalctl -u pontoon-meter.service -f
```

---

## Running Tests

Tests cover NDBC parsing, unit conversion, configuration loading, Go/No-Go scoring, and the disk cache.  They run on any machine — no Pi or display hardware required.

```bash
pip install -r requirements-dev.txt
pytest -q
```

---

## Troubleshooting

```bash
# Check service status
systemctl status pontoon-meter.service

# Follow live logs
journalctl -u pontoon-meter.service -f

# View the last cached snapshot
cat /home/pizero/.cache/pontoon-meter/latest_snapshot.json

# Remove the cache if it is corrupt or you want a clean start
rm /home/pizero/.cache/pontoon-meter/latest_snapshot.json

# Restart the service after editing config.yaml
sudo systemctl restart pontoon-meter.service
```

---

The goal is not to replace official marine forecasts, but to provide a quick visual go / no-go indicator for local recreational boating conditions.
