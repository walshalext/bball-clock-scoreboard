"""Game state for the basketball scoreboard.

Built to cover the software-relevant portions of the FIBA Official Basketball
Rules 2024 -- Equipment appendix (sections 3 Scoreboard, 4 Game clock,
5 Shot clock, 6 Signals, 7 Player foul / GD markers, 8 Team foul markers,
9 Alternating possession arrow). Physical-equipment sections (backboards,
balls, padding, flooring, lighting, whistles) are out of scope.

Clock model
-----------
Both the game clock and the shot clock use a *server-authoritative anchor*:

    running:           bool
    anchor_server_ms:  float    server monotonic time when this state began
    anchor_value_ms:   float    clock value (counting down) at that moment

The current clock value at server time ``now_ms`` is::

    if running: max(0, anchor_value_ms - (now_ms - anchor_server_ms))
    else:       anchor_value_ms

Clients receive the anchor on every change and tick locally at 60fps, so
network RTT is not in the perceived-latency budget for the displayed time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #

def server_now_ms() -> float:
    """Monotonic server clock in milliseconds. Used for all anchors."""
    return time.monotonic_ns() / 1_000_000.0


# --------------------------------------------------------------------------- #
# Roster
# --------------------------------------------------------------------------- #

@dataclass
class Player:
    """A single roster slot.

    Numbers are strings so we preserve "00" as distinct from "0", which is the
    FIBA-prescribed numbering order (00, 0, 1, 2, ..., 99).

    `played` flips to True when the player first takes the court (the
    operator ticks the box or records a positive stat). While False, the
    player's fouls/points render as blank instead of "0" so the rest of
    the roster doesn't look like everyone played and scored zero.
    """
    number: str = ""
    name: str = ""
    fouls: int = 0
    points: int = 0
    disqualified: bool = False   # GD marker (FIBA §7)
    played: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "name": self.name,
            "fouls": self.fouls,
            "points": self.points,
            "disqualified": self.disqualified,
            "played": self.played,
        }


def _default_roster(prefix: str) -> list[Player]:
    """Twelve placeholder slots; the operator fills in real numbers/names."""
    out: list[Player] = []
    suggested = ["4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"]
    for i, n in enumerate(suggested):
        out.append(Player(number=n, name=f"{prefix}{i+1}"))
    return out


def _player_sort_key(number: str) -> tuple[int, int, str]:
    """FIBA jersey-number ordering used by sort_roster.

    Buckets:
      0  -> "00"               (sorts first; FIBA-allowed pre-zero)
      1  -> "0", "1", ..., "99" sorted by integer value
      2  -> blank or non-numeric (sorts last)

    Python's sort is stable, so two players sharing the same number keep
    their original relative order.
    """
    s = (number or "").strip()
    if s == "00":
        return (0, -1, s)
    if not s:
        return (2, 0, s)
    try:
        return (1, int(s), s)
    except ValueError:
        return (2, 1, s)


# --------------------------------------------------------------------------- #
# Team
# --------------------------------------------------------------------------- #

@dataclass
class Team:
    name: str = "HOME"
    short: str = "HOM"            # 3-letter abbreviation per FIBA §3 (≥3 chars)
    score: int = 0
    team_fouls: int = 0           # resets each period
    timeouts_taken: int = 0       # cumulative this game
    bonus: bool = False           # red square (FIBA §3, §8). Auto-set when
                                  # team_fouls >= 5 OR manually by the operator
                                  # to honor "ball live after 4th team foul".
    players: list[Player] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "short": self.short,
            "score": self.score,
            "team_fouls": self.team_fouls,
            "timeouts_taken": self.timeouts_taken,
            "bonus": self.bonus,
            "players": [p.to_dict() for p in self.players],
        }


# --------------------------------------------------------------------------- #
# Period list
# --------------------------------------------------------------------------- #

@dataclass
class PeriodEntry:
    """One row in the customizable period list.

    Three fields, each for a different audience:

      - `name`      Full human label: "First Quarter", "Half Time", "1OT".
                    Used on the operator panel and as a tooltip / subtitle.
      - `short`     2-4 character label intended for a computer-based
                    scoreboard: "Q1", "HALF", "OT1". This is what the main
                    scoreboard view shows in its period box.
      - `indicator` Single-digit number (0-9) for old-style 7-segment
                    displays where only one character fits. Typically the
                    quarter number, with break periods sharing the
                    indicator of the preceding quarter.
    """
    name: str
    short: str
    indicator: int

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "short": self.short, "indicator": self.indicator}


def _default_periods() -> list[PeriodEntry]:
    """Standard FIBA / NBL-style game structure with mid-quarter breaks."""
    return [
        PeriodEntry("Pre Game",            "PRE",   0),
        PeriodEntry("First Quarter",       "Q1",    1),
        PeriodEntry("First Quarter Time",  "1QT",   1),
        PeriodEntry("Second Quarter",      "Q2",    2),
        PeriodEntry("Half Time",           "HLFTM", 2),
        PeriodEntry("Third Quarter",       "Q3",    3),
        PeriodEntry("Third Quarter Time",  "3QT",   3),
        PeriodEntry("Fourth Quarter",      "Q4",    4),
        PeriodEntry("Fourth Quarter Time", "4QT",   4),
        PeriodEntry("1st Overtime",        "1OT",   5),
        PeriodEntry("2nd Overtime",        "2OT",   6),
        PeriodEntry("3rd Overtime",        "3OT",   7),
        PeriodEntry("4th Overtime",        "4OT",   8),
        PeriodEntry("5th Overtime",        "5OT",   9),
    ]


# --------------------------------------------------------------------------- #
# Game state
# --------------------------------------------------------------------------- #

# Default starting game clock value (operator changes it manually via
# presets). The state machine does NOT auto-set the clock on period change.
DEFAULT_CLOCK_MS = 10 * 60 * 1000
SHOT_CLOCK_FULL_MS = 24 * 1000
SHOT_CLOCK_RESET_MS = 14 * 1000
DEFAULT_TIMEOUT_MS = 60 * 1000        # 1:00 default for the time-out clock


@dataclass
class GameState:
    # ---- period list -------------------------------------------------------
    # Editable list of named periods + their single-digit indicators.
    # +/- Period just moves period_index up and down this list; no other
    # side effects (the clock is set manually via presets).
    periods: list[PeriodEntry] = field(default_factory=_default_periods)
    period_index: int = 0                 # 0-based into `periods`

    # ---- game clock --------------------------------------------------------
    running: bool = False
    anchor_server_ms: float = 0.0
    anchor_value_ms: float = DEFAULT_CLOCK_MS

    # ---- shot clock --------------------------------------------------------
    # Operator-controlled switches (never set automatically -- think of them
    # as physical switches on the scorer's table console). The shot clock
    # counts down only when sc_visible AND sc_enabled AND
    # (game.running OR sc_independent). "Independent" force-runs the shot
    # clock regardless of the game clock (rarely used, intentionally a
    # separate toggle). Hiding the clock (sc_visible=False) pauses it
    # without touching the operator's sc_enabled switch -- when re-shown it
    # resumes from the value it had when hidden.
    sc_enabled: bool = False
    sc_independent: bool = False
    sc_anchor_server_ms: float = 0.0
    sc_anchor_value_ms: float = SHOT_CLOCK_FULL_MS
    sc_visible: bool = True               # FIBA §5: clock can show no display

    # ---- time-out (stopwatch) clock ---------------------------------------
    # Independent countdown the scorer uses to time time-outs. Defaults to
    # 1:00, set higher/lower manually. The server fires three buzzer events
    # while it runs (20s warn — only when to_warn_20 is True; 10s warn;
    # end). Auto-stops at zero.
    to_running: bool = False
    to_anchor_server_ms: float = 0.0
    to_anchor_value_ms: float = DEFAULT_TIMEOUT_MS
    to_warn_20: bool = False              # operator-toggleable 20s warning

    # ---- siren -------------------------------------------------------------
    # Externally-controllable horn / siren flag. Held True while the operator
    # is pressing the siren button (momentary). No state machine relies on
    # this -- it's a transparent passthrough that a future external siren
    # integration can read off the websocket to drive a physical horn relay.
    siren_active: bool = False

    # ---- possession --------------------------------------------------------
    # Direction-only (decoupled from teams) per operator preference. The
    # possession-arrow display has its own per-device "invert" toggle so a
    # screen on the opposite side of the court can mirror the value
    # automatically.
    possession: str = "off"               # "left" | "right" | "off"

    # ---- time-out display settings ----------------------------------------
    # FIBA §3 specifies "charged time-outs from 0 to 3" remaining, but the
    # exact display style varies by league and operator preference. These
    # two settings give the operator direct control:
    #   timeout_mode: "remaining" -> dots filled = timeout_max - taken
    #                 "taken"     -> dots filled = taken
    #   timeout_max:  total dots shown (typically 2 or 3)
    timeout_mode: str = "remaining"       # "remaining" | "taken"
    timeout_max: int = 3

    # ---- teams -------------------------------------------------------------
    home: Team = field(default_factory=lambda: Team(name="HOME", short="HOM",
                                                    players=_default_roster("H")))
    away: Team = field(default_factory=lambda: Team(name="AWAY", short="AWY",
                                                    players=_default_roster("A")))

    # ---- bookkeeping -------------------------------------------------------
    # Monotonic version bumped on every state change; lets clients discard
    # stale snapshots if they ever arrive out of order. Also used by the
    # buzzer scheduler to detect when a pending fire is stale.
    version: int = 0

    _lock: RLock = field(default_factory=RLock, repr=False, compare=False)

    # ----------------- lifecycle ------------------------------------------- #

    def __post_init__(self) -> None:
        now = server_now_ms()
        self.anchor_server_ms = now
        self.anchor_value_ms = float(DEFAULT_CLOCK_MS)
        self.sc_anchor_server_ms = now
        self.sc_anchor_value_ms = float(SHOT_CLOCK_FULL_MS)
        self.to_anchor_server_ms = now
        self.to_anchor_value_ms = float(DEFAULT_TIMEOUT_MS)

    def reset_to_defaults(self) -> None:
        """Return the whole game to a blank-slate default setup.

        This is intentionally broader than the period/halftime reset helpers:
        it stops every clock, clears scores/fouls/time-outs/possession, restores
        default teams/rosters/periods/settings, and bumps the state once.
        """
        with self._lock:
            now = server_now_ms()

            self.periods = _default_periods()
            self.period_index = 0

            self.running = False
            self.anchor_server_ms = now
            self.anchor_value_ms = float(DEFAULT_CLOCK_MS)

            self.sc_enabled = False
            self.sc_independent = False
            self.sc_anchor_server_ms = now
            self.sc_anchor_value_ms = float(SHOT_CLOCK_FULL_MS)
            self.sc_visible = True

            self.to_running = False
            self.to_anchor_server_ms = now
            self.to_anchor_value_ms = float(DEFAULT_TIMEOUT_MS)
            self.to_warn_20 = False

            self.siren_active = False
            self.possession = "off"
            self.timeout_mode = "remaining"
            self.timeout_max = 3

            self.home = Team(name="HOME", short="HOM",
                             players=_default_roster("H"))
            self.away = Team(name="AWAY", short="AWY",
                             players=_default_roster("A"))

            self._bump()

    # ----------------- helpers --------------------------------------------- #

    def _current_game_ms(self, now: float) -> float:
        if self.running:
            return max(0.0, self.anchor_value_ms - (now - self.anchor_server_ms))
        return self.anchor_value_ms

    def _sc_effectively_running(self) -> bool:
        """The shot clock is physically counting down only when:
          - the operator has it switched on (sc_enabled), AND
          - the display is unhidden (sc_visible; hiding pauses it), AND
          - either the game clock is running or independent-run mode is on.
        """
        return (self.sc_visible
                and self.sc_enabled
                and (self.running or self.sc_independent))

    def _current_shot_ms(self, now: float) -> float:
        if self._sc_effectively_running():
            return max(0.0, self.sc_anchor_value_ms - (now - self.sc_anchor_server_ms))
        return self.sc_anchor_value_ms

    def _reseat_shot_anchor(self, now: float) -> None:
        """Capture the shot clock's current value into the anchor at `now`.
        Call this BEFORE mutating anything that could flip the effective
        running state (game.running, sc_enabled, sc_independent) so the
        new anchor describes the value at the moment of transition."""
        self.sc_anchor_value_ms = self._current_shot_ms(now)
        self.sc_anchor_server_ms = now

    def _bump(self) -> int:
        self.version += 1
        return self.version

    def _current_period(self) -> PeriodEntry:
        if not self.periods:
            return PeriodEntry("", 0)
        idx = max(0, min(self.period_index, len(self.periods) - 1))
        return self.periods[idx]

    # ----------------- game clock ------------------------------------------ #

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            now = server_now_ms()
            v = self._current_game_ms(now)
            if v <= 0:
                # Don't start a zeroed clock.
                return
            # Capture the shot clock's current value into its anchor BEFORE
            # toggling game.running, so its effective-running transition is
            # represented correctly.
            self._reseat_shot_anchor(now)
            self.running = True
            self.anchor_server_ms = now
            self.anchor_value_ms = v
            self._bump()

    def stop(self) -> None:
        with self._lock:
            if not self.running:
                return
            now = server_now_ms()
            # Reseat the shot anchor before changing game.running. The shot
            # clock's effective running state may flip to False as a result;
            # the new model leaves the operator's sc_enabled switch alone.
            self._reseat_shot_anchor(now)
            self.anchor_value_ms = self._current_game_ms(now)
            self.anchor_server_ms = now
            self.running = False
            self._bump()

    def toggle(self) -> None:
        if self.running:
            self.stop()
        else:
            self.start()

    def set_game_ms(self, value_ms: float) -> None:
        with self._lock:
            now = server_now_ms()
            self.anchor_server_ms = now
            self.anchor_value_ms = max(0.0, float(value_ms))
            self._bump()

    def adjust_game_ms(self, delta_ms: float) -> None:
        """Bump the game clock by a signed delta. Safe while running -- we
        recompute the displayed value at `now`, apply the delta, then
        reseat the anchor. Reseats the shot anchor too so its prediction
        stays consistent. Clamped to >= 0."""
        with self._lock:
            now = server_now_ms()
            current = self._current_game_ms(now)
            self._reseat_shot_anchor(now)
            self.anchor_value_ms = max(0.0, current + float(delta_ms))
            self.anchor_server_ms = now
            self._bump()

    def set_period_index(self, index: int) -> None:
        """Jump to a specific 0-based position in the period list. No other
        side effects -- the clock, shot clock, fouls, etc. all stay as they
        are. The operator is responsible for resetting whatever needs
        resetting via the dedicated buttons."""
        with self._lock:
            if not self.periods:
                return
            idx = max(0, min(int(index), len(self.periods) - 1))
            if idx == self.period_index:
                return
            self.period_index = idx
            self._bump()

    def adjust_period(self, delta: int) -> None:
        self.set_period_index(self.period_index + int(delta))

    def set_periods(self, entries: list[dict]) -> None:
        """Replace the period list. Clamps period_index into range."""
        with self._lock:
            new_list: list[PeriodEntry] = []
            for e in entries or []:
                name = str(e.get("name", "")).strip()
                if not name:
                    continue
                short = str(e.get("short", "")).strip() or name[:4].upper()
                try:
                    indicator = int(e.get("indicator", 0))
                except (TypeError, ValueError):
                    indicator = 0
                new_list.append(PeriodEntry(
                    name=name[:32], short=short[:8], indicator=indicator,
                ))
            if not new_list:
                return
            self.periods = new_list
            self.period_index = max(0, min(self.period_index, len(new_list) - 1))
            self._bump()

    def reset_periods(self) -> None:
        """Restore the default 10-entry period list."""
        with self._lock:
            self.periods = _default_periods()
            self.period_index = 0
            self._bump()

    # ----------------- shot clock ----------------------------------------- #

    def sc_set_enabled(self, on: bool) -> None:
        """Operator switch. NEVER set automatically by the rest of the state
        machine -- think of this as a physical toggle on the console."""
        with self._lock:
            new = bool(on)
            if new == self.sc_enabled:
                return
            now = server_now_ms()
            # Capture the shot clock value as of right now, then flip the
            # switch. Effective running may change, but the anchor describes
            # "value at now", so prediction is correct either way.
            self._reseat_shot_anchor(now)
            self.sc_enabled = new
            self._bump()

    def sc_toggle_enabled(self) -> None:
        self.sc_set_enabled(not self.sc_enabled)

    def sc_set_independent(self, on: bool) -> None:
        """Independent / force-run mode. When True, the shot clock counts
        down even if the game clock is stopped. Normally False."""
        with self._lock:
            new = bool(on)
            if new == self.sc_independent:
                return
            now = server_now_ms()
            self._reseat_shot_anchor(now)
            self.sc_independent = new
            self._bump()

    def sc_toggle_independent(self) -> None:
        self.sc_set_independent(not self.sc_independent)

    def sc_set_ms(self, value_ms: float) -> None:
        """Jump the shot clock to an explicit remaining value. The anchor
        captures (now, new value); if the clock is effectively running it
        will count down from this moment, otherwise it stays put."""
        with self._lock:
            now = server_now_ms()
            self.sc_anchor_server_ms = now
            self.sc_anchor_value_ms = max(0.0, float(value_ms))
            self._bump()

    def sc_reset_full(self) -> None:
        """Standard 24-second reset, and make sure the display is on."""
        with self._lock:
            self.sc_visible = True
            self.sc_set_ms(SHOT_CLOCK_FULL_MS)

    def sc_reset_short(self) -> None:
        """14-second reset (offensive rebound / specific resumption rules)."""
        with self._lock:
            self.sc_visible = True
            self.sc_set_ms(SHOT_CLOCK_RESET_MS)

    def sc_set_visible(self, visible: bool) -> None:
        """Hide / show the shot clock display. Hiding ALSO pauses the
        countdown (without flipping the sc_enabled operator switch) by way
        of _sc_effectively_running checking sc_visible. We reseat the
        anchor here so the value is correctly captured at the moment of
        transition; on re-show it resumes from where it left off."""
        with self._lock:
            new = bool(visible)
            if new == self.sc_visible:
                return
            now = server_now_ms()
            self._reseat_shot_anchor(now)
            self.sc_visible = new
            self._bump()

    def sc_toggle_visible(self) -> None:
        self.sc_set_visible(not self.sc_visible)

    def adjust_shot_ms(self, delta_ms: float) -> None:
        """Bump the shot clock by a signed delta. Safe while running -- we
        recompute the displayed value at `now` first, apply the delta,
        then reseat the anchor. Clamped to >= 0."""
        with self._lock:
            now = server_now_ms()
            current = self._current_shot_ms(now)
            self.sc_anchor_value_ms = max(0.0, current + float(delta_ms))
            self.sc_anchor_server_ms = now
            self._bump()

    # ----------------- time-out (stopwatch) clock -------------------------

    def _current_to_ms(self, now: float) -> float:
        if self.to_running:
            return max(0.0, self.to_anchor_value_ms - (now - self.to_anchor_server_ms))
        return self.to_anchor_value_ms

    def to_start(self) -> None:
        with self._lock:
            if self.to_running:
                return
            now = server_now_ms()
            v = self._current_to_ms(now)
            if v <= 0:
                return
            self.to_running = True
            self.to_anchor_server_ms = now
            self.to_anchor_value_ms = v
            self._bump()

    def to_stop(self) -> None:
        with self._lock:
            if not self.to_running:
                return
            now = server_now_ms()
            self.to_anchor_value_ms = self._current_to_ms(now)
            self.to_anchor_server_ms = now
            self.to_running = False
            self._bump()

    def to_toggle(self) -> None:
        (self.to_stop if self.to_running else self.to_start)()

    def to_set_ms(self, value_ms: float) -> None:
        with self._lock:
            now = server_now_ms()
            self.to_anchor_server_ms = now
            self.to_anchor_value_ms = max(0.0, float(value_ms))
            self._bump()

    def to_reset(self) -> None:
        """Reset to the default value (1:00) AND stop. Convenient operator
        action between consecutive time-outs."""
        with self._lock:
            now = server_now_ms()
            self.to_running = False
            self.to_anchor_server_ms = now
            self.to_anchor_value_ms = float(DEFAULT_TIMEOUT_MS)
            self._bump()

    def to_set_warn_20(self, on: bool) -> None:
        with self._lock:
            self.to_warn_20 = bool(on)
            self._bump()

    def to_toggle_warn_20(self) -> None:
        self.to_set_warn_20(not self.to_warn_20)

    def to_adjust_ms(self, delta_ms: float) -> None:
        """Bump the time-out clock by a signed delta. Safe while running --
        we recompute the displayed value at `now`, apply the delta, then
        reseat the anchor. Clamped to >= 0."""
        with self._lock:
            now = server_now_ms()
            current = self._current_to_ms(now)
            self.to_anchor_value_ms = max(0.0, current + float(delta_ms))
            self.to_anchor_server_ms = now
            self._bump()

    # ----------------- siren ---------------------------------------------- #

    def siren_set(self, on: bool) -> None:
        """Set the externally-readable siren flag. Used for momentary horn
        presses on the timer operator view; a future external siren
        integration can watch the websocket for this flag transitioning
        True -> False and drive a physical horn relay accordingly."""
        with self._lock:
            new = bool(on)
            if new == self.siren_active:
                return
            self.siren_active = new
            self._bump()

    def siren_toggle(self) -> None:
        self.siren_set(not self.siren_active)

    def next_to_clock_event_at_ms(self, warn_seconds: int) -> Optional[float]:
        """Server time at which the time-out clock will read exactly
        `warn_seconds` remaining. Returns None if the clock isn't running
        or the warn point has already passed."""
        with self._lock:
            if not self.to_running:
                return None
            target_ms = float(warn_seconds) * 1000.0
            if self.to_anchor_value_ms <= target_ms:
                return None
            return self.to_anchor_server_ms + (self.to_anchor_value_ms - target_ms)

    # ----------------- scores --------------------------------------------- #

    def _team(self, which: str) -> Team:
        return self.home if which == "home" else self.away

    def add_score(self, team: str, points: int, *, player_index: int | None = None) -> None:
        """Add points to a team and (optionally) credit them to a roster slot."""
        with self._lock:
            t = self._team(team)
            t.score = max(0, t.score + int(points))
            if player_index is not None and 0 <= player_index < len(t.players):
                t.players[player_index].points = max(0, t.players[player_index].points + int(points))
            self._bump()

    def set_score(self, team: str, score: int) -> None:
        with self._lock:
            self._team(team).score = max(0, int(score))
            self._bump()

    # ----------------- fouls ---------------------------------------------- #

    def add_team_foul(self, team: str) -> None:
        with self._lock:
            t = self._team(team)
            t.team_fouls = max(0, t.team_fouls + 1)
            self._bump()

    def set_team_foul(self, team: str, value: int) -> None:
        with self._lock:
            t = self._team(team)
            t.team_fouls = max(0, int(value))
            if t.team_fouls < 5:
                # Don't auto-clear bonus -- once a team is in penalty for the
                # period, they stay in penalty until the period ends, even if
                # the operator corrects the foul count downward.
                pass
            self._bump()

    def set_bonus(self, team: str, on: bool) -> None:
        with self._lock:
            self._team(team).bonus = bool(on)
            self._bump()

    def reset_team_fouls(self) -> None:
        """Zero team fouls and clear bonus for BOTH teams. Bound to the
        prominent "Reset team fouls" button on the operator panel; the
        operator hits it at the end of every period (FIBA §8)."""
        with self._lock:
            self.home.team_fouls = 0
            self.home.bonus = False
            self.away.team_fouls = 0
            self.away.bonus = False
            self._bump()

    def add_player_foul(self, team: str, player_index: int) -> None:
        """Record a personal foul on a player. Per-player counter only --
        does NOT touch the team foul counter (kept independent as a
        double-check / error-catch interlock)."""
        with self._lock:
            t = self._team(team)
            if not (0 <= player_index < len(t.players)):
                return
            p = t.players[player_index]
            p.fouls = max(0, p.fouls + 1)
            # If they're committing fouls, they're on the court.
            p.played = True
            if p.fouls >= 5:
                p.disqualified = True
            self._bump()

    def subtract_player_foul(self, team: str, player_index: int) -> None:
        """Reverse a personal foul. Drops DQ if back below 5. Does NOT
        touch the team foul counter."""
        with self._lock:
            t = self._team(team)
            if not (0 <= player_index < len(t.players)):
                return
            p = t.players[player_index]
            if p.fouls <= 0:
                return
            p.fouls -= 1
            if p.fouls < 5:
                p.disqualified = False
            self._bump()

    def set_player_foul(self, team: str, player_index: int, value: int) -> None:
        with self._lock:
            t = self._team(team)
            if not (0 <= player_index < len(t.players)):
                return
            p = t.players[player_index]
            p.fouls = max(0, int(value))
            p.disqualified = p.fouls >= 5
            self._bump()

    def set_player_disqualified(self, team: str, player_index: int, on: bool) -> None:
        with self._lock:
            t = self._team(team)
            if 0 <= player_index < len(t.players):
                t.players[player_index].disqualified = bool(on)
                self._bump()

    def set_player_played(self, team: str, player_index: int, on: bool) -> None:
        with self._lock:
            t = self._team(team)
            if 0 <= player_index < len(t.players):
                t.players[player_index].played = bool(on)
                self._bump()

    # ----------------- roster import -------------------------------------- #

    def import_roster(self, team: str, *, name: str | None = None,
                      short: str | None = None,
                      players: list[dict] | None = None) -> None:
        """Replace a team's roster with imported player data.

        Each player dict should provide at least 'number' and 'name'. Other
        per-player fields (fouls, points, disqualified) are reset on import
        so the operator starts from a known clean slate. Team-level state
        (score, team fouls, time-outs) is intentionally left untouched.
        """
        with self._lock:
            t = self._team(team)
            if name:
                t.name = str(name)[:24]
                if not short:
                    short = name.split()[0][:5].upper() if name else None
            if short:
                t.short = str(short)[:5].upper()
            if players is not None:
                t.players = [
                    Player(
                        number=str(p.get("number", ""))[:3],
                        name=str(p.get("name", ""))[:24],
                        played=bool(p.get("played", False)),
                    )
                    for p in players
                ]
                # Sportradar (and most sources) deliver players in arbitrary
                # order. Sort by jersey number on import so the operator
                # doesn't have to do it manually before tip-off.
                t.players.sort(key=lambda p: _player_sort_key(p.number))
            self._bump()

    def sort_roster(self, team: str) -> None:
        """Reorder a team's players by jersey number using FIBA ordering
        (see _player_sort_key). Stable: duplicate numbers keep their
        relative order. Used by the operator after manual number edits
        and called automatically at the end of import_roster."""
        with self._lock:
            t = self._team(team)
            t.players.sort(key=lambda p: _player_sort_key(p.number))
            self._bump()

    def add_player(self, team: str, *, number: str = "", name: str = "",
                   played: bool = False) -> int:
        """Append a fresh roster slot. Returns the new player_index."""
        with self._lock:
            t = self._team(team)
            t.players.append(Player(
                number=str(number)[:3],
                name=str(name)[:24],
                played=bool(played),
            ))
            self._bump()
            return len(t.players) - 1

    def remove_player(self, team: str, player_index: int) -> None:
        """Drop a roster slot. Higher indices shift down."""
        with self._lock:
            t = self._team(team)
            if 0 <= player_index < len(t.players):
                t.players.pop(player_index)
                self._bump()

    # ----------------- player points / score -------------------------------

    def add_player_points(self, team: str, player_index: int, points: int) -> None:
        """Credits (or debits) points to a player. Per-player counter only --
        does NOT touch the team score (kept independent so the player-sum
        and the team total can be cross-checked). A positive delta auto-
        flips played=True; a negative delta is a correction and leaves
        played alone."""
        with self._lock:
            t = self._team(team)
            if not (0 <= player_index < len(t.players)):
                return
            p = t.players[player_index]
            old = p.points
            delta = int(points)
            new = max(0, old + delta)
            actual_delta = new - old
            p.points = new
            if actual_delta > 0:
                p.played = True
            self._bump()

    # ----------------- time-outs ------------------------------------------ #

    def add_timeout(self, team: str) -> None:
        with self._lock:
            t = self._team(team)
            t.timeouts_taken = max(0, t.timeouts_taken + 1)
            self._bump()

    def set_timeouts(self, team: str, taken: int) -> None:
        with self._lock:
            self._team(team).timeouts_taken = max(0, int(taken))
            self._bump()

    def reset_timeouts(self) -> None:
        """Zero timeouts_taken for BOTH teams. Bound to the prominent
        "Reset timeouts" button (typically pressed at halftime when each
        team's per-half allowance resets)."""
        with self._lock:
            self.home.timeouts_taken = 0
            self.away.timeouts_taken = 0
            self._bump()

    def set_timeout_settings(self, *, mode: str | None = None,
                             max_count: int | None = None) -> None:
        """Configure how the time-out indicator is displayed."""
        with self._lock:
            if mode in ("remaining", "taken"):
                self.timeout_mode = mode
            if max_count is not None:
                self.timeout_max = max(1, min(9, int(max_count)))
            self._bump()

    # ----------------- possession ----------------------------------------- #

    def set_possession(self, direction: str) -> None:
        with self._lock:
            if direction not in ("left", "right", "off"):
                return
            self.possession = direction
            self._bump()

    def toggle_possession(self) -> None:
        """left -> right -> left (ignores 'off'; explicit set_possession('off')
        is required to clear)."""
        with self._lock:
            if self.possession == "left":
                self.possession = "right"
            elif self.possession == "right":
                self.possession = "left"
            else:
                self.possession = "left"
            self._bump()

    # ----------------- roster editing ------------------------------------- #

    def set_player(self, team: str, player_index: int, *,
                   number: str | None = None, name: str | None = None) -> None:
        with self._lock:
            t = self._team(team)
            if not (0 <= player_index < len(t.players)):
                return
            p = t.players[player_index]
            if number is not None:
                p.number = str(number)[:3]
            if name is not None:
                p.name = str(name)[:24]
            self._bump()

    def set_team_meta(self, team: str, *, name: str | None = None, short: str | None = None) -> None:
        with self._lock:
            t = self._team(team)
            if name is not None:
                t.name = str(name)[:24]
            if short is not None:
                t.short = str(short)[:5].upper()
            self._bump()

    # ----------------- snapshot ------------------------------------------- #

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cur = self._current_period()
            return {
                "version": self.version,
                "server_now_ms": server_now_ms(),

                # Period list + cursor (operator advances/decrements through
                # the list manually; nothing else auto-changes on period
                # transitions).
                "periods": [p.to_dict() for p in self.periods],
                "period_index": self.period_index,
                "period_name": cur.name,
                "period_short": cur.short,
                "period_indicator": cur.indicator,

                "running": self.running,
                "anchor_server_ms": self.anchor_server_ms,
                "anchor_value_ms": self.anchor_value_ms,

                # Operator switches (raw):
                "sc_enabled": self.sc_enabled,
                "sc_independent": self.sc_independent,
                # Effective running state (computed) -- this is what display
                # pages should look at to decide whether to tick locally.
                "sc_running": self._sc_effectively_running(),
                "sc_anchor_server_ms": self.sc_anchor_server_ms,
                "sc_anchor_value_ms": self.sc_anchor_value_ms,
                "sc_visible": self.sc_visible,

                "to_running": self.to_running,
                "to_anchor_server_ms": self.to_anchor_server_ms,
                "to_anchor_value_ms": self.to_anchor_value_ms,
                "to_warn_20": self.to_warn_20,

                "siren_active": self.siren_active,

                "possession": self.possession,
                "timeout_mode": self.timeout_mode,
                "timeout_max": self.timeout_max,
                "home": self.home.to_dict(),
                "away": self.away.to_dict(),
            }

    # ----------------- config defaults ----------------------------------- #

    def apply_config_defaults(self, cfg: dict[str, Any]) -> None:
        """Seed game state from operator config. Called at startup and after a
        full reset so the period list and display settings survive restarts."""
        if cfg.get("periods"):
            self.set_periods(cfg["periods"])
        self.set_timeout_settings(
            mode=cfg.get("timeout_mode"),
            max_count=cfg.get("timeout_max"),
        )

    # ----------------- buzzer scheduling support ------------------------- #

    def next_game_clock_zero_at_ms(self) -> Optional[float]:
        """Server time (ms) at which the game clock will read 0.0, if running."""
        with self._lock:
            if not self.running:
                return None
            return self.anchor_server_ms + self.anchor_value_ms

    def next_shot_clock_zero_at_ms(self) -> Optional[float]:
        """Server time (ms) at which the shot clock will read 0.0, given the
        currently-effective running state. Returns None if the clock isn't
        counting down (either operator switch off or game clock stopped
        without independent mode) OR if the value is already at zero (we
        don't want to keep re-firing the buzzer until the operator resets)."""
        with self._lock:
            if not self._sc_effectively_running():
                return None
            if self.sc_anchor_value_ms <= 0:
                return None
            return self.sc_anchor_server_ms + self.sc_anchor_value_ms
