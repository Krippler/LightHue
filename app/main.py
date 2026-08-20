import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import (
    Body,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import auth, config_store, hue_client, packs
from .auth import ConsoleAuthMiddleware
from .flicker_engine import FlickerEngine
from .hue_client import HueClient
from .patterns import BUILTIN_BY_ID, BUILTIN_PATTERNS, DEFAULT_HZ, GAMES


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = config_store.get_settings()
    engine.limiter.set_rate(settings["max_commands_per_second"])
    engine.restore_on_stop = settings["restore_on_stop"]

    # Snapshots left in the config mean the last run was killed mid-flicker,
    # so those bulbs are still sitting wherever the flicker left them.
    leftover = config_store.load().get("snapshots") or {}
    if leftover:
        engine.load_snapshots(leftover)
        if engine.restore_on_stop and get_client() is not None:
            restored = await engine.restore()
            logger.info("Restored %d light(s) left flickering by a previous run", len(restored))
    yield
    # Cancel the flicker loops before tearing down the pool they send through,
    # otherwise in-flight ticks fail against a closed client on the way out.
    await engine.stop_all()
    await hue_client.aclose()


app = FastAPI(title="Game Hue Flicker", lifespan=lifespan)
app.add_middleware(ConsoleAuthMiddleware)


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
logger = logging.getLogger("game_hue_flicker")

# Fire-and-forget tasks are only weakly held by the loop, so keep a strong
# reference until each one finishes or it can be collected mid-flight.
_background: set[asyncio.Task] = set()


def status_payload() -> dict:
    # "now" is the same monotonic clock the loops derive their frame from, so a
    # browser can work out the offset to its own clock and show the frame each
    # light is actually on rather than animating at its own pace.
    return {
        "lights": engine.status(),
        "snapshots": engine.snapshots,
        "now": time.monotonic(),
    }


def _broadcast_status_soon():
    async def _do():
        await manager.broadcast({"type": "status", **status_payload()})
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return   # no loop (e.g. called from a sync context in tests)
    task = loop.create_task(_do())
    _background.add(task)
    task.add_done_callback(_background.discard)


def get_client() -> HueClient | None:
    cfg = config_store.load()
    if not cfg.get("bridge_ip") or not cfg.get("api_key"):
        return None
    return HueClient(cfg["bridge_ip"], cfg["api_key"])


def _persist_snapshots(snapshots: dict):
    # Kept on disk so a container restart can still put the bulbs back.
    config_store.update(snapshots=snapshots)


engine = FlickerEngine(get_client=get_client, on_change=_broadcast_status_soon,
                       on_snapshots=_persist_snapshots)


# ---------- Models ----------

class BridgeSetRequest(BaseModel):
    bridge_ip: str
    api_key: str


class PairRequest(BaseModel):
    bridge_ip: str


class CustomPatternRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    sequence: str
    hz: float = Field(DEFAULT_HZ, gt=0, le=20)


class LoginRequest(BaseModel):
    password: str


class SetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=4, max_length=128)
    current_password: str | None = None


class ClearPasswordRequest(BaseModel):
    current_password: str


class SettingsRequest(BaseModel):
    max_commands_per_second: float = Field(..., ge=1.0, le=30.0)
    restore_on_stop: bool = True


class StartRequest(BaseModel):
    light_ids: list[str] = Field(..., min_length=1)
    pattern_id: str
    # None means "whatever this pattern was written for" — speed is part of the
    # pattern, so a caller that doesn't care shouldn't have to guess a number.
    hz: float | None = Field(None, gt=0, le=20, description="Hue can't usefully go faster")
    min_bri: int = Field(1, ge=1, le=254)
    max_bri: int = Field(254, ge=1, le=254)
    hue: int | None = Field(None, ge=0, le=65535)
    sat: int | None = Field(None, ge=0, le=254)
    transition_ms: int = Field(0, ge=0, le=60000)

    @model_validator(mode="after")
    def _check_ranges(self):
        if self.min_bri > self.max_bri:
            raise ValueError("min_bri must be less than or equal to max_bri")
        if (self.hue is None) != (self.sat is None):
            raise ValueError("hue and sat must be given together, or not at all")
        return self


class StopRequest(BaseModel):
    light_ids: list[str] | None = None  # None => stop all


class UpdateRequest(BaseModel):
    """Retune lights that are already flickering. Every setting is optional;
    whatever is supplied is applied without restarting the loop."""

    light_ids: list[str] = Field(..., min_length=1)
    pattern_id: str | None = None
    hz: float | None = Field(None, gt=0, le=20)
    min_bri: int | None = Field(None, ge=1, le=254)
    max_bri: int | None = Field(None, ge=1, le=254)
    hue: int | None = Field(None, ge=0, le=65535)
    sat: int | None = Field(None, ge=0, le=254)
    transition_ms: int | None = Field(None, ge=0, le=60000)

    @model_validator(mode="after")
    def _check_ranges(self):
        if self.min_bri is not None and self.max_bri is not None and self.min_bri > self.max_bri:
            raise ValueError("min_bri must be less than or equal to max_bri")
        if (self.hue is None) != (self.sat is None):
            raise ValueError("hue and sat must be given together, or not at all")
        return self


class GroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    light_ids: list[str] = Field(..., min_length=1)


# ---------- Console password ----------

@app.get("/api/auth")
async def auth_state(request: Request):
    return {
        "required": auth.is_enabled(),
        "authenticated": auth.is_authorized(request.scope),
    }


@app.post("/api/auth/login")
async def login(req: LoginRequest, response: Response):
    record = auth.password_record()
    if record is None:
        return {"ok": True, "required": False}
    if not auth.verify_password(record, req.password):
        raise HTTPException(401, "Wrong password")
    response.set_cookie(
        auth.COOKIE_NAME, auth.open_session(),
        httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30,
    )
    return {"ok": True, "required": True}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    auth.close_session(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@app.put("/api/auth/password")
async def set_password(req: SetPasswordRequest, response: Response):
    record = auth.password_record()
    if record is not None:
        if not req.current_password or not auth.verify_password(record, req.current_password):
            raise HTTPException(403, "Current password is wrong")
    auth.set_password(req.new_password)
    # set_password drops every session, including this caller's — hand back a
    # fresh one so whoever just set it isn't immediately locked out.
    response.set_cookie(
        auth.COOKIE_NAME, auth.open_session(),
        httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30,
    )
    return {"ok": True, "required": True}


@app.delete("/api/auth/password")
async def remove_password(req: ClearPasswordRequest):
    record = auth.password_record()
    if record is None:
        return {"ok": True, "required": False}
    if not auth.verify_password(record, req.current_password):
        raise HTTPException(403, "Current password is wrong")
    auth.clear_password()
    return {"ok": True, "required": False}


# ---------- Settings ----------

@app.get("/api/settings")
async def get_settings():
    return config_store.get_settings()


@app.put("/api/settings")
async def put_settings(req: SettingsRequest):
    settings = config_store.update_settings(
        max_commands_per_second=req.max_commands_per_second,
        restore_on_stop=req.restore_on_stop,
    )
    engine.limiter.set_rate(settings["max_commands_per_second"])
    engine.restore_on_stop = settings["restore_on_stop"]
    return settings


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
        raise HTTPException(502, f"Discovery failed: {e}") from e
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
        raise HTTPException(502, f"Could not reach bridge: {e}") from e
    out = []
    for lid, info in lights.items():
        state = info.get("state", {})
        out.append({
            "id": lid,
            "name": info.get("name", f"Light {lid}"),
            "on": state.get("on", False),
            "reachable": state.get("reachable", True),
            # The bulb's colour right now, so the UI can seed its swatch from
            # what the light is actually doing instead of a hardcoded default.
            "bri": state.get("bri"),
            "hue": state.get("hue"),
            "sat": state.get("sat"),
            "colormode": state.get("colormode"),
            "has_color": "hue" in state,
        })
    out.sort(key=lambda x: x["name"])
    return {"lights": out, "snapshots": engine.snapshots}


# ---------- Patterns ----------

@app.get("/api/patterns")
async def list_patterns():
    cfg = config_store.load()
    custom = list(cfg.get("custom_patterns", {}).values())
    return {"builtin": BUILTIN_PATTERNS, "custom": custom, "games": GAMES}


@app.post("/api/patterns")
async def create_pattern(req: CustomPatternRequest):
    # Match what the UI does before it posts, so a hand-rolled API call and a
    # copy-paste into the form accept exactly the same strings.
    seq = "".join(req.sequence.split()).lower()
    if not seq or any(c not in "abcdefghijklmnopqrstuvwxyz" for c in seq):
        raise HTTPException(400, "Sequence must only contain letters a-z")
    pid = f"custom_{uuid.uuid4().hex[:8]}"
    cfg = config_store.load()
    cfg["custom_patterns"][pid] = {
        "id": pid, "name": req.name.strip(), "sequence": seq, "hz": req.hz,
    }
    config_store.save(cfg)
    return cfg["custom_patterns"][pid]


@app.delete("/api/patterns/{pattern_id}")
async def delete_pattern(pattern_id: str):
    if pattern_id in BUILTIN_BY_ID:
        raise HTTPException(400, "Built-in game patterns can't be deleted")
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


@app.get("/api/patterns/export")
async def export_patterns():
    """Download your custom patterns as a pack file you can hand to someone."""
    cfg = config_store.load()
    custom = list(cfg.get("custom_patterns", {}).values())
    if not custom:
        raise HTTPException(404, "You haven't saved any custom patterns yet")
    pack = packs.build(custom)
    stamp = pack["exported_at"][:10]
    return JSONResponse(
        pack,
        headers={
            "Content-Disposition":
                f'attachment; filename="game-hue-flicker-patterns-{stamp}.json"',
        },
    )


@app.post("/api/patterns/import")
async def import_patterns(payload: dict = Body(...)):
    """Load a pack file. Reports what came in and what was left alone."""
    try:
        entries = packs.parse(payload)
    except packs.PackError as e:
        raise HTTPException(400, str(e)) from e

    cfg = config_store.load()
    existing = cfg["custom_patterns"]
    # An identical sequence under a new name is just the same effect twice in
    # the menu, so those are reported rather than added. Built-ins count too.
    by_sequence = {p["sequence"]: p["name"] for p in BUILTIN_PATTERNS}
    by_sequence.update({p["sequence"]: p["name"] for p in existing.values()})
    names = {p["name"] for p in existing.values()}

    added, skipped = [], []
    for entry in entries:
        clash = by_sequence.get(entry["sequence"])
        if clash is not None:
            skipped.append({"name": entry["name"],
                            "reason": f"same sequence as \"{clash}\""})
            continue
        name = packs.unique_name(entry["name"], names)
        pid = f"custom_{uuid.uuid4().hex[:8]}"
        existing[pid] = {"id": pid, "name": name, "sequence": entry["sequence"],
                         "hz": entry["hz"]}
        by_sequence[entry["sequence"]] = name
        names.add(name)
        added.append(existing[pid])

    if added:
        config_store.save(cfg)
    return {"ok": True, "added": added, "skipped": skipped,
            "pack_name": payload.get("name"), "author": payload.get("author")}


def _resolve_pattern(pattern_id: str) -> dict:
    if pattern_id in BUILTIN_BY_ID:
        return BUILTIN_BY_ID[pattern_id]
    custom = config_store.load().get("custom_patterns", {})
    if pattern_id in custom:
        return custom[pattern_id]
    raise HTTPException(404, f"Unknown pattern_id: {pattern_id}")


def _resolve_sequence(pattern_id: str) -> str:
    return _resolve_pattern(pattern_id)["sequence"]


# ---------- Groups ----------

@app.get("/api/groups")
async def list_groups():
    cfg = config_store.load()
    return {"groups": list(cfg.get("groups", {}).values())}


@app.post("/api/groups")
async def create_group(req: GroupRequest):
    gid = f"group_{uuid.uuid4().hex[:8]}"
    cfg = config_store.load()
    cfg["groups"][gid] = {"id": gid, "name": req.name.strip(), "light_ids": req.light_ids}
    config_store.save(cfg)
    return cfg["groups"][gid]


@app.put("/api/groups/{group_id}")
async def replace_group(group_id: str, req: GroupRequest):
    cfg = config_store.load()
    if group_id not in cfg["groups"]:
        raise HTTPException(404, f"Unknown group_id: {group_id}")
    cfg["groups"][group_id] = {"id": group_id, "name": req.name.strip(), "light_ids": req.light_ids}
    config_store.save(cfg)
    return cfg["groups"][group_id]


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: str):
    cfg = config_store.load()
    if cfg["groups"].pop(group_id, None) is None:
        raise HTTPException(404, f"Unknown group_id: {group_id}")
    config_store.save(cfg)
    return {"ok": True}


