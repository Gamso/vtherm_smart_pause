# vtherm_smart_pause: development specification

> Why the plugin exists and the data audit are in the [study](docs/study.md). This document says
> **what to build and how to check it**. Points marked **[TO CHECK]** or **[NOT VERIFIED]** have not
> been confirmed in the code or on hardware: clear them before the step that depends on them.

## 1. Goal and scope

**Goal**: stop the equipment of an `over_climate` VTherm when the room does not need it and will
hold long enough without it, then hand it back before comfort is at stake.

**Success criterion** (on the dataset and the bench, §11): capture at least 70 % of the energy of
the "room ≥ setpoint + 0.2 °C" episodes (7.0 kWh over the reference period) without ever letting the
room fall below `setpoint − margin`.

**In scope**
- **Any heat pump or air conditioner** behind an `over_climate` VTherm, whatever the brand or the
  integration: nothing may depend on an entity or a behaviour specific to one manufacturer.
- `heat` **and** `cool` modes from the first version (mirrored), one VTherm per config entry.
- A setpoint threshold per `hvac_mode`, taken from HomeShift, that picks the profile (eco /
  comfort).
- A learned heat-loss model, the weather forecast, optional equipment entities.

**Out of scope**
- Reading a schedule.
- Fixing the regulation when the room lacks heat (cycles below the setpoint, study §1.1): that is
  VTherm's self-regulation's job.
- Anything but `over_climate` thermostats.

## 2. Platform

- Home Assistant **2026.11** or later.
- `vtherm_api` **≥ 0.9.0**. Not declared in `manifest.json` (VTherm installs it), pinned in
  `requirements_test.txt` (`>=0.9.0,<1.0`).
- Python: Home Assistant's (3.14). No external dependency (no numpy, no pandas).
- Layout, tooling and conventions from `vtherm_mpc_fan`: `black` (180 columns), `pylint`, pytest,
  hassfest, HACS action.

## 3. Architecture

A VTherm **feature manager** plugin, same skeleton as `vtherm_mpc_fan`.

| Module | Role | Depends on HA? |
|---|---|---|
| `__init__.py` | factory registration, config entry lifecycle | yes |
| `factory.py` | `supports()`: `over_climate` and a config entry targeting this VTherm | yes |
| `registry.py` | `hass.data` rendez-vous between manager and platforms, by VTherm `unique_id` | yes |
| `manager.py` | `SmartPauseFeatureManager`: `refresh_state()` on every VTherm cycle, inputs, learning, setpoint modifier, entities | yes |
| `decision.py` | pure decision engine (§5): state + inputs → action + reason | **no** |
| `thermal_model.py` | heat-loss and recovery model, learning, (de)serialisation (§6) | **no** |
| `activity.py` | "something to save" from the optional entities | **no** |
| `solar.py` | sun elevation and sun factor | **no** |
| `forecast.py` | `weather.get_forecasts` reading and cache (§7) | yes (thin layer) |
| `config_flow.py` | config and options (§4) | yes |
| `entity.py` / `sensor.py` / `switch.py` | entities (§9) | yes |
| `log.py` | `vtherm_api`'s `VThermLogger`, with a `logging` fallback | yes |
| `const.py` | constants | no |
| `bootstrap.py` *(planned)* | model bootstrap from the recorder history (§6.4) | yes |
| `data_collection.py` *(planned)* | CSV export in the replay format (§11.1) | yes |

The pure modules import neither Home Assistant nor `vtherm_api`, for the tests and the bench.

## 4. Configuration

### 4.1 Entry (one per VTherm)

| Key | Type | Default | Role |
|---|---|---|---|
| `target_vtherm` | `over_climate` VTherm | required | the equipment driven |
| `threshold_heat` | °C | empty | setpoint ≥ threshold → comfort profile in heating. Empty: always comfort |
| `threshold_cool` | °C | empty | setpoint ≤ threshold → comfort profile in cooling. Empty: always comfort |
| `stop_mode` *(planned)* | `setback` / `inhibit` / `delegate` | `setback` | §8 |
| `setback_heat` / `setback_cool` | °C | the VTherm's `min_temp` / `max_temp` | setpoint sent in `setback` (VTherm clamps it to the device limits anyway) |
| `weather_entity` | `weather.*` | optional | forecast (§7). Without it, `T_out` stays at its current value |
| `power_entity` | W / kW sensor **or** Wh / kWh counter | optional | gain and the "something to save" condition (§5.3) |
| `compressor_entity` | on/off entity | optional | precise cycle detection |
| `defrost_entity` | on/off entity | optional | defrost or preheating: blocks decisions and learning |

