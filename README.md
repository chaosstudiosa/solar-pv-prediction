# Solar PV Prediction (History Based)

A Home Assistant custom integration that forecasts PV output from your own
recorder statistics — no cloud APIs, no model training. Replaces a stack of
SQL + template sensors and a YAML automation with a single config-flow
integration.

Per instance you get:

| Entity | Type | Description |
| --- | --- | --- |
| `sensor.<name>_pv_max` | Sensor | Raw hour-of-day max for the current hour, from the last *N* days of long-term statistics. |
| `sensor.<name>_pv_max_spline_smoothed` | Sensor | Hermite spline (tension + ± shift) over the 24 hourly points, sampled at the current minute-of-day. |
| `sensor.<name>_pv_max_trimmed` | Sensor | Smoothed value × current trim factor. |
| `number.<name>_trim_factor` | Number (restored) | Persists across restarts, sunrise reset from weather entity, per-minute auto-adjust. |

## How it works

1. **Every hour**, a `DataUpdateCoordinator` pulls `max` statistics for each
   configured PV sensor over the last *history_days* (default **7**), sums
   across the sensors per hourly bucket, and projects the result onto 24
   local-time hour-of-day slots keeping the maximum per slot. That gives the
   **7-bucket hour-of-day max** the integration is built around (7 samples
   per slot by default).
2. **Every minute**, the spline is evaluated at the current minute-of-day.
   `shift_minutes` translates the curve; positive values delay the peak,
   negative advance it.
3. **Every minute**, the trim-factor auto-adjust loop:
   - skips if predicted < `min_pv_update` (pre-dawn / post-dusk);
   - skips if the battery is near full **and** household load is low
     (curtailment guard, uses `soc_threshold`, `soc_deadband`, `load_deadband`);
   - skips if the actual-vs-predicted gap, in watts, is below
     `recovery_deadband`;
   - otherwise nudges `factor += rate × (actual_pv / predicted − factor)`,
     using `up_rate` (default 0.7) when we need to raise the factor and
     `down_rate` (default 0.1) when we need to lower it.
4. **At sunrise**, the factor is reset from the current weather condition via
   a built-in map (`sunny → 1.0`, `cloudy → 0.6`, …). Unknown conditions use
   `sunrise_fallback` (default 0.85).

## Requirements

- Home Assistant **2024.11.0** or newer (uses the modern `OptionsFlow`
  auto-populated `config_entry` attribute).
- The **Recorder** integration with long-term statistics enabled (default).
  Each configured PV sensor must have `state_class: measurement` so HA
  records hourly `max` statistics for it.
- A weather entity with standard condition states (`sunny`, `cloudy`, etc.).
- A battery SOC sensor (percentage) and a household load power sensor.

## Installation

### Option A — HACS (recommended)

1. In HACS, open **Integrations → ⋮ → Custom repositories**.
2. Add this repo's URL, category **Integration**.
3. Click **Install** on *Solar PV Prediction (History Based)*.
4. Restart Home Assistant.
5. **Settings → Devices & services → Add integration** → search
   *Solar PV Prediction*.

### Option B — Manual

1. Copy `custom_components/solar_pv_prediction/` into
   `<config>/custom_components/solar_pv_prediction/`.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration** → search
   *Solar PV Prediction*.

## Configuration

### Initial setup (user step)

| Field | Notes |
| --- | --- |
| **Name** | Appears in the device name and as the entity prefix. |
| **PV power sensors** | Multi-select. Only `device_class: power` sensors are shown. Pick one or many (string-level, inverter-level, whatever you record). |
| **Battery SOC sensor** | Percentage. |
| **Load power sensor** | `device_class: power`. |
| **Weather entity** | Any `weather.*` entity. |

### Options — Basic

| Option | Default | Range | Purpose |
| --- | --- | --- | --- |
| `history_days` | 7 | 1–60 | How many days of recorder history feed each hour-of-day bucket. |
| `shift_minutes` | 0 | −180..+180 | Shift the forecast curve (positive = later peak). |
| `max_pv_clamp` | 10000 | ≥ 0 | Watts cap on the hourly combined sum. Set `0` to disable. |

### Options — Advanced

| Option | Default | Range | Purpose |
| --- | --- | --- | --- |
| `tension` | 0.5 | 0.0–1.0 | 0 = smooth Catmull-Rom; 1 = hugs the hourly steps. |
| `up_rate` | 0.7 | 0.0–1.0 | Per-minute gain when raising the trim factor. |
| `down_rate` | 0.1 | 0.0–1.0 | Per-minute gain when lowering it. |
| `soc_deadband` | 2.0 % | 0–50 | Widens the SOC threshold for the curtailment guard. |
| `load_deadband` | 100 W | ≥ 0 | Loads below this count as "low" for the curtailment guard. |
| `recovery_deadband` | 50 W | ≥ 0 | Actual-vs-predicted gaps below this W are ignored. |
| `soc_threshold` | 95 % | 0–100 | SOC at/above which PV may be curtailed by the inverter. |
| `min_pv_update` | 50 W | ≥ 0 | Predicted below this disables auto-adjust (twilight). |
| `sunrise_fallback` | 0.85 | 0.05–1.5 | Factor used at sunrise when weather is unknown. |

