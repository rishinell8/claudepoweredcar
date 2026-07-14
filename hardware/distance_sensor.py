"""
Ultrasonic distance sensor (HC-SR04) on a pan servo, sweeping left/center/
right to check for obstacles.

This replaces the earlier placeholder version of this file. Pin numbers,
ping timing, median sampling, and servo slew logic below are taken directly
from robotcar_standalone.py, which has already been validated on the real
hardware. This module wraps that same validated logic behind the
check()/scan()/is_clear() interface that servers/robot_server.py expects.

Wiring (BCM numbering):
    HC-SR04 TRIG -> GPIO 23
    HC-SR04 ECHO -> GPIO 24  (needs a voltage divider / level shifter --
                              ECHO is 5V, Pi GPIO inputs are only
                              3.3V-tolerant)
    Pan servo    -> GPIO 18
    Buzzer       -> GPIO 22  (active buzzer driven with a PWM tone)

Angle convention (validated): 150 deg = fully left, 90 deg = center,
30 deg = fully right.

This is the car's independent, non-AI safety layer: robot_server.py
consults check("center") before executing any forward move, regardless of
what Claude decides.

Run standalone to test:
    python3 hardware/distance_sensor.py
"""

import atexit
import time

import RPi.GPIO as GPIO

TRIG_PIN = 23
ECHO_PIN = 24
SERVO_PIN = 18
BUZZER_PIN = 22

ANGLE_LEFT = 150
ANGLE_CENTER = 90
ANGLE_RIGHT = 30
_ANGLES = {"left": ANGLE_LEFT, "center": ANGLE_CENTER, "right": ANGLE_RIGHT}

# A positional servo has no speed setting, so instead of snapping straight to
# an angle, slew toward it a few degrees at a time -- gentler on a loose/
# off-center horn and less likely to overshoot.
SERVO_SLEW_DEG_PER_STEP = 5
SERVO_SLEW_STEP_SECONDS = 0.02
SERVO_SETTLE_SECONDS = 0.15

ULTRASONIC_TIMEOUT_SECONDS = 0.02
ULTRASONIC_SAMPLE_COUNT = 5
ULTRASONIC_SAMPLE_DELAY_SECONDS = 0.008

BUZZER_TONE_HZ = 1000  # passive buzzer -- needs a PWM tone, not steady HIGH

_servo_pwm = None
_buzzer_pwm = None
_last_angle = ANGLE_CENTER
_initialized = False


def _setup():
    global _servo_pwm, _buzzer_pwm, _initialized
    if _initialized:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(TRIG_PIN, GPIO.OUT)
    GPIO.setup(ECHO_PIN, GPIO.IN)
    GPIO.setup(SERVO_PIN, GPIO.OUT)
    GPIO.setup(BUZZER_PIN, GPIO.OUT)
    GPIO.output(TRIG_PIN, GPIO.LOW)
    GPIO.output(BUZZER_PIN, GPIO.LOW)

    _servo_pwm = GPIO.PWM(SERVO_PIN, 50)  # 50Hz, standard hobby servo rate
    _servo_pwm.start(0)
    _buzzer_pwm = GPIO.PWM(BUZZER_PIN, BUZZER_TONE_HZ)
    _buzzer_pwm.start(0)
    _initialized = True
    _aim(ANGLE_CENTER)


def _single_ping_cm(timeout_seconds=ULTRASONIC_TIMEOUT_SECONDS):
    """One HC-SR04 ping. Returns distance in cm, or None on timeout (no echo)."""
    GPIO.output(TRIG_PIN, GPIO.LOW)
    time.sleep(0.000002)
    GPIO.output(TRIG_PIN, GPIO.HIGH)
    time.sleep(0.00001)
    GPIO.output(TRIG_PIN, GPIO.LOW)

    wait_start = time.monotonic()
    while GPIO.input(ECHO_PIN) == GPIO.LOW:
        if time.monotonic() - wait_start > timeout_seconds:
            return None
    pulse_start = time.monotonic()

    while GPIO.input(ECHO_PIN) == GPIO.HIGH:
        if time.monotonic() - pulse_start > timeout_seconds:
            return None
    pulse_end = time.monotonic()

    duration_us = (pulse_end - pulse_start) * 1_000_000
    return duration_us / 58.0  # HC-SR04 datasheet: cm = us / 58


