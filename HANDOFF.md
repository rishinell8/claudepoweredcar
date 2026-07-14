# ClaudeExplorer — session handoff

A running log of what this project is, how it was built, and the state it's in,
so a fresh session (or a fresh you) can pick it up cold. Newest session at the
bottom. For how to *run* it, see `README.md`; for how it *behaves*, see
`CLAUDE.md`.

---

## What this project is (one paragraph)

Same Raspberry Pi robot car as the sibling project `~/Documents/ClaudePoweredCar`
(the voice-controlled "Hey Jarvis" car), but flipped into **autonomous
exploration mode**. There is no microphone and no voice input at all. Instead,
one long-running headless Claude turn drives the car on its own — sensing with
the ultrasonic sweep + camera, choosing a logical path, avoiding obstacles, and
**narrating what it sees and thinks out loud over the speaker the whole time** —
until a mission timer runs out. Only one of the two projects can run at a time
(they share the GPIO pins and port 8765).

---

## Layout

| Path | What it is |
|---|---|
| `explorer.py` | **Supervisor / entry point.** The only thing you run. Starts the hardware server, sets the mission clock, launches the Claude expedition turn, supervises + cleans up. |
| `CLAUDE.md` | **The explorer's brain.** Identity + the sense→narrate→decide→move loop, pathfinding heuristics, narration rules, safety rules. Edit this to change *how* it explores. |
| `servers/robot_server.py` | Copy of ClaudePoweredCar's MCP hardware server (move/stop/camera/ultrasonic/speak/beep, Piper TTS, async speech, hard 30cm forward-safety floor) **+ one new tool: `mission_status()`**. |
| `hardware/motor.py`, `hardware/distance_sensor.py` | Verbatim copies of the validated drivers. Wiring + gotchas documented in ClaudePoweredCar. |
| `voice/piper/` | Piper neural TTS voice model (copied). No wake-word/STT code — that path doesn't exist here. |
| `.mcp.json`, `.claude/settings.local.json` | MCP server URL + headless tool allow-list (includes `mission_status`). |
| `.mission_deadline` | Written by `explorer.py` at launch (unix epoch), read by `mission_status()`, deleted on exit. Transient. |

---

## Key design decisions (why it's shaped this way)

- **Copied, not imported, the hardware/server code** — so explorer-mode tweaks
  can never break the working JARVIS car. Cost is duplication; they can't run
  at once anyway.
- **One long expedition turn**, not a step-per-invocation loop — lowest latency
  between decisions and the most fluid narration. `explorer.py` only relaunches
  if the turn ends early with time to spare (with `--continue`, so the
  explorer keeps its mental map of where it's been).
- **Time-boxed missions** (default 4 min via `--minutes`). Two independent
  stop mechanisms:
  1. *Graceful:* Claude polls `mission_status()` every few actions; at ≤30s it's
     told to stop, speak a summary, and end its turn.
  2. *Hard backstop:* the supervisor kills the turn at deadline + 90s grace and
     speaks an espeak fallback sign-off.
- **`--model sonnet --effort low`** — favors a lively decision cadence over
  reasoning depth (same trade as the car). Raise `--effort` toward medium in
  `explorer.py`'s `run_expedition()` if it makes bad calls on ambiguous scenes.
- **Motors can't be left spinning.** Every `move()` in the server is a blocking
  drive-then-stop with a hard 2s cap, so killing the Claude process (or the
  supervisor) mid-move cannot leave the wheels running.

---

## Gotchas / things learned

- **Headless MCP flags are required:** expedition turns run with
  `--strict-mcp-config --mcp-config .mcp.json`. Without them, headless Claude
  silently skips the server (no interactive approval prompt exists) and the car
  has no controls.
- **`pkill -f` / `pgrep -f` self-matches** the Bash tool's own wrapper shell
  (the command text lives in the shell-snapshot cmdline). Use a bracket pattern
  like `pgrep -f "robot_serve[r]"` to avoid matching yourself.
- **onnxruntime GPU warnings** at server startup (`Failed to detect devices
  under /sys/class/drm/...`) are benign — Piper just noting there's no GPU.
- **Port 8765 conflict = the car is running.** `explorer.py` refuses to start
  and prints the fix: `pkill -f wakeword_listener.py; pkill -f robot_server.py`.

---

## Session log — 2026-07-09 (built + first live test)

**Built the whole project from scratch** on top of ClaudePoweredCar: copied the
hardware/server/Piper stack, wrote a new autonomous `CLAUDE.md`, added the
`mission_status()` tool to the server, and wrote `explorer.py` (supervisor) +
`README.md`.

**Verified before driving:** all files `py_compile` clean; live smoke test
against the running server over streamable HTTP confirmed all 8 tools register
and `mission_status()` returns the right message in all four states (no
deadline / plenty left / ≤30s wrap-up / expired).

**First live mission — SUCCESS.** Ran `explorer.py --minutes 2` with the car on
a table stand (wheels free to spin). User confirmed **"everything seems to be
working great"**: wheels actually spun, narration was audible over the speaker,
the servo swept during scans, Claude ran a lively sense→narrate→move cadence
(~28 tool calls), then wrapped up gracefully on the clock with a spoken summary,
and the supervisor tore everything down cleanly (rc=0, port freed).
**This also clears the old wheels-hum / battery issue** that was open in
ClaudePoweredCar — the motors drive fine now.

**Tuning from the run:** bumped `MIN_RELAUNCH_SECONDS` 20 → 30 in `explorer.py`.
The first turn happened to end with 25s left (inside the 30s wrap-up window, so
it had already decided it was done); relaunching it just resumed straight into
"yep, still done." Not relaunching inside the wrap-up window avoids that.

**Current state / next steps:**
- Fully working on a stand. **Not yet driven on the actual floor** — the natural
  next test is a real `--minutes 4` expedition on the ground to see the
  pathfinding and obstacle-avoidance behave in a real space.
- If floor behavior is twitchy (oscillating, too-cautious, too-bold), that's a
  `CLAUDE.md` wording tune, not a code change, in most cases.
