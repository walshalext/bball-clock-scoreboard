/* Shared client helpers for the scoreboard.
 *
 * Responsibilities:
 *   - Maintain a WebSocket connection to /ws with auto-reconnect.
 *   - Run an NTP-style time sync against the server so each client can
 *     convert its own performance.now() into "server time" without RTT skew.
 *   - Hold the latest state snapshot and let pages compute the current game
 *     clock AND shot clock purely client-side, every animation frame.
 *   - Receive buzzer events; display pages turn these into a full-screen
 *     edge-light flash (red for period, yellow for shot clock) per the
 *     FIBA §1.1.6 / §1.1.7 backboard light-strip convention.
 *
 * Public API (window.Scoreboard):
 *   connect()
 *   onState(cb)             ->  unsubscribe()
 *   onStatus(cb)            ->  unsubscribe()  (open/closed/error/connecting)
 *   onBuzzer(cb)            ->  unsubscribe()  ({kind:"period"|"shot_clock"})
 *   sendCommand(op, extra)  ->  bool
 *   getState()              ->  snapshot|null
 *   computeDisplayedGameMs()
 *   computeDisplayedShotMs()
 *   getBestRttMs() / getLastRttMs() / getClockOffsetMs()
 *   formatClock(ms, {showTenths})
 *   formatShot(ms)                  // tenths only below 5.0 seconds (FIBA §5.2)
 *   computeTimeoutDots(team, state) // {filled, max, mode, label}
 *   escapeHTML(s)                   // safe HTML escaping for innerHTML use
 *   patchRoster(host, players, buildRowHTML, patchRow) // rebuild-or-update roster
 */
