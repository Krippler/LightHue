import asyncio
import hashlib
import logging
import socket
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import (
    Body,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from . import auth, config_store, hue_client, hue_v2, packs
from .auth import ConsoleAuthMiddleware
from .dtls_psk import first_flight
from .flicker_engine import FlickerEngine
from .hue_client import BridgeAddressError, HueClient, parse_bridge_address
from .hue_stream import (
    MAX_AREA_LIGHTS,
    MAX_STREAM_HZ,
    STREAM_PORT,
    local_address_for,
    looks_translated,
    openssl_handshake,
    probe_handshake_stage,
    probe_stream_port,
    same_subnet_as_bridge,
)
from .hue_stream import (
    TRANSPORTS as STREAM_TRANSPORTS,
)
from .hue_v2 import (
    HueV2Client,
    can_render,
    channel_ids,
    streaming_state,
    v1_group_id,
    v1_light_id,
)
from .patterns import (
    BUILTIN_BY_ID,
    BUILTIN_PATTERNS,
    DEFAULT_MAX_BRI,
    DEFAULT_MIN_BRI,
    FRAMING_FIELDS,
    GAMES,
    framing_of,
)
from .stream_engine import StreamEngine, StreamError


async def _release_areas_left_claimed():
    """Hand back any entertainment area still recorded as ours."""
    client = get_client()
    if client is None:
        return
    api_key = config_store.load().get("api_key")
    try:
        groups = await client.get_groups()
    except Exception:
        logger.debug("Could not check for stranded entertainment areas", exc_info=True)
        return
    for gid, info in groups.items():
        if (info.get("type") or "") != ENTERTAINMENT_GROUP_TYPE:
            continue
        stream = info.get("stream") or {}
        if stream.get("active") and stream.get("owner") == api_key:
            logger.warning(
                "Entertainment area %s was left claimed by a previous run; releasing it",
                gid)
            try:
                await client.set_stream(gid, False)
            except Exception:
                logger.exception("Could not release stranded entertainment area %s", gid)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()
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

    # An entertainment area we still hold means the last run went away without
    # letting go — killed, redeployed, or crashed mid-stream. The bridge keeps
    # it claimed indefinitely, and while it does, those lights answer to nothing
    # at all: not this console, not the Hue app. Nothing else ever clears it,
    # so a container that restarts mid-stream would strand them for good.
    await _release_areas_left_claimed()
    yield
    # The area has to go back before anything else: while the bridge holds one
    # in streaming mode, nothing can drive those lights — not this console, not
    # the Hue app — so a container stopped mid-stream would strand them.
    streaming = stream_engine.area_id()
    streamed_lights = stream_engine.light_ids()
    stream_engine.stop()
    if streaming:
        await _finish_stream(streaming, streamed_lights)
    # Cancel the flicker loops before tearing down the pool they send through,
    # otherwise in-flight ticks fail against a closed client on the way out.
    await engine.stop_all()
    await hue_client.aclose()
    await hue_v2.aclose()


app = FastAPI(title="LightHue", lifespan=lifespan)
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
        "stream": stream_engine.status(),
    }


# The stream's sender runs on a thread of its own, so anything it needs done
# with the bridge has to be handed back to the loop explicitly.
_loop: "asyncio.AbstractEventLoop | None" = None


def _from_stream_thread(coro):
    if _loop is None or _loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(coro, _loop)
    except RuntimeError:
        logger.warning("Could not schedule stream cleanup; the loop is gone")


def _stream_stopped(area_id: str, light_ids: list[str]):
    """The sender has exited — for any reason, asked for or not."""
    _from_stream_thread(_finish_stream(area_id, light_ids))


async def _finish_stream(area_id: str, light_ids):
    """Hand the area back, then put the bulbs where they were.

    In that order: the bridge ignores REST for an area it is streaming, so a
    restore sent before the release goes nowhere. Streaming leaves a bulb on
    whatever the last frame held, which is why this matters at all — without it
    a room keeps the colour and brightness the pattern happened to stop on.
    """
    await _release_area(area_id)
    if light_ids and engine.restore_on_stop:
        try:
            await engine.restore(list(light_ids))
        except Exception:
            logger.exception("Could not put the streamed lights back")


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


def bridge_error(exc: Exception, doing: str) -> str:
    """A message that says what went wrong without quoting the response.

    The old text interpolated the httpx exception, which carries the upstream
    status line and full URL — enough to tell a live port from a dead one, and
    a 401 from a 404, for whatever host the console had been pointed at. Detail
    still goes to the log, where only the operator sees it.
    """
    logger.warning("Bridge request failed while trying to %s: %r", doing, exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"The bridge refused the request to {doing} — check the API key."
    if isinstance(exc, httpx.HTTPError):
        return f"Could not reach the bridge to {doing}."
    return f"The bridge sent something unexpected when asked to {doing}."


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
stream_engine = StreamEngine(on_change=_broadcast_status_soon,
                             on_stopped=_stream_stopped)


# ---------- Models ----------

class _BridgeAddress(BaseModel):
    bridge_ip: str

    @field_validator("bridge_ip")
    @classmethod
    def _checked(cls, value: str) -> str:
        # Rejected at the edge so the user gets a clear message; HueClient
        # checks again, so nothing reaches the network unvalidated either way.
        try:
            parse_bridge_address(value)
        except BridgeAddressError as e:
            raise ValueError(str(e)) from None
        return value.strip()


class BridgeSetRequest(_BridgeAddress):
    # Optional, because a bridge that moved network keeps its credentials: the
    # key lives on the bridge, not in its address. Leaving it out means "same
    # bridge, new address" — otherwise changing a DHCP lease would mean digging
    # a forty-character key out of the config file to type back in.
    api_key: str | None = Field(None, min_length=1, max_length=128)
    client_key: str | None = Field(None, max_length=128)


class PairRequest(_BridgeAddress):
    pass


class CustomPatternRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    sequence: str
    hz: float = Field(10.0, gt=0, le=20)
    min_bri: int = Field(DEFAULT_MIN_BRI, ge=1, le=254)
    max_bri: int = Field(DEFAULT_MAX_BRI, ge=1, le=254)
    transition_ms: int = Field(0, ge=0, le=60000)
    hue: int | None = Field(None, ge=0, le=65535)
    sat: int | None = Field(None, ge=0, le=254)

    @model_validator(mode="after")
    def _check_range(self):
        if self.min_bri > self.max_bri:
            raise ValueError("min_bri must be less than or equal to max_bri")
        if (self.hue is None) != (self.sat is None):
            raise ValueError("hue and sat must be given together, or not at all")
        return self


class LoginRequest(BaseModel):
    password: str


class SetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=4, max_length=128)
    current_password: str | None = None


class ClearPasswordRequest(BaseModel):
    current_password: str


class SettingsRequest(BaseModel):
    """Whatever is supplied is changed; anything left out is left alone.

    A default here would mean a caller sending one setting silently resets the
    other, which is a nasty way to lose a preference.
    """

    max_commands_per_second: float | None = Field(None, ge=1.0, le=30.0)
    restore_on_stop: bool | None = None
    stream_settle_ms: int | None = Field(None, ge=0, le=10000)


