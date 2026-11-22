# Mixroom transport and track mutes in songml-serve: design

*Mark, 2026-11-21, for sanekits/songml#14. Rev 1: wake
20261121-184009-374489. Rev 2: wake 20261121-214011-470852, answering
Les's question on #15 (move songml-serve out of Coder into WSL?). This is a
design document. It contains no code.*

Tags: **[Les]** marks a decision that belongs to Les. Each one is also
listed as a single line in §9. **[default]** marks an engineering choice
I've made; if nobody objects, it stands. **[spike]** marks something only
the rig can answer.

**What changed in rev 2.** Yes, moving songml-serve into WSL makes this
simpler, and rev 2 does it. songml-serve runs in the hive WSL distro
under systemd, as its own unprivileged user. The separate
`songml-mixbridge` process, its HTTP API, the `--mix-url` proxy and the
container→WSL hop are all gone. The mixroom code becomes a module inside
songml-serve that talks to AbletonOSC over loopback. §2 weighs the move:
what gets simpler, what it costs, and the options I rejected. Rev 1 also
had a factual error, which the move removes rather than fixes. It had the
container reaching WSL at the docker bridge gateway `172.17.0.1`, but
Docker on Stablebeast is **Docker Desktop**, whose containers run in
Docker Desktop's own VM. That address is that VM's bridge, not the hive
distro. The real path would have been `host.docker.internal` → Windows →
mirrored networking → WSL, which nobody has tested. Rev 1's spike row 4
would have caught it. Sections 3–6 are rev 1's design with "bridge"
replaced by "the mix layer". Les's "generally sounds good" covers them.

---

## 0. Summary

1. **songml-serve moves from the music-tools Coder container to the hive
   WSL distro on Stablebeast, and the mixroom code lives inside it.**
   AbletonOSC sends replies to `<sender IP>:11001`, and it sends listener
   pushes and `/live/test` to **127.0.0.1:11001 only**. So whatever talks
   to it has to be on Stablebeast's loopback. WSL there runs in
   **mirrored** networking mode, so a WSL process on 127.0.0.1 shares
   Windows' loopback with Live. Once songml-serve itself is on that
   loopback, it can talk UDP to Live directly, and no relay process is
   needed. The touchscreen URL (`coder-stablehome.ddns.net:8080`) stays
   the same. The cost is that an internet-facing server moves into the
   agents' distro, which is why it runs as a dedicated `songml` user that
   can't read hive credentials. (§2)
2. **Sync is by polling, and polling happens only while someone is
   watching.** The mix layer re-reads transport state at 4 Hz and the
   track list plus mute states at 1 Hz, and does both only while a page
   has asked for state in the last 10 s. It uses no AbletonOSC listeners.
   After every command it re-reads the property it changed before it
   answers, so the page shows Live's actual state. (§3)
3. **The UI is a panel fixed to the bottom of the existing song page**,
   with a slim collapsed strip (Play/Stop, position) and an expanded tray
   (position controls, mute grid). Touch targets are at least 56 px. The
   panel shows only when songml-serve has both editing (the write token)
   and `--mixroom` enabled, and every `/api/mix/*` call is token-gated,
   because port 8080 is reachable from the internet. (§4)
4. **Phase 2 (tap a bar to set the position) is designed in now**, and it
   changes neither the mix layer nor the API. (§6)
5. **Sequencing with #13: build alongside, land #13 first.** (§7)
6. **Decisions for Les:** six, listed in §9. Two are new in rev 2: the
   `songml` user, and retiring the 8080 instance in music-tools.

---

## 1. Starting point: the app as it is

- **songml-serve** (`web_server.py`) is a `ThreadingHTTPServer` with these
  routes: `GET /` (library index), `GET /song/<rel>` (chart page, rendered
  by `html_exporter.to_html_string`), `GET /midi/<rel>`, and the editing
  endpoints `GET /api/source`, `POST /api/save` and `POST /api/format`. The
  editing endpoints require `SONGML_WRITE_TOKEN` (503 when it's unset, 403
  on a mismatch). The token is embedded in the song page when editing is
  enabled.
