# Versatile Thermostat Smart Pause

A [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) (VTherm) plugin that
**pauses a heat pump or an air conditioner while the room does not need it and will hold without
it**, then hands it back before comfort is at stake.

> **Status: 0.2.0, not yet tried on real equipment.** Use it on one thermostat first and watch it
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
   real stops only: nights with the thermostat off, and its own pauses. At install time it reads
   the last ten days of Home Assistant's history. Until it has learned, it predicts with a
   prudent default (a poorly insulated room), so it pauses less, never longer.
2. **Predicts** how long the room would hold, equipment paused, before crossing its comfort limit,
   using the weather forecast for the outdoor temperature and the clouds.
3. **Pauses** when the room has been past the setpoint long enough and will hold long enough without
   the equipment. It **resumes** before the room reaches the limit, or at once if comfort calls for it.

A pause sends the equipment a **setback setpoint** (by default the thermostat's minimum in heating,
its maximum in cooling): the unit's own thermostat goes idle while the unit stays on, so it restarts
immediately. VTherm stays in its HVAC mode. The pause goes through VTherm's setpoint-modifier
contract (`vtherm_api` 0.9.0), so VTherm keeps its own safety features (windows, load shedding,
safety mode) and takes over again at once when the plugin lets go.

Heating and cooling are handled the same way, mirrored.

### Two ways to keep the compressor off

| | VTherm's auto start/stop (**on/off**) | Smart pause (**setback**) |
|---|---|---|
| What it does | Switches the equipment fully off, then on again | Sends a very low setpoint (heating) or a very high one (cooling). The unit stays on and its own thermostat stops the compressor |
| Restart | Slow: the unit starts again from off | Immediate: the previous setpoint is sent back |
| VTherm's HVAC mode | `off` while stopped | unchanged |
| Best for | Long periods without need, such as a night setback | Short, repeated pauses while you are home |

**The criterion is the setpoint threshold of each mode.** It says who decides, as HomeShift's
threshold did:

| Setpoint (heating) | Who decides | VTherm's `enable_auto_start_stop` switch |
|---|---|---|
| below `threshold_heat` (e.g. a 17.5 °C night setback) | VTherm's own auto start/stop | turned **on** |
| at or above `threshold_heat` (e.g. 20 °C by day) | the smart pause, by setback | turned **off** |

Cooling mirrors it with `threshold_cool`: VTherm decides above it, the smart pause at or below it.
A mode without a threshold never delegates: the smart pause always decides there and VTherm's switch
stays off, so only one of them decides.

The switch is only written when the side changes (and once at start), so a flip by hand holds until
the next change. It needs the auto start/stop feature configured on the thermostat. Without it, the
smart pause decides on both sides, with its fallback rules below the threshold.

### What the settings mean

The options form shows the start/stop thresholds and the weather forecast first, then four
sections: **Pause rules** (open), **Setpoint sent while paused**, **Compressor protection** and
**Fallback rules** (collapsed).

**Pause rules.** They apply when the smart pause decides.

| Setting | Meaning | Default |
|---|---|---|
| Excess before pausing | Pause only when the room is at least this far past the setpoint (above it in heating, below it in cooling). With a room sensor that moves by 0.2 °C, 0.3 means a 0.4 °C excess. | 0.3 °C |
| How long the excess must last | The excess must last this long before a pause. | 20 min |
| Comfort margin | The comfort limit: the room must not go further than this on the uncomfortable side of the setpoint (below it in heating). The pause ends at half this margin, or earlier when the model predicts the limit will be reached. | 0.3 °C |
| Minimum autonomy to pause | Pause only if the model predicts the room will hold at least this long without the equipment. | 60 min |

**Fallback rules.** The same four settings, with wider margins (0.2 °C, 10 min, 1.0 °C, 30 min).
They are used only on the eco side of a threshold when VTherm's auto start/stop is not configured on
the thermostat. If you use the auto start/stop, you can ignore them.

**Setpoint sent while paused.** The low setpoint (heating) or high setpoint (cooling) the equipment
receives during a pause. Empty: the thermostat's minimum temperature in heating, its maximum in
cooling. Leave it empty unless your unit keeps heating at that value.

The defaults of the pause rules were calibrated by replaying the ten days of measurements
([study](docs/study.md), §8).

### Compressor protection

At least 15 min of run after a resume, at least 20 min of pause, at most 8 pauses a day, no pause
in heating below −5 °C outside or in cooling above 40 °C. All of them are options.

A defrost reported by VTherm ends a pause and stops the decisions.

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
4. If HomeShift drives that thermostat's auto start/stop switch, remove it there: the smart pause
   owns the switch now (see Start/stop threshold per mode).

## Configuration

| Option | Meaning |
|---|---|
| Versatile Thermostat | The `over_climate` thermostat to pause (one entry per thermostat). |
| Heating / cooling start/stop threshold | Heating: below it VTherm's auto start/stop decides, from it up the smart pause. Cooling: above it VTherm decides, from it down the smart pause. Empty: the smart pause always decides in that mode. |
| Weather forecast | Optional. Hourly forecast of outdoor temperature and clouds. Without it the outdoor temperature is assumed constant. |
| Pause rules, setpoint sent while paused, compressor protection, fallback rules (options) | Collapsible sections of the options form, see the sections above. |

## Entities

For each thermostat, a device "*‹thermostat› Smart Pause*" with:

| Entity | Content |
|---|---|
| Switch **Smart pause** | Turns the plugin on or off for this thermostat. Off ends a pause at the next cycle. |
| Sensor **State** | `running`, `paused` or `inactive`. Attributes: reason, profile, comfort limit. |
| Sensor **Autonomy** | Predicted minutes the room would hold if paused now. |
| Sensor **Heat loss coefficient** | Learned `k` (1/h). Attributes: solar gain, learned stops, lowest and highest `k` over them, readiness, recovery time. |
| Sensor **Solar gain** | Learned `g`: how fast full sun warms the room. 0 until two sunny stops are learned. |
| Sensor **Recovery time** | Minutes the room keeps drifting after the equipment restarts. 30 until a restart was measured. |
| Sensor **Model error** | How far the room ended from what the model predicted, over the last pauses (median, in degrees). Attributes: last error, bias, pauses evaluated. |
| Sensor **Pauses today** | Pauses since midnight. |

The last four are diagnostic sensors. **Model error** is how to judge the model: at the end of each
pause of 30 minutes or more that the room went through undisturbed, the plugin replays the pause through
the model with the weather actually met and compares it with the measured room temperature. The
error and the bias are *measured − predicted*: in heating, a negative bias means the room cools
faster than the model says. It stays unknown until a first pause is evaluated.

The thermostat itself also carries a `smart_pause` attribute section (state, reason, profile,
limit, setback, learned values). Other plugins read `smart_pause.state` to stand down
during a pause.

## Limits of this version

- Pauses by setback only. The full stop (VTherm inhibition) is planned.
- The history does not say why the thermostat was off. With a window sensor configured in the
  VTherm, the stops with the window open are left out; with the automatic window detection only,
  such a stop is read as a stop and overestimates the heat loss a little.
- Not yet checked on hardware: that VTherm sends the setback when its self-regulation is set to
  "none".
- It does not read the equipment's power, compressor or defrost entities, since not every
  installation has them. A pause that does not really stop the compressor lengthens the learned
  autonomy a little, and the pause still ends on comfort.

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
