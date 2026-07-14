"""
Wheel motor driver for a 4-wheel differential-drive car.

This replaces the earlier placeholder version of this file. Pin numbers,
PWM approach, and the kickstart logic below are taken directly from
robotcar_standalone.py, which has already been validated on the real
hardware (motors + servo sweep). This module only wraps that validated
logic behind the drive(direction, speed, duration) / stop() interface that
servers/robot_server.py expects -- the underlying control logic is
unchanged from what was tested.

Wiring (BCM numbering):
    Back-right wheel  -> GPIO 17, 27
    Back-left wheel   -> GPIO 5, 6
    Front-right wheel -> GPIO 13, 19
    Front-left wheel  -> GPIO 20, 21

IMPORTANT -- validated direction mapping:
During hardware testing it was found that this car's "backward" wheel-pin
pattern actually drives the car physically FORWARD, and the "forward"
pattern drives it physically BACKWARD (see robotcar_standalone.py's
back_up_and_turn() / run_obstacle_avoidance(), where this swap is applied
consistently in both places). drive("forward", ...) below accounts for
this automatically, so the rest of the project (CLAUDE.md, robot_server.py)
can keep using plain "forward" / "backward" language that matches what the
car actually does. Don't "fix" this swap without re-testing on hardware.

Run standalone to sanity-check all four motions:
    python3 hardware/motor.py
"""

import atexit
import time

import RPi.GPIO as GPIO

# --- Wheel pins (BCM), validated wiring -------------------------------------
BACK_RIGHT_A, BACK_RIGHT_B = 17, 27
BACK_LEFT_A, BACK_LEFT_B = 5, 6
FRONT_RIGHT_A, FRONT_RIGHT_B = 13, 19
FRONT_LEFT_A, FRONT_LEFT_B = 20, 21

WHEEL_PINS = (
    BACK_RIGHT_A, BACK_RIGHT_B,
    BACK_LEFT_A, BACK_LEFT_B,
    FRONT_RIGHT_A, FRONT_RIGHT_B,
    FRONT_LEFT_A, FRONT_LEFT_B,
)

MOTOR_PWM_HZ = 100  # software-PWM frequency for the H-bridge inputs

# The motors won't reliably start from rest below roughly 70-80% duty, but
# cruise fine at lower duty once already moving. So every time a wheel
# motion starts or changes direction, briefly pulse at full power to break
# static friction, then settle to the requested cruise duty.
KICKSTART_DUTY_PERCENT = 100
# was 0.15 -> 0.25 (front-left slow to spin up) -> 0.35 -> 0.4 (2026-07-10):
# pivots and cold starts sometimes failed to break static friction at all. A
# longer full-power pulse gives every wheel more time to break loose before
# easing to cruise. This is a software band-aid, though -- the real ceiling is
# supply voltage/torque, not duty. With an L298N (~2V dropout) on a 4x1.2V
# NiMH pack (4.8V), the motors only see ~2.8V, below the yellow TT motors'
# ~3V floor -- no amount of duty fixes that; it needs more pack voltage.
KICKSTART_SECONDS = 0.4

# Floor so a low `speed` argument can't request a duty that stalls the car
# mid-move once the kickstart eases off. Tune based on your own motors.
# Raised 50 -> 65 (2026-07-07): the front-left wheel (BCM 20/21) stalls at
# ~50% duty while the other three cruise fine -- it spins normally when
# pulsed at 100% (verified with a direct pin test), so this is friction in
# that motor, not wiring. 65% keeps all four wheels above stall.
# Raised 65 -> 75 (2026-07-10): the on-stand-clear moves were too weak/timid,
# so give every move more torque baseline -- 75% cruises with noticeably more
# authority and pushes through carpet/thresholds the 65% floor stalled on.
# Raised 75 -> 85 (2026-07-10): still wanted more drive, so push the floor
# closer to the 100% kickstart -- 85% cruises hard with a small headroom gap.
MIN_CRUISE_DUTY_PERCENT = 85

# TURNS ARE NOW ARC TURNS, not in-place pivots. An in-place pivot scrubs all
# four tires sideways and, through the L298N's ~2V drop, is too torque-hungry
# for these motors to swing the chassis usefully -- confirmed dead on hardwood
# even at full power for 3s. An arc turn instead drives only the OUTER side
# forward and lets the inner wheels coast, so every powered wheel rolls in its
# natural forward direction (no sideways scrub) and the car curves toward the
# inner side. Far less torque, so it actually turns -- at the cost of rolling
# forward through the turn, so arcs need some room ahead (the server guards it).
# Turns ignore requested speed (always full power) and get their own longer
# duration window. Tune to how far the car actually swings per second.
MIN_TURN_DURATION = 1.2   # a turn shorter than this barely changes heading
TURN_DURATION_CAP = 4.0   # allow one arc to run longer than a straight move
FORWARD_DURATION_CAP = 3.0  # straight-move safety cap (was the global 2.0->3.0)

