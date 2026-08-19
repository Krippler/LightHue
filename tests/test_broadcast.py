import asyncio

import pytest

from app.main import ConnectionManager


class RecordingWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        await asyncio.sleep(0)
        self.sent.append(message)


class JoiningWS(RecordingWS):
    """Simulates a client connecting while a broadcast is mid-flight."""

    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    async def send_json(self, message):
        await asyncio.sleep(0)
        self.manager.active.add(RecordingWS())
        self.sent.append(message)


class BrokenWS(RecordingWS):
    async def send_json(self, message):
        raise RuntimeError("socket already gone")


@pytest.mark.asyncio
async def test_broadcast_survives_a_client_joining_mid_send():
    # Regression: iterating the live set raised "Set changed size during
    # iteration" as soon as two people had the page open.
    manager = ConnectionManager()
    for _ in range(3):
        manager.active.add(JoiningWS(manager))
    await manager.broadcast({"type": "status", "data": {}})


@pytest.mark.asyncio
async def test_broadcast_reaches_every_live_client():
    manager = ConnectionManager()
    sockets = [RecordingWS() for _ in range(3)]
    for ws in sockets:
        manager.active.add(ws)
    await manager.broadcast({"type": "status", "data": {"1": {}}})
    assert all(ws.sent == [{"type": "status", "data": {"1": {}}}] for ws in sockets)


@pytest.mark.asyncio
async def test_broken_sockets_are_dropped_without_stopping_the_broadcast():
    manager = ConnectionManager()
    good, bad = RecordingWS(), BrokenWS()
    manager.active.add(good)
    manager.active.add(bad)
    await manager.broadcast({"type": "status", "data": {}})
    assert len(good.sent) == 1
    assert bad not in manager.active
    assert good in manager.active
