# ClaudeExplorer — autonomous robot exploration

Sibling project to `ClaudePoweredCar`: same Raspberry Pi robot, same hardware
stack, but instead of waiting for "Hey Jarvis" voice commands, Claude explores
**on its own** — scanning, choosing a path, avoiding obstacles, and narrating
everything it sees and thinks over the speaker, until a mission timer runs out.

There is no microphone/voice input path at all in this project.

## Run it

```
cd ~/Documents/ClaudeExplorer
python3 -u explorer.py --minutes 4
```

(Run it backgrounded / under `nohup` so you can stop it instantly.)

One command does everything: `explorer.py` starts the hardware MCP server
(`servers/robot_server.py --http`, port 8765), writes the mission deadline,
launches one long headless Claude "expedition" turn, and cleans everything up
when the mission ends.

**Stop it early:** kill `explorer.py` (Ctrl+C or `pkill -f explorer.py`) — it
tears down the Claude turn and the hardware server itself. Motors can't be
left spinning: every `move()` in the server is a blocking drive-then-stop
with a hard 2s cap.

**Mutual exclusion:** only one of ClaudeExplorer / ClaudePoweredCar can run
at a time — they share the GPIO pins and port 8765. `explorer.py` refuses to
start if 8765 is already taken and tells you what to pkill.

## How it works

- `CLAUDE.md` — the explorer's identity and behavior: the sense → narrate →
  decide → move loop, pathfinding heuristics (favor clearance + unexplored
  ground, dead-end handling), constant speaker narration, and the safety
  rules. **Edit this first to change how it explores.**
- `explorer.py` — supervisor. Time-boxes the mission (default 4 min):
  Claude tracks its own budget via the `mission_status()` tool and wraps up
  gracefully with a spoken summary; if it overruns a 90s grace period, the
  supervisor hard-kills the turn and speaks a fallback sign-off. If the turn
  ends *early*, it's relaunched with `--continue` so the explorer keeps its
  mental map.
- `servers/robot_server.py` — copy of the ClaudePoweredCar hardware server
  (persistent HTTP MCP on 127.0.0.1:8765; move/stop/camera/ultrasonic/
  speak/beep, Piper TTS, async speech queue, hard 30cm forward-safety floor)
  **plus** the `mission_status()` tool, which reads the deadline that
  `explorer.py` writes to `.mission_deadline`.
- `hardware/` — verbatim copies of the validated motor and distance-sensor
  drivers (see ClaudePoweredCar's README for wiring and gotchas, including
  the intentional forward/backward pin swap).
- Expedition turns run `claude --model sonnet --effort low` with
  `--strict-mcp-config --mcp-config .mcp.json` (required headless — without
  it the MCP server is silently skipped).

## Tuning knobs

- Mission length: `--minutes` (float, default 4).
- Exploration style (step size, photo frequency, narration density,
  dead-end behavior): `CLAUDE.md`.
- Wrap-up grace period / relaunch thresholds: constants at the top of
  `explorer.py`.
- Judgment vs speed: raise `--effort` toward medium in `explorer.py`'s
  `run_expedition()` if it makes bad calls on ambiguous scenes.
