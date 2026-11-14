# vtherm_smart_pause: preliminary study

> This is the study that led to the plugin: the need, what Versatile Thermostat (VTherm) already
> does, what a plugin can add and how to build it. What was built is described in the
> [README](../README.md), how it is built in the [specification](../SPEC.md).

## 1. The observation behind the project

11 November 2026, living room, a Mitsubishi MSZ-AY35VGK heat pump driven by an `over_climate`
VTherm (setpoint 20 °C), VTherm's auto start/stop disabled while someone is home:

| Observation | Source |
|---|---|
| From about 08:00 to 15:15 the compressor starts every 7-8 min for 3-4 min, at 250-400 W | `Operating`, `Power Consumption` |
| Every start happens **entirely in Hot Adjust** (the anti-cold-draft preheating): the indoor fan never starts, the louvres stay closed | `Hot Adjust` in step with `Operating` |
| The unit's own sensor saws between 20 and 21 °C while the room stays at 20.2-20.6 °C | unit climate / room sensor curves |
| VTherm reports `idle` (`hvac_action`) from about 07:45 to 17:50. The unit's entity stays `heating` the whole time | history |
| VTherm's self-regulation had already lowered the setpoint sent to 19.5 °C around 12:00: the cycles went on, slightly weaker | unit setpoint |

**Diagnosis**: the normal behaviour of the unit's internal thermostat, fooled by its own sensor,
which warms up in the still air of the unit. Estimated by eye: **about 120-140 W on average**, or
**about 0.8 to 1 kWh** over the day, for heat the room did not need, produced at poor efficiency
(short starts).

### 1.1 Audit over 10 days (1 → 11 November 2026)

Data: a Home Assistant history export (power, `Operating`, `Hot Adjust`, the unit's climate, the
VTherm, the outdoor temperature), resampled to one row per minute in
[`tests/data/replay_2026-11-01_2026-11-11.csv`](../tests/data/replay_2026-11-01_2026-11-11.csv)
(neutral column names, no entity name or location). Mild weather: 8 to 16 °C daily mean.

**Consumption**: 28.2 kWh over 9.5 heating days (about 3 kWh a day), 121 h in heating mode, 622
compressor starts.

**Short-cycling episodes** (at least 3 runs of 10 min or less, at most 20 min apart): 33 episodes,
66 h, **11.3 kWh, 40 % of the consumption**, at 120-240 W on average.

| Room during the episode (mean) | Duration | Energy | Reading |
|---|---|---|---|
| ≥ setpoint + 0.2 °C | 45 h | **7.0 kWh** (25 %) | heat the room does not need: **the plugin's target** |
| between the setpoint and + 0.2 °C | 11 h | 2.2 kWh | holding at equilibrium: leave it alone |
| below the setpoint | 10 h | 2.1 kWh | the unit cycles while the room **lacks** heat (its sensor reads 0.4 to 0.9 °C above the room): a regulation problem, not a pause |

The largest: 10/11, 14 h, 119 starts, 1.9 kWh, the room going from 20.4 to **21.0 °C** for a 20 °C
setpoint. 08/11, 8.7 h, 1.3 kWh. 07/11, 6.8 h, 1.1 kWh. The "room ≥ + 0.2 °C" episodes add up to
**about 370 starts, 60 % of all the starts of the period**.

**What VTherm's auto start/stop alone would do** (a stop at a projected setpoint + 0.5 °C): the
energy drawn with the room at + 0.4 °C or more is only 3.8 kWh, all runs included. It would capture
at best half of the target.

**Nights are already handled**: at midnight the setpoint drops to 17.5 °C and the auto start/stop
switches the unit off until the morning. The plugin adds nothing at night.

**Heat loss with the unit off** (nights with the unit off, fit of `ln(T_in − T_out)`):
`k ≈ 0.03 to 0.05 /h`, a drop of 0.25 to 0.35 °C/h at 3-9 °C outside. Losing 0.7 °C therefore takes
about 2 h at 5-10 °C outside, 3-4 h at 15 °C. Daytime stretches do not fit without a solar term: the
room hardly drops.