def _measure_cm(sample_count=ULTRASONIC_SAMPLE_COUNT) -> float:
    """Median of several pings to reject outliers."""
    readings = []
    for _ in range(sample_count):
        reading = _single_ping_cm()
        readings.append(reading if reading is not None else 9999)
        time.sleep(ULTRASONIC_SAMPLE_DELAY_SECONDS)
    readings.sort()
    return round(readings[len(readings) // 2], 1)


def _servo_duty(angle_degrees):
    return 2.5 + (angle_degrees / 180.0) * 10.0  # 0.5ms-2.5ms pulse at 50Hz


def _aim(angle_degrees):
    global _last_angle
    span = angle_degrees - _last_angle
    steps = max(1, int(abs(span) / SERVO_SLEW_DEG_PER_STEP))
    for i in range(1, steps + 1):
        intermediate = _last_angle + span * (i / steps)
        _servo_pwm.ChangeDutyCycle(_servo_duty(intermediate))
        time.sleep(SERVO_SLEW_STEP_SECONDS)
    _last_angle = angle_degrees
    time.sleep(SERVO_SETTLE_SECONDS)
    # Release the servo once settled instead of holding a continuous PWM
    # signal forever -- the lingering signal from software PWM has enough
    # timing jitter to make the servo buzz/vibrate at rest. A prior fix that
    # called ChangeDutyCycle(0) alone broke the next sweep on the rpi-lgpio
    # backend; a full stop()+start(0) cycle re-primes the PWM object instead
    # of just zeroing its duty cycle, which should avoid whatever left it in
    # a bad state. If a sweep breaks after this, revert to leaving the duty
    # cycle held (remove this stop/start pair).
    _servo_pwm.stop()
    _servo_pwm.start(0)


def check(direction: str = "center") -> float:
    """Point the sensor in one direction ("left"/"center"/"right") and
    return distance to the nearest obstacle, in centimeters."""
    _setup()
    if direction not in _ANGLES:
        raise ValueError(f"direction must be one of {list(_ANGLES)}")
    _aim(_ANGLES[direction])
    return _measure_cm()


def scan() -> dict:
    """Sweep left/center/right and return distance in cm for each, then
    re-center the servo."""
    result = {"left": check("left"), "center": check("center"), "right": check("right")}
    _aim(ANGLE_CENTER)
    return result


def is_clear(direction: str = "center", threshold_cm: float = 30.0) -> bool:
    """Hard safety check used by robot_server.py -- True only if the
    reading is beyond the safety threshold."""
    return check(direction) > threshold_cm


def beep(duration_seconds: float = 0.5) -> str:
    """Sound the onboard buzzer -- validated hardware, exposed here as a
    bonus alert mechanism independent of the speaker."""
    _setup()
    _buzzer_pwm.ChangeDutyCycle(50)
    time.sleep(duration_seconds)
    _buzzer_pwm.ChangeDutyCycle(0)
    return "Beeped"


@atexit.register
def _cleanup():
    # Drop all references to the PWM objects *before* GPIO.cleanup() so their
    # finalizers run while the lgpio backend is still alive -- otherwise
    # RPi.GPIO spews 'Exception ignored in: PWM.__del__ ... NoneType & int'
    # tracebacks when the objects are garbage-collected at interpreter exit,
    # after the chip handle is already gone. (Matches robotcar_standalone.py's
    # _shutdown() ordering.)
    global _servo_pwm, _buzzer_pwm, _initialized
    if not _initialized:
        return
    _servo_pwm.stop()
    _buzzer_pwm.stop()
    _servo_pwm = _buzzer_pwm = None
    GPIO.cleanup([TRIG_PIN, ECHO_PIN, SERVO_PIN, BUZZER_PIN])
    _initialized = False


if __name__ == "__main__":
    print("Scanning left / center / right (cm):")
    print(scan())
