"""Mixroom mix layer: transport and track mutes in the mixroom Live, over AbletonOSC.

Design: ``docs/mixroom-transport-design.md`` (sanekits/songml#14, #15). This module is the
M1 slice: the OSC client, the poll loop, the watched window, the command guards and the
read-after-write checks. ``songml-serve --mixroom`` (M2) serves it as ``/api/mix/*``.

Shape:

* It owns the UDP socket on AbletonOSC's fixed reply port (127.0.0.1:11001) for its whole
  life. If the port is taken, ``MixLayer.start`` raises ``MixroomPortTaken``, a
  ``SystemExit``: the server exits non-zero naming the port and the holder. There is no
  other port to fall back to.
* The socket is never closed while a reply may still be on its way: on Windows, a reply
  to an unheld 11001 comes back as ICMP port-unreachable and kills AbletonOSC's receive
  loop (WinError 10054, seen on the rig in M0). ``close`` stops polling, drains the
  in-flight queries, and only then closes.
* Sync is by polling, only while watched: some ``state()`` call in the last 10 s. Transport
  at 4 Hz, track list at 1 Hz. At most one query per OSC address is in flight, so a
  reply's address identifies its query.
* Every command reads its guard fresh, sends, then re-reads what it changed and reports
  the read state. A missing re-read is ``unconfirmed``, never assumed.
* **Play is play-then-locate** (M0 row 3): while Live is stopped, a located
  ``current_song_time`` reads back but ``start_playing`` starts at the start marker and
  ``continue_playing`` at the last stop. So Play sends ``continue_playing`` and then
  ``set/current_song_time <the stopped position>`` in one burst, and checks Live is
  playing from that position. One method, no try-A-then-B.

Positions are Live beats (quarter notes) throughout. Chart-bar arithmetic belongs to the
page (design §6), never here.
"""

from __future__ import annotations

import errno
import math
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from songml_utils.osc import OSCArg, OSCError, decode_message, encode_message

type Clock = Callable[[], float]
type Address = tuple[str, int]
type MixState = dict[str, Any]
type Status = Literal["ok", "refused", "unconfirmed", "unreachable"]

LIVE_ADDRESS: Address = ("127.0.0.1", 11000)
REPLY_ADDRESS: Address = ("127.0.0.1", 11001)

UNREACHABLE_REASON = (
    "mixroom Live not answering: is Live open with AbletonOSC selected as a control surface?"
)

TRANSPORT_QUERIES = (
    "/live/song/get/is_playing",
    "/live/song/get/current_song_time",
    "/live/song/get/tempo",
    "/live/song/get/signature_numerator",
    "/live/song/get/signature_denominator",
)
NUM_TRACKS = "/live/song/get/num_tracks"
TRACK_DATA = "/live/song/get/track_data"
TRACK_PROPS = ("track.name", "track.mute", "track.is_grouped", "track.is_foldable", "track.color")
TRACK_MUTE = "/live/track/get/mute"
TRACK_NAME = "/live/track/get/name"

# Live reports positions as float32 over OSC.
LOCATE_TOLERANCE = 1e-3
# Slack on top of the beats Live can play between the play burst and the re-read.
PLAY_POSITION_SLACK = 0.25