class StartRequest(BaseModel):
    light_ids: list[str] = Field(..., min_length=1)
    pattern_id: str
    # None means "whatever this pattern was written for" — speed is part of the
    # pattern, so a caller that doesn't care shouldn't have to guess a number.
    hz: float | None = Field(None, gt=0, le=20, description="Hue can't usefully go faster")
    min_bri: int | None = Field(None, ge=1, le=254)
    max_bri: int | None = Field(None, ge=1, le=254)
    hue: int | None = Field(None, ge=0, le=65535)
    sat: int | None = Field(None, ge=0, le=254)
    transition_ms: int | None = Field(None, ge=0, le=60000)

    @model_validator(mode="after")
    def _check_ranges(self):
        if (self.min_bri is not None and self.max_bri is not None
                and self.min_bri > self.max_bri):
            raise ValueError("min_bri must be less than or equal to max_bri")
        if (self.hue is None) != (self.sat is None):
            raise ValueError("hue and sat must be given together, or not at all")
        return self


class StreamStartRequest(BaseModel):
    """Run a pattern across a whole entertainment area over the DTLS stream."""

    area_id: str = Field(..., min_length=1, max_length=64)
    pattern_id: str
    # The stream's ceiling, not the REST path's: 25 frames a second, and the
    # area gets all of it rather than a share.
    hz: float | None = Field(None, gt=0, le=MAX_STREAM_HZ)
    min_bri: int | None = Field(None, ge=1, le=254)
    max_bri: int | None = Field(None, ge=1, le=254)
    # Interpolated here rather than by the bridge: a HueStream frame is a colour
    # and nothing else, so there is nowhere to ask for a ramp.
    transition_ms: int | None = Field(None, ge=0, le=60000)
    hue: int | None = Field(None, ge=0, le=65535)
    sat: int | None = Field(None, ge=0, le=254)

    @model_validator(mode="after")
    def _check_ranges(self):
        if (self.min_bri is not None and self.max_bri is not None
                and self.min_bri > self.max_bri):
            raise ValueError("min_bri must be less than or equal to max_bri")
        if (self.hue is None) != (self.sat is None):
            raise ValueError("hue and sat must be given together, or not at all")
        return self


class StreamAreaRequest(BaseModel):
    area_id: str = Field(..., min_length=1, max_length=64)


class StreamUpdateRequest(BaseModel):
    hz: float | None = Field(None, gt=0, le=MAX_STREAM_HZ)
    min_bri: int | None = Field(None, ge=1, le=254)
    max_bri: int | None = Field(None, ge=1, le=254)
    transition_ms: int | None = Field(None, ge=0, le=60000)
    hue: int | None = Field(None, ge=0, le=65535)
    sat: int | None = Field(None, ge=0, le=254)
    pattern_id: str | None = None


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
    changes = req.model_dump(exclude_none=True)
    settings = config_store.update_settings(**changes) if changes \
        else config_store.get_settings()
    engine.limiter.set_rate(settings["max_commands_per_second"])
    engine.restore_on_stop = settings["restore_on_stop"]
    # The new ceiling changes what every running light is actually getting, and
    # each card reports that. Nothing else pushes a status between ticks, so
    # without this the notes sit at the old figure until something starts or
    # stops — making a rate change look like it did nothing.
    _broadcast_status_soon()
    return settings


# ---------- Bridge setup ----------

@app.get("/api/bridge")
async def get_bridge():
    cfg = config_store.load()
    return {
        "bridge_ip": cfg.get("bridge_ip"),
        "configured": bool(cfg.get("bridge_ip") and cfg.get("api_key")),
        # Never the key itself — just whether streaming is available, which is
        # what the UI needs to decide whether to offer it or ask for a re-pair.
        "can_stream": bool(cfg.get("client_key")),
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
    config_store.update(bridge_ip=req.bridge_ip, api_key=result["api_key"],
                        client_key=result.get("client_key"),
                        keys_paired=bool(result.get("client_key")))
    return {
        "bridge_ip": req.bridge_ip,
        "configured": True,
        # False on firmware old enough not to issue one. Everything except
        # entertainment streaming still works, so it is worth saying rather
        # than failing the pairing.
        "can_stream": bool(result.get("client_key")),
    }


@app.post("/api/bridge/set")
async def set_bridge(req: BridgeSetRequest):
    stored = config_store.load()
    if req.api_key is None and not stored.get("api_key"):
        raise HTTPException(
            400, "No API key stored yet, so one has to be supplied — pair with the "
                 "bridge, or paste a key you already have.")
    # Only overwrite what was actually supplied. A bridge that moved network
    # still knows this console, so re-entering the address must not cost it the
    # key, and a manual save must not silently cost it the streaming key either.
    changes = {"bridge_ip": req.bridge_ip}
    if req.api_key is not None:
        changes["api_key"] = req.api_key
    if req.client_key is not None:
        changes["client_key"] = req.client_key or None
    # The two keys are one credential. The streaming handshake offers the api
    # key as its PSK identity and the client key as the PSK, so keeping a client
    # key from an older pairing beside a new api key builds an offer out of two
    # halves that never belonged together — and the bridge is under no
    # obligation to explain itself about that. Changing one alone drops the
    # other, which costs a re-pair and says so.
    replacing = "api_key" in changes and changes["api_key"] != stored.get("api_key")
    if replacing and "client_key" not in changes:
        # Known split: the client key that was there belonged to the key being
        # replaced, so it is gone, and we can say why.
        changes["client_key"] = None
        changes["keys_paired"] = False
    elif "api_key" in changes or "client_key" in changes:
        # Typed in by hand. They may well be a matched set copied from
        # somewhere, so this is not evidence of a fault — only of nobody having
        # watched the bridge issue them.
        changes["keys_paired"] = None
    config_store.update(**changes)
    return {"ok": True, "can_stream": bool(changes.get("client_key",
                                                       stored.get("client_key")))}


# ---------- Lights ----------

@app.get("/api/lights")
async def list_lights():
    client = get_client()
    if client is None:
        raise HTTPException(400, "Bridge not configured yet")
    try:
        lights = await client.get_lights()
    except Exception as e:
        raise HTTPException(502, bridge_error(e, "read the lights")) from e
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
        "id": pid, "name": req.name.strip(), "sequence": seq,
        **{f: getattr(req, f) for f in FRAMING_FIELDS},
    }
    config_store.save(cfg)
    return cfg["custom_patterns"][pid]


