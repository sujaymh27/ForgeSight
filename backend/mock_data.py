import asyncio
import random
import math
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from backend.models import SensorReading, MachineStatus

# ── Machine configs sourced from Hack Malendau 2026 generate-history.js ────────
# Machine IDs and baselines match the hackathon simulation server exactly.

MACHINE_CONFIGS = {
    "CNC_01": {
        "name": "CNC Machine #1",
        # Baseline from BASELINES.CNC_01 in generate-history.js
        "temp_base": 72.0,  "temp_pct": 0.03,
        "vib_base":  1.8,   "vib_pct":  0.04,
        "rpm_base":  1480.0,"rpm_pct":  0.015,
        "curr_base": 12.5,  "curr_pct": 0.03,
        # Anomaly: gradual bearing wear — vib + temp climb over last 3 days
        "anomaly_type": "bearing_wear",
    },
    "CNC_02": {
        "name": "CNC Machine #2",
        # Baseline from BASELINES.CNC_02
        "temp_base": 68.0,  "temp_pct": 0.03,
        "vib_base":  1.5,   "vib_pct":  0.04,
        "rpm_base":  1490.0,"rpm_pct":  0.015,
        "curr_base": 11.8,  "curr_pct": 0.03,
        # Anomaly: thermal runaway in afternoon shift; fault event on day 1
        "anomaly_type": "thermal_spike",
    },
    "PUMP_03": {
        "name": "Pump #3",
        # Baseline from BASELINES.PUMP_03
        "temp_base": 55.0,  "temp_pct": 0.03,
        "vib_base":  2.2,   "vib_pct":  0.04,
        "rpm_base":  2950.0,"rpm_pct":  0.015,
        "curr_base": 18.0,  "curr_pct": 0.03,
        # Anomaly: cavitation bursts + slow RPM drop (developing clog)
        "anomaly_type": "cavitation",
    },
    "CONVEYOR_04": {
        "name": "Conveyor #4",
        # Baseline from BASELINES.CONVEYOR_04
        "temp_base": 45.0,  "temp_pct": 0.03,
        "vib_base":  0.9,   "vib_pct":  0.04,
        "rpm_base":  720.0, "rpm_pct":  0.015,
        "curr_base": 8.5,   "curr_pct": 0.03,
        # Mostly healthy; one brief warning period
        "anomaly_type": "healthy",
    },
}


def _noise(base: float, pct: float) -> float:
    """Gaussian noise ±pct% of base, matching the JS noise() helper."""
    return base + random.uniform(-base * pct, base * pct)


