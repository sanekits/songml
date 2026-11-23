"""HTML chord-chart export from SongML AST."""

from __future__ import annotations

__all__ = ["to_html_string"]

import html as _html
import json as _json
from typing import NamedTuple

from .ast import Bar, Document, Property, Section


class RowSegment(NamedTuple):
    """One section's bars occupying a slice of a rendered row."""

    section: Section
    bars: list[Bar]
    color: str
    show_label: bool  # False for a non-first chunk of a section wrapped across rows


SECTION_COLORS = [
    "#fffde7",  # soft yellow
    "#e8f5e9",  # soft green
    "#e3f2fd",  # soft blue
    "#fce4ec",  # soft pink
    "#fff3e0",  # soft orange
    "#f3e5f5",  # soft purple
]

_CSS = """\
*,*::before,*::after{box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;background:#e8e8e8;margin:0;padding:1rem;color:#111}
.song{max-width:1500px;margin:0 auto}
.back-link{display:block;font-size:.85rem;margin-bottom:.6rem}
h1{font-size:1.8rem;margin:0 0 .2rem}
.title-row{display:flex;align-items:baseline;gap:.75rem;flex-wrap:wrap;margin-bottom:.2rem}
.midi-btn{
  padding:.25rem .75rem;background:#1a73e8;color:#fff;border-radius:4px;
  font-size:.8rem;font-weight:600;text-decoration:none;white-space:nowrap
}
.midi-btn:hover{background:#1557b0;text-decoration:none;color:#fff}
.edit-btn{
  padding:.25rem .75rem;background:#0b8043;color:#fff;border:none;border-radius:4px;
  font-size:.8rem;font-weight:600;white-space:nowrap;cursor:pointer
}
.edit-btn:hover{background:#076b37}
#songml-edit-overlay{position:fixed;inset:0;z-index:1000;background:#fff;display:flex;flex-direction:column}
.songml-edit-bar{display:flex;align-items:center;gap:.75rem;padding:.5rem .75rem;background:#f1f3f4;border-bottom:1px solid #dadce0}
.songml-edit-title{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.songml-edit-warn{color:#b06000;font-size:.85rem;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.songml-edit-bar button{padding:.35rem .9rem;font-size:.9rem;cursor:pointer}
.songml-edit-body{flex:1;min-height:0;overflow:auto}
.meta{color:#555;font-size:.9rem;margin-bottom:1.25rem}
.strip{margin-bottom:.6rem;border:1px solid #bbb;border-radius:4px;overflow:hidden}
.section-label{
  font-size:.72rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;
  padding:2px 8px;background:rgba(0,0,0,.13);color:#333
}
.grid-row{display:grid}
.bar-num{
  font-size:.68rem;color:#7b1fa2;font-weight:700;padding:1px 4px;white-space:nowrap;
  background:rgba(0,0,0,.07);border-left:2px solid rgba(0,0,0,.22)
}
.bar-num:first-child{border-left:none}
.bar-elapsed{font-style:italic;font-weight:400;color:#555}
.chords-row{min-height:2.5rem}
.chord{
  font-size:.95rem;font-weight:700;padding:3px 4px;
  border-left:1px solid rgba(0,0,0,.1);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  display:flex;align-items:center
}
.chord.bar-start{border-left:2px solid rgba(0,0,0,.28)}
.chord.dense{font-size:.72rem;white-space:normal;overflow-wrap:anywhere;text-overflow:clip}
.lyrics-row{}
.lyric{
  font-size:.8rem;font-style:italic;color:#333;
  padding:1px 4px 3px;
  border-left:2px solid rgba(0,0,0,.15);
  border-top:1px solid rgba(0,0,0,.1);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis
}
.lyric:first-child{border-left:none}
a{color:#1a73e8;text-decoration:none}
a:hover{text-decoration:underline}
"""