- **Where it runs today.** The production instance runs inside the
  **music-tools Coder workspace** container on port 8080. music-tools'
  `.myspaces/init` starts it with `nohup … &` when the workspace starts:
  it `pip install -e`s the workspace's songml checkout, sources the write
  token from the gitignored `music-tools/.env`, and serves
  `/AbletonLive/Sheets`. That's a read-write bind mount of
  `%USERPROFILE%\Dropbox\AbletonLive`, which only music-tools gets. Docker
  Desktop publishes 8080 on the Windows host, a port-scoped Windows
  Firewall rule lets it in, and the router forwards it, so the server is
  **internet-reachable** at `coder-stablehome.ddns.net:8080`. Nothing
  restarts it if it dies, short of a workspace restart. In practice
  there's **no prod/dev split** (songml `CLAUDE.md`):
  `bin/test-serve.sh bounce` replaces the init-started instance on 8080
  with `uv run` of the dev tree under hupper `--reload`. So a code edit
  in music-tools shows up on the touchscreen on the next page load. 8081
  (`dev_port2`) is also published, for a second disposable instance.
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
  `/live/song/{continue_playing, stop_playing}`,
  `/live/song/get/{is_playing, current_song_time, tempo,
  signature_numerator, signature_denominator, num_tracks}`,
  `/live/song/set/current_song_time`,
  `/live/song/get/track_data <from> <to> track.name track.mute ...`,
  `/live/track/get/{name, mute} <i>` and
  `/live/track/set/mute <i> <0|1>`. Track addressing is by index into
  `song.tracks`, which means normal and group tracks in arrangement order,
  without returns or master.
