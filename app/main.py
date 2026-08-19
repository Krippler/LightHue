import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config_store
from . import hue_client
from .hue_client import HueClient
from .patterns import BUILTIN_PATTERNS, BUILTIN_BY_ID
from .flicker_engine import FlickerEngine


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # Cancel the flicker loops before tearing down the pool they send through,
    # otherwise in-flight ticks fail against a closed client on the way out.
    await engine.stop_all()
    await hue_client.aclose()


app = FastAPI(title="Quake Hue Flicker", lifespan=lifespan)


# ---------- WebSocket connection manager ----------

class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, message: dict):
        # Snapshot first: send_json awaits, and a client connecting or dropping
        # during that await would otherwise mutate the set mid-iteration.
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


def _broadcast_status_soon():
    async def _do():
        await manager.broadcast({"type": "status", "data": engine.status()})
    try:
        asyncio.get_event_loop().create_task(_do())
    except RuntimeError:
        pass


def get_client() -> Optional[HueClient]:
    cfg = config_store.load()
    if not cfg.get("bridge_ip") or not cfg.get("api_key"):
        return None
    return HueClient(cfg["bridge_ip"], cfg["api_key"])


engine = FlickerEngine(get_client=get_client, on_change=_broadcast_status_soon)


# ---------- Models ----------

class BridgeSetRequest(BaseModel):
    bridge_ip: str
    api_key: str


class PairRequest(BaseModel):
    bridge_ip: str


class CustomPatternRequest(BaseModel):
    name: str
    sequence: str


class StartRequest(BaseModel):
    light_ids: list[str]
    pattern_id: str
    hz: float = 10.0
    min_bri: int = 1
    max_bri: int = 254
    hue: Optional[int] = None
    sat: Optional[int] = None
    transition_ms: int = 0


class StopRequest(BaseModel):
    light_ids: Optional[list[str]] = None  # None => stop all


# ---------- Bridge setup ----------

@app.get("/api/bridge")
async def get_bridge():
    cfg = config_store.load()
    return {
        "bridge_ip": cfg.get("bridge_ip"),
        "configured": bool(cfg.get("bridge_ip") and cfg.get("api_key")),
    }


@app.get("/api/bridge/discover")
async def discover_bridge():
    try:
        results = await HueClient.discover()
    except Exception as e:
        raise HTTPException(502, f"Discovery failed: {e}")
    return {"bridges": results}


@app.post("/api/bridge/pair")
async def pair_bridge(req: PairRequest):
    result = await HueClient.pair(req.bridge_ip)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Pairing failed — press the link button on the bridge first"))
    config_store.update(bridge_ip=req.bridge_ip, api_key=result["api_key"])
    return {"bridge_ip": req.bridge_ip, "configured": True}


@app.post("/api/bridge/set")
async def set_bridge(req: BridgeSetRequest):
    config_store.update(bridge_ip=req.bridge_ip, api_key=req.api_key)
    return {"ok": True}


# ---------- Lights ----------

@app.get("/api/lights")
async def list_lights():
    client = get_client()
    if client is None:
        raise HTTPException(400, "Bridge not configured yet")
    try:
        lights = await client.get_lights()
    except Exception as e:
        raise HTTPException(502, f"Could not reach bridge: {e}")
    out = []
    for lid, info in lights.items():
        out.append({
            "id": lid,
            "name": info.get("name", f"Light {lid}"),
            "on": info.get("state", {}).get("on", False),
            "reachable": info.get("state", {}).get("reachable", True),
        })
    out.sort(key=lambda x: x["name"])
    return {"lights": out}


# ---------- Patterns ----------

@app.get("/api/patterns")
async def list_patterns():
    cfg = config_store.load()
    custom = list(cfg.get("custom_patterns", {}).values())
    return {"builtin": BUILTIN_PATTERNS, "custom": custom}


@app.post("/api/patterns")
async def create_pattern(req: CustomPatternRequest):
    seq = req.sequence.strip().lower()
    if not seq or any(c not in "abcdefghijklmnopqrstuvwxyz" for c in seq):
        raise HTTPException(400, "Sequence must only contain letters a-z")
    pid = f"custom_{uuid.uuid4().hex[:8]}"
    cfg = config_store.load()
    cfg["custom_patterns"][pid] = {"id": pid, "name": req.name, "sequence": seq}
    config_store.save(cfg)
    return cfg["custom_patterns"][pid]


@app.delete("/api/patterns/{pattern_id}")
async def delete_pattern(pattern_id: str):
    if pattern_id in BUILTIN_BY_ID:
        raise HTTPException(400, "Built-in Quake patterns can't be deleted")
    # A running loop holds its own copy of the sequence, so deleting out from
    # under it would leave lights flickering a pattern the UI can't name.
    in_use = [lid for lid, st in engine.status().items()
              if st.get("running") and st.get("pattern_id") == pattern_id]
    if in_use:
        raise HTTPException(
            409,
            f"That pattern is running on {len(in_use)} light(s) — stop them first",
        )
    cfg = config_store.load()
    if cfg["custom_patterns"].pop(pattern_id, None) is None:
        raise HTTPException(404, f"Unknown pattern_id: {pattern_id}")
    config_store.save(cfg)
    return {"ok": True}


def _resolve_sequence(pattern_id: str) -> str:
    if pattern_id in BUILTIN_BY_ID:
        return BUILTIN_BY_ID[pattern_id]["sequence"]
    cfg = config_store.load()
    custom = cfg.get("custom_patterns", {})
    if pattern_id in custom:
        return custom[pattern_id]["sequence"]
    raise HTTPException(404, f"Unknown pattern_id: {pattern_id}")


# ---------- Flicker control ----------

@app.get("/api/status")
async def get_status():
    return {"lights": engine.status()}


@app.post("/api/flicker/start")
async def start_flicker(req: StartRequest):
    if get_client() is None:
        raise HTTPException(400, "Bridge not configured yet")
    sequence = _resolve_sequence(req.pattern_id)
    if req.hz <= 0 or req.hz > 20:
        raise HTTPException(400, "hz must be between 0 and 20 (Hue can't usefully go faster)")
    if len(req.light_ids) * req.hz > 15:
        # soft warning surfaced as 200 still, but let's just cap silently server-side
        pass
    for lid in req.light_ids:
        await engine.start(
            lid, sequence, req.pattern_id, req.hz, req.min_bri, req.max_bri,
            req.hue, req.sat, req.transition_ms,
        )
    return {"ok": True, "status": engine.status()}


@app.post("/api/flicker/stop")
async def stop_flicker(req: StopRequest):
    if req.light_ids:
        for lid in req.light_ids:
            await engine.stop(lid)
    else:
        await engine.stop_all()
    return {"ok": True, "status": engine.status()}


# ---------- WebSocket ----------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_json({"type": "status", "data": engine.status()})
        while True:
            # We don't expect inbound messages, but keep the socket alive.
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# ---------- Static UI ----------

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