(function () {
  "use strict";

  const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") +
                  location.host + "/ws";

  const SAMPLE_WINDOW = 8;
  const PING_INTERVAL_MS = 2000;
  const FAST_PING_COUNT = 6;
  const FAST_PING_INTERVAL_MS = 100;

  let ws = null;
  let wsReady = false;
  let reconnectDelay = 250;

  let state = null;
  const stateListeners = new Set();
  const statusListeners = new Set();
  const buzzerListeners = new Set();

  const samples = [];
  let lastRtt = NaN;
  let bestOffset = 0;
  let bestOffsetRtt = Infinity;

  let pingTimer = null;
  let fastPingsRemaining = 0;

  // ---- connection -------------------------------------------------------

  function emitStatus(status, extra) {
    for (const cb of statusListeners) {
      try { cb({ status, ...extra }); } catch (e) { console.error(e); }
    }
  }

  function connect() {
    if (ws) return;
    emitStatus("connecting");
    try { ws = new WebSocket(WS_URL); }
    catch (e) { scheduleReconnect(); return; }

    ws.addEventListener("open", () => {
      wsReady = true;
      reconnectDelay = 250;
      emitStatus("open");
      samples.length = 0;
      bestOffsetRtt = Infinity;
      fastPingsRemaining = FAST_PING_COUNT;
      schedulePings();
    });

    ws.addEventListener("message", (evt) => {
      let msg;
      try { msg = JSON.parse(evt.data); } catch { return; }
      if (msg.type === "state" && msg.state) {
        state = msg.state;
        for (const cb of stateListeners) {
          try { cb(state); } catch (e) { console.error(e); }
        }
      } else if (msg.type === "pong") {
        recordPong(msg);
      } else if (msg.type === "buzzer") {
        for (const cb of buzzerListeners) {
          try { cb({ kind: msg.kind, server_now_ms: msg.server_now_ms }); }
          catch (e) { console.error(e); }
        }
      }
    });

    ws.addEventListener("close", () => {
      ws = null; wsReady = false;
      stopPings();
      emitStatus("closed");
      scheduleReconnect();
    });

    ws.addEventListener("error", () => emitStatus("error"));
  }

  function scheduleReconnect() {
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 4000);
  }

  // ---- time sync --------------------------------------------------------

  function schedulePings() {
    stopPings();
    sendPing();
    pingTimer = setInterval(sendPing,
      fastPingsRemaining > 0 ? FAST_PING_INTERVAL_MS : PING_INTERVAL_MS);
  }
  function stopPings() {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  }
  function sendPing() {
    if (!wsReady || !ws) return;
    const t = performance.now();
    try { ws.send(JSON.stringify({ type: "ping", client_t: t })); }
    catch {}
  }
  function recordPong(msg) {
    const now = performance.now();
    const clientT = msg.client_t;
    const serverT = msg.server_now_ms;
    if (typeof clientT !== "number" || typeof serverT !== "number") return;
    const rtt = now - clientT;
    const offset = (serverT + rtt / 2) - now;
    lastRtt = rtt;
    samples.push({ rtt, offset, t: now });
    while (samples.length > SAMPLE_WINDOW) samples.shift();
    bestOffsetRtt = Infinity;
    for (const s of samples) {
      if (s.rtt < bestOffsetRtt) {
        bestOffsetRtt = s.rtt;
        bestOffset = s.offset;
      }
    }
    if (fastPingsRemaining > 0) {
      fastPingsRemaining--;
      if (fastPingsRemaining === 0) schedulePings();
    }
  }

  // ---- clock prediction -------------------------------------------------

  function serverNowMs() { return performance.now() + bestOffset; }

  function computeDisplayedGameMs() {
    if (!state) return 0;
    if (state.running) {
      const elapsed = serverNowMs() - state.anchor_server_ms;
      return Math.max(0, state.anchor_value_ms - elapsed);
    }
    return state.anchor_value_ms;
  }

  function computeDisplayedShotMs() {
    if (!state) return 0;
    if (state.sc_running) {
      const elapsed = serverNowMs() - state.sc_anchor_server_ms;
      return Math.max(0, state.sc_anchor_value_ms - elapsed);
    }
    return state.sc_anchor_value_ms;
  }

  function computeDisplayedTimeoutMs() {
    if (!state) return 0;
    if (state.to_running) {
      const elapsed = serverNowMs() - state.to_anchor_server_ms;
      return Math.max(0, state.to_anchor_value_ms - elapsed);
    }
    return state.to_anchor_value_ms;
  }

  // ---- formatting -------------------------------------------------------

  function formatClock(ms, opts) {
    // FIBA §4.1: mm:ss; ss.f only in the last minute of period/OT.
    const showTenths = opts && opts.showTenths;
    const totalSeconds = ms / 1000;
    if (showTenths && totalSeconds < 60) {
      const s = Math.floor(totalSeconds);
      const t = Math.floor((ms % 1000) / 100);
      return `${s.toString().padStart(2, "0")}.${t}`;
    }
    const m = Math.floor(totalSeconds / 60);
    const s = Math.floor(totalSeconds % 60);
    return `${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  }

  function formatShot(ms) {
    // FIBA §5.2: whole seconds, tenths only in the last 5 seconds.
    //
    // We snap the remaining time to the nearest displayed tenth (floor to a
    // 100ms grid) first, then format. This makes the whole-second display
    // and the tenths display agree on a single rounded value, so each
    // displayed number is shown for its full natural duration:
    //
    //   24.0 .. 23.1  -> "24"      (whole-second ceil, ~1s on screen)
    //   23.0 .. 22.1  -> "23"
    //   ...
    //    6.0 .. 5.1   -> "6"
    //    5.0 .. 4.1   -> "5.0" .. "4.1"   (tenths, ~100ms each on screen)
    //    4.0 .. 3.1   -> "4.0" .. "3.1"
    //
    if (ms <= 0) return "0";
    const tenths = Math.max(0, Math.floor(ms / 100));   // remaining tenths
    if (tenths <= 50) {
      // Last 5.0 seconds: "X.Y"
      return `${Math.floor(tenths / 10)}.${tenths % 10}`;
    }
    return String(Math.ceil(tenths / 10));
  }

  function sendCommand(op, extra) {
    if (!wsReady || !ws) return false;
    const payload = Object.assign({ type: "cmd", op }, extra || {});
    try { ws.send(JSON.stringify(payload)); return true; }
    catch { return false; }
  }

  // ---- global keyboard shortcuts ----------------------------------------
  //
  // Loaded once from /api/shortcuts at page load. Every page gets the same
  // bindings regardless of which operator is focused, so a USB controller
  // with custom keystrokes works identically from any seat.
  //
  // Siren uses keydown+keyup (press-and-hold). All other actions fire on
  // keydown (no repeat).

  const _SHORTCUT_CMDS = {
    start_game:       () => sendCommand("start"),
    stop_game:        () => sendCommand("stop"),
    sc_enable:        () => sendCommand("sc_enable"),
    sc_disable:       () => sendCommand("sc_disable"),
    sc_reset_24:      () => sendCommand("sc_reset_24"),
    sc_reset_14:      () => sendCommand("sc_reset_14"),
    sc_hide:          () => sendCommand("sc_toggle_visible"),
    possession_left:  () => sendCommand("set_possession", { direction: "left"  }),
    possession_right: () => sendCommand("set_possession", { direction: "right" }),
    possession_off:   () => sendCommand("set_possession", { direction: "off"   }),
  };

  let _shortcuts = {};   // action -> key string (lowercase)
  let _keyMap   = {};    // key (lowercase) -> action
  let _sirenKeyHeld = false;

  function _applyShortcuts(data) {
    _shortcuts = data || {};
    _keyMap = {};
    for (const [action, key] of Object.entries(_shortcuts)) {
      if (key) _keyMap[String(key).toLowerCase()] = action;
    }
    _updateKeyHints();
  }

  async function _loadShortcuts() {
    try {
      const r = await fetch("/api/shortcuts");
      if (r.ok) _applyShortcuts(await r.json());
    } catch {}
  }

  function _updateKeyHints() {
    document.querySelectorAll("[data-action]").forEach(el => {
      const key = _shortcuts[el.dataset.action];
      const hint = el.querySelector(".key-hint");
      if (hint) hint.textContent = key ? `(${key.toUpperCase()})` : "";
    });
  }

  document.addEventListener("keydown", (e) => {
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) return;
    const action = _keyMap[e.key.toLowerCase()];
    if (!action) return;
    if (action === "siren") {
      if (e.repeat || _sirenKeyHeld) return;
      _sirenKeyHeld = true;
      sendCommand("siren_on");
    } else {
      if (e.repeat) return;
      _SHORTCUT_CMDS[action]?.();
    }
  });

  document.addEventListener("keyup", (e) => {
    if (_keyMap[e.key.toLowerCase()] === "siren" && _sirenKeyHeld) {
      _sirenKeyHeld = false;
      sendCommand("siren_off");
    }
  });

  window.addEventListener("blur", () => {
    if (_sirenKeyHeld) {
      _sirenKeyHeld = false;
      sendCommand("siren_off");
    }
  });

  // ---- utilities --------------------------------------------------------

  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Rebuild `host`'s children when player count changes; otherwise update
  // each row in place to avoid clobbering focused inputs or pressed states.
  //
  //   buildRowHTML(player, index) -> HTML string   (used for innerHTML)
  //   patchRow(rowEl, player, index)               (mutates existing element)
  function patchRoster(host, players, buildRowHTML, patchRow) {
    if (!host.dataset.built || +host.dataset.built !== players.length) {
      host.innerHTML = players.map((p, i) => buildRowHTML(p, i)).join("");
      host.dataset.built = players.length;
      return;
    }
    players.forEach((p, i) => {
      const row = host.children[i];
      if (row) patchRow(row, p, i);
    });
  }

  // ---- time-outs --------------------------------------------------------
  //
  // The operator controls how the indicator is rendered via two settings on
  // the state: timeout_mode ("remaining" | "taken") and timeout_max (the
  // total number of dots shown, typically 2 or 3).
  //
  // Returns: { filled, max, mode, label }
  //   filled - number of "lit" dots
  //   max    - total dots to render
  //   mode   - "remaining" | "taken"
  //   label  - "Time-outs left" | "Time-outs taken"
  function computeTimeoutDots(team, st) {
    if (!st) return { filled: 0, max: 3, mode: "remaining", label: "Time-outs left" };
    const max = Math.max(1, st.timeout_max || 3);
    const mode = st.timeout_mode === "taken" ? "taken" : "remaining";
    const taken = (st[team] || {}).timeouts_taken || 0;
    const filled = mode === "taken"
      ? Math.max(0, Math.min(max, taken))
      : Math.max(0, Math.min(max, max - taken));
    const label = mode === "taken" ? "Time-outs taken" : "Time-outs left";
    return { filled, max, mode, label };
  }

  // ---- public API -------------------------------------------------------

  window.Scoreboard = {
    connect,
    onState(cb) { stateListeners.add(cb); return () => stateListeners.delete(cb); },
    onStatus(cb) { statusListeners.add(cb); return () => statusListeners.delete(cb); },
    onBuzzer(cb) { buzzerListeners.add(cb); return () => buzzerListeners.delete(cb); },
    sendCommand,
    getState() { return state; },
    computeDisplayedGameMs,
    computeDisplayedShotMs,
    computeDisplayedTimeoutMs,
    getClockOffsetMs() { return bestOffset; },
    getLastRttMs() { return lastRtt; },
    getBestRttMs() { return bestOffsetRtt; },
    formatClock,
    formatShot,
    computeTimeoutDots,
    escapeHTML,
    patchRoster,
    getShortcuts() { return { ..._shortcuts }; },
    async saveShortcuts(data) {
      _applyShortcuts(data);
      await fetch("/api/shortcuts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
    },
  };

  // ---- Status bar + navigation (auto-injected into every page) ----------

  const _NAV_PAGES = [
    { path: "/",                  name: "Control",     desc: "Full operator panel"  },
    { path: "/master",            name: "Master",      desc: "Touch / embed"        },
    { path: "/scoreboard",        name: "Scoreboard",  desc: "FIBA display"         },
    { path: "/clock",             name: "Clock",       desc: "Big game clock"       },
    { path: "/timer",             name: "Timer op",    desc: "Game clock operator"  },
    { path: "/shot",              name: "Shot clock",  desc: "Shot clock display"   },
    { path: "/shotclock-op",      name: "Shot op",     desc: "Shot clock operator"  },
    { path: "/visuals",           name: "Visuals op",  desc: "Scorer's table"       },
    { path: "/possession",        name: "Possession",  desc: "Arrow display"        },
    { path: "/arrow",             name: "Arrow op",    desc: "Arrow operator"       },
    { path: "/players?team=home", name: "Players (H)", desc: "Home roster"          },
    { path: "/players?team=away", name: "Players (A)", desc: "Away roster"          },
    { path: "/fouls?team=home",   name: "Fouls (H)",   desc: "Home team fouls"      },
    { path: "/fouls?team=away",   name: "Fouls (A)",   desc: "Away team fouls"      },
    { path: "/timeout",           name: "Timeout",     desc: "Timeout countdown"    },
    { path: "/shortcuts",         name: "Shortcuts",   desc: "Key bindings"          },
  ];

  function _initUI() {
    // Status bar
    const bar = document.createElement("div");
    bar.id = "csb";
    bar.innerHTML =
      '<div id="csb-left"><span id="csb-dot"></span><span id="csb-txt">connecting…</span></div>' +
      '<div id="csb-right"><span id="csb-rtt">—</span>' +
      '<button id="csb-nav" aria-label="Pages">☰</button></div>';
    document.body.appendChild(bar);

    // Nav overlay
    const curPath = location.pathname + location.search;
    const items = _NAV_PAGES.map(p => {
      const cur = location.pathname === p.path || curPath === p.path;
      return '<a href="' + p.path + '" class="cnav-item' + (cur ? " cnav-cur" : "") + '">' +
        '<span class="cnav-name">' + p.name + "</span>" +
        '<span class="cnav-desc">' + p.desc + "</span></a>";
    }).join("");
    const overlay = document.createElement("div");
    overlay.id = "cnav";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.innerHTML =
      '<div id="cnav-panel">' +
        '<div id="cnav-head"><span id="cnav-title">Go to</span>' +
        '<button id="cnav-close" aria-label="Close">✕</button></div>' +
        '<div id="cnav-grid">' + items + "</div></div>";
    document.body.appendChild(overlay);

    function openNav()  { overlay.classList.add("open");    document.getElementById("cnav-close").focus(); }
    function closeNav() { overlay.classList.remove("open"); document.getElementById("csb-nav").focus(); }
    document.getElementById("csb-nav").addEventListener("click", openNav);
    document.getElementById("cnav-close").addEventListener("click", closeNav);
    overlay.addEventListener("click", e => { if (e.target === overlay) closeNav(); });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && overlay.classList.contains("open")) closeNav();
    });

    // On scrollable pages, push content above the fixed bar.
    // Full-screen pages (overflow: hidden) manage their own bottom spacing.
    if (getComputedStyle(document.body).overflow !== "hidden") {
      document.body.style.paddingBottom = "var(--sb-height)";
    }

    // Drive csb connection indicator from the private statusListeners set
    statusListeners.add(({ status }) => {
      const dot = document.getElementById("csb-dot");
      const txt = document.getElementById("csb-txt");
      if (!dot) return;
      dot.className = "";
      if      (status === "open")       { dot.classList.add("ok");  txt.textContent = "live";           }
      else if (status === "closed")     { dot.classList.add("bad"); txt.textContent = "offline";        }
      else if (status === "connecting") {                           txt.textContent = "connecting…"; }
      else                              {                           txt.textContent = status;            }
    });

    // Drive csb latency from the private bestOffsetRtt variable
    setInterval(() => {
      const el = document.getElementById("csb-rtt");
      if (el) el.textContent = isFinite(bestOffsetRtt) ? bestOffsetRtt.toFixed(1) + " ms" : "—";
    }, 500);
  }

  // common.js is always at the bottom of <body>, so the DOM is already available
  _initUI();
  _loadShortcuts();

})();
