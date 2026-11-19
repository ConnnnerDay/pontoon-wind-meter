# Pontoon Wind Meter

A Raspberry Pi Zero 2 W with a 2.4" TFT that answers one question: is it a
good day to take the pontoon out on the ICW at Wrightsville Beach? It pulls
NOAA buoy data and weather alerts and shows GO, CAUTION, or NO-GO on a gauge.

It's a quick glance, not a forecast. Check the marine forecast before you go.

## What's on the screen

- **Gauge.** The needle shows the overall score (see below), not the raw gust,
  so a calm day with big swell or fog can still put it in the yellow. A small
  diamond marks the sustained wind, and a tick on the rim marks the highest
  gust in the recent history.
- **Status band.** GO / CAUTION / NO-GO. Four dots on the right show each
  factor separately, right to left: wind, waves, water temp, weather (fog,
  falling pressure, cold air, heat index). Grey means no data. A **CACHED**
  label appears on the left when the network is down and it's showing the last
  saved reading.
- **Info row.** Water temp, any weather warnings (`~Fog`, `↓P`, `Chilly 55°`,
  `HI 108°!`), and wave height/period.
- **Big number.** Current gust in mph, with a trend arrow on the left: up,
  down, or flat, comparing the newest observation to three observations back.
- **Top strip.** Rotates between a wind summary, a marine summary (water, air,
  waves, pressure), and the clock with the observation age. When NOAA has
  active alerts for the location, the strip shows those instead.
- **Stale data.** If the newest observation is older than `stale_minutes`
  (90 by default), the gauge goes grey and says STALE.

The same frame is served at `http://<pi>:8080/`, with `/frame` for a PNG and
`/data` for the current state as JSON.

## How the verdict works

Every factor is mapped onto the wind scale, and the worst one wins:

| Factor | Effect with default settings |
|---|---|
| Gust | Under 22 mph GO, 22–30 CAUTION, over 30 NO-GO |
| Hot air (over 80°F) | Up to 5 mph taken off the gust, but never when the gust is already over 30 |
| Waves | 3 ft is CAUTION, over 5 ft is NO-GO. Short-period chop (under 5 s) counts more, long swell (9 s+) counts less |
| Water temp | Starts counting under 65°F; about 51°F and below is CAUTION. Cold water alone never makes it NO-GO |
| Cold air | Feels-like (wind chill) under 50°F is CAUTION |
| Heat index | 103°F+ is CAUTION, climbing to NO-GO at 125°F |
| Fog | Air within 3°F of the dewpoint is CAUTION |
| Falling pressure | A 1.5 hPa drop across the last few observations is CAUTION |
| NOAA alerts | Severe or Extreme alerts force NO-GO. Advisories (Small Craft, etc.) are shown but don't change the verdict. Rip current, beach hazard, heat, and air quality alerts never change it |

All of these thresholds live in `config.yaml`.

## Data sources

All public NOAA data, no API keys.

| Data | Source | How often |
|---|---|---|
| Wind, gust, direction, waves, air/water temp, dewpoint, pressure | NDBC station 41038, `https://www.ndbc.noaa.gov/data/realtime2/41038.txt` | Polled every 5 min |
| Water temp, when the buoy doesn't report it | NOAA CO-OPS station 8658120 (Wilmington), Tides & Currents API. Readings over 3 hours old are ignored | At most every 30 min |
| Alerts | `https://api.weather.gov/alerts/active?status=actual&point=<lat>,<lon>` | Every 10 min |

If the alert fetch keeps failing, alerts already on screen are dropped once
their end time passes, so an old warning can't hold the meter at NO-GO
through a long outage.

The buoy usually reports less often than every 5 minutes. Repeat polls of the
same observation are ignored for the trend arrow and pressure check, so those
compare real observations rather than one reading against itself.

## Configuration

Settings come from, highest priority first: command-line flags, `PONTOON_*`
environment variables, `config.yaml`, then the defaults in `config.py`.

```yaml
location:
  name:          "Wrightsville Beach / ICW"
  ndbc_station:  "41038"
  coops_station: "8658120"
  lat:           34.2108   # used for the alerts lookup
  lon:          -77.5986

thresholds:
  good_mph:    22   # below this is GO
  caution_mph: 30   # up to this is CAUTION, above is NO-GO
  gauge_max:   40   # top of the gauge; must be above caution_mph
```

To move to a different spot, change the `location` section. Find buoy IDs at
[ndbc.noaa.gov](https://www.ndbc.noaa.gov) and CO-OPS stations at
[tidesandcurrents.noaa.gov](https://tidesandcurrents.noaa.gov).

Environment variables use the setting name in caps:

```bash
PONTOON_GOOD_MPH=18 PONTOON_CAUTION_MPH=25 python pontoon_meter.py
```

Or flags:

```bash
python pontoon_meter.py --good-mph 18 --caution-mph 25 --config myconfig.yaml
```

`python pontoon_meter.py --help` lists every flag.

### Offline cache

Each good buoy reading is written to
`/home/pizero/.cache/pontoon-meter/latest_snapshot.json`. If a fetch fails,
the display loads that file and shows CACHED. The observation age keeps
counting while it does, so it will still go STALE. Snapshots older than
`cache.max_age_minutes` (180) are ignored. If there's no usable data at all,
the screen shows NO DATA with the error.

`--no-cache` turns this off for one run. The env vars are
`PONTOON_CACHE_ENABLED`, `PONTOON_CACHE_PATH`, and `PONTOON_CACHE_MAX_AGE_MINUTES`.

## Hardware

- Raspberry Pi Zero 2 W
- 2.4" SPI TFT, ILI9341 controller, 320×240 (DC on GPIO 24, RST on GPIO 25)
- MicroSD card and a 5 V USB supply

## Setup

Start with Raspberry Pi OS Lite (64-bit) and enable SPI:

```bash
sudo raspi-config   # Interface Options → SPI → Enable, then reboot
```

Install system packages:

```bash
sudo apt update
```

```bash
sudo apt install -y python3-pip python3-venv python3-pil python3-numpy python3-rpi.gpio python3-spidev python3-lgpio rsync
```

Create the virtualenv the service expects:

```bash
python3 -m venv --system-site-packages ~/tftenv
```

Then from the repo, install and start the service:

```bash
sudo bash scripts/install_service.sh
```

The script copies the project to `/home/pizero/pontoon-wind-meter`, installs
`requirements.txt` into `~/tftenv`, installs and enables the systemd unit,
creates the cache directory, and restarts the service. It assumes the user is
`pizero`; set `SERVICE_USER=<name>` to change that, and edit
`systemd/pontoon-meter.service` to match.

To run it by hand instead:

```bash
~/tftenv/bin/pip install -r requirements.txt
```

```bash
~/tftenv/bin/python pontoon_meter.py
```

## Running the service

```bash
systemctl status pontoon-meter
```

```bash
journalctl -u pontoon-meter -f
```

```bash
sudo systemctl restart pontoon-meter
```

Restart after editing `config.yaml`. If the cache file gets corrupted, delete
it and restart:

```bash
rm ~/.cache/pontoon-meter/latest_snapshot.json
```

## Tests

The tests cover NDBC, CO-OPS, and alert parsing, the scoring logic, config
loading, and the cache. They don't need a Pi or a display.

```bash
pip install -r requirements-dev.txt
```

```bash
pytest -q
```

## Species reference

`data/species.json` is a list of local sport fish with habitat, rigs, baits,
and season. The app doesn't use it. Note that several of them live outside
the inlet, which is not pontoon water no matter what the meter says.
