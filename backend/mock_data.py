"""
mock_data.py
Live sensor simulation matching the Hack Malendau 2026 generate-history.js spec.
Each machine has its own live state that drifts over time (mirrors server.js nextLiveReading).
"""
import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from backend.models import SensorReading, MachineStatus

MACHINES = ["CNC_01", "CNC_02", "PUMP_03", "CONVEYOR_04"]

BASELINES = {
    "CNC_01":      {"temp": 72.0, "vib": 1.8,  "rpm": 1480.0, "current": 12.5},
    "CNC_02":      {"temp": 68.0, "vib": 1.5,  "rpm": 1490.0, "current": 11.8},
    "PUMP_03":     {"temp": 55.0, "vib": 2.2,  "rpm": 2950.0, "current": 18.0},
    "CONVEYOR_04": {"temp": 45.0, "vib": 0.9,  "rpm":  720.0, "current":  8.5},
}

# Module-level live state — persists across SSE ticks (mirrors server.js liveState)
_live = {mid: {**b, "tick": 0} for mid, b in BASELINES.items()}


def _r(lo, hi): return random.uniform(lo, hi)
def _fix(n, d=2): return round(n, d)


def _next_live(machine_id: str) -> SensorReading:
    s = _live[machine_id]
    b = BASELINES[machine_id]
    s["tick"] += 1
    status = MachineStatus.RUNNING

    # Mean-reversion + noise (mirrors server.js exactly)
    temp    = s["temp"]    + (b["temp"]    - s["temp"])    * 0.02 + _r(-0.4,  0.4)
    vib     = s["vib"]     + (b["vib"]     - s["vib"])     * 0.02 + _r(-0.05, 0.05)
    rpm     = s["rpm"]     + (b["rpm"]     - s["rpm"])     * 0.02 + _r(-8,    8)
    current = s["current"] + (b["current"] - s["current"]) * 0.02 + _r(-0.15, 0.15)

    if machine_id == "CNC_01":
        ramp = min(s["tick"] / (5 * 60), 1.0)
        vib     += ramp * 0.004
        temp    += ramp * 0.01
        current += ramp * 0.005
        if vib > 3.5: status = MachineStatus.WARNING
        if vib > 5.5: status = MachineStatus.FAULT

    elif machine_id == "CNC_02":
        if s["tick"] % 180 < 20:
            temp    += _r(5,  18)
            current += _r(1,   4)
        if temp > 95:  status = MachineStatus.WARNING
        if temp > 110: status = MachineStatus.FAULT

    elif machine_id == "PUMP_03":
        if random.random() < 0.04:
            vib     += _r(1.5, 5)
            current += _r(0.5, 2)
        rpm -= 0.02
        if vib > 5 or rpm < 2800: status = MachineStatus.WARNING

    elif machine_id == "CONVEYOR_04":
        if random.random() < 0.005:
            vib    += _r(0.5, 1.5)
            status  = MachineStatus.WARNING

    temp    = max(20.0, min(130.0, temp))
    vib     = max(0.1,  min(12.0,  vib))
    rpm     = max(100.0, min(4000.0, rpm))
    current = max(1.0,  min(30.0,  current))

    s["temp"] = temp; s["vib"] = vib; s["rpm"] = rpm; s["current"] = current

    return SensorReading(
        machine_id=machine_id,
        timestamp=datetime.now(timezone.utc),
        temperature_C=_fix(temp),
        vibration_mm_s=_fix(vib, 3),
        rpm=_fix(rpm, 0),
        current_A=_fix(current),
        status=status,
    )


class MachineSimulator:
    """Holds 10 080-reading 7-day history; delegates live reads to module-level state."""

    def __init__(self, machine_id: str):
        self.machine_id = machine_id
        self.history_cache: list[SensorReading] = []
        self._generate_history()

    def _generate_history(self):
        now = datetime.now(timezone.utc)
        b   = BASELINES[self.machine_id]
        TOTAL_MIN = 7 * 24 * 60  # 10 080

        for i in range(TOTAL_MIN + 1):
            offset_min = TOTAL_MIN - i
            ts         = now - timedelta(minutes=offset_min)
            day_offset = offset_min // (24 * 60)
            progress   = (ts.hour * 3600 + ts.minute * 60 + ts.second) / 86400.0

            temp    = b["temp"]    + _r(-b["temp"]    * 0.03, b["temp"]    * 0.03)
            vib     = b["vib"]     + _r(-b["vib"]     * 0.04, b["vib"]     * 0.04)
            rpm     = b["rpm"]     + _r(-b["rpm"]     * 0.015, b["rpm"]    * 0.015)
            current = b["current"] + _r(-b["current"] * 0.03, b["current"] * 0.03)
            status  = MachineStatus.RUNNING

            if self.machine_id == "CNC_01":
                d = max(0, 3 - day_offset) / 3.0
                vib += d * 3.5; temp += d * 12.0; current += d * 2.5
                if d > 0.6: status = MachineStatus.WARNING
                if d > 0.9 and progress > 0.8: status = MachineStatus.FAULT

            elif self.machine_id == "CNC_02":
                if 0.5 < progress < 0.75 and day_offset < 2:
                    temp += _r(15, 30); current += _r(2, 5)
                    if temp > 95: status = MachineStatus.WARNING
                if day_offset == 1 and 0.60 < progress < 0.65:
                    temp = 112 + _r(0, 8); current = 22 + _r(0, 3)
                    status = MachineStatus.FAULT

            elif self.machine_id == "PUMP_03":
                if random.random() < 0.08:
                    vib += _r(2, 6); current += _r(1, 3)
                    if vib > 6: status = MachineStatus.WARNING
                rpm -= ((7 - day_offset) / 7.0) * 180
                if rpm < 2820: status = MachineStatus.WARNING

            elif self.machine_id == "CONVEYOR_04":
                if day_offset == 4 and 0.40 < progress < 0.45:
                    vib += _r(1, 2); status = MachineStatus.WARNING

            temp    = max(20.0, min(130.0, temp))
            vib     = max(0.1,  min(12.0,  vib))
            rpm     = max(100.0, min(4000.0, rpm))
            current = max(1.0,  min(30.0,  current))

            self.history_cache.append(SensorReading(
                machine_id=self.machine_id, timestamp=ts,
                temperature_C=_fix(temp), vibration_mm_s=_fix(vib, 3),
                rpm=_fix(rpm, 0), current_A=_fix(current), status=status,
            ))

    def generate_live_reading(self) -> SensorReading:
        return _next_live(self.machine_id)

    def get_history(self) -> list[SensorReading]:
        return list(self.history_cache)


# One simulator per machine — imported by main.py
simulators: dict[str, MachineSimulator] = {mid: MachineSimulator(mid) for mid in MACHINES}


async def sse_stream_machine(machine_id: str) -> AsyncGenerator[str, None]:
    sim = simulators.get(machine_id)
    if not sim:
        yield f'data: {{"error": "unknown machine {machine_id}"}}\n\n'
        return
    while True:
        reading = sim.generate_live_reading()
        yield f"data: {reading.model_dump_json()}\n\n"
        await asyncio.sleep(1.0)
