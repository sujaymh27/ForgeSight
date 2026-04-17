import asyncio
import json
import uuid
import os
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from backend.models import SensorReading
from backend.mock_data import simulators, sse_stream_machine
from backend.agent import PredictiveMaintenanceAgent, MACHINE_IDS

agent = PredictiveMaintenanceAgent()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[FORGESIGHT] Starting predictive maintenance agent...")
    task = asyncio.create_task(agent.start())
    task.add_done_callback(lambda t: print(f"[FORGESIGHT] Task finished. Exception: {t.exception()}" if t.exception() else "[FORGESIGHT] Task finished normally."))
    yield
    await agent.stop()
    task.cancel()


app = FastAPI(title="ForgeSight", lifespan=lifespan)

FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend"))


@app.get("/stream/{machine_id}")
async def stream_sensor(machine_id: str):
    return StreamingResponse(
        sse_stream_machine(machine_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/history/{machine_id}")
async def get_history(machine_id: str):
    sim = simulators.get(machine_id)
    if not sim:
        return JSONResponse({"error": f"Unknown machine {machine_id}"}, status_code=404)
    return [r.model_dump() for r in sim.get_history()]

@app.post("/alert")
async def raise_alert(request: Request):
    body = await request.json()
    alert = {
        "alert_id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **body,
    }
    agent.alerts.insert(0, alert)
    await agent.event_bus.publish("alert", alert)
    return {"status": "acknowledged", "alert_id": alert["alert_id"]}

@app.post("/schedule-maintenance")
async def schedule_maintenance(request: Request):
    body = await request.json()
    slot = {
        "slot_id": str(uuid.uuid4())[:8],
        "created_at": datetime.now(timezone.utc).isoformat(),
        **body,
    }
    agent.maintenance_slots.append(slot)
    await agent.event_bus.publish("maintenance", slot)
    return slot

@app.get("/agent/events")
async def agent_events():
    async def event_generator():
        queue = agent.event_bus.subscribe()
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {msg['type']}\ndata: {json.dumps(msg['data'], default=str)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            agent.event_bus.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/api/alerts")
async def get_alerts():
    return agent.alerts[:50]

@app.get("/api/priority-queue")
async def get_priority_queue():
    return [p.model_dump() for p in agent.priority_queue]

@app.get("/api/maintenance")
async def get_maintenance():
    return agent.maintenance_slots

@app.get("/api/baselines/{machine_id}")
async def get_baselines(machine_id: str):
    bl = agent.baselines.get(machine_id)
    if not bl: return {}
    return {f: {"mean": getattr(bl, f).mean, "lower": getattr(bl, f).lower, "upper": getattr(bl, f).upper} for f in ["temperature_C", "vibration_mm_s", "rpm", "current_A"]}

@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
