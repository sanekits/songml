"""Fake AbletonOSC: a loopback UDP simulator of the calls the mix layer uses.

Run standalone for off-rig development (M2 points a dev ``songml-serve --mixroom`` at it)::

    python -m songml_utils.fake_abletonosc [--port 11000] [--reply-port 11001]

It reproduces the AbletonOSC behaviour the mixroom design depends on, including the two
things the M0 spike found on the real rig (sanekits/songml#14, 2026-11-22):

* **Locate is ignored while stopped.** ``set/current_song_time`` while stopped reads back
  the new value, but ``start_playing`` starts at the start marker (0) and
  ``continue_playing`` resumes where Live last stopped. A set while playing moves the
  playhead.
* **A reply to an unheld 11001 kills the receive loop** (WinError 10054 on Windows).
  Before every reply the fake checks that the reply port is held; if it isn't, the fake
  stops answering for good and records ``dead_reason``.

Faults for tests: ``silent`` (receive but never answer), ``ignore`` (addresses whose
*effects and replies* are dropped), and ``reply_delay`` (seconds added before each reply).
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from songml_utils.osc import OSCArg, OSCError, decode_message, encode_message

type Clock = Callable[[], float]


@dataclass
class FakeTrack:
    name: str
    mute: bool = False
    is_grouped: bool = False
    is_foldable: bool = False
    color: int = 0x808080


@dataclass
class FakeSong:
    """Scripted Live set state. Mutate it directly from tests (under ``FakeAbletonOSC.lock``)."""

    tempo: float = 120.0
    signature_numerator: int = 4
    signature_denominator: int = 4
    tracks: list[FakeTrack] = field(default_factory=list)
    playing: bool = False
    # While stopped: the value get/current_song_time reads back.
    stopped_time: float = 0.0
    # Where continue_playing resumes from (the last stop position).
    last_stop: float = 0.0
    # While playing: position = play_base + elapsed * tempo / 60.
    play_base: float = 0.0
    play_t0: float = 0.0


class FakeAbletonOSC:
    def __init__(
        self,
        song: FakeSong | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        reply_port: int = 11001,
        clock: Clock = time.monotonic,
    ) -> None:
        self.song = song if song is not None else FakeSong()
        self.reply_port = reply_port
        self.clock = clock
        self.lock = threading.RLock()
        self.silent = False
        self.ignore: set[str] = set()
        self.reply_delay = 0.0
        self.dead_reason: str | None = None
        self.received: list[tuple[str, list[OSCArg]]] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((host, port))
        self._sock.settimeout(0.05)
        self.address = self._sock.getsockname()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-abletonosc", daemon=True)

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> FakeAbletonOSC:
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(2)
        self._sock.close()

    def __enter__(self) -> FakeAbletonOSC:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- song model ----------------------------------------------------------------

    def position(self) -> float:
        with self.lock:
            s = self.song
            if s.playing:
                return s.play_base + (self.clock() - s.play_t0) * s.tempo / 60.0
            return s.stopped_time

    def _play_from(self, beat: float) -> None:
        s = self.song
        s.playing = True
        s.play_base = beat
        s.play_t0 = self.clock()

    def _handle(self, address: str, args: list[OSCArg]) -> list[tuple[str, list[OSCArg]]]:
        """Apply one message; return the replies AbletonOSC would send."""
        s = self.song
        tracks = s.tracks
        song_get = {
            "/live/song/get/is_playing": lambda: [s.playing],
            "/live/song/get/current_song_time": lambda: [float(self.position())],
            "/live/song/get/tempo": lambda: [float(s.tempo)],
            "/live/song/get/signature_numerator": lambda: [s.signature_numerator],
            "/live/song/get/signature_denominator": lambda: [s.signature_denominator],
            "/live/song/get/num_tracks": lambda: [len(tracks)],
        }
        if address == "/live/test":
            return [(address, ["ok"])]
        if address in song_get:
            return [(address, song_get[address]())]
        if address == "/live/song/start_playing":
            if not s.playing:
                self._play_from(0.0)  # the start marker, never the located position
            return []
        if address == "/live/song/continue_playing":
            if not s.playing:
                self._play_from(s.last_stop)  # ignores a locate made while stopped
            return []
        if address == "/live/song/stop_playing":
            if s.playing:
                pos = self.position()
                s.playing = False
                s.stopped_time = pos
                s.last_stop = pos
            return []
        if address == "/live/song/set/current_song_time":
            beat = float(args[0])
            if s.playing:
                s.play_base = beat
                s.play_t0 = self.clock()
            else:
                s.stopped_time = beat
            return []
        if address == "/live/song/get/track_data":
            lo, hi, *props = args
            if not (0 <= int(lo) <= int(hi) <= len(tracks)):
                return [("/live/error", [f"track_data: index out of range {lo}..{hi}"])]
            out: list[OSCArg] = []
            for t in tracks[int(lo) : int(hi)]:
                for prop in props:
                    obj, _, attr = str(prop).partition(".")
                    if obj != "track" or not hasattr(t, attr):
                        return [("/live/error", [f"track_data: unknown property {prop}"])]
                    out.append(getattr(t, attr))
            return [(address, out)]
        if address in ("/live/track/get/mute", "/live/track/get/name"):
            index = int(args[0])
            if not 0 <= index < len(tracks):
                return [("/live/error", [f"{address}: index out of range {index}"])]
            attr = address.rsplit("/", 1)[1]
            return [(address, [index, getattr(tracks[index], attr)])]
        if address == "/live/track/set/mute":
            index = int(args[0])
            if not 0 <= index < len(tracks):
                return [("/live/error", [f"{address}: index out of range {index}"])]
            tracks[index].mute = bool(args[1])
            return []
        return [("/live/error", [f"Unknown OSC address: {address}"])]

    # -- transport -----------------------------------------------------------------

    def _reply_port_held(self, host: str) -> bool:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((host, self.reply_port))
        except OSError:
            return True
        finally:
            probe.close()
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, (host, _port) = self._sock.recvfrom(65536)
            except TimeoutError:
                continue
            except OSError:
                return
            if self.dead_reason is not None:
                continue
            try:
                address, args = decode_message(data)
            except OSCError as e:
                print(f"fake-abletonosc: bad datagram: {e}")
                continue
            with self.lock:
                self.received.append((address, args))
                if address in self.ignore:
                    continue
                replies = self._handle(address, args)
                silent = self.silent
                delay = self.reply_delay
            if silent or not replies:
                continue
            if delay:
                time.sleep(delay)
            for reply_address, reply_args in replies:
                if not self._reply_port_held(host):
                    self.dead_reason = (
                        f"reply {reply_address} to unheld {host}:{self.reply_port} "
                        "(WinError 10054: receive loop dead)"
                    )
                    break
                self._sock.sendto(
                    encode_message(reply_address, reply_args), (host, self.reply_port)
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake AbletonOSC on loopback, for dev.")
    parser.add_argument("--port", type=int, default=11000)
    parser.add_argument("--reply-port", type=int, default=11001)
    opts = parser.parse_args()
    song = FakeSong(
        tracks=[
            FakeTrack("Drums", color=0xFF5050),
            FakeTrack("Bass", color=0x50A0FF),
            FakeTrack("Keys", is_foldable=True, color=0x50FF80),
            FakeTrack("Keys L", is_grouped=True, color=0x50FF80),
            FakeTrack("Keys R", is_grouped=True, color=0x50FF80),
            FakeTrack("Click", mute=True, color=0xFFFFFF),
        ]
    )
    fake = FakeAbletonOSC(song, port=opts.port, reply_port=opts.reply_port).start()
    print(f"fake AbletonOSC on {fake.address[0]}:{fake.address[1]}, replies to :{opts.reply_port}")
    with contextlib.suppress(KeyboardInterrupt):
        while fake.dead_reason is None:
            time.sleep(0.5)
        print(f"fake AbletonOSC receive loop dead: {fake.dead_reason}")
    fake.close()


if __name__ == "__main__":
    main()