_EDIT_CSS = """
.edit-btn{
  padding:.25rem .75rem;background:#0b8043;color:#fff;border:none;border-radius:4px;
  font-size:.8rem;font-weight:600;white-space:nowrap;cursor:pointer
}
.edit-btn:hover{background:#076b37}
#songml-edit-overlay{position:fixed;inset:0;z-index:1000;background:#fff;display:flex;flex-direction:column}
.songml-edit-bar{display:flex;align-items:center;gap:.75rem;padding:.5rem .75rem;background:#f1f3f4;border-bottom:1px solid #dadce0}
.songml-edit-title{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.songml-edit-warn{color:#b06000;font-size:.85rem;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.songml-edit-bar button{padding:.35rem .9rem;font-size:.9rem;cursor:pointer}
.songml-edit-body{flex:1;min-height:0;overflow:auto}
"""

_EDIT_JS = """
// SongML in-browser editor overlay. Injected by songml-serve only when editing
// is enabled (SONGML_WRITE_TOKEN set). __REL_PATH__ and __WRITE_TOKEN__ are
// substituted server-side with JSON-quoted values.
(function () {
  'use strict';
  var REL_PATH = __REL_PATH__;
  var WRITE_TOKEN = __WRITE_TOKEN__;
  var VEDITOR_BASE = 'https://stabledog.github.io/veditor.web';
  var btn = document.getElementById('songml-edit-btn');
  if (!btn) return;

  var overlay = null;
  var veditor = null;
  var veditorModule = null;  // imported once per page load, reused across opens
  var view = null;  // CodeMirror EditorView returned by createEditor
  var openedContent = '';
  var savedContent = '';

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }

  function ensureCss() {
    if (document.getElementById('songml-veditor-css')) return;
    var link = document.createElement('link');
    link.id = 'songml-veditor-css';
    link.rel = 'stylesheet';
    link.href = VEDITOR_BASE + '/veditor.css?v=' + Date.now();
    document.head.appendChild(link);
  }

  async function openOverlay() {
    if (overlay) return;
    var resp;
    try {
      resp = await fetch('/api/source?path=' + encodeURIComponent(REL_PATH) +
                         '&token=' + encodeURIComponent(WRITE_TOKEN));
    } catch (e) {
      alert('Could not load source: ' + e);
      return;
    }
    if (!resp.ok) { alert('Could not load source (HTTP ' + resp.status + ')'); return; }
    var data = await resp.json();
    openedContent = data.content || '';
    savedContent = openedContent;

    ensureCss();

    overlay = document.createElement('div');
    overlay.id = 'songml-edit-overlay';
    overlay.innerHTML =
      '<div class="songml-edit-bar">' +
        '<span class="songml-edit-title">Editing ' + esc(REL_PATH) + '</span>' +
        '<span class="songml-edit-warn" id="songml-edit-warn"></span>' +
        '<button type="button" id="songml-edit-format" title="Renumber bars, align columns, fix chord casing">Format</button>' +
        '<button type="button" id="songml-edit-save">Save</button>' +
        '<button type="button" id="songml-edit-close">Close</button>' +
      '</div>' +
      '<div class="songml-edit-body" id="songml-edit-body"></div>';
    document.body.appendChild(overlay);
    document.getElementById('songml-edit-format').addEventListener('click', doFormat);
    document.getElementById('songml-edit-save').addEventListener('click', doSave);
    document.getElementById('songml-edit-close').addEventListener('click', doClose);

    try {
      // Cache-bust once per page load, not per open: a fresh module instance
      // per open would stack up independent copies of veditor and its vim layer.
      if (!veditorModule) {
        veditorModule = await import(VEDITOR_BASE + '/veditor.js?v=' + Date.now());
      }
      veditor = veditorModule;
    } catch (e) {
      alert('Could not load editor: ' + e);
      closeOverlay();
      return;
    }
    // veditor restores the user's CUA/VIM choice from localStorage itself;
    // don't override it here, or the choice never survives a close/reopen.
    view = veditor.createEditor(
      document.getElementById('songml-edit-body'),
      openedContent,
      { onSave: doSave, onQuit: doClose },
      { storagePrefix: 'songml' }
    );
    veditor.focusEditor();
  }

  async function doFormat() {
    if (!veditor || !view) return;
    var warnEl = document.getElementById('songml-edit-warn');
    var content = veditor.getEditorContent();
    warnEl.textContent = 'Formatting…';
    var resp, data;
    try {
      resp = await fetch('/api/format', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: content, token: WRITE_TOKEN })
      });
      data = await resp.json();
    } catch (e) {
      warnEl.textContent = 'Format failed: ' + e;
      return;
    }
    if (!(resp.ok && data.ok)) {
      warnEl.textContent = 'Format failed: ' + (data.error || ('HTTP ' + resp.status));
      return;
    }
    // Bail if the buffer changed while the request was in flight.
    if (!view || veditor.getEditorContent() !== content) {
      warnEl.textContent = 'Buffer changed during format; try again.';
      return;
    }
    if (data.content !== content) {
      // A normal edit (not setEditorContent): undoable, and leaves the buffer
      // dirty so the formatted text still has to be saved.
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: data.content },
        selection: { anchor: Math.min(view.state.selection.main.head, data.content.length) }
      });
    }
    var what = data.content !== content ? 'Formatted (not saved yet)' : 'Already formatted';
    warnEl.textContent = (data.warnings && data.warnings.length)
      ? what + ', ' + data.warnings.length + ' warning(s): ' + data.warnings.join('; ')
      : what + '.';
    veditor.focusEditor();
  }

  async function doSave() {
    if (!veditor) return;
    var warnEl = document.getElementById('songml-edit-warn');
    var content = veditor.getEditorContent();
    warnEl.textContent = 'Saving\u2026';
    var resp, data;
    try {
      resp = await fetch('/api/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: REL_PATH, content: content, token: WRITE_TOKEN })
      });
      data = await resp.json();
    } catch (e) {
      warnEl.textContent = 'Save failed: ' + e;
      return;
    }
    if (resp.ok && data.ok) {
      savedContent = content;
      warnEl.textContent = (data.warnings && data.warnings.length)
        ? 'Saved with ' + data.warnings.length + ' warning(s): ' + data.warnings.join('; ')
        : 'Saved.';
    } else {
      warnEl.textContent = 'Save failed: ' + (data.error || ('HTTP ' + resp.status));
    }
  }

  function doClose() {
    if (veditor && veditor.isEditorDirty(savedContent)) {
      if (!window.confirm('Discard unsaved changes?')) return;
    }
    var changed = savedContent !== openedContent;
    closeOverlay();
    if (changed) window.location.reload();
  }

  function closeOverlay() {
    try { if (veditor) veditor.destroyEditor(); } catch (e) {}
    veditor = null;
    view = null;
    if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
    overlay = null;
  }

  btn.addEventListener('click', openOverlay);
})();
"""

