# Transport-sheet sync in songml-serve: design

*Mark, 2026-11-27, for sanekits/songml#14 (bert-threads task
k-20261127-songml-sync-design, wake 20261127-204013-3378397). This is a
design document. It contains no code. It builds on
`docs/mixroom-transport-design.md` (#15, "the transport design"), whose
mix layer, API and panel are deployed (M3, 2026-11-23).*

Tags, as in the transport design: **[Les]** marks a decision that belongs
to Les, each also listed as one line in §8. **[default]** marks an
engineering choice that stands unless someone objects. **[rig]** marks
something only the mixroom can confirm.

---

## 0. Summary

Les asked for three features (2026-11-27):

1. **Playhead.** As the transport crosses each bar, the sheet marks the
   current bar.
2. **Auto-scroll.** When the transport reaches the second-to-last visible
   row of the sheet, the page scrolls up by one row.
3. **Tap a bar.** Tapping a bar in the sheet moves the transport there
   (phase 2 of the transport design, §6 there).

The design in one paragraph:

- **No change to the mix layer or the API.** The page already polls
  `GET /api/mix/state` and already gets `beat`, `tempo`, `signature`,
  `playing` and `transport_age_ms`. Tap-a-bar uses the existing
  `POST /api/mix/locate`. All three features are page-side: new markup
  from `html_exporter` and new JS beside `_MIX_JS`.
- **The bar ↔ beat map is computed in Python and emitted as data.** Each
  `.bar-num` cell gets `data-bar` and `data-beat` (the Live beat where
  that bar starts). The JS only looks numbers up, and the arithmetic is
  pytest-covered. Chart bar *n* is Live bar *n* by default, which is
  how Les's charts are already numbered (the 1-bar pad is bar 1). An
  optional `Live-Bar-1:` property shifts it.
- **Smooth playhead from 4 Hz polls by dead reckoning.** Between polls,
  the page advances the position from `tempo`. Each poll re-anchors it.
  The page polls at 4 Hz whenever Live is playing, collapsed or not.
- **Auto-scroll follows only while playing, and a manual scroll pauses
  it.** It resumes when Les presses Play, taps the strip's Follow
  button, or the playhead catches up with what he scrolled to.
- **Tap a bar works only while stopped**, the same guard the position
  buttons already have. **[Les]**
- **Build after #13 (#19) lands**, in three slices, each shippable on its
  own (§7).

---

## 1. Starting point

What the app has today that this design uses:

- **The song page** (`html_exporter.to_html_string`) lays the chart out as
  `.strip` rows of up to `bars_per_row` (8) bars. A strip holds up to four
  CSS-grid rows: section labels, bar numbers (`.bar-num`), chords, and
  lyrics if there are any. A bar is not a DOM element. Its cells sit in
  those grid rows at the same columns. A bar that the parser synthesized
  has no chord cell at all. `same-row` sections share a strip. Strip
  heights vary (a strip with lyrics or a section label is taller).
- **Bar numbers** are the chart's own labels. The parser checks that they
  continue across sections (`_validate_bar_continuity`), so they are a
  single run *n₀, n₀+1, …*. Les's eight charts in
  `Dropbox\AbletonLive\Sheets` all start with a 1-bar pad section
  numbered 1 (`[Pad - 1 bars]`, `[Pre-pad - 1 bar]`), then the intro at
  bar 2. Those are the same numbers as Live's bars in the backing sets:
  the pad is the count-in bar. (The MIDI exporter places bars by ordinal
  position, not label, so for these charts the two agree too.)
- **Meter.** `html_exporter` reads one `Time:` property (default 4/4).
  The parser allows `Time:` to change between sections, but the renderer
  doesn't. `pigeon-song` is 6/4; the rest are 4/4.
- **The mix panel** (transport design §4, as built in M2): a fixed bottom
  strip with Play, Stop, `|◀ 1.1`, `◀ bar`, `bar ▶`, position readout and
  Live status, plus a tray with the mute grid. It polls state at 4 Hz
  while the tray is expanded and 1 Hz while collapsed, and stops while
  the tab is hidden. It pads the body so the last chart row clears the
  panel. Commands set `busy` until their response arrives, and the page
  applies only the re-read state in the response.
- **State** from `GET /api/mix/state`: `live`, `playing`, `beat` (Live's
  `current_song_time`, quarter notes from 0), `tempo`, `signature`
  `[num, den]`, `transport_age_ms`, the tracks, and `tracks_rev`. The mix
  layer re-reads transport every 250 ms while watched.
- **Locate** (`POST /api/mix/locate {beat}`) is refused while playing,
  and Play is play-then-locate to the stopped position. So **while
  stopped, the stopped position is exactly where Play will start**. The
  playhead uses this in §3.3.
- **The edit overlay** is `position:fixed; inset:0`, covers the chart and
  the panel, and reloads the page after a save that changed anything.

## 2. The bar ↔ beat map

### 2.1 What the map is

For chart bar *n*, the Live beat where it starts is

```
beat(n) = (n + LB1 − 2) × bar_len
bar_len = num × 4 / den          (chart Time:, in quarter notes)
```

`LB1` is the Live bar on which chart bar 1 falls. This is the transport
design's §6 formula, unchanged. With the default `LB1 = 1`, chart bar *n*
starts at Live bar *n*: in-the-end's bar 17 is Live bar 17, at beat 64.

**`Live-Bar-1:` property [Les].** A chart whose numbering doesn't match
its Live set sets, for example, `Live-Bar-1: 5` (chart bar 1 is Live bar
5). The name is the one bert-architecture#14 §5.2 proposed for the same
job. When the property is absent, `LB1` is 1, which is correct for every
chart Les has today. It's a documented property default, like `Time:`
defaulting to 4/4, so it doesn't count as a masking fallback. A value
that isn't a positive integer is **not** replaced by 1: the page turns
sync off and says why (§2.3).

I considered and rejected two alternatives:

- **Map by bar ordinal** (the first rendered bar is Live bar 1, like the
  MIDI exporter). It gives the same answer on Les's charts. But it
  diverges from the label as soon as a chart starts at bar 0 for a
  pickup, and Les reads labels, not ordinals. The playhead and the
  readout should agree with the numbers printed on the sheet.
- **Require `Live-Bar-1:` in every chart.** That means eight edits for a
  value that is 1 in all of them.

### 2.2 Where it's computed [default]

In Python, in `html_exporter`. Every `.bar-num` cell gets
`data-bar="<n>" data-beat="<beat(n)>"`. The page gets one small JSON blob
beside the mix script:

```
SYNC = { "meter": [num, den], "lb1": 1, "off": null }
```

`off` is a reason string when sync can't work for this chart (§2.3).

The JS never does chart arithmetic. It reads the `data-beat` values once
into a sorted array. "Which bar holds this beat" is a binary search,
and "where does bar *n* start" is a lookup. This keeps the formula in one
place that pytest covers. It also keeps the mix layer in Live beats, as
the transport design §6 requires.

### 2.3 When sync is off

Sync (all three features) is off for a chart, with the reason shown in
the strip's message area, when:

| condition | detected by | reason shown |
|---|---|---|
| the chart has more than one distinct `Time:` value | `html_exporter` | `chart changes meter: bar sync off` |
| `Live-Bar-1:` present but not a positive integer | `html_exporter` | `Live-Bar-1: "<value>" is not a bar number: bar sync off` |
| Live's `signature` ≠ the chart's `Time:` | page, on every state | `chart is 6/4, Live is 4/4: bar sync off` |
| Live unreachable | page (existing `live` state) | the existing "Live not answering" message |

The transport and mutes keep working when sync is off. Only the
playhead, auto-scroll and tapping are disabled. A meter that changes
*inside the Live set* can't be seen from AbletonOSC's song-level
`signature_*` without a spike, so it's out of scope. Les's sets are
single-meter, the same as his charts.

**Wrong set loaded [default].** Nothing in AbletonOSC names the open
set, so the page can't tell that the mixroom has a different song loaded
than the chart on screen. If the chart's `Tempo:` and Live's `tempo`
differ by more than 0.5 BPM, the strip shows a quiet `tempo differs from
chart` hint. It's only a hint and it doesn't disable anything. Tempo
automation or a deliberate tempo change would trip it too.

## 3. Playhead

### 3.1 Position source and cadence [default]

- **Poll rate.** While `playing` is true, the page polls at **4 Hz
  whether the tray is expanded or collapsed** (today, collapsed is 1 Hz).
  Stopped and collapsed stays at 1 Hz. Hidden tab still stops polling.
  The mix layer's own transport poll is already 250 ms while watched,
  so nothing changes server-side.
- **Dead reckoning.** Each state is an anchor: beat `b₀` at local time
  `t₀ = (time the response arrived) − transport_age_ms`. While playing,
  the page's estimate is `b₀ + (now − t₀) × tempo / 60`, updated on
  `requestAnimationFrame`. It never extrapolates more than 1 s past `t₀`.
  After that it holds still. From 1.5 s on, the existing `unreachable`
  state takes over and hides the playhead (§3.3).
- **Re-anchoring.** A new anchor within ±1 beat of the estimate is just
  adopted. A new anchor more than 1 beat away is a **jump**: a locate
  done in Live, a loop wrap, or a stop and restart. It's adopted
  immediately and flagged to auto-scroll (§4.3). To avoid flicker at a
  bar line, the highlight moves back a bar only when the new anchor is
  more than ¼ beat before that bar's start.
- **During commands.** A poll that *started* while a command was in
  flight (`busy`) isn't used for the playhead. The Play burst makes
  Live's position flick to the last stop position and back (transport
  design §1), and a poll catching that flick would make the highlight
  and the scroll jump. The command's own response, which carries the
  confirmed state, re-anchors instead.

