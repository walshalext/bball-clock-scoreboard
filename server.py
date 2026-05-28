"""FastAPI server for the basketball scoreboard.

Routes
------
- GET /              -> control.html        (operator panel)
- GET /scoreboard    -> scoreboard.html     (full FIBA-style display)
- GET /clock         -> clock.html          (simple big game clock)
- GET /shot          -> shot.html           (shot clock + duplicate game clock)
- GET /possession    -> possession.html     (alternating possession arrow)
- GET /fouls?team=…  -> fouls.html          (team foul indicator card)
- GET /common.js     -> shared client JS
- GET /api/state     -> JSON snapshot (debugging)
- WS  /ws            -> JSON command + state + buzzer channel

WebSocket protocol (JSON)
-------------------------
Server -> client:
    {"type": "state",  "state": <snapshot>}
    {"type": "pong",   "client_t": <number>, "server_now_ms": <number>}
    {"type": "buzzer", "kind": "period"|"shot_clock", "server_now_ms": <number>}

Client -> server:
    {"type": "ping", "client_t": <number>}
    {"type": "cmd",  "op": "<op>", ...op-specific fields...}
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from state import GameState, server_now_ms

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scoreboard")

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="bball-clock-scoreboard")
game = GameState()


# --------------------------------------------------------------------------- #
# WebSocket hub
# --------------------------------------------------------------------------- #

class Hub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def leave(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, separators=(",", ":"))
        async with self._lock:
            dead: list[WebSocket] = []
            for ws in self._clients:
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)


hub = Hub()


async def broadcast_state() -> None:
    await hub.broadcast({"type": "state", "state": game.snapshot()})


# --------------------------------------------------------------------------- #
# Buzzer watcher
# --------------------------------------------------------------------------- #
#
# When either clock is running, we schedule an asyncio task that wakes up at
# the expected zero crossing. On wake-up, we recompute -- if the relevant
# clock has been changed since (different version, no longer running, or value
# no longer ~0), we just exit. Otherwise we fire the buzzer broadcast.
#
# Every command that touches a clock calls reschedule_buzzers() afterwards.

_game_buzz_task: Optional[asyncio.Task[None]] = None
_shot_buzz_task: Optional[asyncio.Task[None]] = None
# Three separate tasks for the time-out clock — one per warning event so
# they can be cancelled independently when the clock state changes.
_to_buzz_20_task: Optional[asyncio.Task[None]] = None
_to_buzz_10_task: Optional[asyncio.Task[None]] = None
_to_buzz_0_task:  Optional[asyncio.Task[None]] = None


async def _wait_and_fire(kind: str, fire_at_ms: float, captured_version: int) -> None:
    """Sleep until fire_at_ms (server time), then fire if state hasn't moved."""
    try:
        delay_ms = fire_at_ms - server_now_ms()
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

        snap = game.snapshot()
        if snap["version"] != captured_version:
            # Some command changed state; the new command should have
            # rescheduled (or skipped) us already.
            return

        # Sanity check: the relevant clock should actually be at zero.
        if kind == "period":
            if not snap["running"]:
                return
            remaining = max(0.0, snap["anchor_value_ms"]
                            - (snap["server_now_ms"] - snap["anchor_server_ms"]))
            if remaining > 50:
                return
            # FIBA §4.1: the game clock stops the game; reflect that in state.
            game.stop()
            await broadcast_state()
        elif kind == "shot_clock":
            if not snap["sc_running"]:
                return
            remaining = max(0.0, snap["sc_anchor_value_ms"]
                            - (snap["server_now_ms"] - snap["sc_anchor_server_ms"]))
            if remaining > 50:
                return
            # FIBA §5.5: shot-clock buzzer does NOT stop the game clock.
            # In the new model we ALSO don't touch the operator's sc_enabled
            # switch -- we just zero the value. The next-zero-at guard
            # against value <= 0 prevents the buzzer from re-firing until
            # the operator resets the value (mirrors a physical clock).
            game.sc_set_ms(0)
            await broadcast_state()

        elif kind in ("timeout_warn_20", "timeout_warn_10"):
            # Pure visual events -- just broadcast, don't touch state.
            if not snap["to_running"]:
                return

        elif kind == "timeout_end":
            if not snap["to_running"]:
                return
            remaining = max(0.0, snap["to_anchor_value_ms"]
                            - (snap["server_now_ms"] - snap["to_anchor_server_ms"]))
            if remaining > 50:
                return
            # Stop the time-out clock and zero its displayed value. The
            # operator hits Reset (or Set) to use it again.
            game.to_stop()
            game.to_set_ms(0)
            await broadcast_state()

        await hub.broadcast({
            "type": "buzzer",
            "kind": kind,
            "server_now_ms": server_now_ms(),
        })
    except asyncio.CancelledError:
        # Reschedule path; just exit quietly.
        raise