# ---------- Flicker control ----------

@app.get("/api/status")
async def get_status():
    return status_payload()


@app.post("/api/flicker/start")
async def start_flicker(req: StartRequest):
    if get_client() is None:
        raise HTTPException(400, "Bridge not configured yet")
    pattern = _resolve_pattern(req.pattern_id)
    sequence = pattern["sequence"]
    hz = req.hz if req.hz is not None else pattern.get("hz", DEFAULT_HZ)
    # Snapshot first — one bulk GET for the whole group — so Stop has something
    # to put back. Lights already running keep their earlier snapshot.
    await engine.capture(req.light_ids)
    # One epoch for the whole request: every light in a group then derives the
    # same frame from it and they flicker in step.
    epoch = time.monotonic()
    for lid in req.light_ids:
        await engine.start(
            lid, sequence, req.pattern_id, hz, req.min_bri, req.max_bri,
            req.hue, req.sat, req.transition_ms, epoch=epoch,
        )
    return {"ok": True, **status_payload()}


@app.post("/api/flicker/update")
async def update_flicker(req: UpdateRequest):
    changes = req.model_dump(exclude={"light_ids"}, exclude_none=True)
    if req.pattern_id is not None:
        pattern = _resolve_pattern(req.pattern_id)
        changes["sequence"] = pattern["sequence"]
        if req.hz is None:
            changes["hz"] = pattern.get("hz", DEFAULT_HZ)
    updated = [lid for lid in req.light_ids if engine.update(lid, **changes)]
    if not updated:
        raise HTTPException(409, "None of those lights are currently flickering")
    _broadcast_status_soon()
    return {"ok": True, "updated": updated, **status_payload()}


@app.post("/api/flicker/restore")
async def restore_lights(req: StopRequest):
    """Put lights back to the state captured before they started flickering."""
    if get_client() is None:
        raise HTTPException(400, "Bridge not configured yet")
    restored = await engine.restore(req.light_ids)
    _broadcast_status_soon()
    return {"ok": True, "restored": restored}


@app.post("/api/flicker/stop")
async def stop_flicker(req: StopRequest):
    if req.light_ids:
        for lid in req.light_ids:
            await engine.stop(lid)
    else:
        await engine.stop_all()
    return {"ok": True, **status_payload()}


# ---------- WebSocket ----------

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_json({"type": "status", **status_payload()})
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
