import asyncio
import time
import uuid
import httpx
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

from backend.models import (
    MachineBaseline, SensorBaseline, MaintenanceAlert, MaintenanceSlot,
    PriorityItem, AgentReadingEvent, MachineStatus,
)
from backend.llm_client import generate_reasoning

MACHINE_IDS = ["M-001", "M-002", "M-003", "M-004"]
SENSOR_FIELDS = ["temperature_C", "vibration_mm_s", "rpm", "current_A"]
BASE_URL = "http://localhost:8000"

# ── TUNING (Fixed to allow visible triggers) ──────────────────
SPIKE_WINDOW_SIZE = 10
SPIKE_CONFIRM_THRESHOLD = 2  # FIXED: Was 3, swallowed 3-sec spikes. Now triggers on 2nd tick.
DRIFT_EMA_ALPHA = 0.08
DATA_GAP_WARN_SEC = 5.0
DATA_GAP_CRIT_SEC = 15.0
ALERT_COOLDOWN_SEC = 60.0
AUTO_SCHEDULE_RISK = 60.0   # FIXED: Lowered to allow auto-scheduling
AUTO_SCHEDULE_PERSIST = 2
BASELINE_HISTORY_SAMPLES = 2016

def compute_sensor_baseline(values: List[float]) -> SensorBaseline:
    sorted_v = sorted(values)
    n = len(sorted_v)
    q1 = sorted_v[n // 4]
    q3 = sorted_v[3 * n // 4]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    filtered = [v for v in sorted_v if lo <= v <= hi]
    if not filtered: filtered = sorted_v
    mean = sum(filtered) / len(filtered)
    var = sum((v - mean) ** 2 for v in filtered) / len(filtered)
    std = var ** 0.5 if var > 0 else 0.01
    return SensorBaseline(
        mean=round(mean, 3), std=round(std, 3),
        lower=round(mean - 2.5 * std, 3), upper=round(mean + 2.5 * std, 3),
        q1=round(q1, 3), q3=round(q3, 3), iqr=round(iqr, 3),
    )

async def fetch_and_compute_baselines(client: httpx.AsyncClient) -> Dict[str, MachineBaseline]:
    baselines: Dict[str, MachineBaseline] = {}
    for mid in MACHINE_IDS:
        resp = await client.get(f"{BASE_URL}/history/{mid}", timeout=30.0)
        readings = resp.json()
        by_sensor: Dict[str, List[float]] = {f: [] for f in SENSOR_FIELDS}
        for r in readings:
            for f in SENSOR_FIELDS: by_sensor[f].append(r[f])
        baselines[mid] = MachineBaseline(machine_id=mid, **{f: compute_sensor_baseline(by_sensor[f]) for f in SENSOR_FIELDS})
    return baselines

class MachineState:
    def __init__(self):
        self.spike_windows: Dict[str, deque] = {f: deque(maxlen=SPIKE_WINDOW_SIZE) for f in SENSOR_FIELDS}
        self.ema: Dict[str, float] = {}
        self.consecutive_anomalous: int = 0
        self.last_reading_ts: Optional[float] = None
        self.last_alert_ts: float = 0.0
        self.last_alert_priority: str = "low"
        self.smoothed_risk: float = 0.0
        self.active_anomalies: Dict[str, Any] = {}
        self.data_gap: bool = False
        self.scheduled_slot: bool = False
        self.suppressed_spikes: int = 0
        self.current_anomaly_type: str = "none"

def detect_anomalies(reading: dict, baseline: MachineBaseline, state: MachineState) -> tuple[float, Dict[str, Any], bool, str]:
    anomalies: Dict[str, Any] = {}
    risk_parts: List[float] = []
    has_spike = False
    has_drift = False

    for field in SENSOR_FIELDS:
        value = reading[field]
        bl: SensorBaseline = getattr(baseline, field)
        if field not in state.ema: state.ema[field] = bl.mean

        is_above = value > bl.upper
        is_below = value < bl.lower
        out_of_bounds = is_above or is_below

        state.spike_windows[field].append(1 if out_of_bounds else 0)
        recent = list(state.spike_windows[field])[-5:]
        confirmed_spike = sum(recent) >= SPIKE_CONFIRM_THRESHOLD

        state.ema[field] = DRIFT_EMA_ALPHA * value + (1 - DRIFT_EMA_ALPHA) * state.ema[field]
        ema_dev = abs(state.ema[field] - bl.mean) / bl.std if bl.std > 0 else 0
        drift_detected = ema_dev > 1.5 and not out_of_bounds

        if confirmed_spike:
            has_spike = True
            dev = ((value - bl.upper) / bl.std) if is_above else ((bl.lower - value) / bl.std)
            dev = max(dev, 0)
            anomalies[field] = {
                "value": value, "expected_range": f"{bl.lower:.1f} - {bl.upper:.1f}",
                "deviation_std": round(dev, 2), "direction": "above" if is_above else "below", "baseline_mean": bl.mean,
            }
            risk_parts.append(dev)
        elif drift_detected:
            has_drift = True
            anomalies[field] = {
                "value": value, "expected_range": f"{bl.lower:.1f} - {bl.upper:.1f}",
                "deviation_std": round(ema_dev, 2), "direction": "drift", "baseline_mean": bl.mean,
            }
            risk_parts.append(ema_dev * 0.6)

    # Classify Type
    if has_spike and (has_drift or len(anomalies) > 1): anomaly_type = "compound"
    elif has_drift: anomaly_type = "drift"
    elif has_spike: anomaly_type = "spike"
    else: anomaly_type = "none"

    # Transient Noise Suppression Logic
    is_oob_now = any(list(state.spike_windows[f])[-1] == 1 for f in SENSOR_FIELDS if len(state.spike_windows[f]) > 0)
    if is_oob_now and anomaly_type == "none":
        state.suppressed_spikes += 1

    # FIXED RISK SCORING MATH
    n = len(anomalies)
    compound = {0: 0, 1: 1.0, 2: 2.5, 3: 4.0}.get(n, 6.0)

    if anomalies:
        state.consecutive_anomalous += 1
    else:
        state.consecutive_anomalous = max(0, state.consecutive_anomalous - 2)
    persistence = min(1.0 + state.consecutive_anomalous * 0.25, 3.5)

    status_mult = {"running": 1.0, "warning": 2.5, "fault": 5.5}.get(reading.get("status", "running"), 1.0)

    base_risk = max(risk_parts) if risk_parts else 0
    # FIXED: Scale base risk by 5 so even small deviations visibly move the gauge
    raw_risk = min(base_risk * 5 * compound * persistence * status_mult, 100.0)

    # FIXED SMOTHING: React instantly to danger, decay slowly when safe
    if anomalies:
        state.smoothed_risk = state.smoothed_risk * 0.4 + raw_risk * 0.6
    else:
        state.smoothed_risk = state.smoothed_risk * 0.95
        
    risk_score = round(state.smoothed_risk, 1)
    state.active_anomalies = anomalies
    state.current_anomaly_type = anomaly_type
    
    return risk_score, anomalies, len(anomalies) > 0, anomaly_type

class EventBus:
    def __init__(self): self.subscribers: List[asyncio.Queue] = []
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.append(q); return q
    def unsubscribe(self, q: asyncio.Queue):
        if q in self.subscribers: self.subscribers.remove(q)
    async def publish(self, event_type: str, data: dict):
        payload = {"type": event_type, "data": data, "ts": datetime.now(timezone.utc).isoformat()}
        dead = []
        for q in self.subscribers:
            try: q.put_nowait(payload)
            except asyncio.QueueFull: dead.append(q)
        for q in dead: self.unsubscribe(q)

class PredictiveMaintenanceAgent:
    def __init__(self):
        self.baselines: Dict[str, MachineBaseline] = {}
        self.states: Dict[str, MachineState] = {m: MachineState() for m in MACHINE_IDS}
        self.event_bus = EventBus()
        self.alerts: List[dict] = []
        self.priority_queue: List[PriorityItem] = []
        self.maintenance_slots: List[dict] = []
        self.running = False
        self.start_time: float = 0.0

    def baseline_dict(self, mid: str) -> Dict[str, Dict[str, float]]:
        bl = self.baselines.get(mid)
        if not bl: return {}
        return {f: {"mean": getattr(bl, f).mean, "lower": getattr(bl, f).lower, "upper": getattr(bl, f).upper} for f in SENSOR_FIELDS}

    def _update_priority_queue(self, mid: str, risk: float, reason: str, prio: str):
        self.priority_queue = [p for p in self.priority_queue if p.machine_id != mid]
        if risk > 10:
            self.priority_queue.append(PriorityItem(machine_id=mid, risk_score=risk, priority=prio, reason=reason, since=datetime.now(timezone.utc)))
        self.priority_queue.sort(key=lambda p: p.risk_score, reverse=True)

    async def _handle_alert(self, mid: str, risk: float, anomalies: dict, reading: dict, anomaly_type: str):
        state = self.states[mid]
        prio = priority_from_score(risk)
        now = time.time()

        prio_rank = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        prev_rank = prio_rank.get(state.last_alert_priority, 0)
        curr_rank = prio_rank.get(prio, 0)

        # Cooldown unless priority escalates or status becomes "fault"
        if now - state.last_alert_ts < ALERT_COOLDOWN_SEC and curr_rank <= prev_rank:
            if reading.get("status") != "fault": return

        bl = self.baseline_dict(mid)
        reasoning, is_llm = await generate_reasoning(mid, anomalies, risk, reading.get("status", "running"), bl)

        alert = {
            "alert_id": str(uuid.uuid4())[:8], "machine_id": mid, "risk_score": risk,
            "priority": prio, "anomaly_type": anomaly_type,
            "reason_summary": f"[{anomaly_type.upper()}] {len(anomalies)} sensor(s) anomalous: {', '.join(anomalies.keys())}",
            "llm_reasoning": reasoning, "sensors_affected": list(anomalies.keys()),
            "is_llm": is_llm, "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.alerts.insert(0, alert)
        if len(self.alerts) > 200: self.alerts = self.alerts[:200]

        state.last_alert_ts = now
        state.last_alert_priority = prio

        await self.event_bus.publish("alert", alert)
        self._update_priority_queue(mid, risk, alert["reason_summary"], prio)

        # FIXED AUTO-SCHEDULING: Trigger at 60 risk OR immediate on FAULT
        if (risk > AUTO_SCHEDULE_RISK and state.consecutive_anomalous >= AUTO_SCHEDULE_PERSIST) or reading.get("status") == "fault":
            await self._auto_schedule(mid, risk, reasoning, prio)

    async def _auto_schedule(self, mid: str, risk: float, reason: str, prio: str):
        state = self.states[mid]
        if state.scheduled_slot: return
        for s in self.maintenance_slots:
            if s["machine_id"] == mid: state.scheduled_slot = True; return

        scheduled_time = datetime.now(timezone.utc) + timedelta(hours=2)
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.post(f"{BASE_URL}/schedule-maintenance", json={"machine_id": mid, "reason": reason[:200], "priority": prio, "scheduled_time": scheduled_time.isoformat()})
                resp.raise_for_status()
                slot = resp.json()
        except Exception:
            slot = {"slot_id": str(uuid.uuid4())[:8], "machine_id": mid, "scheduled_time": scheduled_time.isoformat(), "reason": reason[:200], "priority": prio, "created_at": datetime.now(timezone.utc).isoformat()}
        
        self.maintenance_slots.append(slot)
        state.scheduled_slot = True
        await self.event_bus.publish("maintenance", slot)

    async def _check_data_gaps(self):
        now = time.time()
        for mid in MACHINE_IDS:
            state = self.states[mid]
            if state.last_reading_ts is None: continue
            gap = now - state.last_reading_ts
            if gap > DATA_GAP_CRIT_SEC and not state.data_gap:
                state.data_gap = True
                await self.event_bus.publish("alert", {"alert_id": str(uuid.uuid4())[:8], "machine_id": mid, "risk_score": 85.0, "priority": "critical", "anomaly_type": "data_gap", "reason_summary": f"DATA LINK FAILURE: No readings for {gap:.0f}s", "llm_reasoning": f"CRITICAL — No sensor data received from {mid} for {gap:.0f} seconds. Immediate investigation required.", "sensors_affected": ["_data_link"], "is_llm": False, "timestamp": datetime.now(timezone.utc).isoformat()})
                self._update_priority_queue(mid, 85.0, f"DATA LINK FAILURE: {gap:.0f}s", "critical")

    async def _consume_stream(self, mid: str):
        state = self.states[mid]
        while self.running:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    async with client.stream("GET", f"{BASE_URL}/stream/{mid}") as resp:
                        async for line in resp.aiter_lines():
                            if not self.running: break
                            if not line.startswith("data: "): continue
                            try:
                                import json
                                reading = json.loads(line[6:])
                            except json.JSONDecodeError: continue

                            state.last_reading_ts = time.time()
                            if state.data_gap:
                                state.data_gap = False
                                await self.event_bus.publish("alert", {"alert_id": str(uuid.uuid4())[:8], "machine_id": mid, "risk_score": 0, "priority": "info", "anomaly_type": "none", "reason_summary": "Data stream restored", "llm_reasoning": f"Data stream from {mid} restored.", "sensors_affected": ["_data_link"], "is_llm": False, "timestamp": datetime.now(timezone.utc).isoformat()})
                                self.priority_queue = [p for p in self.priority_queue if p.machine_id != mid or "DATA" not in p.reason]

                            if mid not in self.baselines: continue

                            risk, anomalies, confirmed, anomaly_type = detect_anomalies(reading, self.baselines[mid], state)

                            event = AgentReadingEvent(
                                machine_id=mid, timestamp=datetime.now(timezone.utc),
                                temperature_C=reading["temperature_C"], vibration_mm_s=reading["vibration_mm_s"],
                                rpm=reading["rpm"], current_A=reading["current_A"], status=reading["status"],
                                risk_score=risk, baselines=self.baseline_dict(mid),
                                active_anomalies=anomalies, data_gap=False,
                                anomaly_type=anomaly_type, suppressed_spikes=state.suppressed_spikes
                            )
                            await self.event_bus.publish("reading", event.model_dump())

                            # FIXED: Lowered from 15 to 5 to ensure UI triggers
                            if confirmed and risk > 5:
                                await self._handle_alert(mid, risk, anomalies, reading, anomaly_type)
                            elif risk <= 5:
                                self._update_priority_queue(mid, risk, "Normal", "info")

            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
                if self.running: await asyncio.sleep(3)

    async def start(self):
        self.running = True
        self.start_time = time.time()
        await self.event_bus.publish("system", {"status": "initializing", "message": "Computing dynamic per-machine baselines..."})

        async with httpx.AsyncClient(timeout=60.0) as client:
            self.baselines = await fetch_and_compute_baselines(client)

        for mid in MACHINE_IDS:
            for f in SENSOR_FIELDS:
                self.states[mid].ema[f] = getattr(self.baselines[mid], f).mean

        await self.event_bus.publish("system", {"status": "active", "message": "ForgeSight Agent active", "baseline_samples": BASELINE_HISTORY_SAMPLES})

        tasks = [asyncio.create_task(self._consume_stream(mid)) for mid in MACHINE_IDS]
        gap_task = asyncio.create_task(self._gap_checker_loop())
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try: await asyncio.gather(*tasks, gap_task, heartbeat_task)
        except asyncio.CancelledError: pass

    async def _gap_checker_loop(self):
        while self.running:
            await asyncio.sleep(2.0)
            await self._check_data_gaps()

    async def _heartbeat_loop(self):
        while self.running:
            await asyncio.sleep(1.0)
            total_suppressed = sum(s.suppressed_spikes for s in self.states.values())
            active_types = list(set(s.current_anomaly_type for s in self.states.values() if s.current_anomaly_type != "none"))
            uptime = int(time.time() - self.start_time)
            await self.event_bus.publish("heartbeat", {"uptime_seconds": uptime, "total_suppressed": total_suppressed, "active_anomaly_types": active_types})

    async def stop(self):
        self.running = False

def priority_from_score(score: float) -> str:
    if score > 75: return "critical"
    elif score > 50: return "high"
    elif score > 25: return "medium"
    elif score > 10: return "low"
    return "info"