def _cancel(task: Optional[asyncio.Task[None]]) -> None:
    if task is not None and not task.done():
        task.cancel()


def reschedule_buzzers() -> None:
    """Cancel and (re)schedule every buzzer task based on current state."""
    global _game_buzz_task, _shot_buzz_task
    global _to_buzz_20_task, _to_buzz_10_task, _to_buzz_0_task
    snap = game.snapshot()

    _cancel(_game_buzz_task)
    _game_buzz_task = None
    g_zero = game.next_game_clock_zero_at_ms()
    if g_zero is not None:
        _game_buzz_task = asyncio.create_task(
            _wait_and_fire("period", g_zero, snap["version"])
        )

    _cancel(_shot_buzz_task)
    _shot_buzz_task = None
    s_zero = game.next_shot_clock_zero_at_ms()
    if s_zero is not None:
        _shot_buzz_task = asyncio.create_task(
            _wait_and_fire("shot_clock", s_zero, snap["version"])
        )

    # Time-out clock: up to three events per run (20s, 10s, end). The 20s
    # warning only fires when the operator has it enabled.
    _cancel(_to_buzz_20_task); _to_buzz_20_task = None
    if snap.get("to_warn_20"):
        t20 = game.next_to_clock_event_at_ms(20)
        if t20 is not None:
            _to_buzz_20_task = asyncio.create_task(
                _wait_and_fire("timeout_warn_20", t20, snap["version"])
            )
    _cancel(_to_buzz_10_task); _to_buzz_10_task = None
    t10 = game.next_to_clock_event_at_ms(10)
    if t10 is not None:
        _to_buzz_10_task = asyncio.create_task(
            _wait_and_fire("timeout_warn_10", t10, snap["version"])
        )
    _cancel(_to_buzz_0_task); _to_buzz_0_task = None
    t0 = game.next_to_clock_event_at_ms(0)
    if t0 is not None:
        _to_buzz_0_task = asyncio.create_task(
            _wait_and_fire("timeout_end", t0, snap["version"])
        )


# --------------------------------------------------------------------------- #
# Command dispatch
# --------------------------------------------------------------------------- #

# Ops that change clock state (running flags or anchor) and therefore need
# the buzzer scheduler re-run. Any flip of game.running, sc_enabled, or
# sc_independent may change the effective running state.
_CLOCK_OPS = {
    "start", "stop", "toggle", "set_time", "adjust_game",
    "sc_enable", "sc_disable", "sc_toggle",
    "sc_independent_on", "sc_independent_off", "sc_toggle_independent",
    "sc_set", "sc_adjust", "sc_reset_24", "sc_reset_14",
    "sc_show", "sc_hide", "sc_toggle_visible",
    "to_start", "to_stop", "to_toggle",
    "to_set", "to_reset", "to_adjust",
    "to_warn_20_on", "to_warn_20_off", "to_toggle_warn_20",
    # Legacy aliases:
    "sc_start", "sc_stop",
}