`setback` is the default: validated on hardware (16 °C test, study §1.1), it leaves the VTherm in
`heat` / `cool` and the restart is immediate. `inhibit` really switches the device off, but shows the
VTherm as `off` and is not lifted by a user action (§8.2).

### 4.2 Options (per profile)

| Parameter | Eco | Comfort | Meaning |
|---|---|---|---|
| `excess` (δ) | 0.2 °C | 0.3 °C | minimum excess over the setpoint |
| `excess_minutes` | 10 | 20 | how long the excess must last |
| `margin` | 1.0 °C | 0.3 °C | comfort limit: `setpoint − margin` (heating) |
| `min_pause_minutes` | 30 | 60 | predicted autonomy needed for a pause |

Shared: `min_run_minutes` (15), `min_off_minutes` (20), `max_pauses_per_day` (8),
`outdoor_floor_heat` (−5 °C) / `outdoor_ceiling_cool` (40 °C), switch on.

The comfort profile was calibrated on the replay of the ten days (study §8): 0.3 °C / 20 min /
0.3 °C gives 6.2 kWh of gross energy while paused in 14 pauses, at most 3 a day, never closer than
0.15 °C to the limit. The eco profile is not calibrated yet: the dataset's nights are already handled
by VTherm's own auto start/stop.

Without a threshold, the **comfort** profile applies: the most prudent for a thermostat nobody has
tuned yet.

## 5. Decision engine (`decision.py`)

### 5.1 Inputs per cycle

`now`, the VTherm's effective `hvac_mode`, the user's setpoint, the room temperature, the outdoor
temperature, the forecast (outdoor temperature and sun per hour), the reason not to decide (window,
defrost, load shedding, plugin disabled), the activity of the equipment when an entity tells, the
model's readiness and the learned recovery time.

Always the **user's** setpoint, never the regulated one. Never VTherm's `hvac_action` (wrong during
81 % of the energy over the reference period).

### 5.2 State machine

```
             pause allowed (5.3)                    resume (5.4)
 RUNNING ─────────────────────────▶ PAUSED ──────────────────────────▶ RUNNING
 INACTIVE: not heat/cool, VTherm off, window, defrost, load shedding, plugin disabled
           (a pause in progress is released on entering it)
```

### 5.3 Pause: every condition

1. `hvac_mode` ∈ {heat, cool}. Not `INACTIVE`.
2. **Excess**: `error ≤ −δ` (error positive = need) continuously for `excess_minutes`.
3. **Autonomy**: `predicted autonomy ≥ max(min_pause_minutes, min_off_minutes + recovery + 10 min)`
   (§6.2).
4. **Something to save**: when a compressor or power entity is configured, activity in the last 30
   minutes (compressor running, or mean power > 30 W). Otherwise assumed.
5. **Guards**: `T_out` within the profile's bounds, `min_run_minutes` elapsed since the last resume,
   pauses today < `max_pauses_per_day`, model ready for the comfort profile (§6.3).

### 5.4 Resume: one condition is enough

1. Comfort: the room is already at `setpoint − margin / 2`.
2. A setpoint or mode change that removes the excess.
3. Prediction: the room will reach `setpoint − margin` within the learned recovery time plus
   10 minutes, once `min_off_minutes` has passed.

### 5.5 Manual takeover

In `setback`, the user takes over with the switch, a setpoint rise or a mode change. With
`inhibit` *(planned)*, user actions are stored by VTherm and not applied, so the plugin must detect
them (`requested_hvac_mode`, `target_temperature`) and lift the inhibition, and offer a "resume now"
action.

### 5.6 Output

`Decision(state, action: PAUSE | RESUME | None, reason, profile, autonomy_minutes, limit)`, logged
on every cycle at DEBUG, and as a VTherm "NEW EVENT" line on every pause and resume.

## 6. Thermal model (`thermal_model.py`)

### 6.1 Quantities

