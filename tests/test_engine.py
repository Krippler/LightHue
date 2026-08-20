import asyncio
import time

import pytest

from app.flicker_engine import FlickerEngine, RateLimiter


class FakeClient:
    def __init__(self, latency=0.0):
        self.calls = []
        self.latency = latency

    async def set_light_state(self, light_id, **state):
        self.calls.append((time.monotonic(), light_id, state))
        if self.latency:
            await asyncio.sleep(self.latency)


@pytest.mark.asyncio
async def test_rate_limiter_caps_global_throughput():
    limiter = RateLimiter(max_per_second=20.0)
    start = time.monotonic()
    for _ in range(10):
        await limiter.wait()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.4   # 9 gaps at 50ms


@pytest.mark.asyncio
async def test_set_rate_takes_effect():
    limiter = RateLimiter(max_per_second=10.0)
    limiter.set_rate(2.0)
    assert limiter.min_interval == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_limiter_shares_budget_across_lights():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(10.0)
    for lid in ("1", "2", "3"):
        await engine.start(lid, "mmnmmo", "flicker_a", 10.0, 1, 254, None, None, 0)
    await asyncio.sleep(1.0)
    await engine.stop_all()
    # Three lights at 10Hz would be 30/sec unthrottled; the cap is global.
    assert len(fake.calls) <= 14
    assert len(fake.calls) >= 6
    per_light = {lid for _, lid, _ in fake.calls}
    assert per_light == {"1", "2", "3"}   # nobody gets starved out entirely


@pytest.mark.asyncio
async def test_start_stop_lifecycle():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    await engine.start("1", "mz", "steady", 10.0, 1, 254, None, None, 0)
    assert engine.running_light_ids() == ["1"]
    assert engine.status()["1"]["running"] is True
    await engine.stop("1")
    assert engine.running_light_ids() == []
    assert engine.status()["1"]["running"] is False


@pytest.mark.asyncio
async def test_starting_twice_does_not_leak_a_task():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    await engine.start("1", "mz", "steady", 10.0, 1, 254, None, None, 0)
    await engine.start("1", "mz", "steady", 5.0, 1, 254, None, None, 0)
    assert engine.running_light_ids() == ["1"]
    assert engine.status()["1"]["hz"] == 5.0
    await engine.stop_all()


@pytest.mark.asyncio
async def test_start_without_a_bridge_raises():
    engine = FlickerEngine(get_client=lambda: None)
    with pytest.raises(RuntimeError):
        await engine.start("1", "m", "steady", 10.0, 1, 254, None, None, 0)


@pytest.mark.asyncio
async def test_brightness_is_scaled_into_the_requested_window():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    await engine.start("1", "az", "hard_strobe", 20.0, 50, 200, None, None, 0)
    await asyncio.sleep(0.5)
    await engine.stop_all()
    brightnesses = {c[2]["bri"] for c in fake.calls}
    assert brightnesses <= {50, 200}
    assert len(brightnesses) == 2


@pytest.mark.asyncio
async def test_color_is_set_once_up_front():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    await engine.start("1", "mmm", "steady", 20.0, 1, 254, 10000, 200, 0)
    await asyncio.sleep(0.4)
    await engine.stop_all()
    with_color = [c for c in fake.calls if "hue" in c[2]]
    assert len(with_color) == 1
    assert with_color[0][2]["sat"] == 200


@pytest.mark.asyncio
async def test_bridge_errors_do_not_kill_the_loop():
    class Flaky(FakeClient):
        async def set_light_state(self, light_id, **state):
            self.calls.append((time.monotonic(), light_id, state))
            raise RuntimeError("bridge said no")

    fake = Flaky()
    engine = FlickerEngine(get_client=lambda: fake)
    await engine.start("1", "mmm", "steady", 20.0, 1, 254, None, None, 0)
    await asyncio.sleep(0.4)
    assert engine.running_light_ids() == ["1"]
    assert len(fake.calls) > 1
    await engine.stop_all()


@pytest.mark.asyncio
async def test_on_change_fires_for_start_and_stop():
    seen = []
    engine = FlickerEngine(get_client=lambda: FakeClient(), on_change=lambda: seen.append(1))
    await engine.start("1", "m", "steady", 10.0, 1, 254, None, None, 0)
    await engine.stop("1")
    await engine.stop_all()
    assert len(seen) >= 2