async def handle_command(op: str, msg: dict[str, Any]) -> None:
    """Apply a control command, reschedule buzzers, broadcast state."""

    # ----- game clock -------------------------------------------------------
    if op == "start":
        game.start()
    elif op == "stop":
        game.stop()
    elif op == "toggle":
        game.toggle()
    elif op == "set_time":
        if "value_ms" in msg:
            value_ms = float(msg["value_ms"])
        else:
            minutes = float(msg.get("minutes", 0))
            seconds = float(msg.get("seconds", 0))
            value_ms = (minutes * 60 + seconds) * 1000
        game.set_game_ms(value_ms)
    elif op == "adjust_game":
        # Signed delta in ms. Used by the timer operator view's +/- buttons.
        game.adjust_game_ms(float(msg.get("delta_ms", 0)))
    elif op == "set_period":
        # 0-based index into the period list. Legacy aliases (`period_index`
        # or `period`) accepted; whichever is provided wins.
        idx = msg.get("period_index", msg.get("period", 0))
        game.set_period_index(int(idx))
    elif op == "adjust_period":
        game.adjust_period(int(msg.get("delta", 0)))
    elif op == "set_periods":
        game.set_periods(list(msg.get("entries") or []))
    elif op == "reset_periods":
        game.reset_periods()

    # ----- shot clock -------------------------------------------------------
    # Operator switch (sc_enabled). Legacy aliases sc_start/sc_stop are kept
    # for any older client tab that hasn't reloaded yet.
    elif op in ("sc_enable", "sc_start"):
        game.sc_set_enabled(True)
    elif op in ("sc_disable", "sc_stop"):
        game.sc_set_enabled(False)
    elif op == "sc_toggle":
        game.sc_toggle_enabled()
    # Independent / force-run mode.
    elif op == "sc_independent_on":
        game.sc_set_independent(True)
    elif op == "sc_independent_off":
        game.sc_set_independent(False)
    elif op == "sc_toggle_independent":
        game.sc_toggle_independent()
    # Value-only adjustments (don't touch the switch).
    elif op == "sc_set":
        if "value_ms" in msg:
            game.sc_set_ms(float(msg["value_ms"]))
        elif "seconds" in msg:
            game.sc_set_ms(float(msg["seconds"]) * 1000)
    elif op == "sc_adjust":
        # Signed delta in ms for the shot-clock operator view's +/- buttons.
        game.adjust_shot_ms(float(msg.get("delta_ms", 0)))
    elif op == "sc_reset_24":
        game.sc_reset_full()
    elif op == "sc_reset_14":
        game.sc_reset_short()
    elif op == "sc_show":
        game.sc_set_visible(True)
    elif op == "sc_hide":
        game.sc_set_visible(False)
    elif op == "sc_toggle_visible":
        game.sc_toggle_visible()

    # ----- time-out (stopwatch) clock --------------------------------------
    elif op == "to_start":
        game.to_start()
    elif op == "to_stop":
        game.to_stop()
    elif op == "to_toggle":
        game.to_toggle()
    elif op == "to_set":
        if "value_ms" in msg:
            game.to_set_ms(float(msg["value_ms"]))
        elif "seconds" in msg:
            game.to_set_ms(float(msg["seconds"]) * 1000)
    elif op == "to_adjust":
        # Signed delta in ms for the timer operator view's +/- on time-out.
        game.to_adjust_ms(float(msg.get("delta_ms", 0)))
    elif op == "to_reset":
        game.to_reset()
    elif op == "to_warn_20_on":
        game.to_set_warn_20(True)
    elif op == "to_warn_20_off":
        game.to_set_warn_20(False)
    elif op == "to_toggle_warn_20":
        game.to_toggle_warn_20()

    # ----- siren ------------------------------------------------------------
    # Held True while the operator is pressing the siren button on the timer
    # operator view; published in the snapshot so an external siren-relay
    # integration can subscribe and drive a physical horn.
    elif op == "siren_on":
        game.siren_set(True)
    elif op == "siren_off":
        game.siren_set(False)
    elif op == "siren_toggle":
        game.siren_toggle()

    # ----- scores -----------------------------------------------------------
    elif op == "add_score":
        team = msg.get("team", "home")
        points = int(msg.get("points", 1))
        pi = msg.get("player_index")
        game.add_score(team, points, player_index=int(pi) if pi is not None else None)
    elif op == "set_score":
        game.set_score(msg.get("team", "home"), int(msg.get("score", 0)))
    elif op == "add_player_points":
        game.add_player_points(msg["team"], int(msg["player_index"]), int(msg.get("points", 1)))

    # ----- fouls ------------------------------------------------------------
    elif op == "add_team_foul":
        game.add_team_foul(msg.get("team", "home"))
    elif op == "set_team_foul":
        game.set_team_foul(msg.get("team", "home"), int(msg.get("value", 0)))
    elif op == "set_bonus":
        game.set_bonus(msg.get("team", "home"), bool(msg.get("on", True)))
    elif op == "reset_team_fouls":
        game.reset_team_fouls()
    elif op == "reset_timeouts":
        game.reset_timeouts()
    elif op == "add_player_foul":
        game.add_player_foul(msg["team"], int(msg["player_index"]))
    elif op == "subtract_player_foul":
        game.subtract_player_foul(msg["team"], int(msg["player_index"]))
    elif op == "set_player_foul":
        game.set_player_foul(msg["team"], int(msg["player_index"]), int(msg.get("value", 0)))
    elif op == "set_player_dq":
        game.set_player_disqualified(msg["team"], int(msg["player_index"]), bool(msg.get("on", True)))
    elif op == "set_player_played":
        game.set_player_played(msg["team"], int(msg["player_index"]), bool(msg.get("on", True)))
    elif op == "add_player":
        game.add_player(
            msg.get("team", "home"),
            number=msg.get("number", ""),
            name=msg.get("name", ""),
        )
    elif op == "remove_player":
        game.remove_player(msg.get("team", "home"), int(msg.get("player_index", -1)))
    elif op == "sort_roster":
        # Reorders the named team's players by jersey number (FIBA order).
        game.sort_roster(msg.get("team", "home"))

    # ----- time-outs --------------------------------------------------------
    elif op == "add_timeout":
        game.add_timeout(msg.get("team", "home"))
    elif op == "set_timeouts":
        game.set_timeouts(msg.get("team", "home"), int(msg.get("taken", 0)))
    elif op == "set_timeout_settings":
        game.set_timeout_settings(
            mode=msg.get("mode"),
            max_count=int(msg["max_count"]) if "max_count" in msg else None,
        )

    # ----- possession -------------------------------------------------------
    elif op == "set_possession":
        # Accept either "direction" or legacy "who"; normalize.
        d = msg.get("direction", msg.get("who", "off"))
        game.set_possession(d)
    elif op == "toggle_possession":
        game.toggle_possession()

    # ----- roster -----------------------------------------------------------
    elif op == "set_player":
        game.set_player(
            msg["team"], int(msg["player_index"]),
            number=msg.get("number"),
            name=msg.get("name"),
        )
    elif op == "set_team_meta":
        game.set_team_meta(msg["team"], name=msg.get("name"), short=msg.get("short"))

    else:
        log.warning("unknown op: %s", op)
        return

    if op in _CLOCK_OPS:
        reschedule_buzzers()

    await broadcast_state()