@app.put("/api/patterns/{pattern_id}")
async def replace_pattern(pattern_id: str, req: CustomPatternRequest):
    """Edit a custom pattern in place, keeping its id.

    Keeping the id matters: light cards, the stream panel and any saved
    selection all refer to a pattern by it, so a save that minted a new one
    would leave every one of them pointing at something that no longer exists.
    """
    if pattern_id in BUILTIN_BY_ID:
        raise HTTPException(400, "Built-in game patterns can't be edited")
    seq = "".join(req.sequence.split()).lower()
    if not seq or any(c not in "abcdefghijklmnopqrstuvwxyz" for c in seq):
        raise HTTPException(400, "Sequence must only contain letters a-z")
    cfg = config_store.load()
    if pattern_id not in cfg["custom_patterns"]:
        raise HTTPException(404, f"Unknown pattern_id: {pattern_id}")
    # A running loop holds its own copy of the sequence, so editing under it
    # would leave lights flickering something the UI no longer describes.
    in_use = [lid for lid, st in engine.status().items()
              if st.get("running") and st.get("pattern_id") == pattern_id]
    if in_use:
        raise HTTPException(
            409,
            f"That pattern is running on {len(in_use)} light(s) — stop them first",
        )
    cfg["custom_patterns"][pattern_id] = {
        "id": pattern_id, "name": req.name.strip(), "sequence": seq,
        **{f: getattr(req, f) for f in FRAMING_FIELDS},
    }
    config_store.save(cfg)
    return cfg["custom_patterns"][pattern_id]


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
                         **{f: entry[f] for f in FRAMING_FIELDS}}
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

# What the Hue app calls a Room or a Zone. Luminaire and LightSource describe
# the innards of a single fitting, so neither is worth offering as a flicker
# group. Entertainment areas are left out here too, but for the opposite
# reason: they are offered separately, as the thing streaming runs on.
IMPORTABLE_GROUP_TYPES = {"Room", "Zone", "LightGroup"}

ENTERTAINMENT_GROUP_TYPE = "Entertainment"


@app.get("/api/bridge/groups")
async def list_bridge_groups():
    """The rooms and zones already set up in the Hue app, ready to copy over."""
    client = get_client()
    if client is None:
        raise HTTPException(400, "Bridge not configured yet")
    try:
        groups = await client.get_groups()
    except Exception as e:
        raise HTTPException(502, bridge_error(e, "read the rooms")) from e

    out = []
    seen: dict[str, int] = {}
    for gid, info in groups.items():
        kind = info.get("type") or "unknown"
        seen[kind] = seen.get(kind, 0) + 1
        if kind not in IMPORTABLE_GROUP_TYPES:
            continue
        out.append({
            "id": gid,
            "name": info.get("name", f"Group {gid}"),
            "type": kind,
            # Rooms carry a class like "Kitchen"; zones generally don't.
            "class": info.get("class"),
            "light_ids": [str(x) for x in info.get("lights", [])],
        })
    out.sort(key=lambda g: (g["type"] != "Room", g["name"].casefold()))
    # "seen" lets the UI say why nothing is on offer — a bridge with no groups
    # at all and one with only luminaires are different problems.
    return {"groups": out, "seen": seen, "total": len(groups)}


async def _v2_area_uuids() -> dict:
    """v1 group id -> v2 entertainment configuration id, where one exists."""
    cfg = config_store.load()
    if not cfg.get("bridge_ip") or not cfg.get("api_key"):
        return {}
    try:
        v2 = HueV2Client(cfg["bridge_ip"], cfg["api_key"])
        configurations = await v2.entertainment_configurations()
    except Exception:
        logger.debug("Could not read v2 entertainment configurations", exc_info=True)
        return {}
    return {gid: c["id"] for c in configurations
            if (gid := v1_group_id(c)) and c.get("id")}


@app.get("/api/stream/areas")
async def list_stream_areas():
    """Entertainment areas the bridge will stream to.

    These are set up in the Hue app under Entertainment areas, not here: the
    bridge will only stream to one it already knows about, and it wants each
    light positioned in the room before it accepts the area at all.
    """
    client = get_client()
    if client is None:
        raise HTTPException(400, "Bridge not configured yet")
    try:
        groups = await client.get_groups()
    except Exception as e:
        raise HTTPException(502, bridge_error(e, "read the entertainment areas")) from e

    cfg_api_key = config_store.load().get("api_key")
    # The v2 id of each area, keyed by the v1 group it shows up as. Deleting or
    # renaming an area addresses the configuration itself, and the group number
    # is only a compatibility view of it. Absent on firmware that has no v2
    # entertainment API, which is why this is looked up rather than assumed.
    uuids = await _v2_area_uuids()
    out = []
    for gid, info in groups.items():
        if (info.get("type") or "") != ENTERTAINMENT_GROUP_TYPE:
            continue
        light_ids = [str(x) for x in info.get("lights", [])]
        stream = info.get("stream") or {}
        owner = stream.get("owner")
        # Worked out here rather than by handing the UI the API key to compare
        # against: the key is the one thing this endpoint must never return.
        claimed_by_us = bool(stream.get("active")) and owner == cfg_api_key
        out.append({
            "id": gid,
            "uuid": uuids.get(gid),
            "name": info.get("name", f"Area {gid}"),
            "light_ids": light_ids,
            # The bridge caps an area at ten lights, so this should never trip;
            # it is reported rather than assumed so a surprise is visible.
            "too_many_lights": len(light_ids) > MAX_AREA_LIGHTS,
            # Someone else streaming to it — Hue Sync, a game — means the
            # bridge will refuse us until they let go.
            "in_use_by_someone_else": bool(stream.get("active")) and not claimed_by_us,
            # A claim this console left behind is ours to take back; anyone
            # else's is a real conflict. Said plainly so the UI doesn't have to
            # infer it from a failure.
            "claimed_by_us": claimed_by_us,
        })
    out.sort(key=lambda a: a["name"].casefold())
    cfg = config_store.load()
    return {
        "areas": out,
        # Without a client key there is no DTLS credential, and the only way to
        # get one is to pair again.
        "can_stream": bool(cfg.get("client_key")),
        "max_stream_hz": MAX_STREAM_HZ,
        # The UI quotes both ceilings in its help text, and this is the one
        # call it always makes before drawing the entertainment panel.
        "max_lights": MAX_AREA_LIGHTS,
    }


async def _area_or_400(area_id: str) -> dict:
    client = get_client()
    if client is None:
        raise HTTPException(400, "Bridge not configured yet")
    try:
        groups = await client.get_groups()
    except Exception as e:
        raise HTTPException(502, bridge_error(e, "read the entertainment areas")) from e
    info = groups.get(area_id)
    if not info or (info.get("type") or "") != ENTERTAINMENT_GROUP_TYPE:
        raise HTTPException(404, "That entertainment area isn't on this bridge")
    return info


async def _release_area(area_id: str) -> bool:
    """Hand the area back, whatever else went wrong, by whichever route armed it.

    While the bridge has an area in streaming mode nothing else can drive those
    lights — not this console, not the Hue app — so failing to release one
    leaves the user's lights stuck until they restart the bridge. Returns
    whether the bridge actually let go.
    """
    client = get_client()
    if client is None:
        return False
    configuration = await _v2_configuration(area_id)
    if configuration is not None:
        await _arm_v2(configuration, False)
    try:
        await client.set_stream(area_id, False)
    except Exception:
        logger.exception("Could not hand entertainment area %s back", area_id)

    # Check it actually let go. Firing both routes and trusting the response was
    # the same mistake the arming path made: the call succeeds, the area stays
    # held, and the lights stay stuck until the bridge is restarted — with
    # nothing anywhere saying so.
    if await _await_stream_flag(client, area_id, False, timeout=3.0):
        return True
    logger.warning("Area %s is still claimed after being released; trying once more",
                   area_id)
    try:
        await client.set_stream(area_id, False)
    except Exception:
        logger.exception("Could not hand entertainment area %s back", area_id)
    released = await _await_stream_flag(client, area_id, False, timeout=3.0)
    if not released:
        logger.error("Area %s is stranded: the bridge still reports it as streaming",
                     area_id)
    return released