- **Located while stopped is not where Play starts** (M0 row 3, measured
  on the rig 2026-11-22, #14). While Live is stopped,
  `set/current_song_time` reads back the new value, but `start_playing`
  starts at the start marker and `continue_playing` resumes at the last
  stop position. A set while *playing* moves the playhead. So Play is
  **play-then-locate**: `continue_playing` followed at once by
  `set/current_song_time <the stopped position>`, in one burst (§3.3).
  `start_playing` is not used. The cost is a few milliseconds of audio
  from the last stop position before the jump; if that's audible at the
  rig in M2, M2 mutes the master for the burst.
- **11001 stays bound for the mix layer's whole life** (found in M0 row 1,
  #14). On Windows, a reply AbletonOSC sends to an unheld 11001 comes back
  as ICMP port-unreachable and kills its receive loop
  (`ConnectionResetError: [WinError 10054]` in `abletonosc.log`). The mix
  layer never closes and reopens the socket between watched periods, and
  on shutdown it stops polling and waits out in-flight replies before it
  closes (§2.5).

## 2. Where songml-serve runs, and the mix layer inside it

### 2.1 The shape

```
 piano touchscreen ──HTTP :8080──► Windows host (router forward, firewall)
                                        │  mirrored networking
                                        ▼
                     songml-serve  (hive WSL distro, systemd, user `songml`)
                       ├─ chart pages, editor, /api/save …   (unchanged)
                       └─ mix layer: /api/mix/* + poll loop
                                        │  UDP over loopback 127.0.0.1
                                        ▼        :11000 ►   ◄ :11001
                     AbletonOSC in mixroom Live (Windows)
```

The options I weighed:

| option | verdict |
|---|---|
| **A. UDP straight from songml-serve in the container** | **Rejected** (as in rev 1). Query replies go to `<source IP>:11001`, a fixed port, so the container's NAT has nothing to route them back by, and pushes go to 127.0.0.1, which the container never sees. |
| **B. Serve stays in the container; a `songml-mixbridge` process in WSL; serve proxies `/api/mix/*` to it** (rev 1) | **Rejected in rev 2.** It works, but it's two processes, two HTTP APIs, a proxy with its own error mapping, a `--mix-url` to configure in two places, and a container→WSL hop through Docker Desktop's networking that is unproven (see "What changed in rev 2"). Its one advantage is that the internet-facing server stays in a container. §2.4 gets the same isolation more cheaply. |
| **C. Serve in WSL, bridge still a separate process on 127.0.0.1** | **Rejected.** Once serve is on the loopback, the bridge has nothing left to bridge. Keeping it only keeps the proxy. |
| **D. Serve in WSL with the mix layer in-process** | **Chosen.** See §2.2 for what gets simpler and §2.3 for what it costs. |
| E. Serve as a native Windows Python process | Rejected (rev 1's option C). No supervision without Task Scheduler, no dotkit Python on Windows, and agents don't run Git Bash over SSH (Les, 2026-11-15), so every deploy would need Les at the keyboard. |
| F. Max for Live device or a custom remote script | Out of scope: the issue fixes AbletonOSC as the engine. |

### 2.2 What gets simpler

- **One process instead of two.** There's no `songml-mixbridge` console
  script, no bridge HTTP server, no `--listen` address and no second unit.
  The mix layer (`songml_utils/mixroom.py`) is an OSC codec, a poll thread
  and four command functions. `web_server.py` calls them from its
  `/api/mix/*` routes.
- **No proxy.** `/api/mix/*` answers from the mix layer's state directly.
  Rev 1's proxy timeout, "bridge unreachable" 502 mapping and `--mix-url`
  plumbing through `test-serve.sh` and music-tools' init all go. There's
  only one failure to report: Live not answering (§3.2).
- **No container→WSL hop**, which was the least proven link in rev 1.
- **Supervision.** Today the production server is a `nohup` child of the
  workspace's init and nothing restarts it. Under systemd it gets
  `Restart=always` and a journal.
- **Off-rig development gets easier, not harder.** The M1 fake AbletonOSC
  is a loopback UDP simulator. With the mix layer in-process, the dev
  instance and the fake share whatever loopback they run on, including
  the music-tools container's. So the whole panel can be built and tested
  in the container with no bridge to stand up (§2.3, dev loop).

### 2.3 What it costs

**Dev loop. This is the cost Les will feel most.** Today a code edit in
music-tools is live on the touchscreen at once, because the one 8080
instance is the dev tree under `--reload` (§1). After the move, 8080 runs
the released kit code. To see a change at the piano there are two ways:

- *While iterating:* run `bin/test-serve.sh` in music-tools on **8081**
  (`SONGML_TEST_SERVE_PORT=8081`), against the real Sheets, and open
  `stablebeast:8081` on the touchscreen over the LAN. It has the same
  instant reload as today, but it's on a different port and has no mix
  panel, unless it's pointed at the fake.
- *To make it live on 8080:* commit, `make release`, then
  `setup.sh songml` as `songml` plus `systemctl restart songml-serve`
  (M3 wraps the last two in one command). The
  hardcoded-clone rule in songml `CLAUDE.md` applies unchanged inside the
  `songml` user's home.

Mix work off the rig runs in music-tools with `--mixroom` pointed at the
fake on the container's loopback. Testing against **real Live** has to
happen in WSL, because that's where the loopback is: a WSL clone,
`uv run songml-serve --mixroom …` as `songml`, after
`systemctl stop songml-serve` (11001 has a single owner, §2.5). That's a
handful of sessions during M2, not the everyday loop.

If that split bothers Les more than the bridge does, rev 1 (option B) is
still the answer that keeps today's loop. The rest of this design works
with either. That's decision 1 in §9.

**Deploy.** Today, deploying means restarting the workspace, which
re-runs `pip install -e` on the checkout. After the move it's a songml
kit update in the `songml` user's dotkit, then
`systemctl restart songml-serve`. It's one more explicit step, but it's
the paved path: the unit lives in `stablebeast-hive/units`, passes
`units-lint`, and runs
`/bin/bash -i -c 'exec songml-serve --port 8080 --root … --mixroom'`
(dotkit rule for headless callers: `bash -i`, invoked by name, no
hard-coded `~/.local/bin` path). The songml kit installer has to
guarantee songml-serve's Python dependencies in that environment. If it
doesn't today, that gets fixed in the kit, not in the unit.

**Where the sheet files live.** Nowhere new. They stay in
`%USERPROFILE%\Dropbox\AbletonLive\Sheets` on Windows, which is the same
files Dropbox syncs. The container sees them through Docker Desktop's
bind mount, and WSL sees them through drvfs. Both are 9p mounts of the
same Windows folder, so read latency and Dropbox's view of a save are the
same. One detail: the default `/mnt/c` automount gives write access only
to uid 1000 (the `hive` user), so the `songml` user gets its own drvfs
mount of just the AbletonLive folder in `/etc/fstab`, owned by `songml`,
at `/srv/AbletonLive` **[default]**. The music-tools mount stays as it
is, so dev and Les's own work there are unaffected. Spike row 6 proves a
save from WSL reaches Dropbox.

**How the touchscreen reaches it.** Same URL. With mirrored networking, a
WSL process bound to `0.0.0.0:8080` listens on Stablebeast's own
addresses, so the router forward and the existing port-scoped Windows
Firewall rule for 8080 carry over. Two changes go with it:

- Docker Desktop must stop publishing 8080, or the two collide. That
  means music-tools `dev_port=8080` → `0` in `coder-templates`'
  `workspaces.conf`, reprovisioning music-tools, and dropping the
  songml-serve start from music-tools' `.myspaces/init` (Les's repo). 8081
  stays for the dev instance. **[Les]**, because it retires the
  instance Les uses today.
- WSL's Hyper-V firewall may block inbound connections to WSL even when
  the Windows rule allows the port. If it does, a one-time
  `New-NetFirewallHyperVRule` for TCP 8080 is needed, and that's
  Les-at-the-keyboard work. **[spike]**, row 4.

**Write token.** It moves from `music-tools/.env` to
`~songml/.config/songml/serve.env` (mode 600), read by the unit's
`EnvironmentFile=`. Les copies it there once at M3. The same value works,
so the touchscreen's open pages and bookmarks are unaffected.

**Security.** This is the real cost. Port 8080 is internet-facing, and
the server moves from a throwaway container into the distro where the
hive's agents and their credentials live (gh tokens, pipe keys,
bert-helpers state). §2.4 deals with it.