# --------------------------------------------------------------------------- #
# HTTP routes
# --------------------------------------------------------------------------- #

# The HTML/JS we serve changes constantly during development. Tell the
# browser not to cache it so a refresh always picks up the latest version.
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/")
async def control_page() -> FileResponse:
    return FileResponse(BASE_DIR / "control.html", headers=_NO_CACHE)


@app.get("/scoreboard")
async def scoreboard_page() -> FileResponse:
    return FileResponse(BASE_DIR / "scoreboard.html", headers=_NO_CACHE)


@app.get("/clock")
async def clock_page() -> FileResponse:
    return FileResponse(BASE_DIR / "clock.html", headers=_NO_CACHE)


@app.get("/shot")
async def shot_page() -> FileResponse:
    return FileResponse(BASE_DIR / "shot.html", headers=_NO_CACHE)


@app.get("/timer")
async def timer_op_page() -> FileResponse:
    """Dedicated game-clock operator console (touch-friendly).

    Big game/shot/time-out readouts on top, large run indicators and a
    siren button. Buttons live in the bottom half for thumb-reach. Sends
    'start', 'stop', 'adjust_game', 'set_time', 'siren_on'/'siren_off',
    and all time-out clock ops. Read-only for shot-clock state."""
    return FileResponse(BASE_DIR / "timer.html", headers=_NO_CACHE)


