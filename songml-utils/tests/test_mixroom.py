"""M1 tests: the mix layer against the fake AbletonOSC, over real loopback UDP.

Timings are scaled down from the design's (``FAST``); ``test_default_timings_match_design``
pins the real numbers.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Callable, Iterator

import pytest

from songml_utils.fake_abletonosc import FakeAbletonOSC, FakeSong, FakeTrack
from songml_utils.mixroom import (
    TRACK_DATA,
    UNREACHABLE_REASON,
    MixLayer,
    MixroomPortTaken,
    Timings,
)
from songml_utils.osc import OSCError, decode_message, encode_message

FAST = Timings(
    transport_interval=0.02,
    tracks_interval=0.05,
    watch_window=0.5,
    unreachable_after=0.3,
    confirm_timeout=0.2,
)


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(pred: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def song() -> FakeSong:
    return FakeSong(
        tempo=120.0,
        tracks=[
            FakeTrack("Mic 1", color=16249980),
            FakeTrack("Bite piano", color=1090798),
            FakeTrack("Ballroom Blues", is_foldable=True, color=13408551),
            FakeTrack("MONITOR", is_grouped=True, color=16777215),
        ],
    )


type Rig = tuple[FakeAbletonOSC, MixLayer]


@pytest.fixture
def rig() -> Iterator[Rig]:
    reply_port = free_udp_port()
    fake = FakeAbletonOSC(song(), reply_port=reply_port).start()
    layer = MixLayer(live=fake.address, bind=("127.0.0.1", reply_port), timings=FAST).start()
    yield fake, layer
    layer.close()
    fake.close()
    assert fake.dead_reason is None, fake.dead_reason


# -- OSC codec -------------------------------------------------------------------


def test_osc_round_trip() -> None:
    args = [0, 4, "track.name", True, False, 1.5, "Bite piano", -3]
    address, got = decode_message(encode_message("/live/song/get/track_data", args))
    assert address == "/live/song/get/track_data"
    assert got == args


def test_osc_padding_edge_lengths() -> None:
    for text in ["", "a", "ab", "abc", "abcd", "abcde"]:
        assert decode_message(encode_message("/x", [text]))[1] == [text]
    assert decode_message(encode_message("/live/test")) == ("/live/test", [])


def test_osc_refuses_unsupported() -> None:
    with pytest.raises(OSCError):
        encode_message("/x", [b"blob"])  # type: ignore[list-item]
    with pytest.raises(OSCError):
        decode_message(b"#bundle\x00" + b"\x00" * 8)
    with pytest.raises(OSCError):
        decode_message(b"/x\x00\x00,b\x00\x00")


def test_default_timings_match_design() -> None:
    t = Timings()
    assert (t.transport_interval, t.tracks_interval) == (0.25, 1.0)
    assert (t.watch_window, t.unreachable_after, t.confirm_timeout) == (10.0, 1.5, 0.4)


# -- startup ---------------------------------------------------------------------


def test_loud_exit_when_reply_port_taken() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    try:
        layer = MixLayer(live=("127.0.0.1", 9), bind=("127.0.0.1", port), timings=FAST)
        with pytest.raises(SystemExit) as exc:
            layer.start()
        assert isinstance(exc.value, MixroomPortTaken)
        message = str(exc.value.code)
        assert f"127.0.0.1:{port}" in message

        assert f"PID {os.getpid()}" in message
    finally:
        holder.close()


# -- state and polling -----------------------------------------------------------


def test_state_reads_transport_and_tracks(rig: Rig) -> None:
    fake, layer = rig
    st = layer.state()
    assert st["live"] == "ok" and st["reason"] is None
    assert st["playing"] is False and st["beat"] == 0.0
    assert st["tempo"] == 120.0 and st["signature"] == [4, 4]
    assert wait_until(lambda: len(layer.state()["tracks"]) == 4)
    st = layer.state()
    assert [t["name"] for t in st["tracks"]] == ["Mic 1", "Bite piano", "Ballroom Blues", "MONITOR"]
    assert st["tracks"][2]["foldable"] and st["tracks"][3]["grouped"]
    assert st["tracks"][0]["color"] == 16249980
    assert st["tracks_rev"] == 1


def test_polls_only_while_watched(rig: Rig) -> None:
    fake, layer = rig
    time.sleep(0.15)
    assert fake.received == [], "no traffic before anyone watches"
    layer.state()
    time.sleep(0.15)
    assert len(fake.received) > 5
    time.sleep(FAST.watch_window + 0.1)  # window lapses
    with fake.lock:
        n = len(fake.received)
    time.sleep(0.2)
    assert len(fake.received) == n, "polling stops once nobody watches"
    assert layer.state()["live"] == "ok", "a watch after idle waits for a fresh read"


def test_hand_mute_in_live_shows_up(rig: Rig) -> None:
    fake, layer = rig
    assert wait_until(lambda: len(layer.state()["tracks"]) == 4)
    with fake.lock:
        fake.song.tracks[1].mute = True
    assert wait_until(lambda: layer.state()["tracks"][1]["mute"] is True)
    assert layer.state()["tracks_rev"] == 1, "a mute change is not a set change"


def test_tracks_rev_bumps_on_set_change(rig: Rig) -> None:
    fake, layer = rig
    assert wait_until(lambda: len(layer.state()["tracks"]) == 4)
    with fake.lock:
        fake.song.tracks.append(FakeTrack("Click"))
    assert wait_until(lambda: layer.state()["tracks_rev"] == 2)
    with fake.lock:
        fake.song.tracks[0].name = "Mic 1 (dbl)"
    assert wait_until(lambda: layer.state()["tracks_rev"] == 3)
    with fake.lock:
        fake.song.tracks[1].color = 0x00FF00  # rig M2: recolor alone redraws the grid
    assert wait_until(lambda: layer.state()["tracks_rev"] == 4)
    with fake.lock:
        fake.song.tracks.clear()
    assert wait_until(lambda: layer.state()["tracks_rev"] == 5)
    assert layer.state()["tracks"] == []


def test_unreachable_after_silence(rig: Rig) -> None:
    fake, layer = rig
    assert layer.state()["live"] == "ok"
    fake.silent = True
    t0 = time.monotonic()
    assert wait_until(lambda: layer.state()["live"] == "unreachable")
    assert time.monotonic() - t0 >= FAST.unreachable_after - 0.05
    st = layer.state()
    assert st["reason"] == UNREACHABLE_REASON
    fake.silent = False
    assert wait_until(lambda: layer.state()["live"] == "ok")


def test_unreachable_when_nothing_answers() -> None:
    reply_port = free_udp_port()
    dead = ("127.0.0.1", free_udp_port())
    with MixLayer(live=dead, bind=("127.0.0.1", reply_port), timings=FAST) as layer:
        st = layer.state()
        assert st["live"] == "unreachable" and st["transport_age_ms"] is None
        assert layer.play().status == "unreachable"
        assert layer.mute(0, "Mic 1", True).status == "unreachable"


def test_bad_track_data_is_reported_not_parsed(rig: Rig) -> None:
    fake, layer = rig
    layer._on_reply(TRACK_DATA, ["only", True, False])
    assert "not a multiple of 5" in layer.snapshot()["live_error"]
    assert layer.snapshot()["tracks"] == []


# -- commands --------------------------------------------------------------------


def test_play_is_play_then_locate(rig: Rig) -> None:
    fake, layer = rig
    # Live last stopped at beat 4; Les then locates to bar 9 (beat 32) while stopped.
    with fake.lock:
        fake.song.last_stop = 4.0
    assert layer.locate(32).ok
    res = layer.play()
    assert res.ok, res.reason
    assert res.state["playing"] is True
    assert 32.0 <= res.state["beat"] < 33.0
    sent = [a for a, _ in fake.received if not a.startswith("/live/song/get")]
    burst = [a for a in sent if a != "/live/track/set/mute"]
    assert burst[-2:] == ["/live/song/continue_playing", "/live/song/set/current_song_time"]
    assert "/live/song/start_playing" not in sent


def test_play_mutes_audible_tracks_through_the_jump(rig: Rig) -> None:
    """Rig M2: nothing is audible from the old position; Les's own mutes are untouched."""
    fake, layer = rig
    with fake.lock:
        fake.song.tracks[1].mute = True  # Les muted Bite piano
        fake.song.last_stop = 80.0
    fake.start_latency = 0.08
    assert layer.locate(0).ok
    with fake.lock:
        n = len(fake.received)
    res = layer.play()
    assert res.ok, res.reason
    msgs = [(a, list(args)) for a, args in fake.received[n:] if not a.startswith("/live/song/get")]
    cont = msgs.index(("/live/song/continue_playing", []))
    last_locate = max(i for i, (a, _) in enumerate(msgs) if a == "/live/song/set/current_song_time")
    mutes = [args for a, args in msgs[:cont] if a == "/live/track/set/mute"]
    unmutes = [args for a, args in msgs[last_locate:] if a == "/live/track/set/mute"]
    assert sorted(mutes) == [[0, 1], [2, 1], [3, 1]]
    assert sorted(unmutes) == [[0, 0], [2, 0], [3, 0]]
    with fake.lock:
        assert [t.mute for t in fake.song.tracks] == [False, True, False, False]


