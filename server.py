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
- GET /common.css    -> shared client CSS
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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

import config as cfg_module
import roster_import
from state import GameState, server_now_ms

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scoreboard")

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    buzzer.reschedule()
    yield


app = FastAPI(title="bball-clock-scoreboard", lifespan=lifespan)
game = GameState()
_cfg = cfg_module.load()
game.apply_config_defaults(_cfg)


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
# Buzzer scheduler
# --------------------------------------------------------------------------- #
#
# When a clock is running, we schedule an asyncio task that wakes at the
# expected zero crossing. On wake-up we recompute — if state changed since
# (different version, no longer running, or value no longer ~0) we exit.
# Otherwise we fire the buzzer broadcast.
#
# Every command that touches a clock calls buzzer.reschedule() afterwards.

async def _wait_and_fire(kind: str, fire_at_ms: float, captured_version: int) -> None:
    """Sleep until fire_at_ms (server time), then fire if state hasn't moved."""
    try:
        delay_ms = fire_at_ms - server_now_ms()
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

        snap = game.snapshot()
        if snap["version"] != captured_version:
            return

        if kind == "period":
            if not snap["running"]:
                return
            remaining = max(0.0, snap["anchor_value_ms"]
                            - (snap["server_now_ms"] - snap["anchor_server_ms"]))
            if remaining > 50:
                return
            game.stop()
            await broadcast_state()
        elif kind == "shot_clock":
            if not snap["sc_running"]:
                return
            remaining = max(0.0, snap["sc_anchor_value_ms"]
                            - (snap["server_now_ms"] - snap["sc_anchor_server_ms"]))
            if remaining > 50:
                return
            game.sc_set_ms(0)
            await broadcast_state()
        elif kind in ("timeout_warn_20", "timeout_warn_10"):
            if not snap["to_running"]:
                return
        elif kind == "timeout_end":
            if not snap["to_running"]:
                return
            remaining = max(0.0, snap["to_anchor_value_ms"]
                            - (snap["server_now_ms"] - snap["to_anchor_server_ms"]))
            if remaining > 50:
                return
            game.to_stop()
            game.to_set_ms(0)
            await broadcast_state()

        await hub.broadcast({
            "type": "buzzer",
            "kind": kind,
            "server_now_ms": server_now_ms(),
        })
    except asyncio.CancelledError:
        raise


class BuzzerScheduler:
    _KINDS = ("period", "shot_clock", "timeout_warn_20", "timeout_warn_10", "timeout_end")

    def __init__(self) -> None:
        self._tasks: dict[str, Optional[asyncio.Task[None]]] = {k: None for k in self._KINDS}

    def _cancel(self, kind: str) -> None:
        task = self._tasks.get(kind)
        if task and not task.done():
            task.cancel()
        self._tasks[kind] = None

    def _schedule(self, kind: str, fire_at_ms: Optional[float], version: int) -> None:
        self._cancel(kind)
        if fire_at_ms is not None:
            self._tasks[kind] = asyncio.create_task(
                _wait_and_fire(kind, fire_at_ms, version)
            )

    def reschedule(self) -> None:
        """Cancel and (re)schedule every buzzer task based on current state."""
        snap = game.snapshot()
        v = snap["version"]

        self._schedule("period",     game.next_game_clock_zero_at_ms(), v)
        self._schedule("shot_clock", game.next_shot_clock_zero_at_ms(), v)

        t20 = game.next_to_clock_event_at_ms(20) if snap.get("to_warn_20") else None
        self._schedule("timeout_warn_20", t20, v)
        self._schedule("timeout_warn_10", game.next_to_clock_event_at_ms(10), v)
        self._schedule("timeout_end",     game.next_to_clock_event_at_ms(0),  v)


buzzer = BuzzerScheduler()


# --------------------------------------------------------------------------- #
# Command dispatch
# --------------------------------------------------------------------------- #

def _cmd_reset_game(msg: dict) -> None:
    game.reset_to_defaults()
    game.apply_config_defaults(_cfg)

def _cmd_set_time(msg: dict) -> None:
    if "value_ms" in msg:
        game.set_game_ms(float(msg["value_ms"]))
    else:
        minutes = float(msg.get("minutes", 0))
        seconds = float(msg.get("seconds", 0))
        game.set_game_ms((minutes * 60 + seconds) * 1000)

def _cmd_sc_set(msg: dict) -> None:
    if "value_ms" in msg:
        game.sc_set_ms(float(msg["value_ms"]))
    elif "seconds" in msg:
        game.sc_set_ms(float(msg["seconds"]) * 1000)

def _cmd_to_set(msg: dict) -> None:
    if "value_ms" in msg:
        game.to_set_ms(float(msg["value_ms"]))
    elif "seconds" in msg:
        game.to_set_ms(float(msg["seconds"]) * 1000)

def _cmd_add_score(msg: dict) -> None:
    pi = msg.get("player_index")
    game.add_score(msg.get("team", "home"), int(msg.get("points", 1)),
                   player_index=int(pi) if pi is not None else None)