@app.get("/shotclock-op")
async def shotclock_op_page() -> FileResponse:
    """Dedicated shot-clock operator console (touch-friendly).

    Big shot-clock + game-clock readouts on top, large run indicators.
    Separate start/pause for the shot clock, 24/14 resets, +/- adjust,
    independent and hide toggles. Read-only for game-clock state."""
    return FileResponse(BASE_DIR / "shotclock-op.html", headers=_NO_CACHE)


@app.get("/possession")
async def possession_page() -> FileResponse:
    return FileResponse(BASE_DIR / "possession.html", headers=_NO_CACHE)


@app.get("/arrow")
async def arrow_op_page() -> FileResponse:
    """Possession-arrow operator console (touch-friendly).

    Big arrow indicator on top, large LEFT / OFF / RIGHT buttons and a
    swap button below. Sends set_possession / toggle_possession. The
    operator sees the raw server direction (no per-device invert)."""
    return FileResponse(BASE_DIR / "arrow.html", headers=_NO_CACHE)


@app.get("/visuals")
async def visuals_op_page() -> FileResponse:
    """Scorer's table console: period, scores, team fouls + bonus,
    time-outs, and per-player points/fouls. NO clock controls (those
    live on /timer and /shotclock-op)."""
    return FileResponse(BASE_DIR / "visuals.html", headers=_NO_CACHE)


@app.get("/fouls")
async def fouls_page(request: Request) -> FileResponse:
    # The page itself reads ?team= from window.location; we just serve the file.
    return FileResponse(BASE_DIR / "fouls.html", headers=_NO_CACHE)


@app.get("/players")
async def players_page(request: Request) -> FileResponse:
    # Standalone single-team roster view. Reads ?team= and the display
    # toggles from window.location.
    return FileResponse(BASE_DIR / "players.html", headers=_NO_CACHE)


@app.get("/timeout")
async def timeout_page() -> FileResponse:
    # Dedicated full-screen time-out countdown view.
    return FileResponse(BASE_DIR / "timeout.html", headers=_NO_CACHE)


@app.get("/common.js")
async def common_js() -> FileResponse:
    return FileResponse(BASE_DIR / "common.js",
                        media_type="application/javascript",
                        headers=_NO_CACHE)


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(game.snapshot())


# --------------------------------------------------------------------------- #
# Roster import (Sportradar / BasketballVictoria fixture detail)
# --------------------------------------------------------------------------- #

# Each league sits behind a different Sportradar embed-API tenant: BV uses
# embed/2 with a sub=statistics query param; NBL1 uses embed/3 without it.
# The CDN also gates on Referer/Origin matching the public site.
LEAGUE_CONFIGS: dict[str, dict[str, str]] = {
    "bv": {
        "url": ("https://embed-api.eui.connect.sportradar.com/v1/embed/2/"
                "fixture_detail?sub=statistics&fixtureId={fid}"),
        "referer": "https://www.basketballvictoria.com.au/",
        "origin":  "https://www.basketballvictoria.com.au",
    },
    "nbl1": {
        "url": ("https://embed-api.eui.connect.sportradar.com/v1/embed/3/"
                "fixture_detail?fixtureId={fid}"),
        "referer": "https://www.nbl1.com.au/",
        "origin":  "https://www.nbl1.com.au",
    },
}