**Kit install for `songml`.** The songml kit's `setup.sh` clones into a
fixed `~/workarea/songml` and `pip install -e --user`s it, so under the
`songml` user that's `~songml/workarea/songml`, with no kit change. M3
checks that the kit installer brings everything songml-serve needs on a
fresh user. Anything missing gets fixed in the kit.

### 2.4 The `songml` user [Les]

songml-serve runs as a dedicated system user, `songml`, in the hive
distro, with its own home, its own dotkit and the songml kit installed
the normal way, and `User=songml` in the unit. Hive credentials are under
`/home/hive` and other agents' homes, mode 700, so a compromised
songml-serve can't read them. The unit adds `NoNewPrivileges=yes`,
`ProtectSystem=strict`, `ReadWritePaths=/srv/AbletonLive/Sheets` and
`PrivateTmp=yes` **[default]**. Spike row 7 checks the isolation: from a
shell as `songml`, `cat /home/hive/.config/gh/hosts.yml` fails.

Two things it doesn't cover, stated so nobody assumes otherwise. The
default `/mnt/c` automount leaves the whole Windows drive readable by
every WSL user (umask 022). That's no worse than today, since the
container already sees all of `%USERPROFILE%` read-only as `/host_home`.
Tightening the automount would affect every hive tool that reads
`/mnt/c`, so it's out of scope here. And the `songml` user can talk to
AbletonOSC, which is the point.

Options I rejected:

- **Run as `hive`.** Simplest, but then an internet-facing bug reads
  every hive credential.
- **Les's Ubuntu-24.04 distro, as Les.** It's his environment, but his
  credentials are there too, and nothing keeps that distro running when he
  has no session open (rev 1 §2.1).
- **Keep it in a container.** That's option B, which brings back the
  proxy and the unproven hop.

### 2.5 The mix layer

- `songml_utils/mixroom.py`, stdlib only. OSC 1.0 encoding for the few
  types needed (int, float, string, bool) is about 60 lines, so there's no
  `python-osc` dependency.
- It's enabled by `--mixroom` on songml-serve **[default]**. With the flag
  absent, the server neither binds 11001 nor shows the panel, which is
  the same convention as editing without a token. Only the production
  instance sets it. The dev instance sets it only when pointed at the
  fake.