def _cmd_set_timeout_settings(msg: dict) -> None:
    game.set_timeout_settings(
        mode=msg.get("mode"),
        max_count=int(msg["max_count"]) if "max_count" in msg else None,
    )

def _cmd_set_possession(msg: dict) -> None:
    game.set_possession(msg.get("direction", msg.get("who", "off")))


_CMD: dict[str, Callable[[dict], None]] = {
    # game reset
    "reset_game":           _cmd_reset_game,
    # game clock
    "start":                lambda m: game.start(),
    "stop":                 lambda m: game.stop(),
    "toggle":               lambda m: game.toggle(),
    "set_time":             _cmd_set_time,
    "adjust_game":          lambda m: game.adjust_game_ms(float(m.get("delta_ms", 0))),
    "set_period":           lambda m: game.set_period_index(int(m.get("period_index", m.get("period", 0)))),
    "adjust_period":        lambda m: game.adjust_period(int(m.get("delta", 0))),
    "set_periods":          lambda m: game.set_periods(list(m.get("entries") or [])),
    "reset_periods":        lambda m: game.reset_periods(),
    # shot clock
    "sc_enable":            lambda m: game.sc_set_enabled(True),
    "sc_start":             lambda m: game.sc_set_enabled(True),    # legacy alias
    "sc_disable":           lambda m: game.sc_set_enabled(False),
    "sc_stop":              lambda m: game.sc_set_enabled(False),   # legacy alias
    "sc_toggle":            lambda m: game.sc_toggle_enabled(),
    "sc_independent_on":    lambda m: game.sc_set_independent(True),
    "sc_independent_off":   lambda m: game.sc_set_independent(False),
    "sc_toggle_independent":lambda m: game.sc_toggle_independent(),
    "sc_set":               _cmd_sc_set,
    "sc_adjust":            lambda m: game.adjust_shot_ms(float(m.get("delta_ms", 0))),
    "sc_reset_24":          lambda m: game.sc_reset_full(),
    "sc_reset_14":          lambda m: game.sc_reset_short(),
    "sc_show":              lambda m: game.sc_set_visible(True),
    "sc_hide":              lambda m: game.sc_set_visible(False),
    "sc_toggle_visible":    lambda m: game.sc_toggle_visible(),
    # time-out clock
    "to_start":             lambda m: game.to_start(),
    "to_stop":              lambda m: game.to_stop(),
    "to_toggle":            lambda m: game.to_toggle(),
    "to_set":               _cmd_to_set,
    "to_adjust":            lambda m: game.to_adjust_ms(float(m.get("delta_ms", 0))),
    "to_reset":             lambda m: game.to_reset(),
    "to_warn_20_on":        lambda m: game.to_set_warn_20(True),
    "to_warn_20_off":       lambda m: game.to_set_warn_20(False),
    "to_toggle_warn_20":    lambda m: game.to_toggle_warn_20(),
    # siren
    "siren_on":             lambda m: game.siren_set(True),
    "siren_off":            lambda m: game.siren_set(False),
    "siren_toggle":         lambda m: game.siren_toggle(),
    # scores
    "add_score":            _cmd_add_score,
    "set_score":            lambda m: game.set_score(m.get("team", "home"), int(m.get("score", 0))),
    "add_player_points":    lambda m: game.add_player_points(m["team"], int(m["player_index"]), int(m.get("points", 1))),
    # fouls
    "add_team_foul":        lambda m: game.add_team_foul(m.get("team", "home")),
    "set_team_foul":        lambda m: game.set_team_foul(m.get("team", "home"), int(m.get("value", 0))),
    "set_bonus":            lambda m: game.set_bonus(m.get("team", "home"), bool(m.get("on", True))),
    "reset_team_fouls":     lambda m: game.reset_team_fouls(),
    "reset_timeouts":       lambda m: game.reset_timeouts(),
    "add_player_foul":      lambda m: game.add_player_foul(m["team"], int(m["player_index"])),
    "subtract_player_foul": lambda m: game.subtract_player_foul(m["team"], int(m["player_index"])),
    "set_player_foul":      lambda m: game.set_player_foul(m["team"], int(m["player_index"]), int(m.get("value", 0))),
    "set_player_dq":        lambda m: game.set_player_disqualified(m["team"], int(m["player_index"]), bool(m.get("on", True))),
    "set_player_played":    lambda m: game.set_player_played(m["team"], int(m["player_index"]), bool(m.get("on", True))),
    "add_player":           lambda m: game.add_player(m.get("team", "home"), number=m.get("number", ""), name=m.get("name", "")),
    "remove_player":        lambda m: game.remove_player(m.get("team", "home"), int(m.get("player_index", -1))),
    "sort_roster":          lambda m: game.sort_roster(m.get("team", "home")),
    # time-outs
    "add_timeout":          lambda m: game.add_timeout(m.get("team", "home")),
    "set_timeouts":         lambda m: game.set_timeouts(m.get("team", "home"), int(m.get("taken", 0))),
    "set_timeout_settings": _cmd_set_timeout_settings,
    # possession
    "set_possession":       _cmd_set_possession,
    "toggle_possession":    lambda m: game.toggle_possession(),
    # roster editing
    "set_player":           lambda m: game.set_player(m["team"], int(m["player_index"]), number=m.get("number"), name=m.get("name")),
    "set_team_meta":        lambda m: game.set_team_meta(m["team"], name=m.get("name"), short=m.get("short")),
}