def _fetch_fixture_sync(fixture_id: str, league: str = "bv") -> dict[str, Any]:
    """Blocking fetch + decode of the Sportradar embed-api fixture detail.
    Called via asyncio.to_thread so we don't block the event loop."""
    import urllib.request, urllib.error, gzip, zlib
    cfg = LEAGUE_CONFIGS.get(league) or LEAGUE_CONFIGS["bv"]
    url = cfg["url"].format(fid=fixture_id)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0",
        "Accept": "*/*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": cfg["referer"],
        "Origin":  cfg["origin"],
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        enc = resp.headers.get("Content-Encoding", "").lower()
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw)
    return json.loads(raw)


def _parse_fixture(data: dict[str, Any]) -> dict[str, Any]:
    """Pull out only team names + player numbers/names from the API blob.
    Per user spec we deliberately ignore points and fouls."""
    fixture = data.get("data", {}).get("banner", {}).get("fixture", {}) or {}
    competitors = fixture.get("competitors", []) or []
    home_info = next((c for c in competitors if c.get("isHome")), None)
    away_info = next((c for c in competitors if c.get("isHome") is False), None)

    stats_base = (data.get("data", {})
                      .get("statistics", {})
                      .get("data", {})
                      .get("base", {})) or {}

    def _persons(side: str) -> list[dict]:
        block = stats_base.get(side, {}) or {}
        persons = block.get("persons") or []
        if not persons:
            return []
        rows = persons[0].get("rows", []) or []
        out = []
        for r in rows:
            number = str(r.get("bib") or "").strip()
            name = (r.get("personName") or "").strip()
            if not name and not number:
                continue
            # Only names + numbers per user spec. Default played=False --
            # operator ticks the box (or records a stat) as each player
            # actually takes the court.
            out.append({"number": number, "name": name, "played": False})
        return out

    return {
        "home": {
            "name":  (home_info or {}).get("name") or "HOME",
            "players": _persons("home"),
        },
        "away": {
            "name":  (away_info or {}).get("name") or "AWAY",
            "players": _persons("away"),
        },
    }


@app.post("/api/import-roster")
async def api_import_roster(body: dict[str, Any]) -> dict[str, Any]:
    fixture_id = (body.get("fixtureId") or "").strip()
    league = (body.get("league") or "bv").strip().lower()
    swap = bool(body.get("swap", False))
    if not fixture_id:
        raise HTTPException(status_code=400, detail="fixtureId is required")
    if league not in LEAGUE_CONFIGS:
        raise HTTPException(status_code=400,
                            detail=f"unknown league '{league}'; expected one of {list(LEAGUE_CONFIGS)}")

    try:
        data = await asyncio.to_thread(_fetch_fixture_sync, fixture_id, league)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"upstream fetch failed: {e}")

    try:
        parsed = _parse_fixture(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"parse failed: {e}")

    a, b = parsed["home"], parsed["away"]
    if swap:
        a, b = b, a

    game.import_roster("home", name=a["name"], players=a["players"])
    game.import_roster("away", name=b["name"], players=b["players"])
    await broadcast_state()
    return {
        "ok": True,
        "home": {"name": a["name"], "count": len(a["players"])},
        "away": {"name": b["name"], "count": len(b["players"])},
    }


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await hub.join(ws)
    log.info("ws connected: %s", ws.client)
    try:
        await ws.send_text(json.dumps(
            {"type": "state", "state": game.snapshot()},
            separators=(",", ":"),
        ))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("bad json: %s", raw[:120])
                continue

            mtype = msg.get("type")
            if mtype == "ping":
                # Respond ASAP to keep RTT honest.
                await ws.send_text(json.dumps({
                    "type": "pong",
                    "client_t": msg.get("client_t"),
                    "server_now_ms": server_now_ms(),
                }, separators=(",", ":")))
            elif mtype == "cmd":
                await handle_command(msg.get("op", ""), msg)
            else:
                log.warning("unknown message type: %s", mtype)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws error")
    finally:
        await hub.leave(ws)
        log.info("ws closed: %s", ws.client)


# Schedule any buzzers on startup (in case state begins running, e.g. from a
# future persistence layer).
@app.on_event("startup")
async def _on_startup() -> None:
    reschedule_buzzers()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
