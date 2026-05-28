# bball-clock-scoreboard

A web-based basketball scoreboard built with FastAPI + WebSockets. Covers the
software-relevant portions of the FIBA Official Basketball Rules 2024 —
Equipment appendix (§3 Scoreboard, §4 Game clock, §5 Shot clock, §6 Signals,
§7 Player foul / GD markers, §8 Team foul markers, §9 Alternating possession
arrow).

## Views

| URL                         | What it is                                                                                                |
| --------------------------- | --------------------------------------------------------------------------------------------------------- |
| `/`                         | Operator control panel — clocks, scores, rosters, fouls, time-outs, possession, format.                   |
| `/timer`                    | Dedicated game-clock + time-out operator console (touch-friendly). Separate start/pause, +/- adjust, presets, siren button, full-screen time-out warning flashes. |
| `/shotclock-op`             | Dedicated shot-clock operator console (touch-friendly). Separate start/pause, 24/14 resets, +/- adjust, independent toggle, hide-pauses toggle. |
| `/visuals`                  | Scorer's table console: period indicator, scores, team fouls + bonus, time-outs, and per-player points / fouls. No clock controls. |
| `/arrow`                    | Possession-arrow operator console: large arrow indicator and big LEFT / OFF / RIGHT / Swap buttons. |
| `/scoreboard`               | Main scoreboard display: clock, scores, team fouls + bonus, time-outs, possession.                        |
| `/shot`                     | Shot clock display + duplicate game clock (FIBA Diagram 10).                                              |
| `/possession`               | Standalone alternating-possession arrow (FIBA §9).                                                        |
| `/fouls?team=home`          | Per-team foul indicator card for the scorer's table (FIBA §8).                                            |
| `/fouls?team=away`          | Same for the away team.                                                                                   |
| `/clock`                    | Simple big-game-clock view (kept from v1 for testing).                                                    |

All display pages share the same WebSocket; any change made on the operator
panel propagates to every view in real time.

## Why it's fast

The server is the source of truth for both the game clock and shot clock, but
only emits a message when something changes. Each browser does an NTP-style
time-sync burst on connect, then ticks its own display at 60 fps using
`performance.now() + offset`. So the perceived latency for a Start press is
roughly `½·RTT + one animation frame` (typically 5–25 ms on a LAN). Buzzer
events are scheduled server-side at the exact zero crossing and broadcast
when they fire.

## Run it

```bash
. .venv/bin/activate          # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

Then open:

- Operator:     `http://<host>:8000/`
- Main display: `http://<host>:8000/scoreboard`
- Shot clock:   `http://<host>:8000/shot`
- Possession:   `http://<host>:8000/possession`
- Team fouls:   `http://<host>:8000/fouls?team=home` and `…?team=away`

The display pages each show a one-time **"Enable sound"** overlay; clicking it
unlocks audio so the period horn and shot-clock horn can play (browsers block
autoplay until a user gesture).

## Keyboard shortcuts (control panel)

- **Space** — start/stop game clock
- **S** — start/stop shot clock
- **2** / **4** — reset shot clock to 24 / 14
- **R** — reset period
- **←** / **→** — possession arrow to home / away
- **F** — fullscreen (on any display page)

### Keyboard shortcuts — `/timer` operator console

- **Space** — start game clock · **P** — pause
- **1 / 2 / 3 / 4** — quick set 20:00 / 15:00 / 10:00 / 2:00
- **↑ / ↓** — ± 1 minute
- **→ / ←** — ± 1 second  ·  **Shift+→ / ←** — ± 0.1 second
- **T / Y** — time-out start / pause  ·  **R** — reset time-out  ·  **W** — toggle 20s warn
- **H** — siren (hold)

### Keyboard shortcuts — `/shotclock-op` operator console

- **Space** — start shot clock · **P** — pause
- **2 / 4** — reset to 24 / 14
- **→ / ←** — ± 1 second  ·  **Shift+→ / ←** — ± 0.1 second
- **I** — toggle Independent · **V** — toggle Hide / Show

### Keyboard shortcuts — `/visuals` scorer console

- **[ / ]** — previous / next period
- **R** — reset team fouls (both teams)
- **T** — reset time-outs (both teams)
- Per-team score / foul / time-out buttons aren't keyboard-bound (ambiguous which side).

### Keyboard shortcuts — `/arrow` possession console

- **← / →** — set possession left / right
- **Space** — swap (left ↔ right)
- **O / Esc** — clear possession (off)

## What FIBA-specific behavior is built in

- **Game clock** (§4): countdown, tenths only in the last minute, auto signal
  at 0.0 that stops the game.
- **Shot clock** (§5): 24/14 reset, tenths only in the last 5 s, can be
  hidden, manual start, **interlock** — stopping the game clock stops the
  shot clock; starting the game clock does not auto-start the shot clock; the
  shot clock buzzer doesn't stop the game clock (it just stops the shot
  clock itself).
- **Two distinct horns** (§6): period horn (low, ~1.6 s) and shot-clock horn
  (high, ~0.45 s), synthesized via Web Audio.
- **Light-strip simulation** (§1.1.6 / §1.1.7): display pages flash red on the
  period horn and yellow on the shot-clock horn.
- **Period readout** (§3): "1"…"4" then "OT", "OT2", etc.
- **Team fouls + bonus** (§3, §8): 0–4 then a fully red marker once the team
  is in penalty. Auto-set on the 5th team foul; can also be toggled manually
  by the operator (matching FIBA's "ball-live-after-the-4th-foul" trigger).
- **Time-outs** (§3): display shows 0–3 dots remaining; in the last 2:00 of
  Q4 or any OT it caps at 2 remaining.
- **Player roster** (§3, Level 1): 12 slots per team with number + surname,
  per-player fouls (4 amber, 5 red), game-disqualification marker, and
  cumulative player points that also credit the team total.
- **Alternating possession arrow** (§9): red, direction toggles on click or
  arrow keys.

## Layout

```
server.py          FastAPI app, WebSocket hub, buzzer scheduler
state.py           GameState, Team, Player, shot clock anchor model
common.js          Shared WS client + time sync + clock prediction + audio
control.html       Operator panel (Level 1)
timer.html         Touch-friendly game-clock + time-out operator console
shotclock-op.html  Touch-friendly shot-clock operator console
visuals.html      Touch-friendly scorer console (period / scores / fouls / TOs / players)
arrow.html         Touch-friendly possession-arrow operator console
scoreboard.html    Main display
shot.html          Shot clock + mini game clock
possession.html    Standalone possession arrow
fouls.html         Per-team foul indicator card
clock.html         Simple big-game-clock view (v1)
```