# Ops that touch a clock anchor or running flag — buzzer must be rescheduled.
_CLOCK_OPS = {
    "reset_game",
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
    handler = _CMD.get(op)
    if handler is None:
        log.warning("unknown op: %s", op)
        return
    handler(msg)
    if op in _CLOCK_OPS:
        buzzer.reschedule()
    await broadcast_state()


# --------------------------------------------------------------------------- #
# HTTP routes — static pages
# --------------------------------------------------------------------------- #

_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

_PAGES: dict[str, str] = {
    "/":             "control.html",
    "/scoreboard":   "scoreboard.html",
    "/clock":        "clock.html",
    "/shot":         "shot.html",
    "/timer":        "timer.html",
    "/shotclock-op": "shotclock-op.html",
    "/possession":   "possession.html",
    "/arrow":        "arrow.html",
    "/master":       "master.html",
    "/visuals":      "visuals.html",
    "/fouls":        "fouls.html",
    "/players":      "players.html",
    "/timeout":      "timeout.html",
    "/shortcuts":    "shortcuts.html",
}


def _make_page_handler(filename: str):
    async def handler() -> FileResponse:
        return FileResponse(BASE_DIR / filename, headers=_NO_CACHE)
    handler.__name__ = filename.replace(".html", "_page").replace("-", "_")
    return handler


for _path, _fname in _PAGES.items():
    app.get(_path)(_make_page_handler(_fname))


@app.get("/common.js")
async def common_js() -> FileResponse:
    return FileResponse(BASE_DIR / "common.js",
                        media_type="application/javascript",
                        headers=_NO_CACHE)


@app.get("/common.css")
async def common_css() -> FileResponse:
    return FileResponse(BASE_DIR / "common.css",
                        media_type="text/css",
                        headers=_NO_CACHE)


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(game.snapshot())


# --------------------------------------------------------------------------- #
# Keyboard shortcuts config
# --------------------------------------------------------------------------- #

_SHORTCUTS_PATH = BASE_DIR / "shortcuts.json"
_DEFAULT_SHORTCUTS: dict[str, str] = {
    "start_game":       "a",
    "stop_game":        "q",
    "siren":            "w",
    "sc_enable":        "f",
    "sc_disable":       "r",
    "sc_reset_14":      "t",
    "sc_reset_24":      "g",
    "sc_hide":          "y",
    "possession_left":  "b",
    "possession_right": "m",
    "possession_off":   "n",
}


def _load_shortcuts() -> dict[str, str]:
    if _SHORTCUTS_PATH.exists():
        try:
            return json.loads(_SHORTCUTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(_DEFAULT_SHORTCUTS)


@app.get("/api/shortcuts")
async def api_get_shortcuts() -> JSONResponse:
    return JSONResponse(_load_shortcuts())


@app.post("/api/shortcuts")
async def api_save_shortcuts(body: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(body, indent=2, ensure_ascii=False)
    await asyncio.to_thread(_SHORTCUTS_PATH.write_text, text, "utf-8")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Roster import (Sportradar / BasketballVictoria fixture detail)
# --------------------------------------------------------------------------- #

@app.post("/api/import-roster")
async def api_import_roster(body: dict[str, Any]) -> dict[str, Any]:
    fixture_id = (body.get("fixtureId") or "").strip()
    league = (body.get("league") or "bv").strip().lower()
    swap = bool(body.get("swap", False))
    if not fixture_id:
        raise HTTPException(status_code=400, detail="fixtureId is required")
    if league not in roster_import.LEAGUE_CONFIGS:
        raise HTTPException(status_code=400,
                            detail=f"unknown league '{league}'; expected one of {list(roster_import.LEAGUE_CONFIGS)}")

    try:
        data = await asyncio.to_thread(roster_import.fetch_fixture, fixture_id, league)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"upstream fetch failed: {e}")

    try:
        parsed = roster_import.parse_fixture(data)
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
# Config persistence
# --------------------------------------------------------------------------- #

@app.get("/api/config")
async def api_get_config() -> JSONResponse:
    return JSONResponse(_cfg)


@app.post("/api/save-config")
async def api_save_config() -> dict[str, Any]:
    global _cfg
    snap = game.snapshot()
    new_cfg = {
        "periods":       snap["periods"],
        "timeout_mode":  snap["timeout_mode"],
        "timeout_max":   snap["timeout_max"],
        "clock_presets": _cfg.get("clock_presets", cfg_module._DEFAULTS["clock_presets"]),
    }
    await asyncio.to_thread(cfg_module.save, new_cfg)
    _cfg = new_cfg
    return {"ok": True}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
