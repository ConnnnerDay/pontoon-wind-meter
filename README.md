# Pontoon Wind Meter

A Raspberry Pi Zero 2 W marine conditions display that uses live NOAA buoy data to determine whether conditions are suitable for taking a pontoon boat out on the Intracoastal Waterway near Wilmington, North Carolina.

The project uses a low-cost SPI TFT display and displays current wind conditions as a large green / yellow / red gauge similar to a credit score meter.

---

## Features

* Live NOAA / NDBC buoy data
* Green / yellow / red safety gauge
* Designed for pontoon boating conditions
* Optimized for small SPI TFT displays
* Auto-starts at boot using systemd
* Lightweight Python implementation
* Headless operation over SSH
* Runs on a Raspberry Pi Zero 2 W

---

## Current Data Source

NOAA / NDBC Station 41038
Wrightsville Beach Nearshore, NC

The display currently uses:

* Wind speed
* Wind gusts
* Wind direction

Wind data is retrieved directly from NOAA realtime station text feeds.

---

## Hardware

### Main Components

* Raspberry Pi Zero 2 W
* 2.4" SPI TFT display
* ILI9341-compatible display controller
* MicroSD card
* 5V USB power supply

### Display Type

This project currently targets common low-cost SPI TFT displays that expose pins similar to:

```text
SDO
LED
SCK
SDI
DC
RESET
CS
GND
VCC
```

---

## Wiring

### TFT Display → Raspberry Pi Zero 2 W

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

Important:

* Use 3.3V only
* Do not connect the display to 5V
* SPI must be enabled in `raspi-config`

---

## Raspberry Pi OS

Recommended OS:

* Raspberry Pi OS Lite (64-bit)

The project is intended to run headless over SSH.

---

## Enable SPI

```bash
sudo raspi-config
```

Navigate to:

```text
Interface Options
→ SPI
→ Enable
```

Reboot afterward.

---

## Python Environment

### Install Dependencies

```bash
sudo apt update
sudo apt install -y python3-pip python3-pil python3-numpy python3-rpi.gpio python3-spidev python3-lgpio
```

### Create Virtual Environment

```bash
python3 -m venv --system-site-packages ~/tftenv
source ~/tftenv/bin/activate
```

### Install Python Packages

```bash
pip install luma.lcd Pillow
```

---

## Running the Display

### Manual Start

```bash
/home/pizero/tftenv/bin/python /home/pizero/pontoon_meter.py
```

---

## systemd Service

### Service File

`systemd/pontoon-meter.service`

### Enable Service

```bash
sudo systemctl daemon-reload
sudo systemctl enable pontoon-meter.service
sudo systemctl start pontoon-meter.service
```

### Service Commands

Check status:

```bash
systemctl status pontoon-meter.service
```

Restart:

```bash
sudo systemctl restart pontoon-meter.service
```

View logs:

```bash
journalctl -u pontoon-meter.service -f
```

---

## Current Gauge Logic

Current thresholds:

| Status | Wind Gust   |
| ------ | ----------- |
| Green  | Under 10 kt |
| Yellow | 10–15 kt    |
| Red    | Over 15 kt  |

These values are still experimental and may be adjusted for local ICW conditions.

---

## Project Goals

Planned improvements:

* Better gauge graphics
* Touchscreen support
* NOAA marine forecast integration
* Tide data
* Radar overlays
* GPS support
* Multiple buoy selection
* Weather alerts
* Waterproof enclosure
* Battery-powered portable version
* Sunlight-readable display options

---

## Repository Structure

```text
pontoon-wind-meter/
├── README.md
├── pontoon_meter.py
├── requirements.txt
├── systemd/
│   └── pontoon-meter.service

```

---

## Notes

This project is intentionally lightweight and designed around:

* low power consumption
* simple hardware
* easy field deployment
* marine environment usability

The goal is not to replace official marine forecasts, but to provide a quick visual “go / no-go” indicator for local recreational boating conditions.

---

## License

MIT License