Expected lag, highlight behind Live's actual bar line: dead reckoning
removes the poll interval, leaving Live's ~100 ms tick and LAN latency,
corrected at each anchor. Target: the highlight changes within 150 ms of
the bar line, by eye at the piano. **[rig]**

I rejected two alternatives:

- **SSE.** Same reason as the transport design §4.3. It needs a
  long-lived stream on a thread-per-request server and a reconnect path.
  With dead reckoning, 4 Hz polls already give a smooth playhead.
- **Polls alone, no dead reckoning.** Up to about 600 ms of lag (Live
  tick + mix-layer poll + page poll). At 120 BPM that's more than a beat,
  so the highlight would visibly trail the downbeat.

### 3.2 What it looks like [Les: style]

- **One overlay box**, not classes on cells. It's a single
  absolutely-positioned element with `pointer-events:none`, above the
  chart and below the panel. Its x and width come from the current bar's
  `.bar-num` cell, and its y and height from that cell's `.strip`. So it
  covers the bar's number, chords and lyrics, including synthesized bars
  that have no chord cell, and no chart cell reflows when the bar
  changes. It's repositioned on bar change, on `resize`, and when the
  panel's height changes.
- **Playing:** a translucent fill (about 25 % of an accent colour that
  isn't a section colour), so the chord text stays readable through it.
  **Stopped:** the same box as a 3 px outline with no fill, meaning
  "Play starts here" (§3.3).
- **Readout.** The strip's position readout gets the chart's view after
  the Live one: `bar 17 . 3 · Verse 2` (section name from the strip's
  segment). When the chart bar and the Live bar differ (`LB1 ≠ 1`), it
  shows both: `Live 21 . 3 · chart 17 · Verse 2`.

### 3.3 Stopped, outside the chart, unreachable

| state | playhead |
|---|---|
| playing, beat inside the chart | fill on the current bar |
| stopped, beat inside the chart | outline on the bar holding the stopped position. Because Play is play-then-locate, that's where Play will start |
| beat before the chart's first bar or after its last | no box. The readout adds `(before chart)` or `(past end)` |
| sync off (§2.3) | no box |
| Live unreachable | no box. The existing greyed panel says why |
| edit overlay open | not drawn, and polling for it pauses. The page reloads after a save anyway |

## 4. Auto-scroll

### 4.1 The rule

Les's rule, made exact:

- A **row** is a `.strip`.
- The **visible region** is the viewport above the panel, from the
  window's top to `innerHeight − panel.offsetHeight`. It changes when
  the tray expands, and it's recomputed every time.
- The **last visible row** is the last strip whose bottom is inside the
  visible region (fully visible).
- **Trigger:** when the playhead enters a strip whose index is at least
  (last visible row − 1), the second-to-last visible row, the page
  scrolls so that the strip after the current *first* visible strip is
  at the top of the visible region. That is one row, whatever that row's
  height. If the first strip is only partly visible (Les scrolled to
  mid-strip), the next strip is what goes to the top.
- When only one or two rows fit, the trigger is the first row. The
  current row then moves to the top as soon as it's reached.
- At the end of the document, the browser clamps the scroll. Nothing
  special happens.
- Scrolling is `behavior: 'smooth'` **[default]**. A one-row move takes
  about 200 ms, so the eye can follow it.

The trigger is checked only on a **bar change** of the playhead, not
every frame, so a scroll happens at most once per bar line.

### 4.2 When it follows

Auto-scroll is active only **while playing**, sync is on, the edit
overlay is closed, and it isn't paused. Stopped, the page never scrolls
on its own, except as in §5.3.

**Manual scroll pauses it [Les].** At the piano, Les may swipe ahead to
read the next section, or back to check something. Snapping back would
fight him. A user scroll pauses follow, and the strip's `Follow` button
lights up. User scrolls are detected by `wheel`, `touchmove` and the
navigation keys (PgUp/PgDn, arrows, Home/End, Space), not by `scroll`
events, which the page's own scrolling also fires. A desktop scrollbar
drag isn't detected. That doesn't matter on the touchscreen.

Follow resumes when any of these happens:

1. Les taps `Follow` in the strip. The page brings the playhead row into
   view (§4.3).
2. Les presses Play.
3. The playhead reaches the trigger row *in the view Les scrolled to*.
   He scrolled ahead, and the music has caught up.

I rejected one alternative: **snap back after N seconds idle.** It
decides for Les when he's done reading, and at the piano his hands are
busy, so "idle" doesn't mean done.

### 4.3 Jumps and re-entry

When follow is active and the playhead's row isn't in the visible
region, the page places that row **second from the top**, so the row
before it shows as context, or at the top if it's the first row. That
covers:

- a jump (§3.1): a locate done in Live while playing, or a loop wrap;
- resuming follow (§4.2, cases 1 and 2) with the playhead off-screen;
- a page load or reload while Live is playing;
- the tray expanding over the playhead's row.

## 5. Tap a bar

### 5.1 Behaviour

- **Stopped only [Les].** A tap while playing does nothing to the
  transport. The strip flashes `stop first to move the playhead` for
  2 s. This matches the position buttons and `/api/mix/locate`'s
  stopped-only guard. It also means a stray touch during a take can't
  move the backing track. The alternative, a jump while playing, needs
  an API change (locate is refused while playing), and a jump played
  through the speakers is exactly what the Play burst's mutes exist to
  hide. I'd add it only if Les asks for it after using stopped-only.
- **Target:** the start of the tapped bar, `data-beat` of its `.bar-num`
  cell, through `POST /api/mix/locate {beat}`. Within-bar beat taps
  aren't offered: at chart scale a bar is roughly a fingertip wide.
- **Hit area:** the whole bar column of the strip, meaning label row,
  number, chords and lyrics. The page finds the tapped strip, then the
  `.bar-num` cell whose horizontal extent contains the tap's x. A
  synthesized bar with no chord cell is still tappable. A tap on a
  section label selects the bar under it.
- **Tap vs scroll:** the handler listens for `click`, which the browser
  fires only for a tap without a drag, so scrolling the chart by touch
  doesn't locate. That's the transport design §4.1 rule (act on
  release inside the target).
- **Feedback:** the tapped bar gets a pending outline at once. When the
  locate's re-read state comes back, the normal stopped outline (§3.2)
  lands on the bar Live actually reports. A refusal or `no confirmation
  from Live` clears the pending outline and shows the reason, as the
  panel's buttons do. The page never shows a position it only asked for.
- **Disabled** (taps ignored and the cursor isn't a pointer) when the
  panel isn't shown (no token or no `--mixroom`), sync is off, Live is
  unreachable, a command is in flight, the edit overlay is open, or the
  bar's `data-beat` is negative (a bar before Live's bar 1 under
  `Live-Bar-1:`).

### 5.2 Touch targets

The transport design requires at least 56 px targets. A bar column at
8 bars per row on the piano touchscreen is wider than that, and a strip
is taller. So a bar meets the rule without changes. The rig session for
slice T3 confirms it on the actual panel. **[rig]**

### 5.3 Scrolling after a locate

A locate from a tap needs no scroll: the bar is already in view. A
locate from the panel's `◀ bar` / `bar ▶` / `|◀ 1.1` buttons that moves
the stopped playhead out of the visible region brings its row into view
using the §4.3 placement. That's the one case where the page scrolls
while stopped, and it's caused by Les's own press.

## 6. What changes where

| file | change |
|---|---|
| `html_exporter.py` | `data-bar` and `data-beat` on `.bar-num` cells. The `SYNC` blob. `Live-Bar-1:` and multi-`Time:` checks. Overlay element and its CSS beside `_MIX_CSS`. All of it only when the mix panel is shown |
| `html_exporter.py` (`_MIX_JS`) | 4 Hz poll while playing; anchor and dead reckoning; playhead overlay; readout; auto-scroll with pause/Follow; tap handler |
| `docs/songml-syntax.md` | `Live-Bar-1:` under common properties |
| `mixroom.py`, `web_server.py`, `/api/mix/*` | **no change** |

Without `--mixroom` or without the write token, the page is byte-for-byte
what it is today: no `data-beat`, no blob, no overlay. That keeps the
read-only page unchanged and avoids leaking anything into it.

## 7. Slices and sequencing

| slice | content | tested by | depends |
|---|---|---|---|
| **T1 map + playhead** | §2, §3: `data-bar`/`data-beat`, `SYNC`, the §2.3 checks, dead reckoning, overlay, readout, 4 Hz-while-playing, poll-during-busy rule | pytest: `data-beat` for default and explicit `Live-Bar-1:`, 6/4 bar length, multi-`Time:` and bad `Live-Bar-1:` set `off`, no sync markup without the panel. Browser in music-tools against the fake AbletonOSC (its position advances with tempo): highlight follows play, outline when stopped, no box past the end, meter mismatch shows the reason. At the rig: highlight on the downbeat by eye | #19 landed |
| **T2 auto-scroll** | §4: trigger, one-row scroll, pause on manual scroll, Follow, jump placement | browser against the fake, with a long chart and a short viewport: scroll at the second-to-last row, pause on swipe, resume via Follow, Play and catch-up, placement after a fake-side locate. At the rig: a full song at the piano | T1 |
| **T3 tap a bar** | §5, §5.3 | pytest: unchanged (no API change). Browser against the fake: tap stopped → outline moves after the re-read; tap while playing flashes and doesn't locate; drag doesn't locate; negative `data-beat` not tappable. At the rig: tap bar 17, Play starts at bar 17 | T1 |

T2 and T3 are independent once T1 is in, so either can go first. Each
slice is one PR and goes live through the M3 deploy path (release, then
`setup.sh songml` as `songml`, then restart the unit).

**Sequencing with #13 (file management, PR #19): after it.** Les's
priority order puts file maintenance first. The two aren't dependent (the
task's dependency edge was removed 2026-11-27), but #19 also edits
`html_exporter.py`, so T1 starts from main after #19 lands rather than
rebasing over it. This design PR itself doesn't touch code and can be
reviewed now.

## 8. Decisions for Les

1. **Tap a bar:** stopped only (recommended, §5.1), or also while
   playing (needs an API change).
2. **Auto-scroll and manual scroll:** pause on your swipe and resume on
   Follow, Play or catch-up (recommended, §4.2), or always snap back.
3. **Bar mapping:** chart bar *n* = Live bar *n*, with an optional
   `Live-Bar-1:` for charts that differ (recommended, §2.1), or require
   the property in every chart.
4. **Playhead look:** fill while playing, outline while stopped
   (recommended, §3.2), or a different style you describe.
5. **Stopped playhead:** show it as "Play starts here" (recommended,
   §3.3), or hide it while stopped.

Engineering choices that stand unless someone objects: no mix-layer or
API change (§6); map computed in Python and emitted as `data-beat`
(§2.2); dead reckoning on 4 Hz polls, no SSE (§3.1); polls during a
command ignored for the playhead (§3.1); overlay box, not cell classes
(§3.2); tempo-differs hint (§2.3); smooth one-row scroll (§4.1); after
#19 (§7).
