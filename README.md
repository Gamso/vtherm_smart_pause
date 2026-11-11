# Versatile Thermostat Smart Pause

A [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) (VTherm) plugin that
**pauses a heat pump or an air conditioner while the room does not need it and will hold without
it**, then hands it back before comfort is at stake.

> **Status: 0.1.0, not yet tried on real equipment.** Use it on one thermostat first and watch it
> for a day.

## Why

A heat pump regulates on its own indoor sensor, not on your room sensor. Near the setpoint that
sensor warms up in the still air of the unit, and the unit keeps starting its compressor for a few
minutes, often without even blowing air into the room. VTherm sees the room above the setpoint and
reports `idle`, but the unit keeps going.

Ten days of measurements on one installation ([study](docs/study.md), §1.1): **40 % of the energy**
went into such short cycles, a quarter of it with the room already 0.2 °C or more above the
setpoint, and those cycles accounted for **60 % of the compressor starts**. VTherm's own auto
start/stop only stops at 0.5 °C above the setpoint and would have caught at most half of it.

The saving on the bill is modest (about 15-25 € a year on that home in mild weather). The main gain
is **far fewer short compressor starts**.

## How it works

On every VTherm control cycle the plugin:

1. **Learns** how fast the room drifts toward the outdoor temperature when the equipment is
   stopped, and how much the sun warms it (`dT/dt = k · (T_out − T_in) + g · sun`). It learns from
   real stops only: nights with the thermostat off, and its own pauses.
2. **Predicts** how long the room would hold, equipment paused, before crossing its comfort limit,
   using the weather forecast for the outdoor temperature and the clouds.
3. **Pauses** when the room has been past the setpoint long enough, will hold long enough, and the
   equipment is actually doing something. It **resumes** before the room reaches the limit, or at
   once if comfort calls for it.

A pause sends the equipment a **setback setpoint** (by default the thermostat's minimum in heating,
its maximum in cooling): the unit's own thermostat goes idle while the unit stays on, so it restarts
immediately. VTherm stays in its HVAC mode. The pause goes through VTherm's setpoint-modifier
contract (`vtherm_api` 0.9.0), so VTherm keeps its own safety features (windows, load shedding,
safety mode) and takes over again at once when the plugin lets go.

Heating and cooling are handled the same way, mirrored.

### Profiles

A setpoint threshold per mode picks one of two profiles:

| | Comfort | Eco |
|---|---|---|
| When | heating setpoint at or above `threshold_heat` (cooling: at or below `threshold_cool`), or no threshold set | the other side of the threshold |
| Excess needed before a pause | 0.3 °C for 20 min | 0.2 °C for 10 min |
| Comfort limit (setpoint ∓ margin) | 0.3 °C | 1.0 °C |
| Predicted autonomy needed | 60 min | 30 min |
| Needs a learned model | yes (3 night stops) | no, uses a prudent default |

The comfort defaults were calibrated by replaying the ten days of measurements
([study](docs/study.md), §8).

### Compressor guards

At least 15 min of run after a resume, at least 20 min of pause, at most 8 pauses a day, no pause
in heating below −5 °C outside or in cooling above 40 °C. All of them are options.

## Requirements

- Home Assistant **2026.11** or later.
- Versatile Thermostat **10.6.4** or later (it ships `vtherm_api` 0.9.0), with an
  **`over_climate`** thermostat.
- Recommended: a `weather` entity with an hourly forecast.

## Installation

1. In HACS, add this repository as a custom repository (category: integration) and install
   **Versatile Thermostat Smart Pause**.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → Versatile Thermostat Smart Pause**, pick the
   thermostat.
4. Turn VTherm's own auto start/stop off on that thermostat: two deciders would fight. The plugin
   logs a warning while it is on.

## Configuration

| Option | Meaning |
|---|---|
| Versatile Thermostat | The `over_climate` thermostat to pause (one entry per thermostat). |
| Comfort thresholds | Heating: comfort profile from this setpoint up. Cooling: comfort profile from this setpoint down. Empty: always comfort. |
| Weather forecast | Hourly forecast of outdoor temperature and clouds. Without it the outdoor temperature is assumed constant. |
| Power or energy of the equipment | Optional. A power sensor (W, kW) or an energy counter (Wh, kWh), even a delayed one. Pauses only an equipment that draws power, and estimates the avoided energy. |
| Compressor running | Optional. On while the compressor runs. Pauses only a running equipment. A pause during which it still runs teaches nothing. |
| Defrost or preheating | Optional. On during a defrost: no decision, no learning. |
| Setback (options) | Setpoint sent while paused. Default: the thermostat's minimum (heating) or maximum (cooling). |
| Profile margins and guards (options) | See the tables above. |

## Entities

For each thermostat, a device "*‹thermostat› Smart Pause*" with:

| Entity | Content |
|---|---|
| Switch **Smart pause** | Turns the plugin on or off for this thermostat. Off ends a pause at the next cycle. |
| Sensor **State** | `running`, `paused` or `inactive`. Attributes: reason, profile, comfort limit. |
| Sensor **Autonomy** | Predicted minutes the room would hold if paused now. |
| Sensor **Heat loss coefficient** | Learned `k` (1/h). Attributes: solar gain, learned stops, readiness, recovery time. |
| Sensor **Pauses today** | Pauses since midnight. |
| Sensor **Avoided energy** | Energy not drawn while paused (upper bound), with a power or energy entity only. |

The thermostat itself also carries a `smart_pause` attribute section (state, reason, profile,
autonomy, limit, setback, learned values). Other plugins read `smart_pause.state` to stand down
during a pause.

## Limits of this version

- Pauses by setback only. The full stop (VTherm inhibition) is planned.
- VTherm's own auto start/stop is not switched off automatically.
- The model learns from scratch: no bootstrap from the recorder history yet. The comfort profile
  starts after three nights with the thermostat off.
- Not yet checked on hardware: that VTherm sends the setback when its self-regulation is set to
  "none".

## Development

- [Specification](SPEC.md): what to build and how to check it, with the status of each step.
- [Study](docs/study.md): the measurements behind the plugin, the design and the calibration.
- Tests: `python -m pytest tests/ -q` with `requirements_test.txt` (Home Assistant 2026.11, Python
  3.14). Deprecated Home Assistant APIs fail the suite.
- `tests/test_replay.py` replays [ten days of measurements](tests/data/replay_2026-11-01_2026-11-11.csv)
  through the decision and the model.
- Formatting and lint: `black --check custom_components tests`, `pylint custom_components/vtherm_smart_pause`.

## Licence

MIT.
