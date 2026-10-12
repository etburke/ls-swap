"""The guided test procedure as data. The UI renders it; nothing here touches Streamlit."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str = "number"      # "number" | "bool" | "text"
    unit: str = ""


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    kind: str                 # "manual" | "capture" | "checks"
    instructions: str
    expected: str
    fields: tuple = ()


STEPS = (
    Step("static", "1. Static baseline", "manual",
         "Key off. Measure battery voltage at the posts. With a DVOM on ohms, measure resistance "
         "battery negative to engine block, block to chassis, and battery negative to the PCM "
         "ground pin.",
         "Battery about 12.4-12.7 V at rest. Ground paths well under 1 ohm (yours measured "
         "0.3-0.5 ohm earlier). Compare against later readings; a change after a fix is meaningful.",
         (Field("battery_v", "Battery voltage", unit="V"),
          Field("gnd_batt_block_ohm", "Battery neg to block", unit="ohm"),
          Field("gnd_block_chassis_ohm", "Block to chassis", unit="ohm"),
          Field("gnd_batt_pcm_ohm", "Battery neg to PCM ground pin", unit="ohm"))),
    Step("koeo", "2. Key on, engine off", "manual",
         "Key on, engine off. With the DVOM on DC volts and the black lead on battery negative, "
         "measure the 5V reference at the crank sensor connector, the PCM low reference, and the "
         "crank signal wire at the PCM pin with the engine stationary.",
         "5V reference about 4.9-5.1 V. Low reference near 0 V (tens of millivolts). Signal wire "
         "sitting at a steady high or low level.",
         (Field("ref5v_v", "5V ref at sensor connector", unit="V"),
          Field("low_ref_v", "PCM low ref vs battery neg", unit="V"),
          Field("signal_static_v", "Crank signal at PCM pin, stationary", unit="V"))),
    Step("baseline_capture", "3. Baseline cranking capture", "capture",
         "Wire the scope: CH1 crank at the PCM pin, CH2 crank at the sensor connector, CH3 5V "
         "reference, CH4 PCM low reference. All probe grounds go to battery negative. Arm on the "
         "Capture page, then crank for several seconds. Optionally log the lowest battery voltage "
         "you see on the DVOM while cranking.",
         "A battery that stays above about 9.6 V while cranking is a common rule of thumb. The "
         "verdict on the Results page tells you what the capture shows.",
         (Field("battery_crank_min_v", "Lowest battery voltage while cranking", unit="V"),)),
    Step("after_change", "4. Capture after each change", "capture",
         "After each change (re-ground, sensor swap, connector cleaning), capture again and put "
         "what you changed in the capture label. Use the Compare page to see whether it helped.",
         "Edges per revolution, runt counts, and supply/ground deviation should move toward the "
         "healthy values after a real fix."),
    Step("mechanical", "5. Balancer and reluctor check", "checks",
         "Check the crank sensor for a tight mount and correct air gap, the reluctor wheel for "
         "damage or debris, and that the harmonic balancer has not slipped on the crank.",
         "Everything tight, clean and aligned. A slipped balancer usually causes wrong timing "
         "rather than a 0 RPM reading, so it is a low-probability cause here.",
         (Field("sensor_mount_ok", "Sensor tight, air gap OK", kind="bool"),
          Field("reluctor_ok", "Reluctor wheel clean and undamaged", kind="bool"),
          Field("balancer_ok", "Balancer marks aligned, no slip", kind="bool"),
          Field("notes", "Notes", kind="text"))),
)


def step_by_id(step_id: str) -> Step:
    for s in STEPS:
        if s.id == step_id:
            return s
    raise KeyError(step_id)