def test_play_restores_mutes_when_unconfirmed(rig: Rig) -> None:
    fake, layer = rig
    with fake.lock:
        fake.song.last_stop = 80.0
    fake.start_latency = 60.0
    assert layer.locate(0).ok
    res = layer.play()
    assert res.status == "unconfirmed"
    with fake.lock:
        assert not any(t.mute for t in fake.song.tracks)


def test_play_relocates_when_live_starts_late(rig: Rig) -> None:
    """Rig M2: the first locate after continue_playing is lost; Play re-sends it."""
    fake, layer = rig
    with fake.lock:
        fake.song.last_stop = 80.0
    fake.start_latency = 0.08
    assert layer.locate(0).ok
    res = layer.play()
    assert res.ok, res.reason
    assert 0.0 <= res.state["beat"] < 1.0
    sets = [a for a, _ in fake.received if a == "/live/song/set/current_song_time"]
    assert len(sets) >= 3, "the stopped locate, the lost one, and at least one retry"


def test_play_unconfirmed_when_locate_never_sticks(rig: Rig) -> None:
    fake, layer = rig
    with fake.lock:
        fake.song.last_stop = 80.0
    fake.start_latency = 60.0
    assert layer.locate(0).ok
    res = layer.play()
    assert res.status == "unconfirmed" and "not 0.00" in res.reason


