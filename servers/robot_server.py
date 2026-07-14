"""
MCP server exposing the robot car's hardware as tools Claude can call.

Run standalone for testing:
    python3 servers/robot_server.py

Claude Code picks this up automatically via mcp.json (rename to .mcp.json)
in the project root.
"""

import sys
import os
import base64
import queue
import subprocess
import shutil
import tempfile
import threading
import time
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp.server.fastmcp import FastMCP, Image
from hardware import motor
from hardware import distance_sensor

# stateless_http=True because clients are many short-lived `claude -p` runs,
# each doing a fresh handshake -- no cross-request session state to keep.
mcp = FastMCP("robot", host="127.0.0.1", port=8765, stateless_http=True)

# --- Piper neural TTS (natural offline voice) ------------------------------
# Piper's model load takes ~7s but each synthesis is ~1-2s, so we load the
# voice ONCE in a background thread at startup and reuse it for every speak()
# call. The background load warms up while Claude is reasoning/capturing, so
# the voice is usually ready by the first speak(). Falls back to espeak-ng if
# the model or the piper package is missing.
PIPER_MODEL = os.path.join(
    os.path.dirname(__file__), "..", "voice", "piper", "en_US-lessac-medium.onnx"
)
_piper_voice = None
_piper_ready = threading.Event()  # set once loading finishes (success or fail)


def _load_piper():
    global _piper_voice
    try:
        from piper import PiperVoice

        _piper_voice = PiperVoice.load(PIPER_MODEL)
    except Exception as exc:  # missing package/model, etc. -- fall back to espeak
        print(f"[speak] Piper unavailable, will use espeak-ng: {exc}", file=sys.stderr)
    finally:
        _piper_ready.set()


if os.path.exists(PIPER_MODEL):
    threading.Thread(target=_load_piper, daemon=True).start()
else:
    _piper_ready.set()

# Hard, non-AI safety floor: a forward move is refused below this distance,
# no matter what Claude decides. This check runs in code, not in the model.
# Raised 30 -> 45 (2026-07-10): forward moves are now faster (0.9 speed) and
# longer (up to 3s), so the car covers more ground per blind move -- the
# distance is only sampled once, before the move, not during it. A bigger
# buffer keeps a confident move from overrunning something just past the old
# 30cm line before the duration elapses.
SAFE_FORWARD_DISTANCE_CM = 45.0

# Arc turns roll forward while curving, so they also need clearance ahead --
# but less than a straight run, since an arc advances more slowly and curves
# away from dead-ahead. Below this, refuse the turn and tell Claude to back up
# first (arcs can't get you out of a spot you're already nosed into).
ARC_MIN_CLEARANCE_CM = 20.0