- It owns the UDP socket on **127.0.0.1:11001**, AbletonOSC's fixed reply
  port. **Only one process on Stablebeast can bind it.** If the bind
  fails, songml-serve exits non-zero at startup with a message naming the
  port and the holder's PID. systemd restarts it and the journal shows
  why. It never falls back to another port, and it never starts without
  the mix layer when `--mixroom` was asked for. Once bound, it holds the
  port until the process exits, and drains in-flight replies before it
  closes (§1, WinError 10054).
- When Live isn't running or AbletonOSC isn't loaded, the server stays up
  and reports `live: "unreachable"`, and the panel says so in words.

## 3. Reading state back and keeping it in sync

### 3.1 Polling, not listeners [default]

While watched, meaning any `GET /api/mix/state` in the last 10 s, the mix
layer:

- **every 250 ms** sends `get/is_playing`, `get/current_song_time`,
  `get/tempo`, `get/signature_numerator` and `get/signature_denominator`;
- **every 1 s** sends `get/num_tracks`, then
  `get/track_data 0 N track.name track.mute track.is_grouped
  track.is_foldable track.color`, which returns the whole mute array in
  one reply.

Each reply updates a per-address slot with a timestamp. The mix layer is
the only client on 11001 and keeps at most one query per address in
flight, so a reply's OSC address is enough to match it to its query. No
request IDs are needed.

**Why not AbletonOSC listeners?** Listeners would give sub-tick latency
for changes Les makes by hand in Live, but they bring a lifecycle the mix
layer would have to get right every time. AbletonOSC drops all listeners
on `/live/api/reload` and when the control surface is re-instantiated (a
set load). Listeners a crashed server left behind keep firing at 11001.
And there's no listener at all for "tracks were added or removed", so
we'd have to poll for that anyway. Polling handles every one of these
cases the same way. On loopback the cost is about 10 small datagrams a
second, handled on Live's tick, and only while the panel is open. A mute
toggled in Live by hand shows up on the panel within about 1.1 s, which is
fast enough for this job.

### 3.2 Freshness and the "unreachable" state