@pytest.mark.asyncio
async def test_update_retunes_a_running_loop_without_restarting_it():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    await engine.start("1", "aaaa", "steady", 20.0, 1, 254, None, None, 0)
    await asyncio.sleep(0.2)
    task = engine._tasks["1"]

    assert engine.update("1", sequence="zzzz", min_bri=100, max_bri=100) is True
    await asyncio.sleep(0.3)
    await engine.stop_all()

    assert task is engine._tasks.get("1", task)   # same task object, never restarted
    assert fake.calls[0][2]["bri"] == 1           # before the update
    assert fake.calls[-1][2]["bri"] == 100        # after it


@pytest.mark.asyncio
async def test_update_is_rejected_for_a_light_that_is_not_running():
    engine = FlickerEngine(get_client=lambda: FakeClient())
    assert engine.update("nope", hz=5.0) is False
    await engine.start("1", "m", "steady", 10.0, 1, 254, None, None, 0)
    await engine.stop("1")
    assert engine.update("1", hz=5.0) is False


@pytest.mark.asyncio
async def test_colour_can_be_changed_mid_flicker():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    await engine.start("1", "mmmm", "steady", 20.0, 1, 254, 100, 200, 0)
    await asyncio.sleep(0.25)
    engine.update("1", hue=40000, sat=150)
    await asyncio.sleep(0.25)
    await engine.stop_all()

    coloured = [c[2] for c in fake.calls if "hue" in c[2]]
    assert [(c["hue"], c["sat"]) for c in coloured] == [(100, 200), (40000, 150)]


@pytest.mark.asyncio
async def test_colour_is_not_resent_every_tick():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    await engine.start("1", "mmmm", "steady", 20.0, 1, 254, 100, 200, 0)
    await asyncio.sleep(0.4)
    await engine.stop_all()
    assert len([c for c in fake.calls if "hue" in c[2]]) == 1
    assert len(fake.calls) > 3


@pytest.mark.asyncio
async def test_colour_can_be_added_to_a_loop_started_without_one():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    await engine.start("1", "mmmm", "steady", 20.0, 1, 254, None, None, 0)
    await asyncio.sleep(0.2)
    assert not [c for c in fake.calls if "hue" in c[2]]
    engine.update("1", hue=25000, sat=254)
    await asyncio.sleep(0.25)
    await engine.stop_all()
    assert [c[2]["hue"] for c in fake.calls if "hue" in c[2]] == [25000]


@pytest.mark.asyncio
async def test_update_ignores_unknown_and_none_fields():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    await engine.start("1", "m", "steady", 10.0, 1, 254, None, None, 0)
    engine.update("1", hz=None, running=False, bogus="x")
    assert engine.status()["1"]["hz"] == 10.0
    assert engine.status()["1"]["running"] is True
    await engine.stop_all()


@pytest.mark.asyncio
async def test_lights_in_a_group_stay_in_step():
    # The limiter serves lights one at a time, so they can never send at the
    # same instant and one of three will always be the far side of a frame
    # boundary. What must hold is that every light shows the frame belonging
    # to its own send time — which is what keeps them mutually in step, and
    # what stops them drifting further apart the longer they run.
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(30.0)
    epoch = time.monotonic()
    seq = "aeimquy"
    for lid in ("1", "2", "3"):
        await engine.start(lid, seq, "steady", 10.0, 0, 250, None, None, 0, epoch=epoch)
    await asyncio.sleep(1.5)
    await engine.stop_all()

    def bri_at(t):
        frame = int((t - epoch) / 0.1) % len(seq)
        # the engine floors brightness at 1, so mirror that here
        return max(1, round((ord(seq[frame]) - ord("a")) / 25.0 * 250))

    assert len(fake.calls) > 30
    for sent_at, lid, state in fake.calls:
        # allow a few ms either side for the send landing next to a boundary
        allowed = {bri_at(sent_at), bri_at(sent_at - 0.012), bri_at(sent_at + 0.012)}
        assert state["bri"] in allowed, (lid, sent_at - epoch, state["bri"], allowed)


@pytest.mark.asyncio
async def test_group_lights_do_not_drift_apart_over_time():
    # Before frames were derived from a shared clock, each light counted its
    # own ticks, so a light served less often fell progressively further
    # behind. Compare the first second against the last.
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(30.0)
    epoch = time.monotonic()
    seq = "azazaz"
    for lid in ("1", "2", "3"):
        await engine.start(lid, seq, "steady", 10.0, 1, 254, None, None, 0, epoch=epoch)
    await asyncio.sleep(2.0)
    await engine.stop_all()

    def spread(window):
        # how far apart the lights' frame positions are inside one window
        by_light = {}
        for sent_at, lid, state in window:
            frame = int((sent_at - epoch) / 0.1)
            by_light.setdefault(lid, []).append((frame, state["bri"]))
        mismatches = 0
        for frames in by_light.values():
            for frame, bri in frames:
                expected = 1 if seq[frame % len(seq)] == "a" else 254
                if bri != expected:
                    mismatches += 1
        return mismatches

    early = [c for c in fake.calls if c[0] - epoch < 0.8]
    late = [c for c in fake.calls if c[0] - epoch > 1.2]
    assert early and late
    # drift would make the later window steadily worse; it must not
    assert spread(late) <= spread(early) + 2