| Quantity | Model | Learned on |
|---|---|---|
| `k` (1/h) | `dT/dt = k · (T_out − T_in) + g · S` while stopped | stretches with the equipment stopped, ≥ 2 h |
| `g` (°C/h at full sun) | same, `S` ∈ [0, 1] estimated sun (§7) | daytime stops |
| recovery | time from a restart to the worst reading | every resume |

Each HVAC mode keeps its own segments (internal gains differ between seasons). A mode without any
borrows the others'.

### 6.2 Autonomy

Integration in 5-minute steps along the hourly forecast until `setpoint − margin` is crossed or a
6-hour horizon (returns 360). An observed slope already heading for the limit bounds the result.
Without a forecast, `T_out` is constant and the sky counts as half covered.

### 6.3 Learning

- Samples: stretches with the equipment **really** stopped (VTherm `off` other than for a window, or
  paused by the plugin while the compressor entity, when configured, confirms it is idle), without
  defrost, the first 30 minutes dropped (the unit's residual heat).
- Each stretch is reduced to three integrals (temperature change, exposure `∫(T_out − T_in) dt`,
  sun `∫S dt`). `K` is the weighted median of the per-segment ratio over sunless segments, `g` the
  weighted median of the residual over sunny ones. Robust to 0.2 °C sensor steps, no numerical
  library.
- Sensor resolution detected from the readings. A stretch under 3 h must move at least three steps.
- **Cold start**: `k = 0.08 /h` (prudent, high loss), `g = 0`. Comfort profile only from 3 sunless
  segments.
- Retention: 30 days, plus the 20 newest segments whatever their age.

### 6.4 Bootstrap from history *(planned)*

At install time, `bootstrap.py` reads the recorder (VTherm, outdoor, power if any) and extracts the
stop stretches. On the reference period that already gives about 15 usable nights
(`k ≈ 0.03-0.05 /h`).

### 6.5 Persistence

`homeassistant.helpers.storage.Store`, key `vtherm_smart_pause.<config entry id>`: model, engine
state (a pause survives a restart), switch, avoided energy.

## 7. Forecast and sun (`forecast.py`, `solar.py`)

- `weather.get_forecasts` (`type: hourly`), refreshed every 30 min. On failure the last forecast is
  kept for 3 h, then dropped.
- Fields: `datetime`, `temperature`, `cloud_coverage` (%). Otherwise `condition` mapped onto a cloud
  cover (`sunny` 0, `partlycloudy` 50, `cloudy` 90…). **[TO CHECK]** the fields the user's
  Météo-France entity actually provides.
- The forecast is shifted onto the current outdoor reading (removes the station bias, keeps the
  trend).
- `S = max(0, sin(sun elevation)) × (1 − 0.75 · clouds / 100)`. Elevation computed in `solar.py`
  (NOAA approximation), not through `homeassistant.helpers.sun`, whose helpers keep moving.

## 8. Actions

| `stop_mode` | Pause | Resume | Visible state |
|---|---|---|---|
| `setback` | `adjust_regulated_temperature()` returns `setback_heat` / `setback_cool` | returns `None` (VTherm takes its setpoint back) | VTherm stays in `heat` / `cool`. `Regulated_target_temperature` shows the setback |
| `inhibit` *(planned)* | `VThermAPI.set_inhibition(entity_id, stop_underlyings=True)` | `unset_inhibition(entity_id)` | VTherm shown `off`, `hvac_off_reason = hvac_off_inhibition` |
| `delegate` *(planned)* | turns `switch.<vtherm>_enable_auto_start_stop` on (eco profile only) | turns it off in the comfort profile | VTherm's |

What follows was read in the code of `vtherm_api` 0.9.0 and VTherm (`main`, 10.6.4) on 11/11/2026.

### 8.1 `setback`: setpoint modifier

- `InterfaceSetpointModifier.adjust_regulated_temperature(desired, regulation_step, command_cause)
  -> float | None`, **synchronous**. VTherm detects it with `hasattr` on the managers created **by a
  factory** (`register_feature_manager`), and only when `is_configured` is true.
  `register_manager` is not enough.
- Called by `ThermostatOverClimate._send_regulated_temperature`, in `heat` / `cool` only. Order:
  self-regulation → rounding to the step → **modifier** → device offset → rounding → `dtemp` filter
  → `set_temperature`. The returned value becomes `regulated_target_temperature`. Several modifiers
  chain in the alphabetical order of their factory names.
- `None` keeps the setpoint. A number replaces it. `NaN`, a boolean, a coroutine or an exception are
  ignored with a warning.
- The send may be **deferred**: a change below `dtemp` is swallowed, the self-regulation period is
  respected (20 s delay). v0.1.0 lets the pause take effect on VTherm's next regulation, within one
  cycle.
- Also called when self-regulation is "none" **[NOT VERIFIED]**: to test, otherwise document that
  `setback` needs an active self-regulation.
- Before an `off` → `heat`/`cool` switch, VTherm sends the regulated setpoint first (modifiers
  included) so the device does not start on its stored setpoint.
- A change made on the device itself (`command_cause = "underlying_tracking"`) is left alone.

### 8.2 `inhibit`: inhibition

- `set_inhibition(id, True)`: the VTherm goes `off` (`hvac_off_reason = hvac_off_inhibition`), one
  stop command is sent to the device, then **every** command to the device is blocked. With
  `False`, nothing is sent: the device keeps running. Useless here.
- A second `set_inhibition` during an inhibition does **nothing**, even with another
  `stop_underlyings`.
- `unset_inhibition`: VTherm sends the state **requested by the user** (mode, preset, setpoint),
  applying the windows, safety, central mode and native auto start/stop of the moment.
- **User actions do not lift the inhibition**: they are stored and applied when it is lifted. The
  VTherm stays shown `off`.
- Inhibition wins over windows, load shedding, safety, central mode and the native auto start/stop.
- **Home Assistant restart**: the state is persisted (`specific_states.inhibition`) and restored.
  Before the restore, `set` / `unset` raise `InhibitionNotStartedError`: wait for
  `is_inhibition_started()`. The entry stop is not replayed once marked done, even if it failed. A
  device switched back on by someone during the restart is not corrected.
- No caller check, no dedicated event. An example plugin exists:
  [alexlenk/vtherm_inhibition](https://github.com/alexlenk/vtherm_inhibition).

### 8.3 Choice

`setback` by default: the VTherm keeps its mode, a user action takes over naturally, and VTherm only
sees a setpoint change. `inhibit` is for devices that still draw too much in thermo-off, or cannot go
low enough in setpoint.

**One decider**: in `setback` as in `inhibit`, the plugin should switch the native auto start/stop
off while it is active and restore it on unload. Otherwise, after an inhibition is lifted, the native
auto start/stop may send the VTherm back to `off`. v0.1.0 only logs a warning when it is enabled.

**HomeShift migration**: HomeShift stops driving the switch of the VTherms managed by this plugin.
Its thresholds move here.

## 9. Entities and diagnostics

Each VTherm gets a device "‹VTherm› Smart Pause":

| Entity | Content |
|---|---|
| switch **Smart pause** | enables / disables the plugin for this VTherm |
| sensor **State** | `running` / `paused` / `inactive`. Attributes: reason, profile, limit |
| sensor **Autonomy** | predicted autonomy (min) |
| sensor **Heat loss coefficient** | learned `k` (1/h). Attributes: `g`, segments, sunless segments, readiness, recovery |
| sensor **Pauses today** | pauses since midnight |
| sensor **Avoided energy** | estimated avoided energy (kWh, `total_increasing`): mean power over the 30 min before each pause × its duration. Only with a power or energy entity |

Plus a `smart_pause` section in the VTherm's `extra_state_attributes`. Planned: the
`vtherm_smart_pause_event` event (`pause` / `resume`) and an optional CSV export (§11.1).

## 10. Coexistence

- **`vtherm_mpc_fan`**: during a pause it must neither decide nor learn. In `setback`, the regulated
  setpoint drops to the setback while the user's setpoint does not move: its slope samples store
  `regulation_offset` (regulated − user) and would be wrong. It must read
  `extra_state_attributes.smart_pause.state` on the VTherm and pause on `paused`. With `inhibit`, add
  `hvac_off_inhibition` to its pause reasons. `vtherm_mpc_fan` does not implement
  `adjust_regulated_temperature`, so there is no modifier conflict.
- **Native auto start/stop**: one decider (§8.3).
- **Window, load shedding, VTherm safety**: VTherm first. The engine goes `INACTIVE`.

## 11. Tests

### 11.1 Dataset

The past data is kept: it is both the replay set and the calibration of the bench.

- File: [`tests/data/replay_2026-11-01_2026-11-11.csv`](tests/data/replay_2026-11-01_2026-11-11.csv),
  one row per minute (14,904 rows, 1.1 MB), last known value at each minute.
- Neutral columns, no entity name or location: `time_utc, power_w, unit_hvac_mode,
  unit_sensor_temp, unit_setpoint, compressor_on, hot_adjust, vtherm_hvac_mode,
  vtherm_hvac_action, room_temp, user_setpoint, outdoor_temp, unit_outdoor_temp`. `outdoor_temp`
  comes from the weather station, `unit_outdoor_temp` from the outdoor unit's sensor (often 0 or
  unknown while it is stopped: not to be used for `k`).
- More periods (Home Assistant's export is limited to 10 days) through a planned
  `scripts/ha_history_to_replay.py <export.csv> <mapping.yaml> <output.csv>`, the mapping tying each
  neutral column to an entity and an attribute. Priority: a cold period (< 5 °C) and a cooling
  period.
- The planned `data_collection.py` writes the same format, so the plugin's own data becomes replay
  sets directly.

### 11.2 Unit tests

- `decision.py`: each pause and resume condition on its own, profiles, heat/cool mirror, restore.
- `thermal_model.py`: recover known `k` and `g` from synthetic series quantized to 0.2 °C. Autonomy
  against the analytic solution, cold start, retention, persistence.
- `forecast.py` / `activity.py`: missing fields, service failure, `condition` mapping, power,
  energy counter, compressor.
- Home Assistant tests (core 2026.11): setup / unload, factory and `vtherm_api` protocols, manager,
  config and options flows, entities. A deprecation logged about this integration fails the test.
- `vtherm_mpc_fan` conventions: no `time.sleep`, injectable clock, a docstring on every test,
  `# noqa: SLF001` for protected-member access.

### 11.3 Replay (open loop)

On the dataset (`tests/test_replay.py`):
1. **Learning**: `k` from the nights in [0.02, 0.06] /h (measured: 0.030).
2. **Capture**: at least 5.5 kWh of gross energy while paused (measured: 6.2).
3. **Prudence**: the simulated room never reaches the comfort limit while paused.
4. **Compressor**: at most 20 pauses, 4 a day.

### 11.4 Closed-loop bench *(planned)*

A plant (`tests/closed_loop.py`, after `vtherm_mpc_fan`'s) calibrated on the data: heat loss `k` and
the dataset's `T_out`, solar gains, and a unit with an **internal thermostat** whose sensor reads
warmer than the room and that cycles in preheating at about 200 W once its setpoint is reached.
Scenarios: mild, cold, sunny, cooling, Home Assistant restart while paused, forecast unavailable.

Per-scenario measures, with ceilings in the tests: energy, compressor starts, time below
`setpoint − margin` (must be **zero**), minimum temperature reached. Compared with three references:
nothing, VTherm's auto start/stop alone, the plugin.

## 12. Steps

| # | Content | Status |
|---|---|---|
| 1 | Skeleton: factory, registry, config flow, manager, CI | **done** (v0.1.0) |
| 2 | `thermal_model.py`, `k` / autonomy sensors. Bootstrap from history | **done**, except the bootstrap |
| 3 | `forecast.py` | **done** |
| 4 | `decision.py` + replay | **done** |
| 5 | Pause by `setback`. Clear the **[NOT VERIFIED]** of §8.1 with a trial | code **done**. One-day heating trial **to do** |
| 6 | Entities, avoided energy. Event and CSV export | entities and energy **done**. Event and CSV **to do** |
| 7 | Closed-loop bench, eco profile calibration | to do |
| 8 | `inhibit`, `delegate`, switching the native auto start/stop off, HomeShift migration | to do |

## 13. Decisions

Name: `vtherm_smart_pause` (Home Assistant domain, folder and entity prefix). Any brand of heat pump
(§1). Cooling from the first version (§1). Without summer data, cooling is validated on the bench by
mirroring heating, then on a real export next summer.
