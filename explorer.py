"""
Autonomous exploration supervisor.

One command brings the whole thing up:

    python3 explorer.py --minutes 3

It starts the persistent hardware MCP server (servers/robot_server.py --http),
writes the mission deadline, then launches ONE long-running headless Claude
turn ("the expedition") that senses, narrates over the speaker, and drives
until time runs out. The CLAUDE.md in this folder is what shapes the
exploration behavior; this script only supervises:

  - If the expedition turn ends early (Claude wrapped up prematurely or
    crashed) and meaningful time remains, it relaunches with --continue so
    the explorer keeps its mental map of where it's been.
  - If the expedition overruns the deadline past a grace period (Claude
    should notice via the mission_status() tool and wrap up on its own),
    it hard-kills the turn and speaks a fallback sign-off itself.

Safety note: motors are only ever driven by the MCP server, and every
move() there is a blocking drive-then-stop with a hard 2s cap -- killing the
Claude process mid-move cannot leave the wheels spinning.

There is deliberately NO microphone/voice path here (that's ClaudePoweredCar).
Only one of the two projects can run at a time: they share the GPIO pins and
port 8765.
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEADLINE_FILE = os.path.join(PROJECT_ROOT, ".mission_deadline")
SERVER_HOST, SERVER_PORT = "127.0.0.1", 8765

# How long past the deadline the expedition may keep running before we
# hard-kill it. Claude polls mission_status() every 4-6 actions, then needs a
# few more seconds to speak its summary -- 90s covers a slow final loop.
GRACE_SECONDS = 90
# Don't bother relaunching an early-exited expedition for a stub of a mission.
# Matches mission_status()'s 30s wrap-up window: a turn that ended inside that
# window already wrapped up deliberately -- relaunching it just resumes
# straight into "yep, still done" (observed on the first live test).
MIN_RELAUNCH_SECONDS = 30
# If expeditions keep dying this fast, something is broken -- stop looping.
FAST_FAILURE_SECONDS = 15
MAX_CONSECUTIVE_FAST_FAILURES = 3

# Matches robot_server.py: the ALSA default is HDMI (silent), the real
# speaker is the USB DAC. Used only for the hard-kill fallback sign-off.
SPEAKER_DEVICE = "plughw:CARD=UACDemoV10,DEV=0"


def log(msg: str) -> None:
    print(f"[explorer] {msg}", flush=True)


def port_in_use() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((SERVER_HOST, SERVER_PORT)) == 0


def start_server() -> subprocess.Popen:
    server = subprocess.Popen(
        [sys.executable, os.path.join("servers", "robot_server.py"), "--http"],
        cwd=PROJECT_ROOT,
    )
    deadline = time.monotonic() + 60  # cv2/GPIO imports take a few seconds
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(
                f"robot_server.py exited during startup (rc={server.returncode})"
            )
        if port_in_use():
            log("hardware server is up on port 8765")
            return server
        time.sleep(0.5)
    server.terminate()
    raise RuntimeError("robot_server.py never opened port 8765")


def speak_fallback(text: str) -> None:
    # Espeak-quality is fine here: this only plays if Claude was hard-killed
    # and never spoke its own (Piper-voiced) sign-off.
    tts = shutil.which("espeak-ng") or shutil.which("espeak")
    if not tts:
        return
    wav = os.path.join(PROJECT_ROOT, ".fallback_signoff.wav")
    try:
        subprocess.run([tts, "-s", "145", "-a", "150", "-w", wav, text], check=False)
        if subprocess.run(["aplay", "-D", SPEAKER_DEVICE, wav], check=False).returncode != 0:
            subprocess.run([tts, "-s", "145", "-a", "150", text], check=False)
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def run_expedition(prompt: str, resume: bool, hard_deadline: float) -> int:
    """Run one headless Claude expedition turn, killing it at hard_deadline.
    Returns the exit code (negative = killed by signal)."""
    # Same flag set the voice listener validated: --strict-mcp-config +
    # --mcp-config are REQUIRED headless (no interactive MCP-approval prompt;
    # without them the server is silently skipped). sonnet/low favors a lively
    # decision cadence over reasoning depth, same trade as the car.
    cmd = ["claude", "--model", "sonnet", "--effort", "low",
           "--strict-mcp-config", "--mcp-config", ".mcp.json"]
    if resume:
        cmd.append("--continue")
    cmd += ["-p", prompt]
    proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT)
    try:
        proc.wait(timeout=max(1.0, hard_deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        log("grace period exceeded -- hard-killing the expedition")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        speak_fallback(
            "Exploration time is up. Ending my expedition here. Goodbye."
        )
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        raise
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous robot exploration")
    parser.add_argument("--minutes", type=float, default=3.0,
                        help="mission length in minutes (default: 3)")
    args = parser.parse_args()

    if port_in_use():
        log("ERROR: port 8765 is already in use -- probably the ClaudePoweredCar")
        log("server. Shut that project down first:")
        log("  pkill -f wakeword_listener.py; pkill -f robot_server.py")
        return 1

    server = start_server()
    mission_end = time.time() + args.minutes * 60
    with open(DEADLINE_FILE, "w") as f:
        f.write(str(mission_end))
    hard_deadline = time.monotonic() + args.minutes * 60 + GRACE_SECONDS
    log(f"mission started: {args.minutes:g} minutes on the clock")

    prompt = (
        "Your exploration mission starts now. Check mission_status() for your "
        "time budget, then explore -- narrating everything over the speaker."
    )
    resume = False
    fast_failures = 0
    try:
        while True:
            started = time.monotonic()
            rc = run_expedition(prompt, resume, hard_deadline)
            elapsed = time.monotonic() - started
            remaining = mission_end - time.time()
            log(f"expedition turn ended (rc={rc}) with {remaining:.0f}s remaining")
            if remaining <= MIN_RELAUNCH_SECONDS:
                break
            if rc != 0 and elapsed < FAST_FAILURE_SECONDS:
                fast_failures += 1
                if fast_failures >= MAX_CONSECUTIVE_FAST_FAILURES:
                    log("expedition keeps failing immediately -- giving up")
                    return 1
                if resume:  # a bad --continue state? retry fresh once
                    resume = False
                    continue
            else:
                fast_failures = 0
            log("time remains -- resuming the expedition")
            prompt = (
                "You ended your turn but mission time remains -- check "
                "mission_status() and continue exploring from where you left off."
            )
            resume = True
    except KeyboardInterrupt:
        log("interrupted -- shutting down")
    finally:
        try:
            os.remove(DEADLINE_FILE)
        except OSError:
            pass
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        log("hardware server stopped; mission over")
    return 0


if __name__ == "__main__":
    sys.exit(main())