def test_fake_reproduces_m0_row3() -> None:
    """The fake ignores a stopped locate exactly as Live did in M0 (probes A and B)."""
    clock = [100.0]
    fake = FakeAbletonOSC(song(), clock=lambda: clock[0])
    try:
        s = fake.song
        s.last_stop = 1.04
        fake._handle("/live/song/set/current_song_time", [32.0])
        assert fake._handle("/live/song/get/current_song_time", [])[0][1] == [32.0]
        fake._handle("/live/song/start_playing", [])
        assert fake.position() == 0.0  # probe A: start marker
        fake._handle("/live/song/stop_playing", [])
        s.last_stop = 1.04
        fake._handle("/live/song/set/current_song_time", [32.0])
        fake._handle("/live/song/continue_playing", [])
        assert fake.position() == 1.04  # probe B: last stop
        fake._handle("/live/song/set/current_song_time", [32.0])
        assert fake.position() == 32.0  # probe E/H: locate while playing sticks
    finally:
        fake.close()


def test_play_refused_while_playing(rig: Rig) -> None:
    fake, layer = rig
    assert layer.play().ok
    with fake.lock:
        n = len(fake.received)
    res = layer.play()
    assert res.status == "refused" and "never retriggers" in res.reason
    later = [a for a, _ in fake.received[n:]]
    assert "/live/song/continue_playing" not in later
    assert "/live/song/start_playing" not in later


def test_play_unconfirmed_when_live_ignores_it(rig: Rig) -> None:
    fake, layer = rig
    fake.ignore = {"/live/song/continue_playing", "/live/song/set/current_song_time"}
    res = layer.play()
    assert res.status == "unconfirmed" and "still stopped" in res.reason
    assert res.state["playing"] is False, "the page shows what Live read back"


def test_play_unconfirmed_when_position_wrong(rig: Rig) -> None:
    fake, layer = rig
    assert layer.locate(32).ok
    fake.ignore = {"/live/song/set/current_song_time"}  # like start/continue alone
    with fake.lock:
        fake.song.last_stop = 1.0
    res = layer.play()
    assert res.status == "unconfirmed" and "not 32.00" in res.reason


def test_stop(rig: Rig) -> None:
    fake, layer = rig
    assert layer.play().ok
    res = layer.stop()
    assert res.ok, res.reason
    assert res.state["playing"] is False


def test_stop_refused_when_stopped(rig: Rig) -> None:
    fake, layer = rig
    with fake.lock:
        n = len(fake.received)
    res = layer.stop()
    assert res.status == "refused" and "second Stop" in res.reason
    assert "/live/song/stop_playing" not in [a for a, _ in fake.received[n:]]


def test_locate(rig: Rig) -> None:
    fake, layer = rig
    res = layer.locate(16.5)
    assert res.ok and res.state["beat"] == 16.5
    assert fake.position() == 16.5


def test_locate_refused_while_playing(rig: Rig) -> None:
    fake, layer = rig
    assert layer.play().ok
    res = layer.locate(8)
    assert res.status == "refused" and "while stopped" in res.reason