@dataclass(frozen=True)
class Timings:
    """Seconds. The defaults are the design's numbers (§3); tests scale them down."""

    transport_interval: float = 0.25
    tracks_interval: float = 1.0
    watch_window: float = 10.0
    unreachable_after: float = 1.5
    confirm_timeout: float = 0.4


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a command. M2 maps ``refused`` to HTTP 409."""

    status: Status
    reason: str | None
    state: MixState

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class _Slot:
    args: list[OSCArg]
    at: float
    seq: int


class MixroomPortTaken(SystemExit):
    """11001 is held by another process. Raised as SystemExit: songml-serve exits loudly."""

    def __init__(self, address: Address, holder: str) -> None:
        self.address = address
        self.holder = holder
        super().__init__(
            f"songml-serve --mixroom: cannot bind {address[0]}:{address[1]}, AbletonOSC's "
            f"fixed reply port: {holder}. Only one process on this host can hold it; stop "
            "that process first."
        )


def find_udp_port_holder(port: int) -> str:
    """Describe which Linux process holds UDP ``port`` (for the port-taken message)."""
    inodes: set[str] = set()
    for table in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            cols = line.split()
            if len(cols) > 9 and int(cols[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(cols[9])
    if not inodes:
        return (
            "holder not found among Linux processes (in mirrored WSL a Windows process may hold it)"
        )
    targets = {f"socket:[{inode}]" for inode in inodes}
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                if os.readlink(fd) in targets:
                    cmd = (proc / "cmdline").read_bytes().replace(b"\x00", b" ").decode().strip()
                    return f"held by PID {proc.name} ({cmd or '?'})"
        except OSError:
            continue
    return f"held by socket inode {', '.join(sorted(inodes))}, owning PID not visible to this user"


class MixLayer:
    def __init__(
        self,
        *,
        live: Address = LIVE_ADDRESS,
        bind: Address = REPLY_ADDRESS,
        timings: Timings | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self.live = live
        self.bind = bind
        self.t = timings or Timings()
        self.clock = clock
        self._sock: socket.socket | None = None
        self._cv = threading.Condition()
        self._slots: dict[str, _Slot] = {}
        self._inflight: dict[str, float] = {}
        self._tracks: list[dict[str, Any]] = []
        self._tracks_key: tuple[str, ...] | None = None
        self._tracks_rev = 0
        self._tracks_at: float | None = None
        self._last_watch: float | None = None
        self._last_error: str | None = None
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> MixLayer:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(self.bind)
        except OSError as e:
            sock.close()
            if e.errno == errno.EADDRINUSE:
                raise MixroomPortTaken(self.bind, find_udp_port_holder(self.bind[1])) from e
            raise
        sock.settimeout(0.05)
        self._sock = sock
        self._threads = [
            threading.Thread(target=self._receive_loop, name="mixroom-recv", daemon=True),
            threading.Thread(target=self._poll_loop, name="mixroom-poll", daemon=True),
        ]
        for th in self._threads:
            th.start()
        return self

    def close(self) -> None:
        """Stop polling, let in-flight replies land on the held port, then release it."""
        if self._sock is None:
            return
        self._stopping.set()
        self._wake.set()
        self._threads[1].join(2)
        with self._cv:
            self._cv.wait_for(lambda: not self._inflight, self.t.confirm_timeout)
        self._threads[0].join(2)
        self._sock.close()
        self._sock = None

    def __enter__(self) -> MixLayer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- OSC plumbing --------------------------------------------------------------

    def _send(self, address: str, args: list[OSCArg] | tuple[OSCArg, ...] = ()) -> None:
        assert self._sock is not None, "MixLayer used before start()"
        self._sock.sendto(encode_message(address, args), self.live)

    def _inflight_live(self, address: str, now: float) -> bool:
        sent = self._inflight.get(address)
        return sent is not None and now - sent < self.t.confirm_timeout

    def _fire(self, address: str, args: list[OSCArg] | tuple[OSCArg, ...] = ()) -> None:
        """Send a poll query unless one for the same address is still in flight."""
        with self._cv:
            now = self.clock()
            if self._inflight_live(address, now):
                return
            self._inflight[address] = now
            self._send(address, args)

    def _query(
        self, address: str, args: list[OSCArg] | tuple[OSCArg, ...] = ()
    ) -> list[OSCArg] | None:
        """Send a query and wait for its own reply. None if Live doesn't answer in time."""
        with self._cv:
            # One query per address in flight: wait out the poller's (or another command's).
            self._cv.wait_for(
                lambda: not self._inflight_live(address, self.clock()), self.t.confirm_timeout
            )
            slot = self._slots.get(address)
            seq0 = slot.seq if slot else 0
            self._inflight[address] = self.clock()
            self._send(address, args)
            got = self._cv.wait_for(
                lambda: (s := self._slots.get(address)) is not None and s.seq > seq0,
                self.t.confirm_timeout,
            )
            if not got:
                self._inflight.pop(address, None)
                return None
            return list(self._slots[address].args)

    def _receive_loop(self) -> None:
        assert self._sock is not None
        sock = self._sock
        while True:
            try:
                data, _peer = sock.recvfrom(65536)
            except TimeoutError:
                if self._stopping.is_set() and not self._inflight:
                    return
                continue
            except OSError:
                return
            try:
                address, args = decode_message(data)
            except OSCError as e:
                self._last_error = f"bad datagram from Live: {e}"
                continue
            self._on_reply(address, args)

    def _on_reply(self, address: str, args: list[OSCArg]) -> None:
        fire_track_data: int | None = None
        with self._cv:
            now = self.clock()
            if address == "/live/error":
                self._last_error = " ".join(str(a) for a in args)
            slot = self._slots.get(address)
            self._slots[address] = _Slot(args, now, (slot.seq if slot else 0) + 1)
            self._inflight.pop(address, None)
            if address == NUM_TRACKS and args:
                n = int(args[0])
                if n == 0:
                    self._set_tracks([], now)
                else:
                    fire_track_data = n
            elif address == TRACK_DATA:
                self._parse_track_data(args, now)
            elif address == TRACK_MUTE and len(args) == 2:
                index = int(args[0])
                if 0 <= index < len(self._tracks):
                    self._tracks[index]["mute"] = bool(args[1])
            self._cv.notify_all()
        if fire_track_data is not None and not self._stopping.is_set():
            self._fire(TRACK_DATA, [0, fire_track_data, *TRACK_PROPS])

    def _parse_track_data(self, args: list[OSCArg], now: float) -> None:
        width = len(TRACK_PROPS)
        if len(args) % width:
            self._last_error = f"track_data reply has {len(args)} values, not a multiple of {width}"
            return
        tracks = []
        for i in range(0, len(args), width):
            name, mute, grouped, foldable, color = args[i : i + width]
            tracks.append(
                {
                    "index": i // width,
                    "name": str(name),
                    "mute": bool(mute),
                    "grouped": bool(grouped),
                    "foldable": bool(foldable),
                    "color": int(color),
                }
            )
        self._set_tracks(tracks, now)

    def _set_tracks(self, tracks: list[dict[str, Any]], now: float) -> None:
        key = tuple(t["name"] for t in tracks)
        if key != self._tracks_key:
            self._tracks_key = key
            self._tracks_rev += 1
        self._tracks = tracks
        self._tracks_at = now

    # -- polling -------------------------------------------------------------------

    def _watched(self, now: float) -> bool:
        return self._last_watch is not None and now - self._last_watch < self.t.watch_window

    def _poll_loop(self) -> None:
        next_transport = next_tracks = 0.0
        while not self._stopping.is_set():
            now = self.clock()
            if not self._watched(now):
                next_transport = next_tracks = 0.0
                self._wake.wait(0.25)
                self._wake.clear()
                continue
            if now >= next_transport:
                for address in TRANSPORT_QUERIES:
                    self._fire(address)
                next_transport = now + self.t.transport_interval
            if now >= next_tracks:
                self._fire(NUM_TRACKS)
                next_tracks = now + self.t.tracks_interval
            self._wake.wait(max(0.0, min(next_transport, next_tracks) - self.clock()))
            self._wake.clear()

    # -- state ---------------------------------------------------------------------

    def _value(self, address: str) -> OSCArg | None:
        slot = self._slots.get(address)
        return slot.args[0] if slot and slot.args else None

    def snapshot(self) -> MixState:
        """The current state, without counting as a watch."""
        with self._cv:
            now = self.clock()
            ages = [
                now - s.at
                for a in ("/live/song/get/is_playing", "/live/song/get/current_song_time")
                if (s := self._slots.get(a)) is not None
            ]
            transport_age = max(ages) if len(ages) == 2 else None
            reachable = transport_age is not None and transport_age <= self.t.unreachable_after
            playing = self._value("/live/song/get/is_playing")
            beat = self._value("/live/song/get/current_song_time")
            tempo = self._value("/live/song/get/tempo")
            return {
                "live": "ok" if reachable else "unreachable",
                "reason": None if reachable else UNREACHABLE_REASON,
                "playing": bool(playing) if playing is not None else None,
                "beat": float(beat) if beat is not None else None,
                "tempo": float(tempo) if tempo is not None else None,
                "signature": [
                    self._value("/live/song/get/signature_numerator"),
                    self._value("/live/song/get/signature_denominator"),
                ],
                "transport_age_ms": _ms(transport_age),
                "tracks": [dict(t) for t in self._tracks],
                "tracks_rev": self._tracks_rev,
                "tracks_age_ms": _ms(
                    now - self._tracks_at if self._tracks_at is not None else None
                ),
                "live_error": self._last_error,
            }

    def state(self) -> MixState:
        """The state for ``GET /api/mix/state``. Counts as a watch (keeps polling on)."""
        with self._cv:
            now = self.clock()
            waking = not self._watched(now)
            self._last_watch = now
            seq0 = {a: s.seq if (s := self._slots.get(a)) else 0 for a in TRANSPORT_QUERIES}
        if waking:
            # Polling was idle: wait for one fresh transport read rather than report stale.
            self._wake.set()
            with self._cv:
                self._cv.wait_for(
                    lambda: all(
                        (s := self._slots.get(a)) is not None and s.seq > seq
                        for a, seq in seq0.items()
                    ),
                    self.t.confirm_timeout,
                )
        return self.snapshot()

    # -- commands ------------------------------------------------------------------

    def _result(self, status: Status, reason: str | None = None) -> CommandResult:
        return CommandResult(status, reason, self.snapshot())

    def _unreachable(self) -> CommandResult:
        return self._result("unreachable", UNREACHABLE_REASON)

    def _unconfirmed(self, detail: str = "") -> CommandResult:
        reason = "no confirmation from Live" + (f": {detail}" if detail else "")
        return self._result("unconfirmed", reason)

    def play(self) -> CommandResult:
        """Start playback from the position Live shows while stopped (play-then-locate)."""
        playing = self._query("/live/song/get/is_playing")
        if playing is None:
            return self._unreachable()
        if playing[0]:
            return self._result("refused", "already playing: Play never retriggers")
        target_r = self._query("/live/song/get/current_song_time")
        tempo_r = self._query("/live/song/get/tempo")
        if target_r is None or tempo_r is None:
            return self._unreachable()
        target, tempo = float(target_r[0]), float(tempo_r[0])
        sent = self.clock()
        self._send("/live/song/continue_playing")
        self._send("/live/song/set/current_song_time", [target])
        playing = self._query("/live/song/get/is_playing")
        pos_r = self._query("/live/song/get/current_song_time")
        if playing is None or pos_r is None:
            return self._unconfirmed()
        if not playing[0]:
            return self._unconfirmed("Live is still stopped")
        pos = float(pos_r[0])
        reach = tempo / 60.0 * (self.clock() - sent) + PLAY_POSITION_SLACK
        if not target - LOCATE_TOLERANCE <= pos <= target + reach:
            return self._unconfirmed(f"Live is playing from beat {pos:.2f}, not {target:.2f}")
        return self._result("ok")

    def stop(self) -> CommandResult:
        playing = self._query("/live/song/get/is_playing")
        if playing is None:
            return self._unreachable()
        if not playing[0]:
            return self._result(
                "refused", "already stopped: a second Stop would return Live to the start"
            )
        self._send("/live/song/stop_playing")
        playing = self._query("/live/song/get/is_playing")
        pos = self._query("/live/song/get/current_song_time")
        if playing is None or pos is None:
            return self._unconfirmed()
        if playing[0]:
            return self._unconfirmed("Live is still playing")
        return self._result("ok")

    def locate(self, beat: object) -> CommandResult:
        if (
            isinstance(beat, bool)
            or not isinstance(beat, int | float)
            or not math.isfinite(beat)
            or beat < 0
        ):
            return self._result("refused", f"beat must be a number ≥ 0, not {beat!r}")
        playing = self._query("/live/song/get/is_playing")
        if playing is None:
            return self._unreachable()
        if playing[0]:
            return self._result("refused", "position is set while stopped")
        self._send("/live/song/set/current_song_time", [float(beat)])
        pos = self._query("/live/song/get/current_song_time")
        if pos is None:
            return self._unconfirmed()
        if abs(float(pos[0]) - beat) > LOCATE_TOLERANCE:
            return self._unconfirmed(f"Live is at beat {float(pos[0]):.3f}, not {beat}")
        return self._result("ok")

    def mute(self, index: object, name: object, mute: object) -> CommandResult:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return self._result("refused", f"index must be an integer ≥ 0, not {index!r}")
        if not isinstance(name, str) or not isinstance(mute, bool):
            return self._result("refused", "name must be a string and mute a boolean")
        count = self._query(NUM_TRACKS)
        if count is None:
            return self._unreachable()
        if index >= int(count[0]):
            return self._result(
                "refused", f"the set changed: no track {index} any more (reload the page)"
            )
        current = self._query(TRACK_NAME, [index])
        if current is None:
            return self._unreachable()
        if current != [index, name]:
            return self._result(
                "refused",
                f"the set changed: track {index} is now {current[-1]!r}, not {name!r} "
                "(reload the page)",
            )
        self._send("/live/track/set/mute", [index, int(mute)])
        got = self._query(TRACK_MUTE, [index])
        if got is None:
            return self._unconfirmed()
        # Normalize: AbletonOSC may answer the mute as int 0/1 rather than T/F.
        if len(got) != 2 or got[0] != index or bool(got[1]) != mute:
            return self._unconfirmed(f"track {index} mute reads {got[-1]}")
        return self._result("ok")


def _ms(seconds: float | None) -> int | None:
    return None if seconds is None else round(seconds * 1000)