_MIX_CSS = """
#songml-mix{position:fixed;left:0;right:0;bottom:0;z-index:900;background:#202124;color:#e8eaed;
  box-shadow:0 -2px 8px rgba(0,0,0,.35);font-size:16px;user-select:none;-webkit-user-select:none;
  touch-action:manipulation}
#songml-mix button{font:inherit;color:inherit;border:none;border-radius:8px;cursor:pointer;
  min-width:56px;min-height:56px;padding:0 14px;background:#3c4043}
#songml-mix button:disabled{opacity:.35;cursor:default}
#songml-mix button.busy{outline:3px solid #fbbc04}
.mix-strip{display:flex;align-items:center;gap:8px;padding:4px 8px;min-height:64px;flex-wrap:wrap}
#songml-mix .mix-play,#songml-mix .mix-stop{min-width:72px;min-height:56px;font-size:20px;font-weight:700}
#songml-mix .mix-play{background:#0b8043}
#songml-mix .mix-stop{background:#a50e0e}
.mix-pos{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums;min-width:7.5em}
.mix-meta{color:#bdc1c6;font-variant-numeric:tabular-nums}
.mix-live{display:flex;align-items:center;gap:6px;color:#bdc1c6}
.mix-dot{width:12px;height:12px;border-radius:50%;background:#5f6368}
.mix-live.ok .mix-dot{background:#34a853}
.mix-live.down .mix-dot{background:#ea4335}
.mix-msg{flex:1;min-width:8em;color:#fbbc04;font-size:15px;line-height:1.25}
#songml-mix.down .mix-pos,#songml-mix.down .mix-meta{opacity:.4}
#songml-mix .mix-toggle{margin-left:auto}
.mix-tray{display:none;border-top:1px solid #3c4043;padding:8px;max-height:40vh;overflow-y:auto}
#songml-mix.expanded .mix-tray{display:block}
.mix-posctl{display:flex;gap:6px}
#songml-mix .mix-posctl button{min-width:48px;padding:0 10px;font-size:15px}
.mix-grid{display:flex;flex-wrap:wrap;gap:6px}
#songml-mix .mix-tile{min-width:0;width:112px;min-height:44px;padding:3px 6px 3px 8px;
  font-size:13px;line-height:1.2;text-align:left;background:#e8eaed;color:#202124;
  border-left:6px solid var(--track-color,#808080);border-radius:6px;overflow:hidden;
  white-space:normal;overflow-wrap:anywhere;display:-webkit-box;-webkit-box-orient:vertical;
  -webkit-line-clamp:2}
#songml-mix .mix-tile.muted{background:#3c4043;color:#9aa0a6;text-decoration:line-through}
#songml-mix .mix-tile.child{margin-left:0;border-left-style:double;border-left-width:9px}
.mix-group{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
  margin-right:6px;padding:1px 4px;border:1px solid currentColor;border-radius:3px;text-decoration:none}
.mix-empty{color:#9aa0a6}
"""