@pytest.mark.parametrize("beat", [-1, float("nan"), float("inf"), True, "8", None])
def test_locate_refuses_bad_beat(rig: Rig, beat: object) -> None:
    fake, layer = rig
    res = layer.locate(beat)
    assert res.status == "refused" and "beat must be" in res.reason
    assert fake.received == []


def test_locate_unconfirmed_when_ignored(rig: Rig) -> None:
    fake, layer = rig
    fake.ignore = {"/live/song/set/current_song_time"}
    res = layer.locate(8)
    assert res.status == "unconfirmed" and "not 8" in res.reason


def test_mute_and_unmute(rig: Rig) -> None:
    fake, layer = rig
    assert wait_until(lambda: len(layer.state()["tracks"]) == 4)
    res = layer.mute(0, "Mic 1", True)
    assert res.ok, res.reason
    assert fake.song.tracks[0].mute is True
    assert res.state["tracks"][0]["mute"] is True
    res = layer.mute(0, "Mic 1", False)
    assert res.ok and fake.song.tracks[0].mute is False
    assert res.state["tracks"][0]["mute"] is False


def test_mute_refused_on_stale_index(rig: Rig) -> None:
    fake, layer = rig
    with fake.lock:
        fake.song.tracks.insert(0, FakeTrack("New track"))  # the page still has the old set
    res = layer.mute(0, "Mic 1", True)
    assert res.status == "refused" and "is now 'New track'" in res.reason
    assert not any(t.mute for t in fake.song.tracks)


def test_mute_refused_on_index_gone(rig: Rig) -> None:
    fake, layer = rig
    with fake.lock:
        del fake.song.tracks[3]
    res = layer.mute(3, "MONITOR", True)
    assert res.status == "refused" and "no track 3" in res.reason


@pytest.mark.parametrize(
    ("index", "name", "mute"),
    [(-1, "Mic 1", True), (True, "Mic 1", True), (0, 5, True), (0, "Mic 1", 1)],
)
def test_mute_refuses_bad_args(rig: Rig, index: object, name: object, mute: object) -> None:
    fake, layer = rig
    assert layer.mute(index, name, mute).status == "refused"
    assert fake.received == []


def test_mute_unconfirmed_when_ignored(rig: Rig) -> None:
    fake, layer = rig
    fake.ignore = {"/live/track/set/mute"}
    res = layer.mute(1, "Bite piano", True)
    assert res.status == "unconfirmed" and "mute reads False" in res.reason


def test_confirmation_timeout(rig: Rig) -> None:
    fake, layer = rig
    assert layer.locate(4).ok
    fake.reply_delay = FAST.confirm_timeout * 2  # Live slower than the re-read window
    t0 = time.monotonic()
    res = layer.locate(8)
    elapsed = time.monotonic() - t0
    assert res.status in {"unconfirmed", "unreachable"}
    assert elapsed < 4 * FAST.confirm_timeout + 0.5, "never blocks past the confirm window"
    fake.reply_delay = 0.0
    time.sleep(FAST.confirm_timeout * 3)  # let the late replies land on the held port


def test_confirmation_timeout_after_send(rig: Rig) -> None:
    """Guard read answered, then Live goes quiet: the command is unconfirmed, not ok."""
    fake, layer = rig
    original = fake._handle

    def go_quiet(address, args):  # noqa: ANN001, ANN202
        if address == "/live/song/set/current_song_time":
            fake.silent = True
        return original(address, args)

    fake._handle = go_quiet  # type: ignore[method-assign]
    res = layer.locate(8)
    assert res.status == "unconfirmed" and res.reason == "no confirmation from Live"


# -- the 11001 lifetime (WinError 10054) -------------------------------------------


def test_fake_dies_on_reply_to_unheld_port() -> None:
    """The fake reproduces AbletonOSC's 10054 death, so the next test means something."""
    reply_port = free_udp_port()
    with FakeAbletonOSC(song(), reply_port=reply_port) as fake:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(encode_message("/live/test"), fake.address)
        s.close()
        assert wait_until(lambda: fake.dead_reason is not None)
        assert "10054" in fake.dead_reason


def test_reply_port_held_across_idle_and_close() -> None:
    reply_port = free_udp_port()
    with FakeAbletonOSC(song(), reply_port=reply_port) as fake:
        layer = MixLayer(live=fake.address, bind=("127.0.0.1", reply_port), timings=FAST).start()
        layer.state()
        time.sleep(FAST.watch_window + 0.2)  # watched → idle
        assert layer.state()["live"] == "ok"  # idle → watched, same socket
        fake.reply_delay = 0.1  # replies still in flight at close
        layer.state()
        layer.close()
        time.sleep(0.3)
        assert fake.dead_reason is None, fake.dead_reason