@mcp.tool()
def move(direction: str, speed: float = 0.9, duration: float = 1.75, announce: str = "") -> str:
    """
    Move the robot car, optionally announcing intent over the speaker in the
    same call (preferred over a separate speak() call before moving -- it
    saves a round trip).

    Args:
        direction: one of "forward", "backward", "left", "right".
                   left/right are ARC turns -- the car rolls forward while
                   curving toward that side (no longer in-place pivots), so
                   they need some clearance ahead. Back up first if boxed in.
        speed: 0.0 to 1.0. Default 0.9 -- drive with authority; only drop
               lower for a deliberately delicate nudge near an obstacle.
        duration: seconds to move. Straight moves are capped at 3.0; turns get
                  a longer cap (4.0) and a floor since pivots swing slowly.
                  Default 1.75 covers real ground on a straight move; go up
                  toward 2-3s in wide-open space, and 3-4s for turns.
        announce: optional short spoken statement of what you're doing and
                  why (e.g. "Path looks clear, moving forward"). Any earlier
                  narration finishes playing first, then the announce starts
                  speaking at the same moment the wheels start -- phrase it in
                  the present ("Turning left toward the doorway"), since it
                  plays DURING the move.
    """
    # Instrumentation for the say/do disconnect investigation: log the actual
    # direction ARGUMENT next to the spoken announce text, so a run log shows
    # whether a mismatch is model-side (announce disagrees with direction) or
    # driver-side (direction is correct but the wheels go the other way -- see
    # the validated-swap note in hardware/motor.py). flush so it interleaves
    # with the MCP request log in real time.
    print(
        f"[move] direction={direction!r} speed={speed} duration={duration} "
        f"announce={announce!r}",
        file=sys.stderr, flush=True,
    )

    if direction == "forward":
        distance = distance_sensor.check("center")
        print(f"[move] forward pre-check center={distance}cm "
              f"(threshold {SAFE_FORWARD_DISTANCE_CM}cm)", file=sys.stderr, flush=True)
        if distance < SAFE_FORWARD_DISTANCE_CM:
            _clear_pending_speech()  # stale narration would mask this refusal
            _queue_speech("Something's in the way, staying put.")
            return (
                f"Refused: obstacle detected {distance}cm ahead, below the "
                f"{SAFE_FORWARD_DISTANCE_CM}cm safety threshold. Not moving "
                "forward. Use scan_obstacles() to check which way is clear."
            )
    elif direction in ("left", "right"):
        # Arc turns roll forward too, so guard them -- with a smaller buffer.
        distance = distance_sensor.check("center")
        print(f"[move] arc-turn pre-check center={distance}cm "
              f"(threshold {ARC_MIN_CLEARANCE_CM}cm)", file=sys.stderr, flush=True)
        if distance < ARC_MIN_CLEARANCE_CM:
            _clear_pending_speech()  # stale narration would mask this refusal
            _queue_speech("Too tight to turn here, need to back up first.")
            return (
                f"Refused: only {distance}cm ahead, below the "
                f"{ARC_MIN_CLEARANCE_CM}cm arc-turn clearance. Turns now curve "
                "FORWARD and need room. Back up (move 'backward') first, then "
                "turn."
            )
    # Speech/motion sync: first let everything already said finish playing
    # (audio order == action order, nothing gets dropped), then start the
    # announce and the wheels together so the words describe the motion
    # actually happening. See _wait_for_pending_speech/_start_announce_playback.
    _wait_for_pending_speech()
    if announce:
        _start_announce_playback(announce)
    return motor.drive(direction, speed, duration)


@mcp.tool()
def stop(announce: str = "") -> str:
    """
    Immediately stop both motors.

    Args:
        announce: optional short spoken statement of why you stopped,
                  spoken right after the motors cut out.
    """
    result = motor.stop()
    if announce:
        _clear_pending_speech()  # a stop reason shouldn't wait behind a backlog
    _queue_speech(announce)
    return result


@mcp.tool()
def check_distance(direction: str = "center") -> float:
    """
    Point the ultrasonic sensor in one direction and return the distance to
    the nearest obstacle, in centimeters.

    Args:
        direction: one of "left", "center", "right"
    """
    return distance_sensor.check(direction)


@mcp.tool()
def scan_obstacles() -> dict:
    """
    Sweep the ultrasonic sensor left, center, and right, returning the
    distance in centimeters to the nearest obstacle in each direction.
    Use this before turning to decide which direction is clearest, or
    whenever you want a fuller picture of what's around the car than a
    single forward-facing reading gives you.
    """
    return distance_sensor.scan()


# --- Mission clock (ClaudeExplorer autonomous mode) -------------------------
# explorer.py writes the mission deadline (unix epoch seconds) to this file
# before launching the expedition. The tool below is how the exploring Claude
# keeps track of its time budget; the supervisor separately hard-kills the
# expedition past the deadline + a grace period, so this is the *graceful*
# wrap-up path.
MISSION_DEADLINE_FILE = os.path.join(
    os.path.dirname(__file__), "..", ".mission_deadline"
)


