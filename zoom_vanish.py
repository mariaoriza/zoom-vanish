#!/usr/bin/env python3
"""Zoom Vanish: vanish from a Zoom call and reappear, on Linux.

How it works: your real webcam feeds this program, and this program feeds a
virtual camera (v4l2loopback). In Zoom you pick that virtual camera. When you
vanish, the virtual camera shows a still photo of your empty room (with a
little sensor grain so it does not look frozen). Optionally you dissolve into
dust on the way out and reassemble on the way back.

Normal use: open "Zoom (Vanish)" from the app menu. It starts the virtual
camera and Zoom together and stops when you quit Zoom. A room photo is kept
for each place you use it and picked automatically.

Controls while running in a terminal:
  c   capture the empty room (countdown, then step out of frame)
  v   vanish / appear
  d   dust effect on / off
  w   goodbye-wave detection on / off
  s   finger-snap detection on / off
  q   quit

From anywhere (setup.sh binds the first two to Ctrl+Alt+V and Ctrl+Alt+C):
  zoom-vanish --toggle     vanish / appear
  zoom-vanish --capture    capture the empty room for this place
  zoom-vanish --quit       stop
"""

import argparse
import os
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "zoom-vanish"
ROOM_PATH = DATA_DIR / "room.png"
PID_PATH = DATA_DIR / "zoom-vanish.pid"


# ---------------------------------------------------------------------------
# Remote control (handled before importing heavy libraries so it is instant)
# ---------------------------------------------------------------------------

