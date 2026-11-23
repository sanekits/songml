"""M2 tests: songml-serve --mixroom, the /api/mix/* routes and the song-page panel.

The routes run against the real mix layer and the fake AbletonOSC over loopback UDP, with
the M1 tests' scaled-down timings.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from io import BytesIO
from unittest.mock import Mock, patch

import pytest

from songml_utils.fake_abletonosc import FakeAbletonOSC
from songml_utils.html_exporter import to_html_string
from songml_utils.mixroom import MixLayer, MixroomPortTaken
from songml_utils.osc import OSCError, decode_message
from songml_utils.parser import parse_songml
from songml_utils.web_server import _make_handler, main

from .test_mixroom import FAST, free_udp_port, song, wait_until

TOKEN = "secret"

_SONG = """\
Title: Mix Song
Key: Cmaj
Tempo: 120
Time: 4/4

[Intro - 2 bars]
|  0  |  1  |
| C   | G   |
"""


def _handler(**kw):
    handler = object.__new__(kw.pop("cls"))
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = BytesIO()
    handler.log_date_time_string = Mock(return_value="00:00:00")
    for k, v in kw.items():
        setattr(handler, k, v)
    return handler


def get(cls, path: str) -> tuple[int, bytes]:
    h = _handler(cls=cls, path=path, command="GET")
    h.do_GET()
    return h.send_response.call_args[0][0], h.wfile.getvalue()


def post(cls, path: str, payload: object, raw: bytes | None = None) -> tuple[int, dict | bytes]:
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    h = _handler(
        cls=cls,
        path=path,
        command="POST",
        rfile=BytesIO(body),
        headers={"Content-Length": str(len(body))},
    )
    h.do_POST()
    out = h.wfile.getvalue()
    try:
        return h.send_response.call_args[0][0], json.loads(out)
    except ValueError:
        return h.send_response.call_args[0][0], out


def state(cls) -> dict:
    status, body = get(cls, f"/api/mix/state?token={TOKEN}")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    return data["state"]


type Served = tuple[FakeAbletonOSC, MixLayer, type]


@pytest.fixture
def served(tmp_path) -> Iterator[Served]:
    (tmp_path / "song.songml").write_text(_SONG)
    reply_port = free_udp_port()
    fake = FakeAbletonOSC(song(), reply_port=reply_port).start()
    layer = MixLayer(live=fake.address, bind=("127.0.0.1", reply_port), timings=FAST).start()
    cls = _make_handler(tmp_path, bars_per_row=8, write_token=TOKEN, mix=layer)
    yield fake, layer, cls
    layer.close()
    fake.close()
    assert fake.dead_reason is None, fake.dead_reason


# -- without --mixroom ---------------------------------------------------------------


@pytest.mark.parametrize("command", ["play", "stop", "locate", "mute"])
def test_commands_404_without_mixroom(tmp_path, command) -> None:
    cls = _make_handler(tmp_path, bars_per_row=8, write_token=TOKEN)
    status, _ = post(cls, f"/api/mix/{command}", {"token": TOKEN})
    assert status == 404


def test_state_404_without_mixroom(tmp_path) -> None:
    cls = _make_handler(tmp_path, bars_per_row=8, write_token=TOKEN)
    assert get(cls, f"/api/mix/state?token={TOKEN}")[0] == 404


def test_unknown_mix_command_404(served) -> None:
    _, _, cls = served
    assert post(cls, "/api/mix/record", {"token": TOKEN})[0] == 404


# -- token ---------------------------------------------------------------------------


def test_state_needs_token(served) -> None:
    _, _, cls = served
    assert get(cls, "/api/mix/state")[0] == 403
    assert get(cls, "/api/mix/state?token=wrong")[0] == 403


@pytest.mark.parametrize("command", ["play", "stop", "locate", "mute"])
def test_commands_need_token(served, command) -> None:
    fake, _, cls = served
    assert post(cls, f"/api/mix/{command}", {})[0] == 403
    assert post(cls, f"/api/mix/{command}", {"token": "wrong"})[0] == 403
    # Refused before reaching Live: nothing but polling was sent.
    with fake.lock:
        sent = {address for address, _ in fake.received}
    assert not sent & {
        "/live/song/continue_playing",
        "/live/song/stop_playing",
        "/live/song/set/current_song_time",
        "/live/track/set/mute",
    }


def test_mix_routes_503_without_server_token(tmp_path) -> None:
    layer = Mock(spec=MixLayer)
    cls = _make_handler(tmp_path, bars_per_row=8, write_token=None, mix=layer)
    assert get(cls, "/api/mix/state?token=x")[0] == 503
    assert post(cls, "/api/mix/play", {"token": "x"})[0] == 503
    layer.play.assert_not_called()


def test_bad_bodies(served) -> None:
    _, _, cls = served
    assert post(cls, "/api/mix/play", None, raw=b"not json")[0] == 400
    assert post(cls, "/api/mix/play", None, raw=b"[1]")[0] == 400
    assert post(cls, "/api/mix/play", None, raw=b"")[0] == 413
    big = json.dumps({"token": TOKEN, "pad": "x" * 5000}).encode()
    assert post(cls, "/api/mix/play", None, raw=big)[0] == 413


# -- routes ----------------------------------------------------------------------------


def test_state_reports_live(served) -> None:
    _, _, cls = served
    s = state(cls)
    assert s["live"] == "ok"
    assert s["playing"] is False
    assert s["signature"] == [4, 4]
    assert wait_until(lambda: len(state(cls)["tracks"]) == 4)
    names = [t["name"] for t in state(cls)["tracks"]]
    assert names == ["Mic 1", "Bite piano", "Ballroom Blues", "MONITOR"]


def test_play_then_refused(served) -> None:
    fake, _, cls = served
    status, body = post(cls, "/api/mix/locate", {"token": TOKEN, "beat": 32.0})
    assert (status, body["ok"]) == (200, True)
    assert body["state"]["beat"] == pytest.approx(32.0)
    status, body = post(cls, "/api/mix/play", {"token": TOKEN})
    assert (status, body["status"]) == (200, "ok")
    assert body["state"]["playing"] is True
    assert fake.position() >= 32.0  # play-then-locate: started from the located bar
    status, body = post(cls, "/api/mix/play", {"token": TOKEN})
    assert (status, body["status"]) == (409, "refused")
    assert "never retriggers" in body["error"]
    assert body["state"]["playing"] is True


def test_stop_then_refused(served) -> None:
    _, _, cls = served
    assert post(cls, "/api/mix/play", {"token": TOKEN})[0] == 200
    status, body = post(cls, "/api/mix/stop", {"token": TOKEN})
    assert (status, body["ok"], body["state"]["playing"]) == (200, True, False)
    status, body = post(cls, "/api/mix/stop", {"token": TOKEN})
    assert (status, body["status"]) == (409, "refused")
    assert "already stopped" in body["error"]


def test_locate_refusals(served) -> None:
    _, _, cls = served
    for bad in [-1, "8", None, True]:
        status, body = post(cls, "/api/mix/locate", {"token": TOKEN, "beat": bad})
        assert (status, body["status"]) == (409, "refused"), bad
    assert post(cls, "/api/mix/play", {"token": TOKEN})[0] == 200
    status, body = post(cls, "/api/mix/locate", {"token": TOKEN, "beat": 4})
    assert (status, body["error"]) == (409, "position is set while stopped")


def test_mute_and_unmute(served) -> None:
    fake, _, cls = served
    status, body = post(
        cls, "/api/mix/mute", {"token": TOKEN, "index": 1, "name": "Bite piano", "mute": True}
    )
    assert (status, body["ok"]) == (200, True)
    assert fake.song.tracks[1].mute is True
    status, body = post(
        cls, "/api/mix/mute", {"token": TOKEN, "index": 1, "name": "Bite piano", "mute": False}
    )
    assert (status, body["ok"]) == (200, True)
    assert fake.song.tracks[1].mute is False


def test_mute_stale_name_refused(served) -> None:
    fake, _, cls = served
    status, body = post(
        cls, "/api/mix/mute", {"token": TOKEN, "index": 1, "name": "Old name", "mute": True}
    )
    assert (status, body["status"]) == (409, "refused")
    assert "reload the page" in body["error"]
    assert fake.song.tracks[1].mute is False


def test_unconfirmed_is_504(served) -> None:
    fake, _, cls = served
    with fake.lock:
        fake.ignore.add("/live/track/get/mute")
    status, body = post(
        cls, "/api/mix/mute", {"token": TOKEN, "index": 0, "name": "Mic 1", "mute": True}
    )
    assert (status, body["status"]) == (504, "unconfirmed")
    assert body["error"].startswith("no confirmation from Live")


def test_unreachable_is_502(served) -> None:
    fake, _, cls = served
    with fake.lock:
        fake.silent = True
    status, body = post(cls, "/api/mix/play", {"token": TOKEN})
    assert (status, body["status"]) == (502, "unreachable")
    assert "Live not answering" in body["error"]
    assert wait_until(lambda: state(cls)["live"] == "unreachable")


# -- Bert's #16 review items -----------------------------------------------------------


def test_osc_decode_wraps_bad_utf8() -> None:
    with pytest.raises(OSCError):
        decode_message(b"/x\xff\x00")


def test_garbage_datagram_does_not_kill_receive_loop(served) -> None:
    _, layer, cls = served
    assert state(cls)["live"] == "ok"
    junk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        junk.sendto(b"/live/song/get/tempo\x00\x00\x00\x00,s\x00\x00\xff\xfe\x00\x00", layer.bind)
    finally:
        junk.close()
    assert wait_until(lambda: (layer.snapshot()["live_error"] or "").startswith("bad datagram"))
    assert all(th.is_alive() for th in layer._threads)
    status, body = post(cls, "/api/mix/locate", {"token": TOKEN, "beat": 8})
    assert (status, body["ok"]) == (200, True)


def test_mute_confirms_int_reply(served) -> None:
    fake, _, cls = served
    with fake.lock:
        fake.int_mutes = True
    status, body = post(
        cls, "/api/mix/mute", {"token": TOKEN, "index": 2, "name": "Ballroom Blues", "mute": True}
    )
    assert (status, body["ok"]) == (200, True)
    status, body = post(
        cls, "/api/mix/mute", {"token": TOKEN, "index": 2, "name": "Ballroom Blues", "mute": False}
    )
    assert (status, body["ok"]) == (200, True)


# -- the panel in the song page ---------------------------------------------------------


def _page(**kw) -> str:
    return to_html_string(parse_songml(_SONG), **kw)


def test_panel_with_token_and_mixroom() -> None:
    page = _page(editable=True, write_token=TOKEN, song_path="song.songml", mixroom=True)
    assert 'id="songml-mix"' in page
    assert "/api/mix/state" in page
    assert '"secret"' in page
    assert "__WRITE_TOKEN__" not in page


def test_no_panel_without_token() -> None:
    page = _page(editable=False, write_token=None, song_path="song.songml", mixroom=True)
    assert "songml-mix" not in page
    assert "/api/mix/" not in page


def test_no_panel_without_mixroom() -> None:
    page = _page(editable=True, write_token=TOKEN, song_path="song.songml")
    assert "songml-mix" not in page


def test_served_song_page_has_panel(served) -> None:
    _, _, cls = served
    status, body = get(cls, "/song/song.songml")
    assert status == 200
    assert b'id="songml-mix"' in body


def test_served_song_page_no_panel_without_mixroom(tmp_path) -> None:
    (tmp_path / "song.songml").write_text(_SONG)
    cls = _make_handler(tmp_path, bars_per_row=8, write_token=TOKEN)
    status, body = get(cls, "/song/song.songml")
    assert status == 200
    assert b"songml-mix" not in body


# -- main --mixroom -----------------------------------------------------------------------


def _run_main(argv: list[str], env: dict[str, str]):
    with (
        patch("songml_utils.web_server.ThreadingHTTPServer") as srv_cls,
        patch("songml_utils.web_server.MixLayer") as mix_cls,
        patch("songml_utils.web_server.signal.signal") as sig,
        patch.dict("os.environ", env, clear=False),
        patch("sys.argv", ["songml-serve", *argv]),
    ):
        srv = srv_cls.return_value
        srv.serve_forever.side_effect = KeyboardInterrupt()
        mix = mix_cls.return_value
        mix.live, mix.bind = ("127.0.0.1", 11000), ("127.0.0.1", 11001)
        try:
            rc = main()
        except SystemExit as e:
            rc = e
        return rc, srv_cls, mix_cls, sig


def test_main_mixroom_needs_token(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("SONGML_WRITE_TOKEN", raising=False)
    rc, srv, mix_cls, _ = _run_main(["--root", str(tmp_path), "--mixroom"], {})
    assert rc == 1
    assert "--mixroom needs SONGML_WRITE_TOKEN" in capsys.readouterr().err
    mix_cls.assert_not_called()


def test_main_mixroom_starts_and_closes_layer(tmp_path, capsys) -> None:
    rc, srv, mix_cls, sig = _run_main(
        ["--root", str(tmp_path), "--mixroom"], {"SONGML_WRITE_TOKEN": TOKEN}
    )
    assert rc == 0
    mix = mix_cls.return_value
    mix.start.assert_called_once()
    mix.close.assert_called_once()
    assert srv.call_args[0][1].mix is mix  # ThreadingHTTPServer(addr, handler)
    assert sig.call_args[0][0].name == "SIGTERM"  # systemd stop drains, too
    assert "Mixroom:        enabled" in capsys.readouterr().out


def test_main_without_mixroom_has_no_layer(tmp_path, capsys) -> None:
    rc, srv, mix_cls, sig = _run_main(["--root", str(tmp_path)], {"SONGML_WRITE_TOKEN": TOKEN})
    assert rc == 0
    mix_cls.assert_not_called()
    sig.assert_not_called()
    assert srv.call_args[0][1].mix is None
    assert "Mixroom:        disabled" in capsys.readouterr().out


def test_main_mixroom_port_taken_exits_loudly(tmp_path) -> None:
    taken = MixroomPortTaken(("127.0.0.1", 11001), "held by PID 42 (python3)")
    with (
        patch("songml_utils.web_server.ThreadingHTTPServer") as srv_cls,
        patch("songml_utils.web_server.MixLayer") as mix_cls,
        patch("songml_utils.web_server.signal.signal"),
        patch.dict("os.environ", {"SONGML_WRITE_TOKEN": TOKEN}),
        patch("sys.argv", ["songml-serve", "--root", str(tmp_path), "--mixroom"]),
    ):
        mix_cls.return_value.start.side_effect = taken
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value is taken
    assert "11001" in str(exc.value.code)
    srv_cls.return_value.server_close.assert_called_once()
    srv_cls.return_value.serve_forever.assert_not_called()