@mcp.tool()
def mission_status() -> str:
    """
    Check how much exploration time is left in the current mission. Call this
    every few actions. When it says time is up (or nearly up), stop moving,
    speak a short summary of everything you explored, and end your turn.
    """
    try:
        with open(MISSION_DEADLINE_FILE) as f:
            deadline = float(f.read().strip())
    except (OSError, ValueError):
        return "No mission deadline is set -- explore freely until told otherwise."
    remaining = deadline - time.time()
    if remaining <= 0:
        return (
            "TIME IS UP. Stop moving now, speak a brief summary of your "
            "exploration, and end your turn."
        )
    if remaining <= 30:
        return (
            f"Only {remaining:.0f} seconds remain -- wrap up: stop moving, "
            "speak a brief summary of your exploration, and end your turn."
        )
    return f"{remaining:.0f} seconds of exploration time remain."


@mcp.tool()
def beep(duration_seconds: float = 0.5) -> str:
    """
    Sound the onboard buzzer for a short duration. Useful as a quick audible
    alert (e.g. before backing away from an obstacle) independent of speak().
    """
    return distance_sensor.beep(duration_seconds)


# Open/close the webcam per capture rather than holding it open for the
# server's lifetime -- most UVC webcams light their LED for as long as the
# device is open, not just while a frame is being grabbed, so a persistently
# open handle keeps the LED on indefinitely. Costs ~1s of open/warm-up per
# capture in exchange for the LED only being lit during an actual photo.
_camera_lock = threading.Lock()
_CAMERA_WARMUP_FRAMES = 5  # let exposure/white balance settle on a cold open


def _read_frame():
    import cv2
    with _camera_lock:
        camera = cv2.VideoCapture(0)
        try:
            ok, frame = False, None
            for _ in range(_CAMERA_WARMUP_FRAMES):
                ok, frame = camera.read()
            if not ok:
                raise RuntimeError("Could not read from camera (index 0)")
        finally:
            camera.release()
    return frame


@mcp.tool()
def capture_image() -> Image:
    """
    Take a photo from the onboard camera and return it so you can see
    what's in front of the robot.
    """
    path = os.path.join(tempfile.gettempdir(), "robot_capture.jpg")

    # USB webcam via OpenCV. If you're using the Pi Camera Module instead,
    # comment this block out and uncomment the rpicam-jpeg block below.
    import cv2
    frame = _read_frame()
    cv2.imwrite(path, frame)

    # --- Pi Camera Module alternative (rpicam-jpeg) ---
    # subprocess.run(["rpicam-jpeg", "-o", path, "--width", "640",
    #                 "--height", "480", "-t", "1", "-n"], check=True)

    with open(path, "rb") as f:
        data = f.read()
    return Image(data=data, format="jpeg")


# The Pi's default ALSA device is HDMI, which has no display attached on
# the robot, so speech played to the default device is silent. The speaker
# is a USB DAC/speaker (ALSA card "UACDemoV10"). We always synthesize to a
# WAV and play it explicitly on that card.
SPEAKER_DEVICE = "plughw:CARD=UACDemoV10,DEV=0"


def _speak_blocking(text: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        wav = tf.name
    try:
        engine = _synthesize_wav(text, wav)
        played = subprocess.run(
            ["aplay", "-D", SPEAKER_DEVICE, wav], check=False
        )
        if played.returncode != 0 and engine == "espeak":
            # USB speaker card unavailable -- last-ditch play on default device.
            tts = shutil.which("espeak-ng") or shutil.which("espeak")
            if tts:
                subprocess.run([tts, *_ESPEAK_OPTS, text], check=False)
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


# Async speech: synthesis + playback take ~2-4s, which used to block the
# speak() tool call (and therefore Claude's next decision) for that long.
# Utterances now go on a queue drained serially by a worker thread, so the
# tool returns immediately and speech overlaps Claude's next tool calls --
# announcements still come out in order, they just don't stall the turn.
_speech_queue: "queue.Queue[str]" = queue.Queue()


def _speech_worker():
    while True:
        text = _speech_queue.get()
        try:
            _speak_blocking(text)
        except Exception as exc:
            print(f"[speak] playback failed: {exc}", file=sys.stderr)
        finally:
            _speech_queue.task_done()


threading.Thread(target=_speech_worker, daemon=True).start()


def _queue_speech(text: str) -> None:
    if text:
        _speech_queue.put(text)


def _clear_pending_speech() -> None:
    """Drop utterances that are queued but not yet playing.

    Used on the safety-refusal paths, where the refusal message must not wait
    behind narration that no longer applies. Ordinary moves do NOT flush any
    more -- they drain (see _wait_for_pending_speech): dropping unplayed
    speak() lines left audible gaps and made the surviving audio feel
    disconnected from the actions around it.
    """
    try:
        while True:
            _speech_queue.get_nowait()
            _speech_queue.task_done()
    except queue.Empty:
        pass


def _wait_for_pending_speech(timeout_seconds: float = 8.0) -> None:
    """Block until everything already said has finished PLAYING (bounded).

    This is the ordering half of the speech/motion sync: a move only begins
    after the narration that led up to it has been heard, so audio order always
    matches action order and no observation is silently dropped. The timeout
    caps how long a backlog can stall the wheels (CLAUDE.md tells the model to
    keep utterances short); anything still unplayed past the cap is stale
    enough to drop.
    """
    deadline = time.monotonic() + timeout_seconds
    while _speech_queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.05)
    _clear_pending_speech()