class MachineSimulator:
    def __init__(self, machine_id: str, cfg: dict):
        self.machine_id = machine_id
        self.cfg = cfg
        self.tick = 0
        self.history_cache: list[SensorReading] = []
        self._generate_history()

    def _generate_history(self):
        """
        Generate 7 days of historical data sampled every 60 seconds (= 10 080 readings).
        Mirrors the generateAllHistory() / generateReading() logic from generate-history.js.
        """
        now = datetime.now(timezone.utc)
        total_minutes = 7 * 24 * 60  # 10 080 minutes
        for i in range(total_minutes + 1):
            offset_min = total_minutes - i
            ts = now - timedelta(minutes=offset_min)
            day_offset = offset_min // (24 * 60)   # 0 = today, 6 = oldest
            seconds_into_day = (ts.hour * 3600 + ts.minute * 60 + ts.second)
            progress = seconds_into_day / 86400.0   # 0.0–1.0 position within the day
            self.history_cache.append(
                self._make_reading(ts, day_offset=day_offset, progress=progress, is_history=True)
            )

    def _make_reading(
        self,
        ts: datetime,
        day_offset: int = 0,
        progress: float = 0.0,
        is_history: bool = False,
    ) -> SensorReading:
        cfg = self.cfg
        temp    = _noise(cfg["temp_base"],  cfg["temp_pct"])
        vib     = _noise(cfg["vib_base"],   cfg["vib_pct"])
        rpm     = _noise(cfg["rpm_base"],   cfg["rpm_pct"])
        current = _noise(cfg["curr_base"],  cfg["curr_pct"])
        status  = MachineStatus.RUNNING

        anomaly = cfg["anomaly_type"]

        # ── CNC_01 — Gradual bearing wear ─────────────────────────────────────
        # Mirrors: degradeDays = max(0, 3 - dayOffset); d = degradeDays / 3
        if anomaly == "bearing_wear":
            degrade_days = max(0, 3 - day_offset)
            d = degrade_days / 3.0
            vib     += d * 3.5
            temp    += d * 12.0
            current += d * 2.5
            if d > 0.6:
                status = MachineStatus.WARNING
            if d > 0.9 and progress > 0.8:
                status = MachineStatus.FAULT

        # ── CNC_02 — Thermal runaway / afternoon spike ─────────────────────────
        # Mirrors: afternoon = progress > 0.5 && progress < 0.75
        elif anomaly == "thermal_spike":
            afternoon = 0.5 < progress < 0.75
            if afternoon and day_offset < 2:
                temp    += random.uniform(15, 30)
                current += random.uniform(2, 5)
                if temp > 95:
                    status = MachineStatus.WARNING
            # Fault event on day 1, between 60–65 % of the day
            if day_offset == 1 and 0.60 < progress < 0.65:
                temp    = 112 + random.uniform(0, 8)
                current = 22  + random.uniform(0, 3)
                status  = MachineStatus.FAULT

            # Live drift after history: periodic thermal spikes every 3 min
            if not is_history:
                self.tick += 1
                if self.tick % 180 < 20:
                    temp    += random.uniform(5, 18)
                    current += random.uniform(1, 4)
                if temp > 95:
                    status = MachineStatus.WARNING
                if temp > 110:
                    status = MachineStatus.FAULT

        # ── PUMP_03 — Cavitation bursts + gradual RPM drop ─────────────────────
        # Mirrors: random < 0.08 burst; rpmDrop = ((7 - dayOffset) / 7) * 180
        elif anomaly == "cavitation":
            if random.random() < 0.08:
                vib     += random.uniform(2, 6)
                current += random.uniform(1, 3)
                if vib > 6:
                    status = MachineStatus.WARNING
            rpm_drop = ((7 - day_offset) / 7.0) * 180
            rpm -= rpm_drop
            if rpm < 2820:
                status = MachineStatus.WARNING

            # Live: random cavitation bursts + slow RPM decline
            if not is_history:
                self.tick += 1
                if random.random() < 0.04:
                    vib     += random.uniform(1.5, 5)
                    current += random.uniform(0.5, 2)
                rpm -= 0.02  # very slow clog build-up
                if vib > 5 or rpm < 2800:
                    status = MachineStatus.WARNING

        # ── CONVEYOR_04 — Mostly healthy ───────────────────────────────────────
        # One brief warning 4 days ago (day_offset == 4, progress 0.40–0.45)
        elif anomaly == "healthy":
            if day_offset == 4 and 0.40 < progress < 0.45:
                vib    += random.uniform(1, 2)
                status  = MachineStatus.WARNING
            # Live: rare random walk spikes
            if not is_history:
                self.tick += 1
                if random.random() < 0.005:
                    vib    += random.uniform(0.5, 1.5)
                    status  = MachineStatus.WARNING

        # ── Clamp values to physical limits ────────────────────────────────────
        temp    = max(20.0,  min(130.0, temp))
        vib     = max(0.1,   min(12.0,  vib))
        rpm     = max(100.0, min(4000.0, rpm))
        current = max(1.0,   min(30.0,  current))

        return SensorReading(
            machine_id=self.machine_id,
            timestamp=ts,
            temperature_C=round(temp, 2),
            vibration_mm_s=round(vib, 3),
            rpm=round(rpm, 1),
            current_A=round(current, 2),
            status=status,
        )

    def generate_live_reading(self) -> SensorReading:
        """
        Live reading, mirrors nextLiveReading() in server.js.
        day_offset=0 (today) and progress based on current time.
        """
        now = datetime.now(timezone.utc)
        seconds_into_day = now.hour * 3600 + now.minute * 60 + now.second
        progress = seconds_into_day / 86400.0
        return self._make_reading(now, day_offset=0, progress=progress, is_history=False)

    def get_history(self) -> list[SensorReading]:
        return list(self.history_cache)


# ── Simulators keyed by hackathon machine ID ───────────────────────────────────
simulators: dict[str, MachineSimulator] = {
    mid: MachineSimulator(mid, cfg) for mid, cfg in MACHINE_CONFIGS.items()
}


async def sse_stream_machine(machine_id: str) -> AsyncGenerator[str, None]:
    """SSE generator yielding one reading per second for a given machine."""
    sim = simulators.get(machine_id)
    if not sim:
        yield f'data: {{"error": "unknown machine {machine_id}"}}\n\n'
        return

    while True:
        # CONVEYOR_04 simulates occasional data gaps (matches M-004 behaviour)
        if machine_id == "CONVEYOR_04" and random.random() < 0.015:
            gap_seconds = random.uniform(5, 12)
            await asyncio.sleep(gap_seconds)
            continue

        reading = sim.generate_live_reading()
        yield f"data: {reading.model_dump_json()}\n\n"
        await asyncio.sleep(1.0)