_MIX_JS = """
// SongML mixroom panel (design: docs/mixroom-transport-design.md §4). Injected by
// songml-serve only with --mixroom and editing enabled. __WRITE_TOKEN__ is
// substituted server-side with a JSON-quoted value. The page only ever shows state
// read back from Live: a press shows "busy" until the command's re-read arrives.
(function () {
  'use strict';
  var WRITE_TOKEN = __WRITE_TOKEN__;
  var KEY_EXPANDED = 'songml.mix.expanded';
  var panel = document.getElementById('songml-mix');
  if (!panel) return;

  function $(cls) { return panel.querySelector('.' + cls); }
  var playBtn = $('mix-play'), stopBtn = $('mix-stop'), posEl = $('mix-pos');
  var metaEl = $('mix-meta'), liveEl = $('mix-live'), liveText = $('mix-live-text');
  var msgEl = $('mix-msg'), toggleBtn = $('mix-toggle'), grid = $('mix-grid');
  var homeBtn = $('mix-home'), prevBtn = $('mix-prev'), nextBtn = $('mix-next');

  var state = null;          // last state read from the server
  var renderedRev = null;    // tracks_rev the mute grid was built for
  var busy = false;          // a command is in flight
  var pollTimer = null, polling = false;
  var statusMsg = '';        // standing condition (Live down, server down); '' when fine
  var flashMsg = '', flashUntil = 0;  // a command's refusal or failure, shown for 4 s

  var expanded = false;
  try { expanded = localStorage.getItem(KEY_EXPANDED) === '1'; } catch (e) {}

  function barLen(s) {
    var num = s.signature && s.signature[0], den = s.signature && s.signature[1];
    if (!num || !den) return null;
    return num * 4 / den;  // Live beats are quarter notes
  }

  function curBar(s) {  // 1-based Live bar containing the current position
    var len = barLen(s);
    if (len === null || s.beat === null) return null;
    return Math.floor(s.beat / len + 1e-6) + 1;
  }

  function posText(s) {
    var len = barLen(s);
    if (len === null || s.beat === null) return 'bar \\u2013';
    var bar = curBar(s);
    var inBar = (s.beat - (bar - 1) * len) * s.signature[1] / 4;
    return 'bar ' + bar + ' . ' + (Math.floor(inBar + 1e-6) + 1);
  }

  function showMsg() {
    msgEl.textContent = Date.now() < flashUntil ? flashMsg : statusMsg;
  }

  function flash(text) {
    flashMsg = text;
    flashUntil = Date.now() + 4000;
    showMsg();
    setTimeout(showMsg, 4050);
  }

  function setExpanded(on) {
    expanded = on;
    panel.classList.toggle('expanded', on);
    toggleBtn.textContent = on ? '\\u25be' : '\\u25b4';
    toggleBtn.setAttribute('aria-label', on ? 'Hide track mutes' : 'Show track mutes');
    try { localStorage.setItem(KEY_EXPANDED, on ? '1' : '0'); } catch (e) {}
    fitBody();
    schedule(0);
  }

  function fitBody() {  // keep the last chart row clear of the fixed panel
    document.body.style.paddingBottom = (panel.offsetHeight + 16) + 'px';
  }

  function buildGrid(tracks) {
    grid.textContent = '';
    if (!tracks.length) {
      var empty = document.createElement('span');
      empty.className = 'mix-empty';
      empty.textContent = 'No tracks in the mixroom set.';
      grid.appendChild(empty);
      return;
    }
    tracks.forEach(function (t) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'mix-tile' + (t.grouped ? ' child' : '');
      b.dataset.index = String(t.index);
      b.dataset.name = t.name;
      b.style.setProperty('--track-color', '#' + ('000000' + t.color.toString(16)).slice(-6));
      if (t.foldable) {
        var g = document.createElement('span');
        g.className = 'mix-group';
        g.textContent = 'group';
        b.appendChild(g);
      }
      b.appendChild(document.createTextNode(t.name));
      b.addEventListener('click', function () { onMute(b); });
      grid.appendChild(b);
    });
  }

  function render() {
    var s = state;
    var up = !!s && s.live === 'ok';
    liveEl.classList.toggle('ok', up);
    liveEl.classList.toggle('down', !!s && !up);
    panel.classList.toggle('down', !up);  // greys the last-read position: it's stale
    liveText.textContent = !s ? 'Live \\u2026' : up ? 'Live' : 'Live not answering';
    liveEl.title = (s && s.reason) || '';
    if (s) statusMsg = up ? '' : (s.reason || 'mixroom Live not answering');
    showMsg();
    if (!s) {
      [playBtn, stopBtn, homeBtn, prevBtn, nextBtn].forEach(function (b) { b.disabled = true; });
      Array.prototype.forEach.call(grid.querySelectorAll('.mix-tile'),
                                   function (b) { b.disabled = true; });
      return;
    }
    posEl.textContent = posText(s);
    var sig = s.signature && s.signature[0] ? s.signature[0] + '/' + s.signature[1] : '';
    metaEl.textContent = (s.tempo !== null ? Math.round(s.tempo * 10) / 10 + ' BPM ' : '') + sig;
    var stopped = up && s.playing === false, playing = up && s.playing === true;
    playBtn.disabled = busy || !stopped;
    stopBtn.disabled = busy || !playing;
    var bar = curBar(s);
    homeBtn.disabled = busy || !stopped;
    prevBtn.disabled = busy || !stopped || bar === null || s.beat <= 0;
    nextBtn.disabled = busy || !stopped || bar === null;
    if (s.tracks_rev !== renderedRev) {
      buildGrid(s.tracks);
      renderedRev = s.tracks_rev;
      fitBody();
    }
    var byIndex = {};
    s.tracks.forEach(function (t) { byIndex[t.index] = t; });
    Array.prototype.forEach.call(grid.querySelectorAll('.mix-tile'), function (b) {
      var t = byIndex[Number(b.dataset.index)];
      b.classList.toggle('muted', !!t && t.mute);
      b.setAttribute('aria-pressed', t && t.mute ? 'true' : 'false');
      b.disabled = busy || !up;
    });
  }

  function schedule(delay) {
    clearTimeout(pollTimer);
    if (document.hidden) return;  // resumes on visibilitychange
    pollTimer = setTimeout(poll, delay);
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      var resp = await fetch('/api/mix/state?token=' + encodeURIComponent(WRITE_TOKEN),
                             { cache: 'no-store' });
      var data = await resp.json();
      if (resp.ok && data.ok) {
        state = data.state;
      } else {
        state = null;
        statusMsg = 'Mix state failed: ' + (data.error || ('HTTP ' + resp.status));
      }
    } catch (e) {
      state = null;
      statusMsg = 'songml-serve not answering';
    } finally {
      polling = false;
    }
    render();
    schedule(expanded ? 250 : 1000);
  }

  async function command(name, body, btn) {
    if (busy) return;
    busy = true;
    if (btn) btn.classList.add('busy');
    render();
    var payload = Object.assign({ token: WRITE_TOKEN }, body || {});
    try {
      var resp = await fetch('/api/mix/' + name, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      var data = await resp.json();
      if (data.state) state = data.state;  // the re-read state, whatever the outcome
      if (!data.ok) flash(data.error || ('HTTP ' + resp.status));
    } catch (e) {
      flash(name + ' failed: ' + e);
    } finally {
      busy = false;
      if (btn) btn.classList.remove('busy');
    }
    render();
  }

  function locateBar(bar) {  // bar is a 1-based Live bar
    var len = barLen(state);
    if (len === null) return;
    return command('locate', { beat: Math.max(0, (bar - 1) * len) });
  }

  function onMute(b) {
    var muted = b.classList.contains('muted');
    command('mute', { index: Number(b.dataset.index), name: b.dataset.name, mute: !muted }, b);
  }

  // Buttons act on click, which fires only when the press is released inside the
  // target: a finger sliding off cancels (§4.1).
  playBtn.addEventListener('click', function () { command('play', null, playBtn); });
  stopBtn.addEventListener('click', function () { command('stop', null, stopBtn); });
  homeBtn.addEventListener('click', function () { command('locate', { beat: 0 }, homeBtn); });
  prevBtn.addEventListener('click', function () {
    // From mid-bar, back to this bar's start; from a bar start, the previous bar.
    var len = barLen(state), bar = curBar(state);
    var atStart = Math.abs(state.beat - (bar - 1) * len) < 1e-3;
    locateBar(Math.max(1, atStart ? bar - 1 : bar));
  });
  nextBtn.addEventListener('click', function () { locateBar(curBar(state) + 1); });
  toggleBtn.addEventListener('click', function () { setExpanded(!expanded); });
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) clearTimeout(pollTimer); else schedule(0);
  });
  window.addEventListener('resize', fitBody);

  setExpanded(expanded);
  render();
})();
"""

