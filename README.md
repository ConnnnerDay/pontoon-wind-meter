# Pontoon Wind Meter

A Raspberry Pi Zero 2 W marine conditions display that uses live NOAA buoy data to determine whether conditions are suitable for taking a pontoon boat out on the Intracoastal Waterway near Wilmington, North Carolina.

The project uses a low-cost SPI TFT display and shows current wind conditions as a green / yellow / red gauge similar to a credit score meter.

---

## Features

* Live NOAA / NDBC buoy data (Station 41038, Wrightsville Beach Nearshore, NC)
* Green / yellow / red safety gauge with needle
* Wind speed, gust speed, and wind direction display
* Data age indicator — turns yellow when the reading is older than 90 minutes
* Arc color zones matched precisely to the GOOD / CAUTION / TOO WINDY thresholds
* TrueType font rendering (DejaVuSans) with bitmap fallback
* Auto-retries failed fetches (up to 3 attempts) before showing an error screen
* Graceful shutdown — clears the display when the service is stopped
* Auto-starts at boot using systemd
* All log output routed to `journalctl`
* Headless operation over SSH
* Runs on a Raspberry Pi Zero 2 W

---

## Current Data Source

NOAA / NDBC Station 41038 — Wrightsville Beach Nearshore, NC

The display uses:

* Wind speed (m/s → mph)
* Wind gust speed (m/s → mph)
* Wind direction (compass label: N / NE / E / … )

Wind data is retrieved directly from NOAA realtime station text feeds every 5 minutes.

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
* 2.4" SPI TFT display (ILI9341 controller)
* MicroSD card
* 5V USB power supply

### Display Pinout

| TFT Pin | Raspberry Pi Pin |
| ------- | ---------------- |
| SDO     | GPIO9 / Pin 21   |
| LED     | 3.3V / Pin 1     |
| SCK     | GPIO11 / Pin 23  |
| SDI     | GPIO10 / Pin 19  |
| DC      | GPIO24 / Pin 18  |
| RESET   | GPIO25 / Pin 22  |
| CS      | GPIO8 / Pin 24   |
| GND     | GND / Pin 6      |
| VCC     | 3.3V / Pin 17    |

**Important:** use 3.3 V only — do not connect the display to 5 V. SPI must be enabled in `raspi-config`.

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
pip install luma.core luma.lcd "Pillow>=8.0"
```

### Deploy the Script

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

Tests cover the NDBC parsing and conversion functions and run on any machine — no Pi or display hardware required.

```bash
pip install pytest        # or: pip install -r requirements-dev.txt
pytest test_ndbc.py -v
```

---

## Repository Structure

```text
pontoon-wind-meter/
├── .gitignore
├── README.md
├── ndbc.py               # pure functions: parsing, conversion, data age
├── pontoon_meter.py      # main display loop (hardware)
├── requirements.txt      # runtime dependencies
├── requirements-dev.txt  # test dependencies
├── systemd/
│   └── pontoon-meter.service
└── test_ndbc.py          # 26 unit tests for ndbc.py
```

---

## Notes

This project is intentionally lightweight:

* Low power consumption
* Simple hardware
* Easy field deployment
* Marine environment usability

The goal is not to replace official marine forecasts, but to provide a quick visual go / no-go indicator for local recreational boating conditions.

---

## Project Goals

Planned improvements:

* Touchscreen support
* NOAA marine forecast integration
* Tide data
* Multiple buoy selection
* Weather alerts
* Waterproof enclosure
* Battery-powered portable version
* Sunlight-readable display options

---

## License

MIT License