@pytest.mark.asyncio
async def test_an_unthrottled_light_tracks_wall_clock_frames():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(30.0)
    epoch = time.monotonic()
    seq = "aeimquy"
    await engine.start("1", seq, "steady", 10.0, 0, 250, None, None, 0, epoch=epoch)
    await asyncio.sleep(1.2)
    await engine.stop_all()

    for sent_at, _lid, state in fake.calls:
        frame = int((sent_at - epoch) / 0.1) % len(seq)
        expected = max(1, round((ord(seq[frame]) - ord("a")) / 25.0 * 250))
        assert abs(state["bri"] - expected) <= 20, (frame, state["bri"], expected)


@pytest.mark.asyncio
async def test_a_throttled_pattern_still_advances():
    # Under a limiter slower than the frame rate the clock would alias; the
    # pattern must keep moving rather than freezing on one value.
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(10.0)
    await engine.start("1", "az", "steady", 20.0, 50, 200, None, None, 0)
    await asyncio.sleep(1.0)
    await engine.stop_all()
    assert {c[2]["bri"] for c in fake.calls} == {50, 200}


@pytest.mark.asyncio
async def test_separately_started_lights_get_their_own_epoch():
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    await engine.start("1", "az", "steady", 10.0, 1, 254, None, None, 0)
    await asyncio.sleep(0.05)
    await engine.start("2", "az", "steady", 10.0, 1, 254, None, None, 0)
    assert engine.status()["1"]["epoch"] != engine.status()["2"]["epoch"]
    await engine.stop_all()


class DeadClient(FakeClient):
    async def set_light_state(self, light_id, **state):
        self.calls.append((time.monotonic(), light_id, state))
        raise RuntimeError("light is not reachable")


@pytest.mark.asyncio
async def test_a_light_that_never_answers_is_written_off():
    # Regression: an unplugged bulb kept its loop forever, taking rate-limiter
    # slots from lights that could still answer and logging every tick.
    from app.flicker_engine import GIVE_UP_AFTER_FAILURES

    fake = DeadClient()
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(30.0)
    await engine.start("1", "mmnn", "x", 20.0, 1, 254, None, None, 0)
    for _ in range(200):
        await asyncio.sleep(0.05)
        if not engine.running_light_ids():
            break
    assert engine.running_light_ids() == []
    assert engine.status()["1"]["running"] is False
    assert len(fake.calls) == GIVE_UP_AFTER_FAILURES
    # the snapshot is kept so a manual revert can still try later
    await engine.stop_all()


@pytest.mark.asyncio
async def test_writing_one_light_off_leaves_the_others_running():
    class Mixed(FakeClient):
        async def set_light_state(self, light_id, **state):
            self.calls.append((time.monotonic(), light_id, state))
            if light_id == "dead":
                raise RuntimeError("gone")

    fake = Mixed()
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(40.0)
    for lid in ("alive", "dead"):
        await engine.start(lid, "mmnn", "x", 20.0, 1, 254, None, None, 0)
    for _ in range(200):
        await asyncio.sleep(0.05)
        if "dead" not in engine.running_light_ids():
            break
    assert engine.running_light_ids() == ["alive"]

    # once it's gone, the working light has the budget to itself
    before = len([c for c in fake.calls if c[1] == "alive"])
    await asyncio.sleep(0.5)
    after = len([c for c in fake.calls if c[1] == "alive"])
    assert after - before > 5
    assert not [c for c in fake.calls[-10:] if c[1] == "dead"]
    await engine.stop_all()


@pytest.mark.asyncio
async def test_an_occasional_failure_does_not_write_a_light_off():
    class Flaky(FakeClient):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def set_light_state(self, light_id, **state):
            self.calls.append((time.monotonic(), light_id, state))
            self.n += 1
            if self.n % 3 == 0:
                raise RuntimeError("transient")

    fake = Flaky()
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(40.0)
    await engine.start("1", "mmnn", "x", 20.0, 1, 254, None, None, 0)
    await asyncio.sleep(2.0)
    assert engine.running_light_ids() == ["1"]   # a third failing is not fatal
    await engine.stop_all()