_MIX_HTML = """<div id="songml-mix" role="region" aria-label="Mixroom transport">
<div class="mix-strip">
<button type="button" class="mix-play" aria-label="Play" disabled>&#9654;</button>
<button type="button" class="mix-stop" aria-label="Stop" disabled>&#9632;</button>
<span class="mix-posctl">
<button type="button" class="mix-home" disabled>|&#9664; 1.1</button>
<button type="button" class="mix-prev" disabled>&#9664; bar</button>
<button type="button" class="mix-next" disabled>bar &#9654;</button>
</span>
<span class="mix-pos">bar &ndash;</span>
<span class="mix-meta"></span>
<span class="mix-live"><span class="mix-dot"></span><span class="mix-live-text">Live &hellip;</span></span>
<span class="mix-msg" role="status"></span>
<button type="button" class="mix-toggle" aria-label="Show track mutes">&#9652;</button>
</div>
<div class="mix-tray">
<div class="mix-grid"></div>
</div>
</div>
"""


def to_html_string(
    doc: Document,
    bars_per_row: int = 8,
    back_url: str | None = None,
    midi_url: str | None = None,
    editable: bool = False,
    write_token: str | None = None,
    song_path: str | None = None,
    mixroom: bool = False,
) -> str:
    title = _prop(doc, "Title", "Untitled")
    key = _prop(doc, "Key", "")
    tempo = _prop(doc, "Tempo", "")
    time_sig = _prop(doc, "Time", "4/4")
    beats_per_bar = int(time_sig.split("/")[0])
    cols_per_bar = beats_per_bar * 2  # half-beat column resolution
    max_cols = bars_per_row * cols_per_bar  # fixed grid width so all bars are equal-width

    sections = [item for item in doc.items if isinstance(item, Section)]

    rows: list[list[RowSegment]] = []
    open_row: list[RowSegment] | None = None
    open_row_cols = 0

    for color_idx, section in enumerate(sections):
        color = SECTION_COLORS[color_idx % len(SECTION_COLORS)]
        bars = section.bars
        needed_cols = len(bars) * cols_per_bar

        # A "same-row" section packs into whatever space is left on the previous
        # row if (and only if) it fits there whole. Otherwise it falls back to
        # starting its own row(s), same as an unmarked section.
        if section.same_row and open_row is not None and open_row_cols + needed_cols <= max_cols:
            open_row.append(RowSegment(section, bars, color, True))
            open_row_cols += needed_cols
            continue

        if open_row is not None:
            rows.append(open_row)
            open_row = None
            open_row_cols = 0

        chunk_starts = range(0, len(bars), bars_per_row)
        last_chunk_start = ((len(bars) - 1) // bars_per_row) * bars_per_row
        for row_start in chunk_starts:
            row_bars = bars[row_start : row_start + bars_per_row]
            segment = RowSegment(section, row_bars, color, row_start == 0)
            if row_start == last_chunk_start:
                open_row = [segment]
                open_row_cols = len(row_bars) * cols_per_bar
            else:
                rows.append([segment])

    if open_row is not None:
        rows.append(open_row)

    strips = [_render_row(row, cols_per_bar, max_cols, beats_per_bar, tempo) for row in rows]

    meta_parts = [
        p
        for p in [
            f"Key: {key}" if key else "",
            f"Tempo: {tempo}" if tempo else "",
            f"Time: {time_sig}",
        ]
        if p
    ]

    t = _html.escape(title)
    back_html = (
        f'<a class="back-link" href="{_html.escape(back_url)}">&larr; Library</a>'
        if back_url
        else ""
    )
    midi_btn = (
        f'<a class="midi-btn" href="{_html.escape(midi_url)}">&#9654; Download MIDI</a>'
        if midi_url
        else ""
    )
    can_edit = bool(editable and write_token and song_path)
    edit_btn = (
        '<button type="button" class="edit-btn" id="songml-edit-btn">\u270e Edit</button>'
        if can_edit
        else ""
    )
    edit_css = _EDIT_CSS if can_edit else ""
    edit_script = (
        "<script>\n"
        + _EDIT_JS.replace("__REL_PATH__", _json.dumps(song_path)).replace(
            "__WRITE_TOKEN__", _json.dumps(write_token)
        )
        + "\n</script>"
        if can_edit
        else ""
    )
    # The mixroom panel needs the write token too: every /api/mix/* call is gated.
    show_mix = can_edit and mixroom
    mix_css = _MIX_CSS if show_mix else ""
    mix_html = _MIX_HTML if show_mix else ""
    mix_script = (
        "<script>\n" + _MIX_JS.replace("__WRITE_TOKEN__", _json.dumps(write_token)) + "\n</script>"
        if show_mix
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t}</title>
<style>{_CSS}{edit_css}{mix_css}</style>
</head>
<body>
<div class="song">
{back_html}
<div class="title-row"><h1>{t}</h1>{midi_btn}{edit_btn}</div>
<div class="meta">{" &bull; ".join(meta_parts)}</div>
{"".join(strips)}
</div>
{mix_html}{edit_script}{mix_script}</body>
</html>"""


def _render_row(
    segments: list[RowSegment],
    cols_per_bar: int,
    max_cols: int,
    beats_per_bar: int,
    tempo: str,
) -> str:
    gs = f"grid-template-columns:repeat({max_cols},1fr)"
    parts: list[str] = []

    offsets: list[int] = []
    col = 0
    for seg in segments:
        offsets.append(col)
        col += len(seg.bars) * cols_per_bar

    # Section-label row — one label per segment that starts a section, positioned
    # over that segment's own bars (so packed sections each keep their own label).
    if any(seg.show_label for seg in segments):
        cells: list[str] = []
        for seg, offset in zip(segments, offsets, strict=True):
            if not seg.show_label:
                continue
            span = len(seg.bars) * cols_per_bar
            cells.append(
                f'<div class="section-label" style="grid-column:{offset + 1}/span {span}">'
                f"{_html.escape(seg.section.name)}</div>"
            )
        parts.append(f'<div class="grid-row" style="{gs}">{"".join(cells)}</div>')

    # Bar numbers row
    cells = []
    for seg, offset in zip(segments, offsets, strict=True):
        for i, bar in enumerate(seg.bars):
            bcol = offset + i * cols_per_bar + 1
            elapsed = _format_elapsed(bar.number, beats_per_bar, tempo)
            cells.append(
                f'<div class="bar-num" style="grid-column:{bcol}/span {cols_per_bar}">'
                f'{bar.number} <span class="bar-elapsed">[{elapsed}]</span></div>'
            )
    parts.append(f'<div class="grid-row" style="{gs}">{"".join(cells)}</div>')

    # Chords row
    cells = []
    for seg_idx, (seg, offset) in enumerate(zip(segments, offsets, strict=True)):
        for i, bar in enumerate(seg.bars):
            bar_offset = offset + i * cols_per_bar
            is_first_bar_of_row = seg_idx == 0 and i == 0
            for j, chord in enumerate(bar.chords):
                ccol = bar_offset + int(round(chord.start_beat * 2)) + 1
                span = max(1, int(round(chord.duration_beats * 2)))
                bar_start_cls = " bar-start" if j == 0 and not is_first_bar_of_row else ""
                dense_cls = " dense" if span <= 2 else ""
                text = "" if chord.text in ("...", ".") else _html.escape(chord.text)
                tip = _html.escape(chord.text)
                cells.append(
                    f'<div class="chord{bar_start_cls}{dense_cls}" title="{tip}" style="grid-column:{ccol}/span {span}">'
                    f"{text}</div>"
                )
    parts.append(f'<div class="grid-row chords-row" style="{gs}">{"".join(cells)}</div>')

    # Lyrics row — only if any bar in this row has lyrics
    if any(bar.lyrics for seg in segments for bar in seg.bars):
        cells = []
        for seg, offset in zip(segments, offsets, strict=True):
            for i, bar in enumerate(seg.bars):
                lcol = offset + i * cols_per_bar + 1
                lyric = _html.escape(bar.lyrics or "")
                cells.append(
                    f'<div class="lyric" title="{lyric}" style="grid-column:{lcol}/span {cols_per_bar}">'
                    f"{lyric}</div>"
                )
        parts.append(f'<div class="grid-row lyrics-row" style="{gs}">{"".join(cells)}</div>')

    bg = _row_background(segments, cols_per_bar, max_cols)
    return f'<div class="strip" style="background:{bg}">{"".join(parts)}</div>\n'


def _row_background(segments: list[RowSegment], cols_per_bar: int, max_cols: int) -> str:
    """Build a hard-edged left-to-right gradient: one color band per segment,
    plus a trailing gray band for any unused space at the end of the row."""
    stops: list[str] = []
    cum_cols = 0
    for seg in segments:
        start_pct = cum_cols / max_cols * 100
        cum_cols += len(seg.bars) * cols_per_bar
        end_pct = cum_cols / max_cols * 100
        stops.append(f"{seg.color} {start_pct:.4f}%")
        stops.append(f"{seg.color} {end_pct:.4f}%")

    if cum_cols < max_cols:
        used_pct = cum_cols / max_cols * 100
        stops.append(f"#d0d0d0 {used_pct:.4f}%")

    return f"linear-gradient(to right,{','.join(stops)})"


def _format_elapsed(bar_number: int, beats_per_bar: int, tempo: str) -> str:
    """Elapsed time at the start of a bar, as M:SS. "-:--" if tempo is unknown."""
    try:
        bpm = float(tempo)
    except ValueError:
        bpm = 0.0
    if bpm <= 0:
        return "-:--"

    seconds = (bar_number - 1) * beats_per_bar * 60.0 / bpm
    total_seconds = round(seconds)
    minutes, secs = divmod(total_seconds, 60)
    return f"{minutes}:{secs:02d}"


def _prop(doc: Document, name: str, default: str) -> str:
    for item in doc.items:
        if isinstance(item, Property) and item.name == name:
            return item.value
    return default