**Expected gain**:
- Upper bound 7 kWh in 9.5 days, about 0.7 kWh a day. In practice 0.4-0.6 kWh a day (delay before
  the pause, restarts).
- Over a season, about 60-100 kWh, **about 15-25 € a year** at 0.25 €/kWh, less in cold weather
  (less excess).
- **About 60 % fewer compressor starts**: the strongest argument (wear, noise).

**Side findings**:
- VTherm reports `idle` during **81 %** of the energy drawn (22.8 kWh), including long runs with the
  fan blowing below the setpoint: its `hvac_action` is useless here.
- The gap between the room and the unit's sensor ranges from −0.9 to +0.6 °C depending on the
  situation: not a fixed offset a constant correction could compensate.
- **16 °C test** (11/11, 18:21): it falls one minute before the end of the export, so it is not in
  the data. The observation (the compressor no longer runs) confirms that a low setpoint is enough
  to put the unit in thermo-off: the setback pause (§4.4) works mechanically.

**Verdict**: the financial interest is modest (about 15-25 € a year for this home and this
weather). The real interest is to **remove most of the useless short starts**. The bill alone does
not justify it.

## 2. What already exists

### 2.1 VTherm's auto start/stop

(Read in VTherm's `auto_start_stop_algorithm.py` and `feature_auto_start_stop_manager.py`.)

- `over_climate` thermostats only, without valve regulation. Enabled by
  `switch.<vtherm>_enable_auto_start_stop`. Level (very slow / slow / medium / fast) set **only in
  the configuration**, no service changes it.
- Rule, in heating:
  - **Stop** when the accumulated error (time-weighted, clamped to ±20 / 10 / 5 / 2 depending on
    the level) reaches `-threshold` **and** the temperature projected at `dt` (60 / 30 / 15 / 7 min)
    is **≥ setpoint + 0.5 °C**.
  - **Restart** when the projected temperature is **≤ setpoint − 0.5 °C**.
  - At least `dt` minutes between two switches. Mirrored in cooling. Nothing outside heat/cool.
- It only restarts what it stopped itself (`hvac_off_reason = "hvac_off_auto_start_stop"`), starts
  over on every setpoint or preset change, and fires `versatile_thermostat_auto_start_stop_event`
  (`start` / `stop`).
- Configurable stop mode (`select.<vtherm>_auto_start_stop_stop_mode`): off, fan only, dry.

**Consequence here**: with a fixed **+0.5 °C** gap, merely enabling the switch would have stopped
the unit only after about 12:50 (room at 20.6 °C), and only once the accumulated error was full,
around 14:00 at "very slow". The cycles from 08:00 to 12:50 (room at 19.8-20.4 °C) would not have
been touched: **about 0.1 to 0.2 kWh** saved that day. Driving the switch alone is not enough.

### 2.2 Power on the VTherm side

(Read in `thermostat_climate.py`, `feature_power_manager.py`, `sensor.py`.)

VTherm **does not measure** the equipment's power: it **estimates** it from a configured nominal
power (`device_power`) and the underlying's `hvac_action` (heating, cooling, fan or drying = full
power). There is no setting to give it a real power sensor of the equipment (VTherm's power sensors
are the home's, for load shedding).

On this unit, whose entity stays `heating` the whole time, the estimate would be the nominal power
all day: it does **not** show the useless cycles.

### 2.3 Driving the switch on the setpoint (HomeShift)

The HomeShift integration drives the `enable_auto_start_stop` switch from the climate's
**setpoint**: one threshold per thermostat mode (heating: setpoint ≥ threshold → switch off, the
equipment keeps running. Cooling / ventilation: setpoint ≤ threshold). It acts on transitions only
(mode change, setpoint crossing the threshold, Home Assistant start): a manual flip holds until the
next transition.

This threshold **moves into the plugin** (§3), where it picks the profile. HomeShift stops driving
those switches.

### 2.4 What `vtherm_api` lets a plugin do

(`vtherm_api` 0.9.0, 9 November 2026.)

| Need | Means |
|---|---|
| Be called on every VTherm cycle with setpoint, temperature, slope, outdoor temperature, `hvac_off_reason` | Feature manager (`InterfaceThermostatRuntime`), as `vtherm_mpc_fan` does |
| Stop and give control back | `VThermAPI.set_inhibition(vtherm_entity_id, stop_underlyings)`, `unset_inhibition(...)`, `is_inhibition_started(...)` (`hvac_off_reason = "hvac_off_inhibition"`) |
| Change the setpoint sent to the underlying | `InterfaceSetpointModifier.adjust_regulated_temperature(desired, regulation_step, command_cause) -> float \| None`, implemented by the feature manager (detected with `hasattr`) |
| Be told about commands sent to the underlying | hooks `async_before_underlying_hvac_mode`, `async_on_underlying_power`… |
| Read or change the level of VTherm's auto start/stop | **No**. Only its Home Assistant switch |
| Change the VTherm's `hvac_mode` | **Not** on the runtime (only the `set_hvac_mode` service) |

How inhibition and the setpoint modifier behave was then read in VTherm's code (§2.5).

### 2.5 Setpoint modifier and inhibition, as VTherm implements them

(Read in `vtherm_api` 0.9.0 and VTherm 10.6.4.)

**Setpoint modifier.** `adjust_regulated_temperature(desired, regulation_step, command_cause)` is a
plain function, asked only of managers created by a registered factory
(`register_feature_manager`) whose `is_configured` is true. VTherm calls it in heat and cool only,
after its self-regulation and before the device offset, the rounding to the step and the
minimum-change filter. `None` keeps VTherm's value, a number replaces it and becomes
`regulated_target_temperature`. VTherm computes and sends it before refreshing its plugins, so
the plugin asks for a forced control cycle on a pause or a resume: the setpoint reaches the
equipment at once instead of a cycle later.

**Inhibition.** `set_inhibition(id, stop_underlyings=True)` shows the VTherm as `off`
(`hvac_off_reason = hvac_off_inhibition`), sends one stop command and then blocks every command to
the device. With `False` nothing is sent and the device keeps running. A second call while inhibited
does nothing. User actions do not lift it: they are stored and applied by `unset_inhibition`, which
restores the user's requested state. It wins over windows, load shedding, safety and VTherm's own
auto start/stop, survives a restart (wait for `is_inhibition_started()`), and its entry stop is not
replayed after a restart once marked done.

**Choice**: setback first. The VTherm keeps its HVAC mode, a user action takes over naturally, and
VTherm only sees a setpoint change. Inhibition stays for units that still draw too much in
thermo-off.

## 3. Decisions

| Question | Decision |
|---|---|
| Who owns the setpoint threshold? | **The plugin.** HomeShift stops driving the switch |
| Minimum `vtherm_api` | **The latest** (0.9.0): inhibition and the setpoint modifier |
| Unit-specific entities | **Not read** in the end: not every installation has them (§5) |
| Source of solar gains | **Weather forecast** (e.g. Météo-France) |
| Read the schedule? | **No.** If the setpoint goes to comfort and everything shows the unit is not needed, it is paused anyway |
| Brands | **Any heat pump or air conditioner** behind an `over_climate` VTherm |
| Cooling | **In the first version** |

## 4. Functional proposal

### 4.1 One decision engine, two profiles

Rather than two mechanisms (VTherm's switch on the eco side, own logic on the comfort side), one
engine decides. The per-`hvac_mode` **setpoint threshold** only picks the **profile**:

| Profile | When | Behaviour |
|---|---|---|
| Eco | setpoint on the eco side of the threshold | wide margins, pause as soon as it is safe |
| Comfort | setpoint on the comfort side | tight margins: pause only on a clear excess and a long autonomy |

### 4.2 Pause conditions

The plugin pauses when **all** of these hold:

1. **Excess**: room ≥ setpoint + `δ` (profile) for at least the excess duration.
2. **Enough autonomy**: the model (§6) predicts the room will take longer than the minimum pause to
   reach `setpoint − margin`, given the outdoor temperature and the solar gains **forecast** over
   that horizon.
3. **Something to save** (when an entity tells, §5): the unit draws power or cycles while the room
   does not need it. Without an entity this is assumed.
4. **Nothing against it**: no defrost or open window, outdoor temperature above a floor, minimum run
   time elapsed, daily pause count below a maximum.

### 4.3 Resume conditions

As soon as the model announces the room will reach `setpoint − margin` within the learned
**recovery time**, on any setpoint rise that creates a real need, or when the plugin is turned off.
The minimum pause is kept unless comfort requires otherwise.

### 4.4 Means of action (`vtherm_api` 0.9.0)

| Pause mode | Means | For |
|---|---|---|
| **Setback** | `adjust_regulated_temperature` returns a setpoint low enough for the unit to go thermo-off without being switched off | immediate restart, no cold compressor off/on. Treats the observed problem directly (an internal thermostat fooled by its sensor) |
| **Full stop** | `set_inhibition(..., stop_underlyings=True)` / `unset_inhibition` | maximum gain on units that still draw power in thermo-off |
| Delegation | the `enable_auto_start_stop` switch | eco profile only |

### 4.5 Other points

- **Compressor protection**: minimum run and pause durations, maximum pauses per day.
- **Cooling**: mirrored logic.
- **Measuring the gain**: estimated avoided energy, pause count, minimum temperature reached during
  pauses (comfort check).
- **Diagnostics**: the decision's reason as an attribute, log lines, CSV export.
- **Coexistence with `vtherm_mpc_fan`**: during a pause, the MPC must neither decide nor learn.

## 5. Optional entities: how useful are they?

> **Outcome**: the first release reads none of them. Not every installation exposes a power
> sensor, a compressor state or a defrost state, so the plugin was kept free of any such
> setting. This section records what they would have added.

The heart of the decision (excess and autonomy) only uses the room temperature, the setpoint, the
outdoor temperature and the forecast. The unit's entities serve three purposes:

| Use | No entity | **Delayed** energy / power (cloud integrations, hourly counter) | **Real-time** state (`Operating`, `Hot Adjust`, instant power) |
|---|---|---|---|
| Knowing whether there is a gain (condition 3) | assumed: a unit already in thermo-off may be paused, no gain but no harm beyond the restart | over 30-60 min: "it draws power while the room is above" | precise: short cycles, duty cycle, `Hot Adjust` on every run |
| Measuring the gain | rough (nominal power × pause duration, overestimated) | good, to the hour | good |
| Learning | pauses made by the plugin and nights in `off` | same | same, plus excluding defrosts |

**Conclusion**: without an entity the plugin works and protects comfort, but sometimes pauses for
nothing and cannot measure its gain. Even a delayed energy counter gives the essential: whether
**there is something to save**. Real time only adds precision. VTherm's estimated power (§2.2) does
not help: it follows `hvac_action`, which stays `heating` on this kind of unit.

## 6. The thermal model

### 6.1 What to learn

1. **Heat loss while stopped**, Newton's law:
   `dT/dt = k · (T_out − T_in) + g · sun`,
   hence the autonomy down to `T_min = setpoint − margin` (without gains):
   `t = ln((T_in − T_out) / (T_min − T_out)) / k`.
   `k` (1/h) is learned on the stretches where the unit is stopped (plugin pauses, nights in `off`),
   `g` on the same stretches by day, against the forecast or estimated sun.
2. **Recovery time**: how long the room keeps drifting after a restart, to resume in time.

At 20 °C inside, 10 °C outside and `k = 0.05 /h` (an example value), the room loses about
0.5 °C/h: going from 20.6 to 19.7 °C takes about 1 h 50. At 0 °C outside, about 55 min. That is
what sets a safe pause length.

### 6.2 The weather forecast

Home Assistant's `weather.get_forecasts` service (`hourly`) gives the outdoor temperature and,
depending on the integration, the cloud cover or the condition. The forecast integrates `T_out` and
the sun **over the pause horizon**, not just now. A clear-sky estimate from the sun's elevation,
weighted by the cloud cover, is a good start.

### 6.3 Cold start

Until `k` is learned: eco profile only, with a prudent `k` (high loss) and wide margins. The
comfort profile only comes in with a `k` measured over enough stops.

## 7. Intelligent Heating Pilot (IHP): what to reuse?

[RastaChaum/Intelligent-Heating-Pilot](https://github.com/RastaChaum/Intelligent-Heating-Pilot)
(v0.7.1) starts a VTherm's preheating early enough to reach the next slot's setpoint on time.

**What it learns**: the **heating slope** (°C/h, global and per hour of the day) and the **dead
time**, on cycles found in the recorder history. No machine learning: plain means, a 2 °C/h default,
the ML path is a stub. The humidity and sun corrections are hand-set coefficients. The outdoor
temperature is recorded but unused. The cloud cover is the current value, not a forecast.

**What does not fit**: IHP only models **warming up**. It has no heat-loss model, no time constant
and no forecast, exactly what this plugin needs (§6.1).

**What can be reused (ideas, not code)**:

| IHP idea | Use here |
|---|---|
| Cycle detection in the recorder history, tolerant of short interruptions (10 min) | bootstrap `k` from the existing history at install time, instead of waiting weeks |
| Filters: disturbance presets excluded, minimum duration, flat cycles excluded | filter the stop stretches (window, defrost, setpoint change) |
| Anticipation formula `dead time + ΔT / slope` | recovery time for the resume (§4.3) |
| Storage through `homeassistant.helpers.storage.Store`, one key per entry | model persistence |

**A better source in-house**: `vtherm_mpc_fan` already learns the dead time per mode and robust
slopes, with a closed-loop bench. For `k` and `g`, its tools are sturdier than IHP's means.

IHP's licence: MIT according to its `pyproject.toml` and README, but no `LICENSE` file was seen at
its root: check before copying code.

## 8. Calibration of the comfort defaults

The comfort profile was calibrated by replaying the ten days of §1.1 through the decision engine
and the model (`tests/replay.py`). While paused, the replayed room follows the model instead of the
recorded room, which still had the unit's heat. The energy is the **gross** energy drawn during the
pauses, an upper bound of the saving: part of that heat must be supplied again after the pause.
Grid: excess ∈ {0.2, 0.3, 0.4} °C, duration ∈ {10, 20, 30} min, margin ∈ {0.2, 0.3, 0.5} °C.

| Excess / duration / margin | Energy while paused | Pauses | Max per day | Worst gap to the limit |
|---|---|---|---|---|
| 0.2 / 20 / 0.3 | 9.4 kWh | 25 | 6 | +0.15 °C |
| **0.3 / 20 / 0.3** (chosen) | **6.2 kWh** | **14** | **3** | **+0.15 °C** |
| 0.3 / 30 / 0.3 | 4.1 kWh | 10 | 3 | +0.15 °C |

With a 0.2 °C room sensor, an excess of 0.3 °C means waiting for 0.4 °C: the plugin targets clear
episodes. 0.2 °C captures 50 % more gross energy but doubles the pauses, hence the compressor
restarts, for a smaller net saving. The minimum predicted autonomy (60 min) has no effect on this
dataset: in mild weather the predicted autonomy always exceeds an hour. The heat-loss coefficient
learned online from the nights is 0.030 /h. The eco profile is not calibrated: the dataset's nights
are already handled by VTherm's own auto start/stop.