### Built-in weather → factor map (sunrise reset)

```
sunny / clear-night   1.00        pouring              0.25
partlycloudy          0.85        rainy                0.45
cloudy                0.60        snowy / snowy-rainy  0.30
fog                   0.50        windy                0.80
hail                  0.30        windy-variant        0.75
lightning             0.40        exceptional          0.50
lightning-rainy       0.35
```

Unmapped conditions fall back to `sunrise_fallback`.

## Testing / verifying it works

A short checklist to validate the installation in order of increasing depth.

### 1. Entities show up

After adding the integration you should see, under
**Settings → Devices & services → Solar PV Prediction → <your name>**:

- 3 sensors (all in watts, `device_class: power`)
- 1 number (Trim Factor)

All four share one device, so they group cleanly in the UI.

### 2. Raw PV Max populates

Open `sensor.<name>_pv_max` and check the **attributes**. You should see 24
keys `hour_00 … hour_23` populated from history. If they're all 0:

- Confirm each PV sensor has `state_class: measurement` in developer tools.
- Confirm the recorder has been running long enough to have hourly
  statistics (HA compiles them at the top of each hour).
- Try lowering `history_days` to 1 temporarily to confirm fresh data is
  fetched.

### 3. Spline curve looks right

Build a quick lovelace card plotting the smoothed sensor over 24 h:

```yaml
type: custom:apexcharts-card
graph_span: 24h
series:
  - entity: sensor.<name>_pv_max
    type: column
    group_by:
      func: raw
      duration: 1h
  - entity: sensor.<name>_pv_max_spline_smoothed
    type: line
    stroke_width: 2
```

You should see a smooth bell-shaped curve tracing over the hourly bars. Play
with `tension` (smoother vs. tighter fit) and `shift_minutes` to verify the
whole curve slides.

### 4. Trim factor persists

1. Move `number.<name>_trim_factor` to 0.42.
2. Restart Home Assistant.
3. Verify the entity comes back as 0.42 (RestoreNumber in action).

### 5. Auto-adjust responds

Pick a mid-day minute where predicted is well above `min_pv_update`:

- If actual PV is currently *higher* than `pv_max_spline_smoothed × factor`,
  you should see the trim factor drift **up** over the next several minutes
  (default `up_rate=0.7` gets you there quickly).
- If actual is *lower*, you should see it drift **down** more slowly
  (`down_rate=0.1`).
- When SOC ≥ `soc_threshold − soc_deadband` **and** load < `load_deadband`,
  the factor should **stop** moving down — the curtailment guard is
  protecting it.

### 6. Sunrise reset fires

Enable debug logging (below). At the next local sunrise you should see a log
line like:

```
DEBUG ... Trim factor -> 0.850 (source=sunrise)
```

…and the Number entity's value in the UI should jump to match the weather
condition at sunrise.

### 7. Enable debug logging

Add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.solar_pv_prediction: debug
```

Then tail the log and watch for `Trim factor -> …` and `Updated hour-of-day
max map: …` lines. Restart (or call `logger.set_level`) to apply.

## Reconfiguring

**Settings → Devices & services → Solar PV Prediction → Configure** re-opens
the options flow (basic → advanced). Saving triggers a reload of the entry,
so changes take effect within a second or two.

To change the *entities* (PV/SOC/load/weather) or the instance name, remove
the entry and add a new one.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `pv_max` stuck at 0 for all hours. | PV sensors lack `state_class: measurement`, or recorder hasn't captured an hour yet. |
| Spline flat / always 0. | Same as above — spline reads from `pv_max` data. |
| Trim never moves from 1.0. | Predicted < `min_pv_update`, or every tick hits the curtailment guard or the recovery deadband. Lower `recovery_deadband` to 10 W temporarily to confirm. |
| Factor drifts erratically at low sun angles. | Raise `min_pv_update` (e.g. to 100 W). |
| Sunrise reset seems wrong. | Your weather entity may report a non-standard condition string; check the map in the README. |

## Layout

```
custom_components/solar_pv_prediction/
├── __init__.py          # entry setup, shared hass.data dict
├── manifest.json
├── const.py             # keys + defaults + weather map
├── config_flow.py       # user step + basic/advanced options flow
├── coordinator.py       # hourly recorder fetch -> 24-slot HoD max
├── spline.py            # cubic Hermite with tension + shift
├── trim.py              # TrimManager: sunrise reset + per-minute adjust
├── sensor.py            # 3 sensors
├── number.py            # Trim Factor RestoreNumber
└── translations/
    └── en.json
hacs.json
info.md
README.md
LICENSE
```

`hass.data[DOMAIN][entry.entry_id]` holds the three shared objects:
`coordinator`, `spline`, `trim`.

## License

MIT. See [LICENSE](LICENSE).