# Pin patterns for each physical motion (kept from robotcar_standalone.py).
_FORWARD_PINS = (BACK_RIGHT_B, BACK_LEFT_B, FRONT_RIGHT_A, FRONT_LEFT_B)
_BACKWARD_PINS = (BACK_RIGHT_A, BACK_LEFT_A, FRONT_RIGHT_B, FRONT_LEFT_A)
_TURN_LEFT_PINS = (FRONT_RIGHT_B, BACK_RIGHT_A, FRONT_LEFT_B, BACK_LEFT_B)
_TURN_RIGHT_PINS = (FRONT_LEFT_A, BACK_LEFT_A, FRONT_RIGHT_A, BACK_RIGHT_B)

# Arc-turn drive groups: one side's wheels driven PHYSICALLY FORWARD while the
# other side coasts, so the car rolls forward and curves toward the coasting
# side. These are the left/right subsets of _BACKWARD_PINS (the validated
# physically-forward pattern -- see the swap note in the module docstring), so
# they're trusted to move those wheels forward. Arc right = drive the LEFT side
# (car curves right); arc left = drive the RIGHT side. If left/right come out
# mirrored on the real car, swap these two -- verify on hardware, don't guess.
_ARC_RIGHT_PINS = (BACK_LEFT_A, FRONT_LEFT_A)     # left side fwd -> curves right
_ARC_LEFT_PINS = (BACK_RIGHT_A, FRONT_RIGHT_B)    # right side fwd -> curves left
# (_TURN_LEFT_PINS/_TURN_RIGHT_PINS are the old in-place pivots -- kept for
# reference but no longer used; pivots don't work on this hardware.)

_wheel_pwms = {}
_current_active = frozenset()
_initialized = False


def _setup():
    global _initialized
    if _initialized:
        return
    GPIO.setmode(GPIO.BCM)
    for pin in WHEEL_PINS:
        GPIO.setup(pin, GPIO.OUT)
        pwm = GPIO.PWM(pin, MOTOR_PWM_HZ)
        pwm.start(0)
        _wheel_pwms[pin] = pwm
    _initialized = True


def _set_wheels(active, duty):
    for pin in WHEEL_PINS:
        _wheel_pwms[pin].ChangeDutyCycle(duty if pin in active else 0)


def _drive_pins(active_pins, duty):
    global _current_active
    active = frozenset(active_pins)
    if active and active != _current_active:
        _set_wheels(active, KICKSTART_DUTY_PERCENT)
        time.sleep(KICKSTART_SECONDS)
    _set_wheels(active, duty)
    _current_active = active


def stop() -> str:
    global _current_active
    _setup()
    for pin in WHEEL_PINS:
        _wheel_pwms[pin].ChangeDutyCycle(0)
    _current_active = frozenset()
    return "Stopped"


def drive(direction: str, speed: float = 0.9, duration: float = 1.75) -> str:
    """
    Blocking drive command. Runs the motors for `duration` seconds then
    stops -- one call, one motion, matching a turn-based control model.

    Args:
        direction: "forward", "backward", "left", "right"
                   (left/right are ARC turns -- the car rolls forward while
                   curving toward that side; see the arc-group note above)
        speed: 0.0-1.0, clamped to MIN_CRUISE_DUTY_PERCENT as a floor.
               Defaults high (0.9) so moves are powerful/confident by default.
        duration: seconds, hard-capped at 3.0. A default move now covers real
                  ground instead of inching forward a couple of inches.
    """
    _setup()
    speed = max(0.0, min(1.0, float(speed)))
    duty = max(MIN_CRUISE_DUTY_PERCENT, speed * 100)
    duration = max(0.0, float(duration))

    # Turns get full power and their own longer duration window; straight moves
    # keep the forward safety cap. See the turn constants above.
    if direction in ("left", "right"):
        duty = 100.0
        duration = max(MIN_TURN_DURATION, min(TURN_DURATION_CAP, duration))
    else:
        duration = min(FORWARD_DURATION_CAP, duration)

    if direction == "forward":
        _drive_pins(_BACKWARD_PINS, duty)  # validated swap -- see module docstring
    elif direction == "backward":
        _drive_pins(_FORWARD_PINS, duty)   # validated swap -- see module docstring
    elif direction == "left":
        _drive_pins(_ARC_LEFT_PINS, duty)   # arc turn -- see arc groups above
    elif direction == "right":
        _drive_pins(_ARC_RIGHT_PINS, duty)  # arc turn -- see arc groups above
    else:
        raise ValueError(f"Unknown direction: {direction}")

    time.sleep(duration)
    stop()
    return f"Moved {direction} at speed {speed:.2f} for {duration:.2f}s"


@atexit.register
def _cleanup():
    if not _initialized:
        return
    stop()
    for pwm in _wheel_pwms.values():
        pwm.stop()
    _wheel_pwms.clear()
    GPIO.cleanup(list(WHEEL_PINS))  # only release these pins, not the sensor's


if __name__ == "__main__":
    print("Sanity check: forward, backward, left, right, each 0.4s at moderate speed")
    for d in ("forward", "backward", "left", "right"):
        print(drive(d, speed=0.5, duration=0.4))
        time.sleep(0.5)
    print("Done. If a direction is reversed from what you expect, double-check")
    print("the wiring against the pin map above before changing any code --")
    print("the forward/backward swap in drive() is intentional and validated.")