def running_pid():
    """Return the PID of the running instance, or None."""
    try:
        pid = int(PID_PATH.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    # Only signal a process that really is this program.
    return pid if b"zoom_vanish" in cmdline else None


def send_remote(sig):
    pid = running_pid()
    if pid is None:
        print("Zoom Vanish is not running.", file=sys.stderr)
        try:  # from a keyboard shortcut there is no terminal, so say it on screen
            import subprocess
            subprocess.Popen(["notify-send", "--app-name=Zoom Vanish", "--icon=camera-web",
                              "--transient", "Zoom Vanish is not running",
                              "Open Zoom (Vanish) from the app menu."],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        return 1
    os.kill(pid, sig)
    return 0


if __name__ == "__main__":
    _remote = {"--toggle": signal.SIGUSR1, "--capture": signal.SIGUSR2, "--quit": signal.SIGTERM}
    for _flag, _sig in _remote.items():
        if _flag in sys.argv[1:]:
            sys.exit(send_remote(_sig))


import numpy as np  # noqa: E402
import cv2  # noqa: E402


# ---------------------------------------------------------------------------
# Person mask: compare the live frame with the captured empty room
# ---------------------------------------------------------------------------

MASK_SCALE = 4  # masks are computed at 1/4 resolution, which is plenty and fast


def blur_for_mask(img):
    small = cv2.resize(img, (img.shape[1] // MASK_SCALE, img.shape[0] // MASK_SCALE),
                       interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(small, (0, 0), 1.2)


def person_mask(frame, room_small_blur, threshold=26, min_area_frac=0.01, with_hull=False):
    """Boolean mask of pixels that differ from the empty room (that is, you).

    No AI model needed: since we have a photo of the room without you, anything
    that differs from it is you. Works best with stable lighting and a camera
    that is not moved after the room capture. Parts of you that are the same
    color as the wall behind them can be missed, which is why the effect also
    uses the filled outline (hull) of the whole shape.
    """
    h, w = frame.shape[:2]
    fb = blur_for_mask(frame)
    diff = cv2.absdiff(fb, room_small_blur).max(axis=2)
    m = (diff > threshold).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k, iterations=3)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    if n > 1:
        min_area = min_area_frac * m.size
        keep = np.nonzero(stats[1:, cv2.CC_STAT_AREA] >= min_area)[0] + 1
        m = np.isin(labels, keep).astype(np.uint8)
    # Fill holes inside each blob.
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(m, contours, -1, 1, thickness=cv2.FILLED)
    m = cv2.dilate(m, k, iterations=1)
    mask = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST).astype(bool)
    if not with_hull:
        return mask
    hull = np.zeros_like(m)
    if contours:
        pts = np.vstack(contours)
        cv2.fillConvexPoly(hull, cv2.convexHull(pts), 1)
        hull = cv2.dilate(hull, k, iterations=2)
    return mask, cv2.resize(hull, (w, h), interpolation=cv2.INTER_NEAREST).astype(bool)


# ---------------------------------------------------------------------------
# Transition effects
# ---------------------------------------------------------------------------

class Crossfade:
    """Plain fade between you and the empty room (used when dust is off)."""

    def __init__(self, direction, duration=0.35):
        self.direction = direction  # "out" (vanish) or "in" (appear)
        self.duration = duration
        self.start = time.monotonic()

    def render(self, live, room, live_region, now):
        p = min(1.0, (now - self.start) / self.duration)
        a = p if self.direction == "out" else 1.0 - p  # weight of the room
        out = cv2.addWeighted(live, 1.0 - a, room, a, 0)
        return out, p >= 1.0


class Dissolve:
    """Dust effect: you break apart into drifting particles, or reassemble.

    A trigger time is assigned to every pixel (a left to right sweep with some
    low-frequency noise). When a pixel's time comes, it detaches as a particle
    that drifts right and upward, fades, and turns dusty. Appearing plays the
    same animation backwards.
    """

    SWEEP_FRAC = 0.62   # share of the duration spent sweeping across you
    MAX_PARTICLES = 45000
    DUST_COLOR = np.array([62, 78, 96], dtype=np.float32)  # BGR, dusty brown-gray

    def __init__(self, snapshot, mask, direction, duration=2.6, seed=None):
        rng = np.random.default_rng(seed)
        h, w = mask.shape
        self.direction = direction
        self.duration = duration
        self.start = time.monotonic()
        self.w, self.h = w, h
        self.life = duration * (1.0 - self.SWEEP_FRAC)
        scale = w / 1280.0

        cols = np.nonzero(mask.any(axis=0))[0]
        x0, x1 = (cols.min(), cols.max()) if cols.size else (0, w - 1)
        ramp = np.clip((np.arange(w, dtype=np.float32) - x0) / max(1, x1 - x0), 0, 1)
        coarse = rng.random((h // 24 + 2, w // 24 + 2)).astype(np.float32)
        noise = cv2.resize(coarse, (w, h), interpolation=cv2.INTER_CUBIC)
        sweep = np.clip(0.65 * ramp[None, :] + 0.35 * noise, 0, 1)
        self.trigger = sweep * (duration * self.SWEEP_FRAC)

        stride = 3 if w >= 960 else 2
        ys, xs = np.nonzero(mask[::stride, ::stride])
        ys, xs = ys * stride, xs * stride
        if ys.size > self.MAX_PARTICLES:
            pick = rng.choice(ys.size, self.MAX_PARTICLES, replace=False)
            ys, xs = ys[pick], xs[pick]
        n = ys.size
        self.count = n
        self.x0 = xs.astype(np.float32)
        self.y0 = ys.astype(np.float32)
        self.color = snapshot[ys, xs].astype(np.float32)
        self.t0 = self.trigger[ys, xs]
        self.vx = rng.uniform(70, 260, n).astype(np.float32) * scale
        self.vy = rng.uniform(-170, -25, n).astype(np.float32) * scale
        self.ax = rng.uniform(30, 120, n).astype(np.float32) * scale
        self.ay = rng.uniform(-60, 10, n).astype(np.float32) * scale
        self.phase = rng.uniform(0, 2 * np.pi, n).astype(np.float32)
        self.wobble = rng.uniform(2, 9, n).astype(np.float32) * scale
        self.size = 2 if w >= 960 else 1

    def render(self, live, room, live_region, now):
        t = now - self.start
        done = t >= self.duration
        t_eff = t if self.direction == "out" else self.duration - t

        # Inside your outline, whatever the sweep has not reached yet stays live
        # video. Outside it the live picture and the room photo look the same.
        solid = live_region & (self.trigger > t_eff)
        out = np.where(solid[..., None], live, room)

        age = t_eff - self.t0
        act = (age > 0) & (age < self.life)
        if act.any():
            a = age[act]
            px = self.x0[act] + self.vx[act] * a + 0.5 * self.ax[act] * a * a \
                + self.wobble[act] * np.sin(self.phase[act] + 7.0 * a)
            py = self.y0[act] + self.vy[act] * a + 0.5 * self.ay[act] * a * a
            frac = a / self.life
            alpha = np.clip(1.0 - frac, 0, 1) ** 1.3
            col = self.color[act] * (1 - 0.65 * frac[:, None]) + self.DUST_COLOR * (0.65 * frac[:, None])
            pxi = px.astype(np.int32)
            pyi = py.astype(np.int32)
            for dy in range(self.size):
                for dx in range(self.size):
                    xx, yy = pxi + dx, pyi + dy
                    ok = (xx >= 0) & (xx < self.w) & (yy >= 0) & (yy < self.h)
                    if not ok.any():
                        continue
                    xo, yo, al, co = xx[ok], yy[ok], alpha[ok][:, None], col[ok]
                    base = out[yo, xo].astype(np.float32)
                    out[yo, xo] = (base * (1 - al) + co * al).astype(np.uint8)
        return out, done


# ---------------------------------------------------------------------------
# Finger-snap detection (optional, uses the microphone)
# ---------------------------------------------------------------------------

class SnapDetector:
    """Detects a finger snap: a sudden, very short, high-frequency burst.

    Heuristic, not a trained model. Claps and sharp knocks can also trigger it.
    Run with --snap-debug to see the scores and tune --snap-sensitivity.
    """

    def __init__(self, on_snap, sensitivity=1.0, debug=False, samplerate=48000, block=512):
        self.on_snap = on_snap
        self.sens = sensitivity
        self.debug = debug
        self.sr = samplerate
        self.block = block
        self.window = np.hanning(block).astype(np.float32)
        freqs = np.fft.rfftfreq(block, 1.0 / samplerate)
        self.hf_band = (freqs >= 2500) & (freqs <= 9000)
        self.lf_band = (freqs >= 100) & (freqs < 1000)
        self.history = deque(maxlen=150)  # about 1.6 s of background level
        self.prev = 0.0
        self.last_fire = 0.0
        self.stream = None

    def score_block(self, x):
        """Return True if this audio block looks like a snap onset."""
        spec = np.abs(np.fft.rfft(x * self.window))
        hf = float(spec[self.hf_band].mean())
        lf = float(spec[self.lf_band].mean()) + 1e-9
        base = float(np.median(self.history)) if len(self.history) >= 40 else None
        prev = self.prev
        self.history.append(hf)
        self.prev = hf
        if base is None:
            return False
        rise_bg = hf / (base + 1e-9)
        rise_prev = hf / (prev + 1e-9)
        need = 14.0 / self.sens
        hit = (rise_bg > need and rise_prev > need / 2.5 and hf > lf * 0.7
               and hf > 0.02 / self.sens)
        if self.debug and rise_bg > need / 3:
            print(f"  [snap] hf={hf:.3f} vs background x{rise_bg:.1f}, vs previous x{rise_prev:.1f}, "
                  f"hf/lf={hf / lf:.2f} -> {'SNAP' if hit else 'no'}")
        return hit

    def _callback(self, indata, frames, time_info, status):
        if self.score_block(indata[:, 0].astype(np.float32)):
            now = time.monotonic()
            if now - self.last_fire > 1.5:
                self.last_fire = now
                self.on_snap()

    def start(self):
        import sounddevice as sd
        self.stream = sd.InputStream(channels=1, samplerate=self.sr, blocksize=self.block,
                                     dtype="float32", callback=self._callback)
        self.stream.start()

    def stop(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None


# ---------------------------------------------------------------------------
# Wave detection (optional, uses the camera picture you already send)
# ---------------------------------------------------------------------------

class WaveDetector:
    """Decides whether a stream of hand positions is a goodbye wave.

    Feed it one sample per analysed frame: time, palm x and y (0 to 1 across
    the picture) and whether the palm is open, or None when no hand is seen.
    A wave is an open hand, raised, swinging left and right at least three
    times in a row, each swing quick (between about 0.1 and 0.55 s) and wide
    enough. Talking with your hands usually fails one of these: the hand is
    not open the whole time, the swings are uneven, or there are fewer than
    three in a row.
    """

    def __init__(self, sensitivity=1.0, debug=False):
        self.sens = sensitivity
        self.debug = debug
        self.samples = deque()          # (t, x, y, open)
        self.window = 2.2               # seconds of history considered
        self.min_swings = 3
        self.min_amp = 0.045 / sensitivity   # fraction of the picture width
        self.cooldown = 2.5
        self.last_fire = -1e9
        self.armed = True   # after a wave, wait until you stop waving

    def add(self, t, hand):
        if hand is None:
            # Fast waving can blur the hand for a frame or two; only start over
            # if it has been gone for a moment.
            if self.samples and t - self.samples[-1][0] > 0.4:
                self.samples.clear()
            return False
        x, y, is_open = hand
        self.samples.append((t, x, y, is_open))
        while self.samples and t - self.samples[0][0] > self.window:
            self.samples.popleft()
        if len(self.samples) < 6:
            return False
        swings = self._swings_in_a_row()
        if not self.armed:
            # Re-arm only once the waving has stopped, so one long wave
            # does not vanish you and then bring you straight back.
            if swings == 0 and t - self.last_fire > self.cooldown:
                self.armed = True
            return False
        if swings >= self.min_swings:
            self.last_fire = t
            self.armed = False
            return True
        return False

    def _swings_in_a_row(self):
        ts = np.array([s[0] for s in self.samples])
        xs = np.array([s[1] for s in self.samples])
        ys = np.array([s[2] for s in self.samples])
        opens = np.array([s[3] for s in self.samples], dtype=bool)
        if opens.mean() < 0.8:
            return 0
        if np.median(ys) > 0.75:      # hand down by the desk, not raised
            return 0
        if ys.max() - ys.min() > 0.35:  # big vertical motion: not a wave
            return 0
        if xs.size >= 3:              # light smoothing against tracking jitter
            xs = np.convolve(np.pad(xs, 1, mode="edge"), np.ones(3) / 3, mode="valid")
        # Turning points of the horizontal motion, ignoring jitter smaller
        # than the minimum swing.
        extrema = [(ts[0], xs[0])]
        direction = 0
        for t, x in zip(ts[1:], xs[1:]):
            last_t, last_x = extrema[-1]
            dx = x - last_x
            if direction == 0:
                if abs(dx) >= self.min_amp:
                    direction = 1 if dx > 0 else -1
                    extrema.append((t, x))
            elif dx * direction > 0:
                extrema[-1] = (t, x)          # still moving the same way
            elif abs(dx) >= self.min_amp:
                direction = -direction
                extrema.append((t, x))        # turned around
        swings = []
        for (t0, x0), (t1, x1) in zip(extrema, extrema[1:]):
            swings.append((t1 - t0, abs(x1 - x0)))
        # The first point is the start of the window, not a real turn.
        run = 0
        best = 0
        for dur, amp in swings[1:] if len(swings) > 1 else []:
            if 0.11 <= dur <= 0.55 and amp >= self.min_amp:
                run += 1
                best = max(best, run)
            else:
                run = 0
        if self.debug and best >= 1:
            print(f"  [wave] swings in a row={best} (need {self.min_swings}), "
                  f"open palm {opens.mean():.0%}, height {np.median(ys):.2f}")
        return best


GESTURE_MODEL_PATH = DATA_DIR / "models" / "gesture_recognizer.task"
FINGERS = ((8, 6), (12, 10), (16, 14), (20, 18))  # (tip, middle joint) landmark ids


def palm_from_landmarks(lm):
    """Palm center x, y and whether at least 3 of 4 fingers are stretched out."""
    pts = np.array([(p.x, p.y) for p in lm], dtype=np.float32)
    palm = pts[[0, 5, 9, 13, 17]].mean(axis=0)
    wrist = pts[0]
    stretched = sum(np.linalg.norm(pts[tip] - wrist) > np.linalg.norm(pts[mid] - wrist) * 1.1
                    for tip, mid in FINGERS)
    size = float(np.ptp(pts[:, 0]) + np.ptp(pts[:, 1]))
    return float(palm[0]), float(palm[1]), stretched >= 3, size


class HandWatcher(threading.Thread):
    """Runs Google's MediaPipe hand model on the camera picture in the
    background (about 15 times a second, at low resolution, entirely on your
    computer) and reports goodbye waves."""

    def __init__(self, on_wave, sensitivity=1.0, debug=False, rate=15.0):
        super().__init__(daemon=True)
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
        if not GESTURE_MODEL_PATH.exists():
            raise RuntimeError(f"hand model missing at {GESTURE_MODEL_PATH} (run setup.sh again)")
        self.mp = mp
        opts = vision.GestureRecognizerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(GESTURE_MODEL_PATH)),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=2,
            min_hand_detection_confidence=0.5,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.recognizer = vision.GestureRecognizer.create_from_options(opts)
        self.on_wave = on_wave
        self.detector = WaveDetector(sensitivity, debug)
        self.period = 1.0 / rate
        self.lock = threading.Lock()
        self.latest = None
        self.stop_event = threading.Event()
        self.t_start = time.monotonic()

    def submit(self, frame):
        """Called from the video loop with every frame; only the newest is kept."""
        with self.lock:
            self.latest = frame

    def analyse(self, frame, t):
        small = cv2.resize(frame, (640, 360), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        result = self.recognizer.recognize_for_video(image, int((t - self.t_start) * 1000))
        best = None
        for i, lm in enumerate(result.hand_landmarks):
            x, y, stretched, size = palm_from_landmarks(lm)
            labelled_open = bool(result.gestures and result.gestures[i]
                                 and result.gestures[i][0].category_name == "Open_Palm")
            if best is None or size > best[3]:
                best = (x, y, stretched or labelled_open, size)
        return None if best is None else best[:3]

    def run(self):
        while not self.stop_event.is_set():
            t = time.monotonic()
            with self.lock:
                frame, self.latest = self.latest, None
            if frame is not None:
                try:
                    if self.detector.add(t, self.analyse(frame, t)):
                        say("Wave detected.")
                        self.on_wave()
                except Exception as exc:
                    say("Wave detection stopped", popup=True, body=str(exc))
                    return
            self.stop_event.wait(max(0.0, self.period - (time.monotonic() - t)))

    def stop(self):
        self.stop_event.set()
        self.join(timeout=1)
        self.recognizer.close()


# ---------------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------------

def make_grain_frames(room, count=8, sigma=1.6, seed=1):
    """Pre-render a few copies of the room with sensor grain."""
    rng = np.random.default_rng(seed)
    base = room.astype(np.int16)
    frames = []
    for _ in range(count):
        g = rng.normal(0, sigma, room.shape[:2] + (1,)).astype(np.int16)
        frames.append(np.clip(base + g, 0, 255).astype(np.uint8))
    return frames


def open_camera(src, width, height, fps):
    dev = int(src) if str(src).isdigit() else src
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    return cap


class KeyReader(threading.Thread):
    """Reads single key presses from the terminal without needing Enter."""

    KEYS = {"c": "capture", "v": "toggle", " ": "toggle", "d": "dust", "s": "snap",
            "w": "wave", "q": "quit"}

    def __init__(self, commands):
        super().__init__(daemon=True)
        self.commands = commands
        self.stop_event = threading.Event()
        self.saved = None

    def run(self):
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        try:
            while not self.stop_event.is_set():
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    ch = os.read(fd, 1).decode(errors="ignore").lower()
                    if ch in self.KEYS:
                        self.commands.append(self.KEYS[ch])
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, self.saved)


# ---------------------------------------------------------------------------
# Notifications and logging (so it can run without a terminal)
# ---------------------------------------------------------------------------

LOG_PATH = DATA_DIR / "zoom-vanish.log"


def notify(title, body=""):
    """Show a desktop notification. Silently does nothing if unavailable."""
    import subprocess
    try:
        subprocess.Popen(["notify-send", "--app-name=Zoom Vanish", "--icon=camera-web",
                          "--expire-time=4000", "--transient", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def say(msg, popup=False, body=""):
    """Print (to the terminal or the log file) and optionally pop up a notification."""
    print(time.strftime("%H:%M:%S"), msg, flush=True)
    if popup:
        notify(msg, body)


def make_private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)  # room photos show your home: keep them to yourself
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Room photos: one per place, picked automatically
# ---------------------------------------------------------------------------

ROOMS_DIR = DATA_DIR / "rooms"
MATCH_OK = 0.6        # share of background pixels that must agree with a room photo
SAME_PLACE = 0.85     # a new photo this similar to an old one replaces it
MAX_ROOMS = 20


def room_signature(img):
    small = cv2.resize(img, (160, 90), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(small, (0, 0), 1.0).astype(np.float32)


def _background_band(shape):
    """Where background usually shows even when you sit in front of the camera:
    the top of the picture and the outer left and right strips."""
    h, w = shape[:2]
    band = np.zeros((h, w), dtype=bool)
    band[: int(h * 0.3), :] = True
    band[:, : int(w * 0.2)] = True
    band[:, int(w * 0.8):] = True
    return band


_BAND = _background_band((90, 160))


def agreement(sig_a, sig_b):
    """How much of the background band looks the same in both pictures (0 to 1),
    after evening out overall brightness so a lamp or auto-exposure change does
    not count as a different place."""
    gain = float(sig_a[_BAND].mean()) / max(1.0, float(sig_b[_BAND].mean()))
    b = np.clip(sig_b * gain, 0, 255)
    d = np.abs(sig_a - b).max(axis=2)
    return float((d[_BAND] < 30).mean())


class RoomLibrary:
    def __init__(self, width, height):
        self.w, self.h = width, height
        make_private_dir(DATA_DIR)
        make_private_dir(ROOMS_DIR)
        if ROOM_PATH.exists():  # photo from the first version: keep it as a place
            ROOM_PATH.rename(ROOMS_DIR / "room-first.png")
        self.rooms = {}
        for path in sorted(ROOMS_DIR.glob("*.png")):
            img = cv2.imread(str(path))
            if img is not None:
                self.rooms[path] = room_signature(img)

    def best(self, frame):
        """(path, score) of the photo that best matches what the camera sees."""
        sig = room_signature(frame)
        scored = [(agreement(sig, r), p) for p, r in self.rooms.items()]
        if not scored:
            return None, 0.0
        score, path = max(scored)
        return path, score

    def image(self, path):
        img = cv2.imread(str(path))
        if img is not None and (img.shape[1] != self.w or img.shape[0] != self.h):
            img = cv2.resize(img, (self.w, self.h), interpolation=cv2.INTER_AREA)
        return img

    def add(self, room):
        """Save a new photo, replacing an older one of the same place."""
        sig = room_signature(room)
        same = [p for p, r in self.rooms.items() if agreement(sig, r) >= SAME_PLACE]
        for p in same:
            p.unlink(missing_ok=True)
            del self.rooms[p]
        path = ROOMS_DIR / f"room-{time.strftime('%Y%m%d-%H%M%S')}.png"
        cv2.imwrite(str(path), room)
        os.chmod(path, 0o600)
        self.rooms[path] = sig
        while len(self.rooms) > MAX_ROOMS:  # forget the oldest place
            oldest = min(self.rooms, key=lambda q: q.stat().st_mtime)
            oldest.unlink(missing_ok=True)
            del self.rooms[oldest]
        return path, bool(same)


# ---------------------------------------------------------------------------
# Zoom launching
# ---------------------------------------------------------------------------

ZOOM_PROCESS_NAMES = {"zoom", "ZoomLauncher", "ZoomWebviewHost", "zoom.real"}


def zoom_running():
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            if comm.read_text().strip() in ZOOM_PROCESS_NAMES:
                return True
        except OSError:
            continue
    return False


def launch_zoom():
    import shutil
    import subprocess
    exe = shutil.which("zoom") or ("/usr/bin/zoom" if os.path.exists("/usr/bin/zoom") else None)
    if exe is None:
        return False
    subprocess.Popen([exe], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


class ZoomWatcher:
    """In --with-zoom mode: notices when Zoom has been closed."""

    def __init__(self, start_grace=90.0, gone_after=5.0):
        self.started = time.monotonic()
        self.seen = False
        self.last_seen = None
        self.next_check = 0.0
        self.start_grace = start_grace
        self.gone_after = gone_after

    def zoom_closed(self, now, running=None):
        if now < self.next_check:
            return False
        self.next_check = now + 2.0
        running = zoom_running() if running is None else running
        if running:
            self.seen, self.last_seen = True, now
            return False
        if self.seen:
            return now - self.last_seen > self.gone_after
        return now - self.started > self.start_grace  # Zoom never came up


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------

class App:
    SETTLE_FRAMES = 45  # let the webcam adjust exposure before matching a room

    def __init__(self, args, width, height):
        self.args = args
        self.commands = deque()
        self.state = "live"         # live, countdown, capturing, vanishing, vanished, appearing
        self.effect = None
        self.dust = not args.no_dust
        self.library = RoomLibrary(width, height)
        self.room = None
        self.room_path = None
        self.room_blur = None
        self.grain = []
        self.grain_i = 0
        self.capture_buf = []
        self.countdown_end = 0.0
        self.last_count = None
        self.snap = None
        self.wave = None
        self.frames_seen = 0
        self.matched_once = False

    # -- room ---------------------------------------------------------------
    def set_room(self, room, path=None):
        self.room = room
        self.room_path = path
        self.room_blur = blur_for_mask(room)
        self.grain = make_grain_frames(room)

    def pick_room(self, frame, at_startup=False):
        """Use the saved photo of this place. Returns True if one matched."""
        path, score = self.library.best(frame)
        if path is not None and score >= MATCH_OK:
            if path != self.room_path:
                self.set_room(self.library.image(path), path)
                say(f"Using room photo {path.name} (match {score:.0%}).")
            return True
        if at_startup:
            if not self.library.rooms:
                say("No room photo yet.", popup=True,
                    body="Press Ctrl+Alt+C, then step out of frame for the countdown.")
            else:
                say(f"New place? Best saved photo only matches {score:.0%}.", popup=True,
                    body="Press Ctrl+Alt+C and step out of frame to capture this room.")
        return False

    def room_frame(self):
        self.grain_i = (self.grain_i + 1) % len(self.grain)
        return self.grain[self.grain_i]

    # -- commands -----------------------------------------------------------
    def handle(self, cmd, frame):
        if cmd == "quit":
            return False
        if cmd == "dust":
            self.dust = not self.dust
            say(f"Dust effect {'ON' if self.dust else 'OFF'}")
        elif cmd == "snap":
            self.toggle_snap()
        elif cmd == "wave":
            self.toggle_wave()
        elif cmd == "capture":
            if self.state in ("live", "vanished"):
                self.state = "countdown"
                self.countdown_end = time.monotonic() + self.args.countdown
                self.last_count = None
                say(f"Capturing the room in {self.args.countdown} seconds", popup=True,
                    body="Step out of frame now.")
        elif cmd == "toggle":
            self.toggle(frame)
        return True

    def toggle(self, frame):
        if self.state in ("vanishing", "appearing", "countdown", "capturing"):
            return  # ignore while a transition is running
        if self.state == "live" and not self.pick_room(frame):
            if self.room is None:
                say("Can't vanish yet: no room photo for this place.", popup=True,
                    body="Press Ctrl+Alt+C and step out of frame first.")
                return
            say("The room photo doesn't match this place well.", popup=True,
                body="Vanishing anyway. Capture a new one with Ctrl+Alt+C.")
        now_mask = person_mask(frame, self.room_blur, self.args.threshold)
        direction = "out" if self.state == "live" else "in"
        use_dust = self.dust and now_mask.sum() > 0.005 * now_mask.size
        if self.dust and not use_dust:
            say("Could not find you in the frame, so no dust this time.")
        self.effect = (Dissolve(frame, now_mask, direction, self.args.duration) if use_dust
                       else Crossfade(direction))
        self.state = "vanishing" if direction == "out" else "appearing"
        say("Vanishing..." if direction == "out" else "Appearing...")

    def toggle_snap(self):
        if self.snap is None:
            try:
                self.snap = SnapDetector(lambda: self.commands.append("toggle"),
                                         self.args.snap_sensitivity, self.args.snap_debug)
                self.snap.start()
                say("Snap detection ON")
            except Exception as exc:  # missing sounddevice or no microphone
                self.snap = None
                say("Snap detection unavailable", popup=True, body=str(exc))
        else:
            self.snap.stop()
            self.snap = None
            say("Snap detection OFF")

    def toggle_wave(self):
        if self.wave is None:
            try:
                self.wave = HandWatcher(lambda: self.commands.append("toggle"),
                                        self.args.wave_sensitivity, self.args.wave_debug)
                self.wave.start()
                say("Wave detection ON")
            except Exception as exc:  # mediapipe or the hand model missing
                self.wave = None
                say("Wave detection unavailable", popup=True,
                    body=f"{exc}. Ctrl+Alt+V still works.")
        else:
            self.wave.stop()
            self.wave = None
            say("Wave detection OFF")

    # -- per frame ------------------------------------------------------------
    def process(self, frame):
        now = time.monotonic()
        self.frames_seen += 1
        if not self.matched_once and self.frames_seen >= self.SETTLE_FRAMES:
            self.matched_once = True
            if self.state == "live":
                self.pick_room(frame, at_startup=True)
        if self.state == "countdown":
            left = int(np.ceil(self.countdown_end - now))
            if left != self.last_count and left > 0:
                say(f"  {left}...")
                self.last_count = left
            if now >= self.countdown_end:
                self.state = "capturing"
                self.capture_buf = []
            return frame
        if self.state == "capturing":
            self.capture_buf.append(frame.astype(np.float32))
            if len(self.capture_buf) >= 20:
                room = np.mean(self.capture_buf, axis=0).astype(np.uint8)
                path, replaced = self.library.add(room)
                self.set_room(room, path)
                self.capture_buf = []
                self.state = "live"
                say("Room captured" + (" (updated this place)" if replaced else " (new place saved)"),
                    popup=True, body="You can come back now. Wave goodbye or press Ctrl+Alt+V to vanish.")
            return frame
        if self.state == "vanished":
            return self.room_frame()
        if self.state in ("vanishing", "appearing"):
            _, region = person_mask(frame, self.room_blur, self.args.threshold, with_hull=True)
            out, done = self.effect.render(frame, self.room_frame(), region, now)
            if done:
                self.effect = None
                self.state = "vanished" if self.state == "vanishing" else "live"
                say("Gone." if self.state == "vanished" else "Back.")
            return out
        return frame


def parse_args():
    p = argparse.ArgumentParser(description="Vanish from Zoom calls via a virtual camera.")
    p.add_argument("--camera", default="/dev/video0", help="real webcam (default /dev/video0)")
    p.add_argument("--output", default="/dev/video10", help="v4l2loopback device (default /dev/video10)")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--countdown", type=int, default=5, help="seconds before the room photo")
    p.add_argument("--duration", type=float, default=2.6, help="dust animation length in seconds")
    p.add_argument("--threshold", type=int, default=26,
                   help="how different a pixel must be from the room to count as you (raise if the room "
                        "is grainy, lower if parts of you are missed)")
    p.add_argument("--no-dust", action="store_true", help="start with the dust effect off")
    p.add_argument("--snap", action="store_true", help="start with finger-snap detection on")
    p.add_argument("--snap-sensitivity", type=float, default=1.0, help="higher = triggers more easily")
    p.add_argument("--snap-debug", action="store_true", help="print snap detector scores")
    p.add_argument("--wave", action="store_true", help="start with goodbye-wave detection on")
    p.add_argument("--wave-sensitivity", type=float, default=1.0,
                   help="higher = smaller waves count")
    p.add_argument("--wave-debug", action="store_true", help="print wave detector details")
    p.add_argument("--with-zoom", action="store_true",
                   help="start Zoom too, and stop when Zoom is closed (used by the menu entry)")
    p.add_argument("--toggle", action="store_true", help="vanish/appear in the running instance")
    p.add_argument("--capture", action="store_true", help="capture the room in the running instance")
    p.add_argument("--quit", action="store_true", help="stop the running instance")
    return p.parse_args()


def fail(msg, body=""):
    say(msg, popup=True, body=body)
    return 1


def main():
    args = parse_args()
    make_private_dir(DATA_DIR)
    if not sys.stdout.isatty():
        # Started from the menu: keep a log instead of printing to nowhere.
        log = open(LOG_PATH, "a", buffering=1)
        sys.stdout = sys.stderr = log
    say("---- starting")

    if running_pid() is not None:
        if args.with_zoom:
            launch_zoom()  # camera already running: just open Zoom
            return 0
        return fail("Zoom Vanish is already running.")
    try:
        import pyvirtualcam
    except ImportError:
        return fail("Zoom Vanish isn't installed properly", "Run setup.sh again.")
    if not os.path.exists(args.output):
        return fail("Vanish Cam is missing", f"{args.output} doesn't exist. Reboot, or run setup.sh again.")
    if os.path.realpath(args.camera) == os.path.realpath(args.output):
        return fail("--camera and --output are the same device.")

    cap = open_camera(args.camera, args.width, args.height, args.fps)
    ok, frame = (cap.read() if cap is not None else (False, None))
    if not ok:
        if args.with_zoom and zoom_running():
            return fail("Zoom is already using the webcam",
                        "Quit Zoom completely, then open Zoom (Vanish) again.")
        return fail("Could not open the webcam",
                    f"{args.camera} is busy or missing. Close other video apps and try again.")

    w, h = args.width, args.height
    app = App(args, w, h)

    PID_PATH.write_text(str(os.getpid()))
    signal.signal(signal.SIGUSR1, lambda *_: app.commands.append("toggle"))
    signal.signal(signal.SIGUSR2, lambda *_: app.commands.append("capture"))
    signal.signal(signal.SIGTERM, lambda *_: app.commands.append("quit"))
    for sig in (signal.SIGUSR1, signal.SIGUSR2, signal.SIGTERM):
        # Resume the webcam read after a shortcut's signal instead of failing it
        # ("Interrupted system call"), which used to stop the program.
        signal.siginterrupt(sig, False)

    keys = None
    if sys.stdin.isatty():
        keys = KeyReader(app.commands)
        keys.start()
        print("Controls" + __doc__.split("Controls", 1)[1])
    if args.snap:
        app.toggle_snap()
    if args.wave:
        app.toggle_wave()

    watcher = None
    try:
        with pyvirtualcam.Camera(width=w, height=h, fps=args.fps, device=args.output,
                                 fmt=pyvirtualcam.PixelFormat.BGR) as cam:
            cam.send(np.ascontiguousarray(frame))  # Vanish Cam is now visible to apps
            say(f"Virtual camera running on {cam.device}.")
            if args.with_zoom:
                if not zoom_running() and not launch_zoom():
                    raise RuntimeError("Zoom is not installed (no 'zoom' command found).")
                watcher = ZoomWatcher()
            running = True
            failures = 0
            while running:
                ok, frame = cap.read()
                if not ok:
                    # A dropped frame is not the end: retry, then reopen the webcam.
                    failures += 1
                    if failures == 15:
                        say("Webcam stopped answering, reopening it.")
                        cap.release()
                        cap = open_camera(args.camera, w, h, args.fps) or cap
                    elif failures >= 45:
                        say("Lost the webcam signal.", popup=True)
                        break
                    time.sleep(0.02)
                    continue
                failures = 0
                if frame.shape[1] != w or frame.shape[0] != h:
                    frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
                if app.wave is not None:
                    app.wave.submit(frame)
                while app.commands and running:
                    running = app.handle(app.commands.popleft(), frame)
                cam.send(np.ascontiguousarray(app.process(frame)))
                if watcher is not None and watcher.zoom_closed(time.monotonic()):
                    say("Zoom closed, stopping Vanish Cam.")
                    running = False
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        say(f"Stopped because of an error: {exc}", popup=True)
    finally:
        if keys is not None:
            keys.stop_event.set()
            keys.join(timeout=1)
        if app.snap is not None:
            app.snap.stop()
        if app.wave is not None:
            app.wave.stop()
        cap.release()
        try:
            PID_PATH.unlink()
        except OSError:
            pass
        say("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