# What the last start attempt actually did, step by step. Streaming failures
# happen against hardware that isn't here, and "timed out" on its own says
# almost nothing — this is the difference between a guess and a diagnosis.
_last_attempt: dict = {}
_last_local_address = None


def _note_local_address(cfg) -> str | None:
    """Which local address a datagram to the bridge would leave from.

    Read by opening a socket and asking, without sending anything. The bridge
    answers to the address it saw, so on a host with several interfaces — or
    inside container networking — this is the first thing to check when nothing
    comes back.
    """
    global _last_local_address
    try:
        host, _ = parse_bridge_address(cfg["bridge_ip"])
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((host, STREAM_PORT))
            _last_local_address = f"{probe.getsockname()[0]}:{probe.getsockname()[1]}"
        finally:
            probe.close()
    except Exception:
        _last_local_address = None
    return _last_local_address


def _client_hello_shape() -> dict:
    try:
        flight = first_flight(config_store.load().get("api_key") or "lighthue")
    except Exception:
        logger.debug("Could not build a sample ClientHello", exc_info=True)
        return {}
    return {"bytes": len(flight), "hex": flight.hex()}


async def _bridge_identity() -> dict:
    client = get_client()
    if client is None:
        return {}
    try:
        config = await client.get_config()
    except Exception:
        logger.debug("Could not read the bridge's config", exc_info=True)
        return {}
    return {k: config.get(k) for k in
            ("name", "modelid", "swversion", "apiversion", "bridgeid",
             "starterkitid", "factorynew")
            if config.get(k) is not None}


def _pairing_provenance(cfg: dict) -> str:
    """Whether the two streaming credentials are known to belong together.

    Deliberately not a boolean. "unknown" is the honest answer for keys typed in
    by hand or stored before this was tracked, and reporting those as "no" would
    make every older console look like it had a fault to chase.
    """
    paired = cfg.get("keys_paired")
    if paired is None:
        return "unknown"
    return "yes" if paired else "no"


def _arm_looked_real() -> bool:
    """Did the last arm end with the bridge itself reporting the area as up?

    Not "did the call succeed" — that only says the bridge took the word. An
    area that reads inactive right afterwards was never armed, and saying so is
    worth more than another sentence about the network.
    """
    for step in reversed(_last_attempt.get("steps", [])):
        if step["step"] in ("armed", "re-armed"):
            says = step.get("bridge_says") or {}
            return says.get("status") == "active" or bool(says.get("active"))
    return False


def _note(step: str, **detail):
    _last_attempt.setdefault("steps", []).append(
        {"step": step, "at": round(time.monotonic() - _last_attempt.get("t0", 0), 2), **detail})
    logger.info("stream/start %s %s", step, detail or "")


async def _stream_flag(client, area_id: str) -> bool:
    """What the bridge currently thinks about this area's stream."""
    groups = await client.get_groups()
    return bool(((groups.get(area_id) or {}).get("stream") or {}).get("active"))


