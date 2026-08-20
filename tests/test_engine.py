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


def _rounds(calls, n):
    """Group sends into rounds of n, one per light."""
    rounds, current = [], {}
    for sent_at, light_id, state in calls:
        if light_id in current:
            rounds.append(current)
            current = {}
        current[light_id] = (sent_at, state["bri"])
    return [r for r in rounds if len(r) == n]


def _stagger(round_):
    times = [t for t, _ in round_.values()]
    return max(times) - min(times)


@pytest.mark.asyncio
async def test_a_groups_lights_are_sent_together():
    # The Hue app looks simultaneous because one group command is broadcast to
    # every bulb. Sending per-light can't match that exactly, but the lights
    # must go out in one burst rather than spread across the budget: a fixed
    # gap between every command put 200ms between the first and last of three,
    # which is plainly visible on a strobe.
    fake = FakeClient(latency=0.015)
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(10.0)
    await asyncio.sleep(0.4)              # idle, as the console is before Start
    engine.expect_batch(3)
    epoch = time.monotonic()
    for lid in ("1", "2", "3"):
        await engine.start(lid, "azazazaz", "x", 10.0, 1, 254, None, None, 0, epoch=epoch)
    await asyncio.sleep(2.5)
    await engine.stop_all()

    rounds = _rounds(fake.calls, 3)
    assert len(rounds) >= 4
    staggers = sorted(_stagger(r) for r in rounds)
    median = staggers[len(staggers) // 2]
    assert median < 0.02, f"median stagger {median * 1000:.0f}ms"
    # including the very first round, which is the one you actually watch for
    assert _stagger(rounds[0]) < 0.02, f"first round {_stagger(rounds[0]) * 1000:.0f}ms"


@pytest.mark.asyncio
async def test_lights_sent_together_carry_the_same_frame():
    fake = FakeClient(latency=0.015)
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(10.0)
    await asyncio.sleep(0.4)
    engine.expect_batch(3)
    epoch = time.monotonic()
    for lid in ("1", "2", "3"):
        await engine.start(lid, "azazazaz", "x", 10.0, 1, 254, None, None, 0, epoch=epoch)
    await asyncio.sleep(2.5)
    await engine.stop_all()

    for round_ in _rounds(fake.calls, 3):
        assert len({bri for _, bri in round_.values()}) == 1, round_


@pytest.mark.asyncio
async def test_group_lights_do_not_drift_apart_over_time():
    # Before frames came from a shared clock, a light served less often fell
    # progressively further behind. Compare the first second to the last.
    fake = FakeClient(latency=0.015)
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(10.0)
    await asyncio.sleep(0.4)
    engine.expect_batch(3)
    epoch = time.monotonic()
    for lid in ("1", "2", "3"):
        await engine.start(lid, "azazaz", "x", 10.0, 1, 254, None, None, 0, epoch=epoch)
    await asyncio.sleep(3.0)
    await engine.stop_all()

    rounds = _rounds(fake.calls, 3)
    early = [_stagger(r) for r in rounds[:2]]
    late = [_stagger(r) for r in rounds[-2:]]
    assert max(late) < 0.02
    assert max(late) <= max(early) + 0.01


@pytest.mark.asyncio
async def test_one_light_alone_keeps_the_whole_budget():
    engine = FlickerEngine(get_client=lambda: FakeClient())
    engine.limiter.set_rate(10.0)
    await engine.start("1", "azaz", "x", 20.0, 1, 254, None, None, 0)
    assert engine.status()["1"]["effective_hz"] == 10.0    # no headroom given up
    await engine.stop_all()


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
    alive_before = len([c for c in fake.calls if c[1] == "alive"])
    dead_before = len([c for c in fake.calls if c[1] == "dead"])
    await asyncio.sleep(0.5)
    alive_after = len([c for c in fake.calls if c[1] == "alive"])
    dead_after = len([c for c in fake.calls if c[1] == "dead"])
    assert alive_after - alive_before > 3
    # counted rather than sampling the tail, which depended on how many sends
    # the working light happened to fit into the window
    assert dead_after == dead_before
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


@pytest.mark.asyncio
async def test_a_frame_is_never_sent_twice():
    # Waking a hair before the frame boundary used to send the same frame
    # again and then immediately send the next: about one wasted command per
    # round out of a budget the whole group shares.
    fake = FakeClient(latency=0.01)
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(20.0)
    await asyncio.sleep(0.3)
    engine.expect_batch(2)
    epoch = time.monotonic()
    for lid in ("1", "2"):
        await engine.start(lid, "azazazaz", "x", 5.0, 1, 254, None, None, 0, epoch=epoch)
    await asyncio.sleep(2.5)
    await engine.stop_all()

    interval = 1.0 / 5.0
    for lid in ("1", "2"):
        frames = [int((t - epoch) / interval) for t, light, _ in fake.calls if light == lid]
        assert len(frames) == len(set(frames)), f"light {lid} sent a frame twice: {frames}"
        assert frames == sorted(frames)


@pytest.mark.asyncio
async def test_each_light_sends_about_once_per_frame():
    fake = FakeClient(latency=0.01)
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(20.0)
    await asyncio.sleep(0.3)
    engine.expect_batch(2)
    epoch = time.monotonic()
    for lid in ("1", "2"):
        await engine.start(lid, "azaz", "x", 5.0, 1, 254, None, None, 0, epoch=epoch)
    await asyncio.sleep(2.0)
    await engine.stop_all()

    for lid in ("1", "2"):
        sends = len([c for c in fake.calls if c[1] == lid])
        # 5Hz for 2s is ten frames; allow a little either side for scheduling
        assert 8 <= sends <= 12, f"light {lid} sent {sends} times"


@pytest.mark.asyncio
async def test_lowering_the_speed_keeps_the_light_sending():
    """A frame number is only meaningful next to the interval it was counted in.

    Dropping 20 Hz to 4 Hz after a second renumbers "now" from frame 20 to
    frame 4. Comparing the two directly made the duplicate-frame guard wait for
    frame 21 at the *new* interval — five seconds out, and further the longer
    the light had been running — so the bulb simply stopped.
    """
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(40)
    await engine.start("1", "mmzz", "p", 20.0, 1, 254, None, None, 0)
    await asyncio.sleep(1.0)

    assert engine.update("1", hz=4.0) is True
    before = len(fake.calls)
    await asyncio.sleep(1.2)
    await engine.stop_all()

    sent = len(fake.calls) - before
    # ~4 in 1.2s. The bug produced 0; anything at all proves it isn't stalled,
    # and the upper bound catches it ignoring the new rate entirely.
    assert 2 <= sent <= 8, f"expected roughly 4 sends after slowing down, got {sent}"


@pytest.mark.asyncio
async def test_a_second_light_starting_does_not_stall_the_first():
    """The share is recut when another light joins, changing the interval for
    every light already running — the same renumbering, with nobody touching a
    slider.

    The first light has to run a while for this to bite: the stall is the gap
    between the frame it had reached and that number re-read at the slower
    interval, so it grows with runtime. 2.5s at 20 Hz is enough to make it
    plain; a real light left going for a minute stalls for minutes.
    """
    fake = FakeClient()
    engine = FlickerEngine(get_client=lambda: fake)
    engine.limiter.set_rate(20)
    await engine.start("1", "mmzz", "p", 20.0, 1, 254, None, None, 0)
    await asyncio.sleep(2.5)                    # reaches ~frame 50 at 0.05s a frame

    engine.expect_batch(2)
    await engine.start("2", "mmzz", "p", 20.0, 1, 254, None, None, 0)
    before = sum(1 for c in fake.calls if c[1] == "1")
    await asyncio.sleep(1.5)
    await engine.stop_all()

    sent = sum(1 for c in fake.calls if c[1] == "1") - before
    assert sent >= 3, f"light 1 stalled when light 2 joined: {sent} sends in 1.5s"
