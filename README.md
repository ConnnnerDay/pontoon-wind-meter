# Pontoon Wind Meter

A Raspberry Pi Zero 2 W marine conditions display that pulls live NOAA buoy data and shows whether conditions are suitable for taking a pontoon boat out on the Intracoastal Waterway near Wilmington, North Carolina.

A 2.4" SPI TFT display shows a color-zoned speedometer gauge, current wind and gust speeds, water temperature, wave height, wind direction compass, gust trend arrow, and a scrolling strip for active NOAA weather alerts — all updated live, five frames per second.

---

## Features

**Wind & conditions**
* Live NDBC buoy data — Station 41038, Wrightsville Beach Nearshore, NC, polled every 5 minutes
* Wind speed and gust speed (m/s → mph), wind direction compass label (N/NE/E/…)
* Water temperature (°C → °F) and wave height (m → ft)
* Observation age indicator — turns yellow when the reading is older than 90 minutes
* Stale-data dimming — when age ≥ 90 min, arc zones drop to 30% brightness and animated streaks stop so the gauge visually communicates uncertainty

**Display**
* Animated speedometer gauge with green / yellow / red arc zones matching GOOD / CAUTION / TOO WINDY thresholds
* Smooth exponential needle animation (closes 35% of the gap each frame)
* Wind streak animation along the inner gauge face — speed proportional to wind gust
* Radial speed-line texture behind the gauge arc
* Gust trend indicator (↑ amber / ↓ blue / — grey) based on the 15-minute delta
* Compact compass rose showing current wind direction
* Pulsing status badge (GOOD / CAUTION / TOO WINDY)

**Weather alerts**
* Active NOAA weather alerts fetched every 10 minutes from the Weather.gov API
* Alert names shown in the bottom strip (truncated to fit the screen), with a pulsing dot colored by severity (Extreme/Severe → red, Moderate → orange, Minor → yellow)
* Cycles through multiple simultaneous alerts with a page indicator
* When no alerts are active, the strip shows a scrolling marine wave and the local time

**Architecture**
* Background data thread refreshes NDBC and NOAA alerts on independent timers; animation loop runs at 5 fps and never blocks on network I/O
* Thread-safe state snapshot with `threading.Lock()`
* Auto-retries failed fetches (up to 3 attempts with 5-second back-off)
* Graceful shutdown — clears display on SIGTERM/SIGINT
* Startup "Connecting…" screen while waiting for first data
* Error screen with truncated message if all fetch attempts fail
* All log output routed to `journalctl`

**Deployment**
* Systemd service (`After=network-online.target time-sync.target`) with `Restart=always`
* Headless operation over SSH
* TrueType font rendering (DejaVuSans) with bitmap fallback if fonts are unavailable

---

## Gauge Logic

| Status    | Gust speed    | Arc color |
| --------- | ------------- | --------- |
| GOOD      | Under 12 mph  | Green     |
| CAUTION   | 12 – 18 mph   | Yellow    |
| TOO WINDY | Over 18 mph   | Red       |

These thresholds are experimental and may be adjusted for local ICW conditions. Change `GOOD_MPH` and `CAUTION_MPH` at the top of `pontoon_meter.py`.

---

## Hardware

### Main Components

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

### Deploy the Scripts

```bash
cp pontoon_meter.py ndbc.py ~/
```

---

## Running Manually

```bash
/home/pizero/tftenv/bin/python /home/pizero/pontoon_meter.py
```

---

## systemd Service

Copy the service file and enable it:

```bash
sudo cp systemd/pontoon-meter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pontoon-meter.service
sudo systemctl start pontoon-meter.service
```

### Service Commands

```bash
systemctl status pontoon-meter.service
sudo systemctl restart pontoon-meter.service
journalctl -u pontoon-meter.service -f
```

---

## Running Tests

Tests cover NDBC parsing and unit conversion functions and run on any machine — no Pi or display hardware required.

```bash
pip install pytest        # or: pip install -r requirements-dev.txt
pytest test_ndbc.py -v
```

33 tests cover `ms_to_mph`, `celsius_to_f`, `m_to_ft`, `wind_direction`, `parse_ndbc`, and `obs_age_minutes`.

---

## Notes

This project is intentionally lightweight:

* Low power consumption
* Simple hardware
* Easy field deployment
* Marine environment usability

The goal is not to replace official marine forecasts, but to provide a quick visual go / no-go indicator for local recreational boating conditions.

---

## Planned Improvements

* Touchscreen support
* Tide data
* Multiple buoy selection
* Waterproof enclosure
* Battery-powered portable version
* Sunlight-readable display options

---

## License

MIT License
