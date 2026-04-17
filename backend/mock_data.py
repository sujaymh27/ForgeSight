import asyncio
import random
import math
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from backend.models import SensorReading, MachineStatus

random.seed(42)

MACHINE_CONFIGS = {
    "M-001": {
        "name": "Pump-Alpha",
        "temp_base": 65.0, "temp_std": 2.5,
        "vib_base": 2.5, "vib_std": 0.4,
        "rpm_base": 1480.0, "rpm_std": 8.0,
        "curr_base": 12.5, "curr_std": 0.7,
        "anomaly_type": "none",
    },
    "M-002": {
        "name": "Compressor-Beta",
        "temp_base": 70.0, "temp_std": 2.0,
        "vib_base": 3.0, "vib_std": 0.5,
        "rpm_base": 1450.0, "rpm_std": 10.0,
        "curr_base": 18.0, "curr_std": 0.9,
        "anomaly_type": "drift",
    },
    "M-003": {
        "name": "Motor-Gamma",
        "temp_base": 72.0, "temp_std": 3.0,
        "vib_base": 3.2, "vib_std": 0.5,
        "rpm_base": 1760.0, "rpm_std": 12.0,
        "curr_base": 22.0, "curr_std": 1.1,
        "anomaly_type": "spikes",
    },
    "M-004": {
        "name": "Conveyor-Delta",
        "temp_base": 72.0, "temp_std": 2.5,
        "vib_base": 2.8, "vib_std": 0.4,
        "rpm_base": 950.0, "rpm_std": 6.0,
        "curr_base": 15.0, "curr_std": 0.8,
        "anomaly_type": "compound",
    },
}


class MachineSimulator:
    def __init__(self, machine_id: str, cfg: dict):
        self.machine_id = machine_id
        self.cfg = cfg
        self.tick = 0
        self.spike_active = False
        self.spike_remaining = 0
        self.history_cache: list[SensorReading] = []
        self._generate_history()

    def _generate_history(self):
        """Generate 7 days of historical data (sampled every 5 min = 2016 readings)."""
        now = datetime.now(timezone.utc)
        for i in range(2016):
            ts = now - timedelta(minutes=5) * (2016 - i)
            self.history_cache.append(self._make_reading(ts, history=True))

    def _make_reading(self, ts: datetime, history: bool = False) -> SensorReading:
        cfg = self.cfg
        temp = random.gauss(cfg["temp_base"], cfg["temp_std"])
        vib = random.gauss(cfg["vib_base"], cfg["vib_std"])
        rpm = random.gauss(cfg["rpm_base"], cfg["rpm_std"])
        curr = random.gauss(cfg["curr_base"], cfg["curr_std"])
        status = MachineStatus.RUNNING

        if not history:
            self.tick += 1
            anomaly = cfg["anomaly_type"]

            if anomaly == "drift":
                drift = self.tick * 0.018
                temp += drift
                if self.tick > 350:
                    status = MachineStatus.WARNING

            elif anomaly == "spikes":
                if not self.spike_active and random.random() < 0.015:
                    self.spike_active = True
                    self.spike_remaining = random.randint(3, 8)
                if self.spike_active:
                    vib += random.uniform(6.0, 11.0)
                    self.spike_remaining -= 1
                    if self.spike_remaining <= 0:
                        self.spike_active = False
                    if vib > 8:
                        status = MachineStatus.WARNING

            elif anomaly == "compound":
                temp += 6.0
                if random.random() < 0.08:
                    curr -= random.uniform(4.0, 7.0)
                    if curr < 10:
                        status = MachineStatus.WARNING
                if self.tick % 150 == 0 and self.tick > 50:
                    status = MachineStatus.FAULT

        rpm = max(0, rpm)
        curr = max(0, curr)
        vib = max(0, vib)

        return SensorReading(
            machine_id=self.machine_id,
            timestamp=ts,
            temperature_C=round(temp, 2),
            vibration_mm_s=round(vib, 3),
            rpm=round(rpm, 1),
            current_A=round(curr, 2),
            status=status,
        )

    def generate_live_reading(self) -> SensorReading:
        return self._make_reading(datetime.now(timezone.utc), history=False)

    def get_history(self) -> list[SensorReading]:
        return list(self.history_cache)


simulators: dict[str, MachineSimulator] = {
    mid: MachineSimulator(mid, cfg) for mid, cfg in MACHINE_CONFIGS.items()
}


async def sse_stream_machine(machine_id: str) -> AsyncGenerator[str, None]:
    """SSE generator yielding one reading per second for a given machine."""
    sim = simulators.get(machine_id)
    if not sim:
        yield f"data: {{\"error\": \"unknown machine {machine_id}\"}}\n\n"
        return

    while True:
        # M-004 simulates occasional data gaps
        if machine_id == "M-004" and random.random() < 0.015:
            gap_seconds = random.uniform(5, 12)
            await asyncio.sleep(gap_seconds)
            continue

        reading = sim.generate_live_reading()
        yield f"data: {reading.model_dump_json()}\n\n"
        await asyncio.sleep(1.0)