- `GET /api/mix/state` returns the latest values together with `age_ms`
  for each group. If no transport reply has arrived for 1.5 s, it reports
  `live: "unreachable"` and the panel greys every control, with the reason
  showing ("mixroom Live not answering: is Live open with AbletonOSC
  selected as a control surface?").
- **Set changes** are detected from the track list: a change in count or
  names bumps `tracks_rev`. The page re-renders the mute grid whenever
  `tracks_rev` changes.

### 3.3 Commands and read-after-write

Every command re-reads what it changed before it answers, and the
response carries the resulting state:

| endpoint | what it sends | guard (refusal → HTTP 409 with reason) | re-read before reply |
|---|---|---|---|
| `POST /api/mix/play` | **play-then-locate** (M0 row 3, §1): reads the stopped `current_song_time` (the position the panel shows), then `continue_playing` + `set/current_song_time <that position>` in one burst | refused if a fresh `is_playing` read is true: **never retrigger while playing** (Les's rule, 2026-10-31) | `is_playing`, and `current_song_time` within the beats Live can have played since the burst (+¼ beat) of the target |
| `POST /api/mix/stop` | `stop_playing` | refused if already stopped, so the API never issues Live's second-stop "return to start" by accident | `is_playing`, `current_song_time` |
| `POST /api/mix/locate {beat}` | `set/current_song_time beat` | refused while playing (position is set while stopped); `beat ≥ 0` | `current_song_time` |
| `POST /api/mix/mute {index, name, mute}` | `track/set/mute index 0/1` | refused if the track at `index` no longer has `name` (the page is stale after a set change) | that track's `mute` |

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
I rejected SSE for phase 1: it means a long-lived stream on
songml-serve's thread-per-request server and a reconnect path, all for a
4 Hz number. If phase 2 wants a smooth playhead, SSE (or dead reckoning
from `tempo` + `age_ms`) can be added then without changing the mix
layer.

### 4.4 songml-serve endpoints

`GET /api/mix/state?token=…`, `POST /api/mix/{play,stop,locate,mute}` with
`token` in the JSON body, which is the same shape and the same
`_check_token` as `/api/save`. Every `/api/mix/*` call is token-gated,
reads included, because port 8080 is internet-reachable and the state
includes the mix set's track names. Without `--mixroom` the routes answer
404 and the page has no panel.

## 5. Security

- **AbletonOSC listens on 0.0.0.0:11000 with no authentication.** If
  Live.exe has a broad Windows Firewall allow rule, as it usually does
  after the first-run prompt, anyone on the LAN or the tailnet can drive
  the mixroom Live. Since songml-serve uses loopback only, **blocking
  inbound UDP 11000 on Stablebeast from everything except loopback costs
  nothing [Les]**. Spike row 5 proves that the rule works and that
  loopback still does.
- The only internet-facing surface is songml-serve, as today. Its
  `/api/mix/*` routes are gated by the existing write token, and the
  process runs as the `songml` user (§2.4).

## 6. Phase 2: tap a bar to set the position (not built now; not blocked)

Phase 2 needs:

1. `html_exporter` emits `data-bar="<chart bar number>"` on each
  `.bar-num` cell and on the chord cells under it.
2. A chart-bar → Live-beat map: `beat = (chart_bar + LB1 − 2) ×
  bar_len`, where `LB1` is the Live bar on which chart bar 1 falls in the
  mixroom set. That's the `Live-Bar-1:` property from
  bert-architecture#14 §5.2, default 1. `bar_len` comes from the meter in
  state.
3. A tap handler that calls the existing `POST /api/mix/locate` and is
  inert while playing (the same guard as phase 1's position buttons).

Phase 1 already provides `locate {beat}`, the stopped-only guard, the
meter in state, and the position readout. Phase 2 changes no mix-layer
code and no API. The one thing phase 1 must avoid is putting chart-bar
arithmetic into the mix layer, and this design keeps it in Live beats
throughout.

## 7. Slices and sequencing

| slice | content | tested by | depends |
|---|---|---|---|
| **M0 spike** (~45 min, mixroom Live open, Les's OK to touch it) | probe script from WSL (`tools/spike/mixosc.py`, deleted after) plus a throwaway listener on 8080 | rows below | — |
| **M1 mix layer** | `mixroom.py` (OSC codec, poll loop, watched-window, guards, read-after-write) and a **fake AbletonOSC** (loopback UDP sim of the calls in §1, scripted state) | pytest against the fake: every guard and refusal, stale-index mute, unreachable after 1.5 s, `tracks_rev` bump on set change, confirmation timeout, loud exit when 11001 is taken. The fake reproduces both M0 findings: a stopped locate is ignored by `start_playing`/`continue_playing`, and a reply to an unheld 11001 kills its receive loop | M0 rows 1–3 (done 2026-11-22, #14) |
| **M2 serve + panel** | `--mixroom`, the `/api/mix/*` routes (token-gated), the panel in `html_exporter` (CSS/JS beside `_EDIT_CSS`/`_EDIT_JS`) | pytest: routes, token, 404 without `--mixroom`, no panel without token; browser in music-tools against the fake; at the rig (WSL, §2.3): Les mutes and plays from the piano | M1 |
| **M3 move + deploy** | `songml` user with dotkit + songml kit; fstab drvfs mount (§2.3); unit in `stablebeast-hive/units`; token file; Hyper-V rule if row 4 needs it; firewall rule (§5); `coder-templates` music-tools `dev_port=0` + reprovision; music-tools init stops starting serve | M0 rows 4–7 re-run against the deployed unit; the touchscreen opens a chart and saves an edit | M2, §9 decisions |
| *phase 2* | §6 | — | M2 |

M3's pieces land in four repos (songml kit in `local.bin` if it needs
changes, `stablebeast-hive`, `coder-templates`, `music-tools`). The
cut-over order is: unit installed but stopped → music-tools stops
publishing 8080 → unit started → touchscreen check. The touchscreen is
down for about as long as the reprovision takes.

**Spike M0 rows**

| # | test | pass |
|---|---|---|
| 1 | From WSL: `/live/test` → 127.0.0.1:11000 | `"ok"` arrives on 127.0.0.1:11001 in WSL (AbletonOSC is loaded and mirrored loopback carries UDP both ways) |
| 2 | `get/track_data 0 N track.name track.mute track.is_grouped track.color`; set mute on one throwaway track and clear it again | names and mutes match Live's UI; the UI follows the set |
| 3 | Stopped. `set/current_song_time` to bar 9, then `start_playing`; stop; repeat with `continue_playing` | which one starts at the located position. The mix layer uses that one, and only that one (no try-A-then-B). **Result (2026-11-22): neither does**; a locate sticks only while playing, so Play is play-then-locate (§1, §3.3) |
| 4 | music-tools stopped (or a spare port, e.g. 8092, if Les won't stop it yet): a throwaway listener in WSL on `0.0.0.0:<port>`; fetch it from the touchscreen, from a LAN host and from outside via the ddns name | reachable all three ways. If not, record whether the Hyper-V firewall is what blocks it; that sets M3's Les step |
| 5 | After the UDP 11000 rule (§5): `/live/test` from another LAN host | no reply, while row 1 still passes |
| 6 | As `songml`, through the fstab mount: write a scratch `.songml` file under Sheets, then delete it | it appears in Windows Explorer and Dropbox syncs it |
| 7 | As `songml`: read `/home/hive/.config/gh/hosts.yml` | permission denied |

**Sequencing with #13: alongside, with #13 landing first [default].**
M0 and M1 share no files with #13. M2 and #13 both touch
`web_server.py`'s `do_POST` dispatch, and #13 also touches the index page,
which M2 doesn't. If both are in flight, #13 lands first and M2 rebases,
which is a mechanical change to the route table. #13 needs no rig time and
M0 does, so the two shouldn't wait on each other. The move (M3) doesn't
affect #13: #13 serves the same Sheets folder from wherever the server
runs.

## 8. Relation to bert-architecture#14 (two-Live verified start)

#14 (draft, rev 2.1) designs a verified, Link-propagated start across the
studio and mixroom Lives, through a bespoke `SongMLTx` remote script. It
keeps AbletonOSC for spikes only, because AbletonOSC is an unauthenticated
full-control port. This issue fixes AbletonOSC as the mixroom engine, so
the two overlap:

- This design is **single-Live** (mixroom only) and does not implement
  #14's readiness gate. A panel Play starts the mixroom, and Link takes it
  from there, the same as the spacebar.
- §5's loopback-only use and firewall rule answer #14's security
  objection to AbletonOSC as far as the mixroom goes.
- **[Les]** whether #14 is parked, or whether its two-Live gate later
  builds on this mix layer instead of on `SongMLTx`. Phase 1 here doesn't
  depend on the answer, and I'd park #14 until phase 1 has been used at
  the piano.

## 9. Decisions for Les

1. **Where songml-serve runs:** the hive WSL distro, with the mix layer
   in-process (recommended, §2), or rev 1's container plus bridge. The
   trade: one process, no proxy and no unproven Docker hop, against
   losing "edit in music-tools, live on 8080 at once" (it moves to 8081
   on the LAN; 8080 runs releases; §2.3).
2. **The `songml` user** in the hive distro, with its own dotkit
   (recommended, §2.4), or run it as `hive`.
3. **Retire the 8080 instance in music-tools** at M3 cut-over:
   `dev_port=0`, init stops starting serve, and 8081 stays for dev
   (recommended; it follows from 1).
4. **Firewall:** block inbound UDP 11000 on Stablebeast except loopback
   (recommended: yes).
5. **Panel and grid:** slim collapsed strip (recommended) or fully hidden;
   every track in Live order (recommended) or a filter you describe.
6. **bert-architecture#14:** park it until phase 1 is in use
   (recommended), or keep it moving.

Engineering choices that stand unless someone objects: polling, not
listeners (§3.1); page polling, not SSE (§4.3); stdlib OSC codec (§2.5);
`--mixroom` flag and loud exit on a taken 11001 (§2.5); `/srv/AbletonLive`
mount and unit hardening (§2.3, §2.4); #13 lands first (§7); touch sizes
(§4.1).