async def _await_stream_flag(client, area_id: str, wanted: bool,
                             timeout: float = 5.0) -> bool:
    """Wait for the bridge to actually be in the state we asked for.

    Setting the flag and sleeping a guessed interval was the bug: the REST call
    returns before the bridge has finished, and claiming an area while it is
    still tearing the last session down leaves it in a state where it accepts
    the claim and then never answers on the streaming port. How long that takes
    is not ours to guess, so we ask until it is true.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            if await _stream_flag(client, area_id) is wanted:
                return True
        except Exception:
            logger.debug("Could not read back area %s", area_id, exc_info=True)
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.25)


async def _v2_configuration(area_id: str) -> dict | None:
    """The v2 entertainment configuration behind a v1 group id, if there is one.

    An area made by the current Hue app lives in v2; the v1 group is a
    compatibility view of it. Setting stream.active through that view binds the
    port without arming the service, so the v2 record is what actually matters.
    Absent on older firmware, where the v1 flag is the real thing.
    """
    cfg = config_store.load()
    if not cfg.get("bridge_ip") or not cfg.get("api_key"):
        return None
    try:
        v2 = HueV2Client(cfg["bridge_ip"], cfg["api_key"])
        for configuration in await v2.entertainment_configurations():
            if v1_group_id(configuration) == str(area_id):
                return configuration
    except Exception:
        logger.debug("No v2 entertainment configuration for area %s", area_id,
                     exc_info=True)
    return None


async def _await_v2_status(v2, area_uuid: str, wanted: str,
                           tries: int = 10, delay: float = 0.3) -> dict:
    """Poll a v2 configuration until it reports the status we asked for.

    The PUT returning 200 only says the bridge accepted the word "start". It
    does not say the stream came up, and a configuration that still reads
    inactive afterwards was never armed — which looks, from the socket, exactly
    like a bridge that will not answer a handshake.
    """
    state = {"status": None, "active_streamer": None}
    for _ in range(tries):
        try:
            state = streaming_state(await v2.configuration(area_uuid))
        except Exception:
            logger.debug("Could not read back v2 configuration %s", area_uuid,
                         exc_info=True)
            return state
        if state["status"] == wanted:
            return state
        await asyncio.sleep(delay)
    return state


async def _arm_v2(configuration: dict, active: bool) -> dict | None:
    """Start or stop a v2 entertainment configuration, and check that it took.

    Starting clears any existing hold first. A configuration the bridge already
    considers active — because a previous session died without stopping it —
    treats a second "start" as nothing to do, and the port stays bound to a
    session that no longer exists. That is the shape of "works once, then times
    out forever": the socket is open, and the thing behind it is a ghost.

    Returns what the bridge reports afterwards, or None if the call failed.
    """
    cfg = config_store.load()
    area_uuid = configuration["id"]
    try:
        v2 = HueV2Client(cfg["bridge_ip"], cfg["api_key"])
        if active:
            with suppress(Exception):
                await v2.set_streaming(area_uuid, False)
                await _await_v2_status(v2, area_uuid, "inactive")
        await v2.set_streaming(area_uuid, active)
        return await _await_v2_status(v2, area_uuid,
                                      "active" if active else "inactive")
    except Exception as e:
        logger.warning("Could not %s the v2 entertainment configuration: %s",
                       "start" if active else "stop", e)
        return None


async def _arm_area(client, area_id: str, configuration: dict | None) -> dict:
    """Arm the stream by whichever route this bridge actually listens to.

    One place, so a retry cannot re-claim over v1 an area that was armed over
    v2 — which would flip the compatibility flag while leaving the real switch
    untouched, and look for all the world like the claim had worked.

    Returns what to record about the arm: which route was used, and what the
    bridge said about the area once it was done.
    """
    if configuration is not None:
        state = await _arm_v2(configuration, True)
        if state is not None:
            return {"over": "v2", "bridge_says": state}
        logger.warning("v2 arming failed for area %s; falling back to the v1 flag",
                       area_id)
    await _claim_area(client, area_id)
    return {"over": "v1", "bridge_says": await _v1_stream_state(client, area_id)}


async def _v1_stream_state(client, area_id: str) -> dict:
    try:
        groups = await client.get_groups()
    except Exception:
        logger.debug("Could not read back group %s", area_id, exc_info=True)
        return {}
    return (groups.get(area_id) or {}).get("stream") or {}


async def _claim_area(client, area_id: str):
    """Take the area for streaming, clearing any stale hold first.

    The bridge keeps an area claimed until something tells it otherwise, and a
    session that died without releasing leaves the claim behind. From the
    outside that looks like the bridge simply not answering on port 2100: the
    REST call to claim it succeeds, and then the handshake times out.

    Both transitions are waited for rather than assumed. The previous session
    has to be fully down before the next claim, or the bridge ends up holding
    an area it is not listening for — which is exactly the "works once, then
    times out until the container restarts" shape.
    """
    try:
        await client.set_stream(area_id, False)
        if not await _await_stream_flag(client, area_id, False):
            logger.warning("Area %s still reads as claimed after being released", area_id)
    except Exception:
        # Nothing held it, or the bridge disliked being told so. Either way the
        # claim below is the call that actually matters.
        logger.debug("Pre-emptive release of area %s did nothing", area_id, exc_info=True)
    await client.set_stream(area_id, True)
    if not await _await_stream_flag(client, area_id, True):
        raise HTTPException(
            502,
            "The bridge accepted the area but never reported it as streaming. "
            "Give it a moment and try again, or use Release area first.",
        )


class AreaRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=32)
    light_ids: list[str] = Field(..., min_length=1, max_length=MAX_AREA_LIGHTS)


@app.get("/api/stream/candidates")
async def stream_candidates():
    """Lights that could go in an entertainment area.

    Not every bulb can. An area is built from each light's *entertainment
    service*, and a light without one — a plug, a white-only bulb — has nothing
    to put in. That is the real difference between an area and a group, so the
    lights that cannot join are returned too rather than quietly missing.
    """
    cfg = config_store.load()
    client = get_client()
    if client is None or not cfg.get("api_key"):
        raise HTTPException(400, "Bridge not configured yet")
    try:
        v2 = HueV2Client(cfg["bridge_ip"], cfg["api_key"])
        services = await v2.entertainment_services()
        lights = await client.get_lights()
    except Exception as e:
        raise HTTPException(502, bridge_error(e, "read the lights")) from e

    renderable = {}
    for service in services:
        light_id = v1_light_id(service)
        if light_id and can_render(service):
            renderable[light_id] = service["id"]

    return {
        "candidates": sorted(
            ({"light_id": lid,
              "name": (info.get("name") or f"Light {lid}"),
              "service_rid": renderable[lid]}
             for lid, info in lights.items() if lid in renderable),
            key=lambda c: c["name"].casefold()),
        "excluded": sorted(
            ({"light_id": lid, "name": (info.get("name") or f"Light {lid}")}
             for lid, info in lights.items() if lid not in renderable),
            key=lambda c: c["name"].casefold()),
        "max_lights": MAX_AREA_LIGHTS,
    }


@app.post("/api/stream/areas")
async def create_stream_area(req: AreaRequest):
    """Create an entertainment area on the bridge.

    This writes to the user's Hue setup rather than to this console's config —
    the area shows up in the Hue app and outlives this container — so it is
    worth being deliberate about. Only lights that can actually render are
    accepted, and the bridge's ten-light ceiling is checked here so the failure
    names the problem instead of arriving as a validation error from the bridge.
    """
    cfg = config_store.load()
    client = get_client()
    if client is None or not cfg.get("api_key"):
        raise HTTPException(400, "Bridge not configured yet")
    try:
        v2 = HueV2Client(cfg["bridge_ip"], cfg["api_key"])
        services = await v2.entertainment_services()
    except Exception as e:
        raise HTTPException(502, bridge_error(e, "read the lights")) from e

    renderable = {v1_light_id(s): s["id"] for s in services if can_render(s)}
    wanted = [str(x) for x in req.light_ids]
    if len(set(wanted)) != len(wanted):
        raise HTTPException(422, "The same light is listed more than once")
    missing = [lid for lid in wanted if lid not in renderable]
    if missing:
        raise HTTPException(
            422,
            f"{'These lights cannot' if len(missing) > 1 else 'This light cannot'} "
            f"be in an entertainment area: {', '.join(missing)}. Only "
            "colour-capable lights can — plugs and white-only bulbs have no "
            "entertainment service for the bridge to stream to.")
    try:
        area_id = await v2.create_entertainment_configuration(
            req.name, [renderable[lid] for lid in wanted])
    except Exception as e:
        raise HTTPException(502, bridge_error(e, "create the entertainment area")) from e
    return {"ok": True, "id": area_id, "name": req.name, "light_ids": wanted}


class AreaRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=32)


@app.put("/api/stream/areas/{area_uuid}")
async def rename_stream_area(area_uuid: str, req: AreaRenameRequest):
    cfg = config_store.load()
    if not cfg.get("bridge_ip") or not cfg.get("api_key"):
        raise HTTPException(400, "Bridge not configured yet")
    try:
        v2 = HueV2Client(cfg["bridge_ip"], cfg["api_key"])
        await v2.rename_entertainment_configuration(area_uuid, req.name)
    except Exception as e:
        raise HTTPException(502, bridge_error(e, "rename the entertainment area")) from e
    return {"ok": True, "name": req.name}


@app.delete("/api/stream/areas/{area_uuid}")
async def delete_stream_area(area_uuid: str):
    """Remove an entertainment area from the bridge.

    Takes the v2 id rather than the v1 group number, because that is what
    identifies the configuration itself — the group number is a compatibility
    view of it.
    """
    cfg = config_store.load()
    if not cfg.get("bridge_ip") or not cfg.get("api_key"):
        raise HTTPException(400, "Bridge not configured yet")
    if stream_engine.running:
        raise HTTPException(
            409, "Stop the stream before deleting an area.")
    try:
        v2 = HueV2Client(cfg["bridge_ip"], cfg["api_key"])
        await v2.delete_entertainment_configuration(area_uuid)
    except Exception as e:
        raise HTTPException(502, bridge_error(e, "delete the entertainment area")) from e
    return {"ok": True}


@app.post("/api/stream/release")
async def release_stream_area(req: StreamAreaRequest):
    """Hand an area back by hand, for when a claim outlived its session.

    Recovering otherwise means restarting the bridge, because an area it thinks
    is being streamed to answers to nothing else — not this console, not the
    Hue app.
    """
    client = get_client()
    if client is None:
        raise HTTPException(400, "Bridge not configured yet")
    if stream_engine.area_id() == req.area_id:
        stream_engine.stop()
    try:
        await client.set_stream(req.area_id, False)
        await _await_stream_flag(client, req.area_id, False)
    except Exception as e:
        raise HTTPException(502, bridge_error(e, "hand the area back")) from e
    _broadcast_status_soon()
    return {"ok": True, **status_payload()}


@app.get("/api/stream/diagnostics")
async def stream_diagnostics():
    """What the bridge says, and what the last start attempt did.

    Streaming fails against hardware that isn't in front of whoever is fixing
    it, so this exists to be pasted into a bug report.
    """
    cfg = config_store.load()
    out = {
        "can_stream": bool(cfg.get("client_key")),
        # The handshake offers the api key as its PSK identity and the client
        # key as the PSK. Two halves of two pairings is a credential the bridge
        # has no reason to answer, and nothing else here would show it.
        "keys_from_same_pairing": _pairing_provenance(cfg),
        # Which bridge, running what. Never captured until now, and the first
        # thing anyone else looking at a streaming fault would ask for.
        "bridge": await _bridge_identity(),
        "engine": stream_engine.status(),
        "last_attempt": _last_attempt or None,
        "stream_port": STREAM_PORT,
        # What the opening datagram looks like, for reading a packet capture
        # against. A capture that shows this leaving and nothing coming back
        # narrows the question to whether these bytes are acceptable, and that
        # is only answerable against the bytes themselves.
        "client_hello": _client_hello_shape(),
        # Where a reply would have to come back to. On a host with more than one
        # interface this is the first thing worth checking when nothing does.
        "local_address": _last_local_address,
        "areas": None,
        "bridge_error": None,
    }
    client = get_client()
    if client is None:
        out["bridge_error"] = "Bridge not configured"
        return out
    try:
        host, _ = parse_bridge_address(cfg["bridge_ip"])
        state, how = await asyncio.to_thread(probe_stream_port, host, STREAM_PORT)
        local = local_address_for(host)
        translated = bool(local and looks_translated(local))
        out["network"] = {
            "bridge": host,
            "we_appear_as": local,
            # Inconclusive behind Docker's NAT: the bridge sees the host's
            # address, not this one, so comparing this one proves nothing.
            "same_subnet": None if translated else (
                same_subnet_as_bridge(local, host) if local else None),
            "behind_container_nat": translated,
            "note": (
                "this is a container address, translated to the host's before the "
                "bridge sees it — host networking removes the translation and makes "
                "the comparison meaningful"
                if translated else
                "streaming wants the client on the bridge's own network; REST does "
                "not, which is why everything else works across a routed hop"
            ),
        }
        out["udp_to_stream_port"] = {
            "state": state, "detail": how,
            # Filled in below, once the bridge has been asked what it is
            # actually holding. Deriving it from our own engine was wrong: an
            # area left claimed by a failed attempt is exactly the case worth
            # naming, and the engine knows nothing about it.
            "note": None,
        }
    except Exception as e:
        out["udp_to_stream_port"] = {"reachable": None, "detail": str(e)}
    try:
        groups = await client.get_groups()
    except Exception as e:
        out["bridge_error"] = bridge_error(e, "read the entertainment areas")
        return out
    out["areas"] = {
        gid: {
            "name": info.get("name"),
            "lights": info.get("lights"),
            # Verbatim, including proxymode and proxynode: whatever the bridge
            # thinks is what matters here, not our reading of it.
            "stream": info.get("stream"),
            # Whether the lights were ever positioned. The bridge refuses to
            # stream to an area that was never finished in the Hue app.
            "has_locations": bool(info.get("locations")),
        }
        for gid, info in groups.items()
        if (info.get("type") or "") == ENTERTAINMENT_GROUP_TYPE
    }
    if isinstance(out.get("udp_to_stream_port"), dict):
        out["udp_to_stream_port"]["note"] = _probe_note(out["areas"],
                                                        cfg.get("api_key"))
    return out


def _probe_note(areas: dict, api_key: str | None) -> str:
    """What the port probe means, given what the bridge is holding.

    The bridge only binds UDP 2100 while it holds an area, so the same answer
    means opposite things either side of a claim: refused with nothing claimed
    says the path is healthy, refused with an area claimed says the bridge took
    the claim without arming anything.

    Who holds it matters as much as whether anyone does. An area this console
    claimed and never released is stranded and worth fixing; the same area held
    by Hue Sync or a game is working exactly as intended, and calling that a
    fault would send someone chasing their own television.
    """
    ours, theirs = [], []
    for gid, area in areas.items():
        stream = area.get("stream") or {}
        if not stream.get("active"):
            continue
        (ours if stream.get("owner") == api_key else theirs).append(gid)

    if ours:
        return (f"probed while this console is holding area {', '.join(ours)} — if no "
                "stream is running, that area is stranded and Release area will free it")
    if theirs:
        return (f"probed while something else is streaming to area {', '.join(theirs)} "
                "— the port is bound for that session, not for us")
    return "probed with no area claimed, so a refusal here is the healthy answer"


@app.post("/api/stream/start")
async def start_stream(req: StreamStartRequest):
    _last_attempt.clear()
    _last_attempt.update({"t0": time.monotonic(), "area_id": req.area_id, "steps": []})
    cfg = config_store.load()
    if not cfg.get("client_key"):
        raise HTTPException(
            409,
            "This console was paired before it could stream. Pair with the bridge "
            "again — press its link button and hit Pair — to get a streaming key.",
        )
    info = await _area_or_400(req.area_id)
    light_ids = [str(x) for x in info.get("lights", [])]
    if not light_ids:
        raise HTTPException(422, "That entertainment area has no lights in it")
    stream_info = info.get("stream") or {}
    if stream_info.get("active"):
        # The bridge records who claimed it. Ours means a session that died
        # without letting go — a killed container, a dropped network — and the
        # claim below clears it. Anyone else's is a real conflict: two things
        # streaming to one area would fight over every frame.
        owner = stream_info.get("owner")
        if owner and owner != cfg.get("api_key"):
            raise HTTPException(
                409,
                "Something else is already streaming to that area — Hue Sync, or a game. "
                "Stop it there first, or use Release area to take it back.",
            )
        logger.info("Area %s was left claimed by this console; taking it back", req.area_id)

    pattern = _resolve_pattern(req.pattern_id)
    framing = framing_of(pattern)
    for field in FRAMING_FIELDS:
        if field in ("hue", "sat"):
            if field in req.model_fields_set:
                framing[field] = getattr(req, field)
            continue
        supplied = getattr(req, field, None)
        if supplied is not None:
            framing[field] = supplied
    if (framing["hue"] is None) != (framing["sat"] is None):
        framing["hue"] = framing["sat"] = None
    if framing["min_bri"] > framing["max_bri"]:
        raise HTTPException(
            422, "min_bri must be less than or equal to max_bri "
                 f"(got {framing['min_bri']} against the pattern's {framing['max_bri']})",
        )

    # The REST path and the stream would fight over any light in both, and the
    # stream wins because the bridge stops listening to REST for those lights.
    for lid in light_ids:
        await engine.stop(lid, notify=False, restore=False)

    # Read the bulbs before the area is claimed: once the bridge is streaming
    # to it, REST no longer speaks for those lights, and there would be nothing
    # to put back afterwards. Lights already holding a snapshot keep it.
    engine.expect_batch(len(light_ids) + 1)
    try:
        await engine.capture(light_ids)
    except Exception:
        logger.warning("Could not snapshot the area's lights before streaming",
                       exc_info=True)

    client = get_client()
    configuration = await _v2_configuration(req.area_id)
    _note("before-claim", bridge_says=(info.get("stream") or {}),
          has_locations=bool(info.get("locations")),
          v2_configuration=bool(configuration))
    try:
        # The v1 flag binds the port; on a bridge that knows the area in v2,
        # only the v2 call arms what is behind it.
        _note("armed", **await _arm_area(client, req.area_id, configuration))
    except HTTPException as e:
        _note("claim-failed", detail=e.detail)
        raise
    except Exception as e:
        _note("claim-error", detail=str(e))
        raise HTTPException(502, bridge_error(e, "hand the area to the stream")) from e
    _note("claimed")

    # One client per attempt, each against a claim of its own. The bridge stops
    # listening about ten seconds after arming an area nobody connects to, so
    # both the claim and the client have to be fresh: two clients sharing one
    # claim means the second speaks into a window the first has already spent,
    # and a retry against a stale claim is guaranteed to fail whatever it says.
    last_error = None
    settle = config_store.get_settings()["stream_settle_ms"] / 1000
    for attempt, transport in enumerate(STREAM_TRANSPORTS):
        if attempt:
            try:
                _note("re-armed", attempt=attempt + 1,
                      **await _arm_area(client, req.area_id, configuration))
            except Exception as e:
                _note("re-claim-failed", detail=str(e))
                break
        # Let the bridge finish arming before speaking to it. The socket answers
        # a cookie as soon as it is bound, which is well before the session
        # behind it exists — handshaking into that gap gets a HelloVerifyRequest
        # and then nothing, because there is nowhere yet to put the session.
        # This wait used to happen by accident, in the polling the old claim did.
        if settle:
            await asyncio.sleep(settle)
            _note("settled", seconds=round(settle, 2))
        try:
            stream_engine.start(
                cfg["bridge_ip"], cfg["api_key"], cfg["client_key"],
                req.area_id, light_ids, pattern["sequence"], req.pattern_id,
                min(framing["hz"], MAX_STREAM_HZ),
                framing["min_bri"], framing["max_bri"], framing["hue"], framing["sat"],
                transition_ms=framing["transition_ms"],
                connect_timeout=6.0,
                area_uuid=(configuration or {}).get("id"),
                channels=channel_ids(configuration) if configuration else None,
                transport=transport,
            )
            last_error = None
            break
        except StreamError as e:
            last_error = e
            _note("handshake-attempt-failed", attempt=attempt + 1, detail=str(e),
                  transport=transport, from_address=_note_local_address(cfg))
    if last_error is not None:
        e = last_error
        _note("handshake-failed", detail=str(e))
        detail = str(e)
        # Every handshake failure gets the deeper look, not just the ones whose
        # message happens to say "timed out". Gating on that string meant that
        # making an error message more precise silently switched this off, and
        # the most useful diagnostic in here stopped appearing at all.
        host, _ = parse_bridge_address(cfg["bridge_ip"])
        # Claim it again first. The bridge stops listening about ten seconds
        # after a claim nobody connects to, and by the time the attempts
        # above have run out that window is gone — probing then measures a
        # closed port and reports "silent" for a bridge that was answering
        # perfectly well a moment earlier.
        try:
            await _arm_area(client, req.area_id, configuration)
        except Exception:
            logger.debug("Could not re-arm %s before probing", req.area_id,
                         exc_info=True)
        # Carried as far as the credentials. Stopping at the first reply
        # could not tell a bridge that rejects our key from one that rejects
        # our ClientHello — the identity is not sent until the fifth
        # message of the flight.
        stage, how = await asyncio.to_thread(probe_handshake_stage, host, STREAM_PORT)
        _note("handshake-stage-while-claimed", stage=stage, detail=how)
        # And a third implementation, sharing no code with either of ours.
        # Two clients agreeing may only mean they share a mistake.
        ssl_verdict, ssl_detail = await asyncio.to_thread(
            openssl_handshake, host, cfg["api_key"], cfg["client_key"],
            STREAM_PORT)
        _note("openssl", verdict=ssl_verdict, detail=ssl_detail)
        if ssl_verdict == "connected":
            detail += (
                " — but OpenSSL completed the same handshake against the same "
                "claim, so the fault is in this app rather than the bridge or "
                f"the network ({ssl_detail})."
            )
            await _release_area(req.area_id)
            raise HTTPException(502, detail) from e
        if ssl_verdict == "alert":
            detail += (
                f" — and OpenSSL got an answer worth reading: {ssl_detail}. That "
                "is the bridge saying what it objects to, rather than dropping "
                "the flight in silence."
            )
            await _release_area(req.area_id)
            raise HTTPException(502, detail) from e
        if stage == "server-hello":
            detail += (
                f" — but a bare handshake to UDP {STREAM_PORT} gets all the way to "
                "ServerHello while the bridge holds the area. So the path is fine, "
                "the port is open, and the bridge accepts our offer: what it will not "
                "accept is the streaming key. That key is only issued alongside the "
                "API key it belongs to, at pairing time, so a mismatched pair can only "
                "be fixed by pairing again — Change bridge, press the link button, Pair."
            )
        elif stage in ("hello-verify-only", "alert", "no-hello-verify", "unexpected",
                       "handshake-other"):
            detail += (
                f" — the bridge is listening on UDP {STREAM_PORT} and answers, but "
                f"will not get past our ClientHello ({how}). The path works in both "
                "directions or the cookie could not have arrived, and the key is not "
                "offered until several messages later, so neither is the problem. "
                "What is left is the entertainment session behind the port: "
                "answering a cookie costs a DTLS server no session state at all, "
                "and finishing the handshake needs somewhere to put one. A bridge "
                "whose entertainment service is wedged — which a run of aborted "
                "sessions will do — behaves exactly like this. Power-cycle the "
                "bridge; nothing else clears that from the outside."
            )
        elif stage == "refused":
            detail += (
                f" — and UDP {STREAM_PORT} is shut even while the bridge says it is "
                "holding the area. The path is fine (the refusal had to reach us), so "
                "the bridge took the v1 claim without arming the stream behind it."
            )
        else:
            local = local_address_for(host)
            detail += (
                f" — and nothing comes back on UDP {STREAM_PORT} at all ({how}), "
                "while HTTP to the same bridge works."
            )
            if local and looks_translated(local):
                detail += (
                    f" This is a container address ({local}), translated to the "
                    "host's before the bridge ever sees it — so whether it shares "
                    "the bridge's network cannot be told from here. Switch the "
                    "container to Host networking: that removes the translation, "
                    "and if the host is on the bridge's network the client then "
                    "genuinely is too."
                )
            elif local and not same_subnet_as_bridge(local, host):
                detail += (
                    f" This machine reaches the bridge as {local}, which is not on "
                    f"the bridge's own network ({host}). Streaming is the one part of "
                    "the Hue API that wants the client on the same network as the "
                    "bridge — REST routes anywhere, which is why pairing, rooms and "
                    "the per-light flicker all work across the hop and only the "
                    "stream does not. Run the console on the bridge's subnet, or "
                    "move the bridge."
                )
            elif _pairing_provenance(cfg) != "yes":
                detail += (
                    " Everything the console can see is in order, so the next "
                    "thing to rule out is the credential itself: the handshake "
                    "offers the API key as its identity and the streaming key as "
                    "the secret, and this console cannot show that the two were "
                    "issued together. Press the bridge's link button and pair "
                    "again — that takes a minute and either fixes this or removes "
                    "it from the list. If it changes nothing, capture the wire "
                    "next: `docker exec -it <container> sh "
                    "/srv/scripts/capture_stream.sh`."
                )
            elif not _arm_looked_real():
                detail += (
                    " The bridge accepted the call to start the area and then went "
                    "on reporting it as not streaming, so nothing was ever armed "
                    "behind the port. Check that this console's key still owns the "
                    "area — the Hue app takes it back when it streams to it — and "
                    "try Release area, then Start."
                )
            else:
                detail += (
                    f" This machine is already on the bridge's network as {local}, "
                    "with nothing translating the address, and the bridge reports "
                    "the area as armed. So the path and the claim are both ruled "
                    "out, and guessing further from here is not worth your time: "
                    "run `docker exec -it <container> sh "
                    "/srv/scripts/capture_stream.sh` — it captures the wire and "
                    "drives the handshake inside the capture, so nothing has to be "
                    "timed by hand. Whether our packets leave, whether anything "
                    "comes back, and whether an ICMP refusal names a firewall "
                    "splits this four ways in one look."
                )
        # Never leave the area held by a stream that isn't running.
        if not await _release_area(req.area_id):
            _note("release-failed")
            detail += (
                f" The bridge is also still holding area {req.area_id} after being "
                "told to let go, which blocks everything else from driving those "
                "lights — the Hue app included. Use Release area, and restart the "
                "bridge if that does not clear it."
            )
        raise HTTPException(502, detail) from e

    _note("streaming")
    _broadcast_status_soon()
    return {"ok": True, **status_payload()}


@app.post("/api/stream/update")
async def update_stream(req: StreamUpdateRequest):
    changes = req.model_dump(exclude_none=True)
    # Colour is the one field where null means "clear it" rather than "not
    # mentioned", so it is decided by whether it was sent at all. Dropping it
    # with the other empties made unticking the colour box do nothing.
    for field in ("hue", "sat"):
        if field in req.model_fields_set:
            changes[field] = getattr(req, field)
    if req.pattern_id is not None:
        pattern = _resolve_pattern(req.pattern_id)
        changes["sequence"] = pattern["sequence"]
        for field, value in framing_of(pattern).items():
            if field in ("hue", "sat", "transition_ms"):
                continue
            if getattr(req, field, None) is None:
                changes[field] = value
    current = stream_engine.status().get("settings") or {}
    low = changes.get("min_bri", current.get("min_bri", 1))
    high = changes.get("max_bri", current.get("max_bri", 254))
    if low > high:
        raise HTTPException(422, "min_bri must be less than or equal to max_bri")
    if not stream_engine.update(**changes):
        raise HTTPException(409, "Nothing is streaming right now")
    _broadcast_status_soon()
    return {"ok": True, **status_payload()}


@app.post("/api/stream/stop")
async def stop_stream():
    area_id = stream_engine.area_id()
    light_ids = stream_engine.light_ids()
    stream_engine.stop()
    if area_id:
        await _finish_stream(area_id, light_ids)
    _broadcast_status_soon()
    return {"ok": True, **status_payload()}


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
    # Anything the caller left out comes from the pattern: the speed, the
    # brightness window and the transition are all part of how it was written.
    framing = framing_of(pattern)
    for field in FRAMING_FIELDS:
        if field in ("hue", "sat"):
            # Colour is the one field where None is a meaningful request, so
            # "was it mentioned at all" decides rather than "is it None".
            if field in req.model_fields_set:
                framing[field] = getattr(req, field)
            continue
        supplied = getattr(req, field)
        if supplied is not None:
            framing[field] = supplied
    if (framing["hue"] is None) != (framing["sat"] is None):
        framing["hue"] = framing["sat"] = None
    if framing["min_bri"] > framing["max_bri"]:
        raise HTTPException(
            422, "min_bri must be less than or equal to max_bri "
                 f"(got {framing['min_bri']} against the pattern's {framing['max_bri']})",
        )
    # Size the send budget before anything goes out, or the snapshot read below
    # spends the only token and the group's first round is strung out behind
    # it. The read counts against the budget too, hence the extra one.
    engine.expect_batch(len(req.light_ids) + 1)
    # Snapshot first — one bulk GET for the whole group — so Stop has something
    # to put back. Lights already running keep their earlier snapshot.
    await engine.capture(req.light_ids)
    # One epoch for the whole request: every light in a group then derives the
    # same frame from it and they flicker in step.
    epoch = time.monotonic()
    for lid in req.light_ids:
        await engine.start(
            lid, sequence, req.pattern_id, framing["hz"],
            framing["min_bri"], framing["max_bri"],
            framing["hue"], framing["sat"], framing["transition_ms"], epoch=epoch,
        )
    return {"ok": True, **status_payload()}


@app.post("/api/flicker/update")
async def update_flicker(req: UpdateRequest):
    changes = req.model_dump(exclude={"light_ids"}, exclude_none=True)
    if req.pattern_id is not None:
        pattern = _resolve_pattern(req.pattern_id)
        changes["sequence"] = pattern["sequence"]
        for field, value in framing_of(pattern).items():
            if field in ("hue", "sat"):
                continue        # only ever changed when the caller asks
            if getattr(req, field) is None:
                changes[field] = value
    # A bound supplied on its own still has to make sense against the one the
    # light is already running, or the brightness window inverts and the
    # waveform quietly plays upside down.
    status = engine.status()
    for lid in req.light_ids:
        state = status.get(lid)
        if not state or not state.get("running"):
            continue
        low = changes.get("min_bri", state["min_bri"])
        high = changes.get("max_bri", state["max_bri"])
        if low > high:
            raise HTTPException(
                422,
                "min_bri must be less than or equal to max_bri "
                f"(light {lid} is running {state['min_bri']}-{state['max_bri']}, "
                f"so this would leave it {low}-{high})",
            )

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

STATIC_DIR = Path("static")
INDEX_FILE = STATIC_DIR / "index.html"
VERSIONED_ASSETS = ("app.js", "style.css")


def asset_version() -> str:
    """A short hash of the UI files, used to bust caches on every change.

    Without it a browser or a reverse proxy can hold on to an old app.js
    indefinitely, so an updated console keeps running the previous build and
    nothing you change appears to take effect.
    """
    digest = hashlib.sha256()
    for name in VERSIONED_ASSETS:
        path = STATIC_DIR / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:8]


def render_index() -> str:
    html = INDEX_FILE.read_text()
    version = asset_version()
    for name in VERSIONED_ASSETS:
        html = html.replace(f"/static/{name}", f"/static/{name}?v={version}")
    return html.replace("__BUILD__", version)


@app.get("/api/version")
async def get_version():
    """Which build is actually being served, for when the UI looks stale."""
    return {"assets": asset_version()}


@app.get("/")
async def index():
    # The HTML itself must never be cached, or the versioned asset URLs inside
    # it never reach the browser and the whole scheme is pointless.
    return HTMLResponse(
        render_index(),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )
