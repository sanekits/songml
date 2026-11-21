# Mixroom transport and track mutes in songml-serve: design

*Mark, 2026-11-21, wake 20261121-184009-374489, for sanekits/songml#14.
This is a design document. It contains no code.*

Tags: **[Les]** marks a decision that belongs to Les. Each one is also
listed as a single line in §9. **[default]** marks an engineering choice
I've made; if nobody objects, it stands. **[spike]** marks something only
the rig can answer.

---

## 0. Summary

1. **The relay is a small new process, `songml-mixbridge`, and it has to
   run on Stablebeast itself, not inside the Coder container.** AbletonOSC
   sends replies to `<sender IP>:11001`, and it sends listener pushes and
   `/live/test` to **127.0.0.1:11001 only** (`osc_server.py`, `send()`
   with no address). The fixed reply port breaks any relay behind the
   container's NAT, and the loopback-only pushes rule out anything that
   isn't on Stablebeast's loopback. WSL on Stablebeast runs in **mirrored**
   networking mode (`wslinfo --networking-mode` prints `mirrored`), so a
   WSL process on 127.0.0.1 shares Windows' loopback with Live. The bridge
   runs in WSL under systemd and talks to Live over loopback. songml-serve
   reaches it over TCP. The browser only ever talks to songml-serve. (§2)
2. **Sync is by polling, and polling happens only while someone is
   watching.** The bridge re-reads transport state at 4 Hz and the track
   list plus mute states at 1 Hz, and does both only while a page has
   asked for state in the last 10 s. It uses no AbletonOSC listeners. That
   keeps things correct across set loads, AbletonOSC reloads and bridge
   restarts, which are exactly the cases where listeners go stale. After
   every command, the bridge re-reads the property it changed before it
   answers, so the page shows Live's actual state, not what it hoped
   would happen. (§3)
3. **The UI is a panel fixed to the bottom of the existing song page.**
   It has a slim collapsed strip (Play/Stop and the position readout) and
   an expanded tray (position controls and the mute grid). Touch targets
   are at least 56 px, and Play/Stop are 72 px. The panel shows up only
   when songml-serve has both editing (the write token) and `--mix-url`
   configured, and every `/api/mix/*` call is token-gated, because port
   8080 is reachable from the internet. (§4)
4. **Phase 2 (tap a bar to set the position) is designed in now.** The
   bridge's `locate` takes a position in beats, and phase 1's own
   bar-step and "to start" buttons already use it. Phase 2 adds only a
   `data-bar` attribute on the bar cells and a chart-bar → Live-beat map.
   Neither touches the bridge or the API. (§6)
5. **Sequencing with #13: build alongside, and land #13 first.** The
   bridge (slice M1) shares no files with #13. The serve/panel slice (M2)
   shares only the POST dispatch in `web_server.py`, which is a mechanical
   rebase. (§7)
6. **Five decisions for Les, plus one interaction with
   bert-architecture#14 that he should rule on.** See §9.

---

## 1. Starting point: the app as it is

- **songml-serve** (`web_server.py`) is a `ThreadingHTTPServer` with these
  routes: `GET /` (library index), `GET /song/<rel>` (chart page, rendered
  by `html_exporter.to_html_string`), `GET /midi/<rel>`, and the editing
  endpoints `GET /api/source`, `POST /api/save` and `POST /api/format`. The
  editing endpoints require `SONGML_WRITE_TOKEN` (503 when it's unset, 403
  on a mismatch). The token is embedded in the song page when editing is
  enabled.
- On Stablebeast, songml-serve runs inside the **music-tools Coder
  workspace** container on port 8080. The router forwards that port, so it
  is **internet-reachable** at `coder-stablehome.ddns.net:8080`. It starts
  from music-tools' `.myspaces/init` (the installed clone) or from
  `bin/test-serve.sh` (the dev tree, `uv run`).
- The page that matters is the **song page** from `html_exporter.py`: the
  title row with the MIDI and Edit buttons, then CSS-grid strips of bars.
  Bar numbers are plain `.bar-num` divs. The edit overlay is
  `position:fixed; inset:0; z-index:1000`. `data/viewer.html` is an older
  ABC viewer that calls `/api/files`, which songml-serve doesn't serve, and
  this design leaves it alone.
- **#13** (file management) adds create/delete/rename endpoints and
  index-page UI. It has no PR yet.
- **AbletonOSC** in the mixroom Live is the engine, per the issue. Its
  surface as of upstream HEAD (pushed 2025-11-19): it listens on
  `0.0.0.0:11000`; query replies go to `<sender IP>:11001`; listener pushes
  and `/live/test` go to `127.0.0.1:11001`; messages are processed on
  Live's ~100 ms tick. The calls this design uses:
  `/live/song/{start_playing, continue_playing, stop_playing}`,
  `/live/song/get/{is_playing, current_song_time, tempo,
  signature_numerator, signature_denominator, num_tracks}`,
  `/live/song/set/current_song_time`,
  `/live/song/get/track_data <from> <to> track.name track.mute ...`, and
  `/live/track/set/mute <i> <0|1>`. Track addressing is by index into
  `song.tracks`, which means normal and group tracks in arrangement order,
  without returns or master.

## 2. The relay: `songml-mixbridge`

### 2.1 Where it runs, and why there

```
 piano touchscreen ──HTTP──► songml-serve (music-tools container, :8080)
                                  │  /api/mix/*  (token-gated, proxied)
                                  ▼  TCP, docker bridge gateway
                             songml-mixbridge (WSL on Stablebeast, systemd)
                                  │  UDP over loopback 127.0.0.1
                                  ▼        :11000 ►   ◄ :11001
                             AbletonOSC in mixroom Live (Windows)
```

Options I considered:

| option | verdict |
|---|---|
| **A. UDP straight from songml-serve in the container** | **Rejected.** The query replies go to `<source IP>:11001`, a fixed port, so the container's NAT has no conntrack entry to route them back. Listener pushes and `/live/test` go to 127.0.0.1, which the container never sees. Making it work would need a template change (publish 11001/udp), and even then pushes would be lost. |
| **B. A bridge in WSL on Stablebeast (mirrored networking), supervised by systemd** | **Chosen.** Loopback is shared with Windows, so every AbletonOSC reply arrives, pushes included. TCP from the container to the host is ordinary traffic. systemd already supervises long-running services on this box. |
| C. A bridge as a native Windows Python process | Rejected. Supervision would mean Task Scheduler or a Windows service, there's no dotkit-owned Python on the Windows side, and agents don't run Git Bash over SSH (Les, 2026-11-15). That would make every deploy a Les-at-the-keyboard job. |
| D. A Max for Live device or a custom remote script instead of AbletonOSC | Out of scope, because the issue fixes AbletonOSC as the engine. |

**Which WSL distro is a decision for Les [Les].** Mirrored networking is
a VM-wide setting, so every distro sees the same loopback, and the choice
is about ownership and uptime:

- **hive** (my recommendation): it's the distro that is kept running
  24/7 under systemd today (Jenny's units). Its units live in
  `stablebeast-hive/units` with `units-lint` and the rebuild check. The
  cost is that one of Les's music tools sits in the agents' distro.
- **Ubuntu-24.04 (Les's own login)**: this is his user environment, but
  as far as I know nothing keeps that distro running when he has no
  session open, so the bridge would die with his last terminal.

### 2.2 What the bridge is

- It's a console script in songml-utils (`songml_utils/mixbridge.py`,
  entry point `songml-mixbridge`), so the existing **songml kit** puts it
  on PATH. There's no new kit and no new repo. It's stdlib only: OSC 1.0
  encoding for the handful of types we need (int, float, string, bool) is
  about 60 lines, and there's no `python-osc` dependency to add.
- It owns the UDP socket on **127.0.0.1:11001**, which is AbletonOSC's
  fixed reply port. **Only one process on Stablebeast can bind it**, so
  nothing else may use AbletonOSC replies while the bridge runs. If the
  bind fails, the bridge exits non-zero with a message naming the port and
  the holder's PID. systemd restarts it, and the journal shows why. It
  never falls back to a different port.
- It serves HTTP/JSON on a configured address. The default is the docker
  bridge gateway, on port 8091 **[default]**. It does *not* bind 0.0.0.0,
  so nothing on the LAN or Tailscale can reach it. Its only client is
  songml-serve, and songml-serve does the authentication (§4.4). The
  listen address is a required flag (`--listen 172.17.0.1:8091`); there's
  no built-in default address to guess wrong.

### 2.3 Start and supervision

- It runs as a systemd unit, `songml-mixbridge.service`, with
  `Restart=always` and `RestartSec=2`. ExecStart is
  `/bin/bash -i -c 'exec songml-mixbridge --listen …'`, following the
  dotkit rule for headless callers (`bash -i`, invoked by name, no
  hard-coded `~/.local/bin` path). If the bridge goes in hive, the unit
  goes in `stablebeast-hive/units` and has to pass `units-lint`. If it
  goes in Les's distro, it's a user unit that the songml kit installs
  through a second-stage `songml-mixbridge install-unit` (install logic
  lives in the kit, per DOTKIT_GUIDE, "Where Kits Live").
- It runs always, because it costs almost nothing when idle: it polls Live
  only while it's being watched (§3.1). When Live isn't running or
  AbletonOSC isn't loaded, the bridge stays up and reports
  `live: "unreachable"`, and the panel says so in words.
- **songml-serve learns the bridge URL from a new flag,
  `--mix-url http://172.17.0.1:8091`.** `bin/test-serve.sh` passes it
  through from `SONGML_MIX_URL`, and music-tools' `.myspaces/init` sets it
  for the installed instance. That's a one-line change in Les's repo. With
  the flag absent, mixroom control simply isn't enabled on that server:
  there's no panel, which is the same convention as editing without a
  token. With the flag set but the bridge down, the panel shows a red
  **"mixroom bridge unreachable: <url>: <error>"**. It never fails
  silently.

## 3. Reading state back and keeping it in sync

### 3.1 Polling, not listeners [default]

While watched, meaning any `GET /state` in the last 10 s, the bridge:

- **every 250 ms** sends `get/is_playing`, `get/current_song_time`,
  `get/tempo`, `get/signature_numerator` and `get/signature_denominator`;
- **every 1 s** sends `get/num_tracks`, then
  `get/track_data 0 N track.name track.mute track.is_grouped
  track.is_foldable track.color`, which returns the whole mute array in
  one reply.

Each reply updates a per-address slot with a timestamp. The bridge is the
only client on 11001 and keeps at most one query per address in flight,
so a reply's OSC address is enough to match it to its query. No request
IDs are needed.

**Why not AbletonOSC listeners?** Listeners would give sub-tick latency
for changes Les makes by hand in Live, but they bring a lifecycle the
bridge would have to get right every time. AbletonOSC drops all listeners
on `/live/api/reload` and when the control surface is re-instantiated (a
set load). Listeners a crashed bridge left behind keep firing at 11001.
And there's no listener at all for "tracks were added or removed", so
we'd have to poll for that anyway. Polling handles every one of these
cases the same way. On loopback the cost is about 10 small datagrams a
second, handled on Live's tick, and only while the panel is open. A mute
toggled in Live by hand shows up on the panel within about 1.1 s, which is
fast enough for this job.

### 3.2 Freshness and the "unreachable" state

- `GET /state` returns the latest values together with `age_ms` for each
  group. If no transport reply has arrived for 1.5 s, the bridge reports
  `live: "unreachable"` and the panel greys every control, with the reason
  showing ("mixroom Live not answering: is Live open with AbletonOSC
  selected as a control surface?").
- **Set changes** are detected from the track list: a change in count or
  names bumps `tracks_rev`. The page re-renders the mute grid whenever
  `tracks_rev` changes.

### 3.3 Commands and read-after-write

Every command re-reads what it changed before it answers, and the
response carries the resulting state:

| bridge endpoint | what it sends | guard (refusal → HTTP 409 with reason) | re-read before reply |
|---|---|---|---|
| `POST /play` | `start_playing` **or** `continue_playing`, per spike row 3 | refused if a fresh `is_playing` read is true: **never retrigger while playing** (Les's rule, 2026-10-31) | `is_playing` |
| `POST /stop` | `stop_playing` | refused if already stopped, so the API never issues Live's second-stop "return to start" by accident | `is_playing`, `current_song_time` |
| `POST /locate {beat}` | `set/current_song_time beat` | refused while playing (position is set while stopped); `beat ≥ 0` | `current_song_time` |
| `POST /mute {index, name, mute}` | `track/set/mute index 0/1` | refused if the track at `index` no longer has `name` (the page is stale after a set change) | that track's `mute` |

The re-read waits up to 400 ms, about 3 Live ticks. If it times out, the
command answers `ok: false, error: "no confirmation from Live"`, and the
page keeps showing the last *read* state, not the requested one. The page
never shows a state that it only asked for.

**Link note.** If the mixroom Live has Link Start Stop Sync on, Play and
Stop from this panel propagate to the studio Live exactly as a spacebar
press in the mixroom would. Phase 1 adds no two-Live phase checking: that's
the job of bert-architecture#14's readiness gate (§8).

## 4. The panel in the song page

### 4.1 Layout

```
┌─────────────────── chart (scrolls) ───────────────────┐
│ …bars…                                                │
├───────────────────────────────────────────────────────┤  collapsed strip, 64 px
│ [▶ Play] [■ Stop]   bar 17 . 3   104 BPM 4/4   ● Live  [▴]│
├───────────────────────────────────────────────────────┤  expanded tray (+ ~40% vh max)
│ [|◀ 1.1] [◀ bar] [bar ▶]                               │
│ ┌─────┐┌─────┐┌─────┐┌─────┐┌─────┐┌─────┐┌─────┐      │
│ │Drums││Bass ││Keys ││Gtr  ││Pad  ││Vox  ││Click│ …    │  mute grid (wraps; scrolls if needed)
│ └─────┘└─────┘└─────┘└─────┘└─────┘└─────┘└─────┘      │
└───────────────────────────────────────────────────────┘
```

- The panel is `position:fixed; bottom:0`. The chart's `body` gets a
  bottom padding equal to the panel height, so the last row is never
  hidden. The panel sits below the edit overlay (z-index under 1000), so
  editing covers it.
- **Collapsed** is the slim strip: Play, Stop, the position readout, tempo
  and meter, Live status, and the expand toggle **[default; Les may
  prefer fully hidden, §9]**. Collapsed vs expanded is kept in
  `localStorage`, like the CUA/VIM editor choice from #12.
- **Touch sizing:** at least 56 px for every target, 72 px for Play and
  Stop, and at least 8 px between targets. Labels are at least 16 px.
  Hover-only affordances aren't allowed. Buttons react on `pointerup`
  inside the target, so a finger sliding off cancels the press.
- **Mute tiles** follow Live's own Track Activator meaning: a lit tile is
  a sounding track, and a dark tile with the name struck through is muted.
  The tile's left edge carries the track colour, so it matches what Les
  sees in Live. Group tracks get a small "group" marker. Child tracks
  appear in Live order right after their group. **[default: show every
  track in `song.tracks`, in order; §9]**
- **Position readout** is `bar.beat` from `current_song_time` and the
  meter, with Live bars and beats 1-based. For x/4 the bar length is
  `numerator` quarter notes. For other meters it's `numerator × 4 /
  denominator`, which bert-architecture#14's S1 row 11 confirms when it
  runs. Phase 1 shows **Live** bars. The chart-bar offset is a phase-2
  concern (§6).

### 4.2 Control enablement

| control | enabled when |
|---|---|
| Play | Live reachable and stopped |
| Stop | Live reachable and playing |
| `|◀ 1.1`, `◀ bar`, `bar ▶` | Live reachable and stopped (`◀ bar` is clamped at 1.1) |
| mute tiles | Live reachable (playing or stopped) |

A press disables the control until the command's response comes back. The
response's re-read state is then applied. A refusal (409) shows its reason
in the strip for 4 s.

### 4.3 Page ↔ server traffic [default: polling]

The page polls `GET /api/mix/state` at 4 Hz while expanded and at 1 Hz
while collapsed, and stops while the tab is hidden
(`visibilitychange`). Between polls the position readout doesn't dead
reckon in phase 1: a 250 ms granularity at bar.beat resolution is fine.
I rejected SSE for phase 1 because it means proxying a long-lived stream
through songml-serve's thread-per-request server and a reconnect path for
a 4 Hz number. If phase 2 wants a smooth playhead, SSE (or dead reckoning
from `tempo` + `age_ms`) can be added then without changing the bridge's
API.

### 4.4 songml-serve endpoints (proxy only)

`GET /api/mix/state?token=…`, `POST /api/mix/{play,stop,locate,mute}` with
`token` in the JSON body, which is the same shape and the same
`_check_token` as `/api/save`. Every `/api/mix/*` call is token-gated,
reads included, because port 8080 is internet-reachable and the state
includes the mix set's track names. songml-serve forwards to `--mix-url`
with a 1 s timeout, passes the bridge's JSON and status code through, and
maps connection errors to `502 {"ok":false,"error":"mixroom bridge
unreachable: …"}`. songml-serve holds no mix state of its own.

## 5. Security

- **AbletonOSC listens on 0.0.0.0:11000 with no authentication.** If
  Live.exe has a broad Windows Firewall allow rule, as it usually does
  after the first-run prompt, anyone on the LAN or the tailnet can drive
  the mixroom Live. Since the bridge uses loopback only, **blocking inbound
  UDP 11000 on Stablebeast from everything except loopback costs nothing
  [Les]**. Spike row 5 proves that the rule works and that the bridge
  still does.
- The bridge binds the docker bridge gateway, not 0.0.0.0, so it isn't on
  the LAN either.
- The only internet-facing surface is songml-serve's `/api/mix/*`, gated
  by the existing write token.

## 6. Phase 2: tap a bar to set the position (not built now; not blocked)

Phase 2 needs:

1. `html_exporter` emits `data-bar="<chart bar number>"` on each
  `.bar-num` cell and on the chord cells under it.
2. A chart-bar → Live-beat map: `beat = (chart_bar + LB1 − 2) ×
  bar_len`, where `LB1` is the Live bar on which chart bar 1 falls in the
  mixroom set. That's the `Live-Bar-1:` property from
  bert-architecture#14 §5.2, default 1. `bar_len` comes from the bridge's
  meter.
3. A tap handler that calls the existing `POST /api/mix/locate` and is
  inert while playing (the same guard as phase 1's position buttons).

Phase 1 already provides `locate {beat}`, the stopped-only guard, the
meter in state, and the position readout. Phase 2 changes no bridge code
and no API. The one thing phase 1 must avoid is putting chart-bar
arithmetic into the bridge, and this design keeps the bridge in Live beats
throughout.

## 7. Slices and sequencing

| slice | content | tested by | depends |
|---|---|---|---|
| **M0 spike** (~30 min, mixroom Live open, Les's OK to touch it) | probe script from WSL (`tools/spike/mixosc.py`, deleted after) | rows below | — |
| **M1 bridge** | `mixbridge.py` (OSC codec, poll loop, watched-window, HTTP API, guards, read-after-write), console script, **fake AbletonOSC** (loopback UDP sim of the calls in §1, scripted state) | pytest against the fake: every guard and refusal, stale-index mute, unreachable after 1.5 s, `tracks_rev` bump on set change, confirmation timeout | M0 rows 1–3 |
| **M2 serve + panel** | `--mix-url`, `/api/mix/*` proxy (token-gated), the panel in `html_exporter` (CSS/JS beside `_EDIT_CSS`/`_EDIT_JS`), `test-serve.sh` passes `SONGML_MIX_URL` | pytest: proxy, token, 502 mapping, no panel without `--mix-url`/token; browser against M1 + fake; at the rig: Les mutes and plays from the piano | M1 |
| **M3 deploy** | systemd unit (hive: stablebeast-hive PR; Les's distro: kit `install-unit`), music-tools `.myspaces/init` sets `SONGML_MIX_URL`, firewall rule (§5) | M0 rows 4–5 re-run against the deployed unit | M2, §9 decisions |
| *phase 2* | §6 | — | M2 |

**Spike M0 rows**

| # | test | pass |
|---|---|---|
| 1 | From WSL: `/live/test` → 127.0.0.1:11000 | `"ok"` arrives on 127.0.0.1:11001 in WSL (proves AbletonOSC is loaded and mirrored loopback carries UDP both ways) |
| 2 | `get/track_data 0 N track.name track.mute track.is_grouped track.color`; set mute on one throwaway track and clear it again | names and mutes match Live's UI; the UI follows the set |
| 3 | Stopped. `set/current_song_time` to bar 9, then `start_playing`; stop; repeat with `continue_playing` | which one starts at the located position. The bridge uses that one, and only that one (no try-A-then-B) |
| 4 | From the music-tools container: `curl` a test listener on the docker bridge gateway in the chosen distro | reachable. If the workspace uses a non-default network, record its gateway; that becomes the `--listen`/`--mix-url` address |
| 5 | After the firewall rule (§5): `/live/test` from another LAN host | no reply, while row 1 still passes |

**Sequencing with #13: alongside, with #13 landing first [default].**
M0 and M1 share no files with #13. M2 and #13 both touch
`web_server.py`'s `do_POST` dispatch, and #13 also touches the index page,
which M2 doesn't. If both are in flight, #13 lands first and M2 rebases,
which is a mechanical change to the route table. I'd rather not serialize:
#13 needs no rig time, M0 does, and the two shouldn't wait on each other.
Neither needs the other's features.

## 8. Relation to bert-architecture#14 (two-Live verified start)

#14 (draft, rev 2.1) designs a verified, Link-propagated start across the
studio and mixroom Lives, through a bespoke `SongMLTx` remote script. It
keeps AbletonOSC for spikes only, because AbletonOSC is an unauthenticated
full-control port. This issue fixes AbletonOSC as the mixroom engine, so
the two overlap:

- This design is **single-Live** (mixroom only) and does not implement
  #14's readiness gate. A panel Play starts the mixroom, and Link takes it
  from there, the same as the spacebar.
- §5's loopback-only bridge and firewall rule answer #14's security
  objection to AbletonOSC as far as the mixroom goes.
- **[Les]** whether #14 is parked, or whether its two-Live gate later
  builds on this bridge (adding a studio-side bridge) instead of on
  `SongMLTx`. Phase 1 here doesn't depend on the answer, and I'd park #14
  until phase 1 has been used at the piano.

## 9. Decisions for Les

1. **Bridge host:** hive distro (recommended) or your Ubuntu-24.04 distro.
2. **Firewall:** block inbound UDP 11000 on Stablebeast except loopback
   (recommended: yes).
3. **Collapsed panel:** slim strip with Play/Stop/position (recommended),
   or fully hidden.
4. **Mute grid:** every track in Live order (recommended), or a filtered
   list (say how: names, colour, group).
5. **bert-architecture#14:** park it until phase 1 is in use (recommended),
   or keep it moving.

Engineering choices that stand unless someone objects: polling, not
listeners (§3.1); page polling, not SSE (§4.3); stdlib OSC codec (§2.2);
#13 lands first (§7); touch sizes (§4.1).