def _start_announce_playback(text: str) -> None:
    """Synthesize `text` NOW, then start playback and return immediately.

    This is the simultaneity half of the sync: the caller starts the motors
    right after this returns, so the words and the wheels start together and
    the announce describes the motion as it happens. (The old path queued the
    announce and drove at once -- Piper's ~1-2s synthesis meant the words came
    out after the move was mostly over.) Playback is reaped in the background;
    utterances are short, so it never needs to be cancelled.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        wav = tf.name
    _synthesize_wav(text, wav)
    proc = subprocess.Popen(["aplay", "-D", SPEAKER_DEVICE, wav])

    def _reap():
        proc.wait()
        try:
            os.remove(wav)
        except OSError:
            pass

    threading.Thread(target=_reap, daemon=True).start()


@mcp.tool()
def speak(text: str) -> str:
    """
    Speak text out loud through the onboard speaker. Returns immediately;
    the audio plays in the background. Utterances play in order, and a move
    waits for pending speech to finish before the wheels start -- so what you
    say is always heard, in order, before the action that follows it. Keep
    each utterance short: a long backlog delays your next move while it plays.

    Args:
        text: what to say
    """
    _queue_speech(text)
    return f"Speaking: {text}"


# -s 145: slow the default 175 wpm down so it's clearer and less rushed.
# -a 150: moderate amplitude (the 0-200 max clips/distorts on this speaker).
# -g 3: a little inter-word gap smooths the choppy run-together phrasing.
_ESPEAK_OPTS = ["-s", "145", "-a", "150", "-g", "3"]


def _synthesize_wav(text: str, wav_path: str) -> str:
    """Render `text` to a WAV file. Prefer the natural Piper neural voice
    (loaded once at startup); fall back to espeak-ng. Returns the engine used
    ("piper" or "espeak")."""
    # Give the background Piper load a chance to finish (it starts at server
    # startup, so it's usually done by now); wait a bounded time if not.
    _piper_ready.wait(timeout=15)
    if _piper_voice is not None:
        try:
            with wave.open(wav_path, "wb") as wf:
                _piper_voice.synthesize_wav(text, wf)
            return "piper"
        except Exception as exc:  # fall through to espeak on any synth error
            print(f"[speak] Piper synth failed, using espeak-ng: {exc}", file=sys.stderr)
    tts = shutil.which("espeak-ng") or shutil.which("espeak")
    if tts is not None:
        subprocess.run([tts, *_ESPEAK_OPTS, "-w", wav_path, text], check=False)
    return "espeak"


if __name__ == "__main__":
    # --http: run as a PERSISTENT server on http://127.0.0.1:8765/mcp that
    # every `claude -p` connects to. This is the low-latency mode: Piper, the
    # GPIO setup, and OpenCV are loaded once here instead of once per voice
    # command (the old per-command stdio spawn re-paid the ~7s Piper load on
    # every single command). Keep this process running alongside the wake-word
    # listener; .mcp.json points Claude at the URL.
    #
    # With no flag it still runs in classic stdio mode for manual testing.
    if "--http" in sys.argv:
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
