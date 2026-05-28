# Pontoon Wind Meter

Raspberry Pi Zero 2 W + SPI TFT display project that shows a live wind safety gauge for pontoon boating near Wilmington, NC using NOAA buoy data.

## Features

- Green / yellow / red wind safety meter
- NOAA/NDBC live buoy data
- ILI9341 SPI TFT display
- Auto-starts with systemd
- Designed for Raspberry Pi Zero 2 W

## NOAA Data Source

Station 41038 — Wrightsville Beach Nearshore, NC

## Hardware

- Raspberry Pi Zero 2 W
- 2.4" SPI TFT display
- ILI9341-compatible controller

## Wiring

| TFT Pin | Pi GPIO |
|---|---|
| SDO | GPIO9 / Pin 21 |
| LED | 3.3V / Pin 1 |
| SCK | GPIO11 / Pin 23 |
| SDI | GPIO10 / Pin 19 |
| DC | GPIO24 / Pin 18 |
| RESET | GPIO25 / Pin 22 |
| CS | GPIO8 / Pin 24 |
| GND | GND / Pin 6 |
| VCC | 3.3V / Pin 17 |

## Service

```bash
sudo systemctl restart pontoon-meter.service
sudo systemctl status pontoon-meter.service
journalctl -u pontoon-meter.service -f
```
