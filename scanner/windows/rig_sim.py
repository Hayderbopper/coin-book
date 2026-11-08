"""rig_sim.py: a pretend coin rig, so coin_scan.py runs with no hardware.

Put this file next to coin_scan.py. Importing it swaps the two pieces of
hardware for simulated ones, and nothing in coin_scan.py has to change:

  * The Arduino Nano.  `serial.Serial` is replaced by a fake port that runs a
    Python copy of coin_stage.ino: the same PING / MOVE / JOG / HOME / POS / OFF
    commands, the same half-step maths, soft limits and backlash overshoot.
    The settings at the top of the .ino (LEAD_MM, LIMIT_MM, ...) are read from
    ../arduino/coin_stage/coin_stage.ino when it is there, so changing the
    firmware changes the simulation.
  * The USB microscope.  `cv2.VideoCapture` is replaced by a fake camera that
    looks down at a coin lying on a mat. It renders what a real one would see
    at the stage's actual position, including the things that make the real
    rig hard: gear play, a camera that is slightly rotated, a coin that is not
    quite flat (so the focus drifts), uneven ring-light, sensor noise, stale
    frames left in the camera's buffer, and wobble if a photo is taken before
    the stage has settled.

Two ways to use it:

    python rig_sim.py calibrate
    python rig_sim.py scan 16 --out test
    python rig_sim.py stitch test           (needs Hugin)
    python rig_sim.py cropfinal test/coin.tif

which runs coin_scan.py with the simulated hardware, or from your own code:

    import rig_sim                 # must come before the hardware is opened
    rig_sim.configure(coin="my_coin.png", coin_mm=19.05)
    import coin_scan
    ...

Options can also go in rig_sim.json next to this file, or on the command line
as --sim-<name> VALUE (for example --sim-coin my_coin.png --sim-coin-mm 19.05).
They are listed in SETTINGS below.

Only numpy and opencv are needed; pyserial is not.
"""

import atexit
import collections
import json
import math
import os
import re
import sys
import time
import types

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- settings

SETTINGS = {
    # What is under the microscope.
    "coin": "",                  # coin picture: a transparent PNG cut out round the coin (like the
                                 # coin book's images), or a plain photo; empty = draw a test coin
    "coin_mm": 16.0,             # the coin's real diameter
    "coin_offset_mm": [0.0, 0.0],  # how far off the stage centre the coin was put down
    "coin_tilt_mm": 0.04,        # how much higher one side of the coin sits than the other
    "background": "mat",         # mat, felt, paper or a picture file

    # The microscope.
    "resolution": [800, 600],    # what the camera really gives, whatever is asked for
    "px_per_mm": 100.0,          # magnification (800 x 600 at 100 px/mm sees 8 x 6 mm)
    "camera_angle_deg": 3.0,     # camera turned slightly on its mount
    "camera_mirrored": False,    # some microscopes flip the picture
    "depth_of_field_mm": 0.15,   # how far from focus before the picture blurs
    "focus_error_mm": 0.0,       # focus set at the centre, plus this
    "vignette": 0.12,            # how much darker the corners are (0 = even light)
    "noise": 2.0,                # sensor noise, in grey levels
    "buffered_frames": 3,        # old frames the camera keeps; coin_scan throws 5 away
    "settle_time_s": 0.25,       # the stage wobbles for this long after a move

    # The stage, on top of what the .ino says.
    "gear_play_mm": 0.12,        # backlash in the 28BYJ-48 gearbox
    "step_error_mm": 0.008,      # random error in where each move really ends
    "time_scale": 0.0,           # 1 = moves take as long as the real motors, 0 = instant

    "seed": 1,                   # change for a different random wobble
    "ports": ["COM3", "COM4", "COM5", "/dev/ttyUSB0"],  # names the fake Nano answers on
    "quiet": False,              # True = no summary at the end
}


DEFAULT_SETTINGS = json.loads(json.dumps(SETTINGS))  # before rig_sim.json changes anything


def configure(**kwargs):
    """Change settings, e.g. configure(coin="cent.png", coin_mm=19.05). Call before the hardware opens."""
    for key, value in kwargs.items():
        if key not in SETTINGS:
            raise KeyError(f"rig_sim has no setting {key!r}. It has: {', '.join(SETTINGS)}")
        SETTINGS[key] = value
    global _world
    _world = None  # rebuilt with the new settings next time it is needed


def _load_settings_file():
    path = os.path.join(HERE, "rig_sim.json")
    if os.path.exists(path):
        with open(path) as f:
            configure(**json.load(f))


# ---------------------------------------------------------------- the Arduino

FIRMWARE_DEFAULTS = {
    "LEAD_MM": 8.0, "STEPS_PER_REV": 4096.0, "BACKLASH_MM": 0.3, "LIMIT_MM": 24.0,
    "STEP_US": 1200, "INVERT_X": False, "INVERT_Y": False,
}


def read_firmware_settings(path=None):
    """The `const` settings at the top of coin_stage.ino, so the simulation follows the real firmware."""
    path = path or os.path.join(HERE, "..", "arduino", "coin_stage", "coin_stage.ino")
    found = dict(FIRMWARE_DEFAULTS)
    if not os.path.exists(path):
        return found
    with open(path) as f:
        text = f.read()
    for name in FIRMWARE_DEFAULTS:
        m = re.search(rf"const\s+[\w ]+?\s+{name}\s*=\s*([^;]+);", text)
        if m:
            value = m.group(1).strip()
            if value in ("true", "false"):
                found[name] = value == "true"
            else:
                found[name] = float(value)
    return found


def _to_float(text):
    """Arduino's String.toFloat(): reads a leading number and ignores the rest (0 if there is none)."""
    m = re.match(r"\s*[-+]?(\d+\.?\d*|\.\d+)", text)
    return float(m.group(0)) if m else 0.0


class Axis:
    """One lead-screw axis. `pos` is the firmware's half-step count. `carriage_mm` is where the
    carriage really is (0 = the coin's centre under the lens); it lags behind the motor by the
    gear play whenever the direction changes."""

    def __init__(self, name, invert, fw, carriage_mm=0.0):
        self.name, self.invert, self.fw = name, invert, fw
        self.pos = 0
        self.phase = 0
        self.energised = False
        self.carriage_mm = carriage_mm
        # The Nano counts from wherever it is when it switches on. The last move always ends
        # going forward (the firmware sees to that), so the play is taken up on that side.
        self.origin_mm = carriage_mm + SETTINGS["gear_play_mm"] / 2
        self.steps_taken = 0

    @property
    def motor_mm(self):
        """Where the motor side of the gear is, in the same units as carriage_mm."""
        return self.origin_mm + self.pos / self.fw["STEPS_PER_MM"]

    def step_to(self, target):
        """stepTo() from the .ino: half-steps in one direction until pos == target."""
        if target == self.pos:
            return 0
        direction = 1 if target > self.pos else -1
        n = abs(target - self.pos)
        self.phase = (self.phase + 8 + (-direction if self.invert else direction) * n) % 8
        self.pos = target
        self.steps_taken += n
        # The carriage only moves once the play is taken up, then trails the motor by half of it.
        half = SETTINGS["gear_play_mm"] / 2
        self.carriage_mm = min(max(self.carriage_mm, self.motor_mm - half), self.motor_mm + half)
        return n

    def home(self):
        """HOME only renames the spot: the motor stays put and its count becomes 0."""
        self.origin_mm = self.motor_mm
        self.pos = 0


class CoinStageFirmware:
    """A line-for-line Python copy of coin_stage.ino's command handling."""

    def __init__(self, fw=None, seed=0, carriage_mm=(0.0, 0.0)):
        self.fw = dict(fw or read_firmware_settings())
        self.fw["STEPS_PER_MM"] = self.fw["STEPS_PER_REV"] / self.fw["LEAD_MM"]
        self.rng = np.random.default_rng(seed)
        self.x = Axis("X", self.fw["INVERT_X"], self.fw, carriage_mm[0])
        self.y = Axis("Y", self.fw["INVERT_Y"], self.fw, carriage_mm[1])
        self.error = np.zeros(2)       # where each move really ended, off from where it was meant to
        self.busy_s = 0.0              # total time the motors would have run
        self.moves = 0
        self.last_move_end = 0.0       # wall-clock time the last move finished
        self.log = []

    # -- helpers from the .ino
    def move_axis(self, axis, mm):
        spm = self.fw["STEPS_PER_MM"]
        target = round(mm * spm)
        backlash = round(self.fw["BACKLASH_MM"] * spm)
        axis.energised = True
        steps = 0
        if target < axis.pos:
            steps += axis.step_to(target - backlash)
        steps += axis.step_to(target)
        axis.energised = False
        return steps * self.fw["STEP_US"] / 1e6 + 0.020

    def in_limits(self, x, y):
        return abs(x) <= self.fw["LIMIT_MM"] and abs(y) <= self.fw["LIMIT_MM"]

    def report_pos(self):
        spm = self.fw["STEPS_PER_MM"]
        return f"OK {self.x.pos / spm:.3f} {self.y.pos / spm:.3f}"

    def move_to(self, x, y):
        if not self.in_limits(x, y):
            return "ERR outside limits"
        seconds = self.move_axis(self.x, x) + self.move_axis(self.y, y)
        self.error = self.rng.normal(0, SETTINGS["step_error_mm"], 2)
        self.busy_s += seconds
        self.moves += 1
        if SETTINGS["time_scale"]:
            time.sleep(seconds * SETTINGS["time_scale"])
        self.last_move_end = time.monotonic()
        return self.report_pos()

    def handle_line(self, line):
        """handleLine() from the .ino. Returns the reply line, or None for a blank line."""
        line = line.strip().upper()
        if not line:
            return None
        cmd, _, args = line.partition(" ")
        args = args.strip()
        first, _, second = args.partition(" ")
        a1 = _to_float(first)
        a2 = _to_float(second) if second else 0.0
        self.log.append(line)
        spm = self.fw["STEPS_PER_MM"]
        if cmd == "MOVE":
            return self.move_to(a1, a2)
        if cmd == "JOG":
            return self.move_to(self.x.pos / spm + a1, self.y.pos / spm + a2)
        if cmd == "HOME":
            self.x.home()
            self.y.home()
            return self.report_pos()
        if cmd == "POS":
            return self.report_pos()
        if cmd == "OFF":
            self.x.energised = self.y.energised = False
            return "OK off"
        if cmd == "PING":
            return "OK coin_stage"
        return "ERR unknown command"

    def carriage_mm(self):
        """Where the stage really is: 0, 0 puts the coin's centre under the lens."""
        return np.array([self.x.carriage_mm, self.y.carriage_mm]) + self.error


class FakeSerial:
    """Stands in for serial.Serial, wired to the simulated Nano."""

    def __init__(self, port=None, baudrate=9600, timeout=None, **kwargs):
        if port not in SETTINGS["ports"]:
            raise SerialException(f"could not open port '{port}': FileNotFoundError "
                                  f"(the simulated Nano is on {', '.join(SETTINGS['ports'])})")
        self.port, self.baudrate, self.timeout = port, baudrate, timeout
        self.is_open = True
        self._in = ""
        self._out = collections.deque()
        wd = world()
        # Opening the port resets the Nano, but the stage stays wherever it was. Each new run
        # starts as if the coin had just been centred with `jog` and HOME.
        start = wd.stage.carriage_mm() if wd.stage else (0.0, 0.0)
        wd.stage = self.firmware = CoinStageFirmware(seed=SETTINGS["seed"], carriage_mm=start)
        self._send("OK coin_stage ready")  # setup() prints this when the Nano boots

    def _send(self, text):
        if self.baudrate != 115200:
            # Wrong speed on a real Nano gives gibberish.
            text = "".join(chr(0x80 + (ord(c) * 7) % 0x7F) for c in text)
        self._out.append((text + "\r\n").encode("latin-1"))

    def write(self, data):
        if not self.is_open:
            raise SerialException("Attempting to use a port that is not open")
        for c in data.decode("latin-1"):
            if c in "\r\n":
                reply = self.firmware.handle_line(self._in)
                self._in = ""
                if reply is not None:
                    self._send(reply)
            elif len(self._in) < 40:  # the .ino drops anything past 40 characters
                self._in += c
        return len(data)

    def readline(self):
        if self._out:
            return self._out.popleft()
        if self.timeout is None:
            raise SerialException("readline() would wait forever: the simulated Nano has nothing to say")
        return b""  # a real port gives up after `timeout` seconds

    def reset_input_buffer(self):
        self._out.clear()

    @property
    def in_waiting(self):
        return sum(len(x) for x in self._out)

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SerialException(IOError):
    pass


# ---------------------------------------------------------------- the coin and the camera

def _noise_texture(size, rng, scales=(4, 16, 64)):
    """Smooth random texture in 0..1, made of a few layers of blurred noise."""
    out = np.zeros((size, size), np.float32)
    for s in scales:
        small = rng.random((max(size // s, 2), max(size // s, 2))).astype(np.float32)
        out += cv2.resize(small, (size, size), interpolation=cv2.INTER_CUBIC) / len(scales)
    return (out - out.min()) / (np.ptp(out) + 1e-6)


def make_test_coin(diameter_px, rng):
    """A made-up coin (BGRA) with enough detail for Hugin to match."""
    d = diameter_px
    r = d / 2
    yy, xx = np.mgrid[0:d, 0:d].astype(np.float32) - (d - 1) / 2
    rho = np.hypot(xx, yy) / r
    base = np.array([60, 120, 190], np.float32)  # copper, in BGR
    tex = _noise_texture(d, rng, (2, 8, 32))
    shade = 0.75 + 0.35 * tex - 0.15 * rho
    img = (base[None, None, :] * shade[:, :, None]).clip(0, 255).astype(np.uint8)
    cv2.circle(img, (d // 2, d // 2), int(r * 0.93), (40, 80, 140), max(d // 120, 2), cv2.LINE_AA)
    cv2.circle(img, (d // 2, d // 2), int(r * 0.42), (95, 160, 225), max(d // 150, 2), cv2.LINE_AA)
    for k in range(12):  # star in the middle
        a = k * math.pi / 6
        p = (int(d / 2 + math.cos(a) * r * 0.38), int(d / 2 + math.sin(a) * r * 0.38))
        cv2.line(img, (d // 2, d // 2), p, (35, 75, 130), max(d // 200, 1), cv2.LINE_AA)
    letters = "RIG SIM * TEST COIN * 2026 * "
    for k, ch in enumerate(letters):  # lettering round the rim
        a = k * 2 * math.pi / len(letters) - math.pi / 2
        scale = d / 900
        size, _ = cv2.getTextSize(ch, cv2.FONT_HERSHEY_DUPLEX, scale, max(int(scale * 2), 1))
        p = (int(d / 2 + math.cos(a) * r * 0.75 - size[0] / 2), int(d / 2 + math.sin(a) * r * 0.75 + size[1] / 2))
        cv2.putText(img, ch, p, cv2.FONT_HERSHEY_DUPLEX, scale, (30, 60, 110), max(int(scale * 2), 1), cv2.LINE_AA)
    alpha = np.zeros((d, d), np.uint8)
    cv2.circle(alpha, (d // 2, d // 2), int(r), 255, -1, cv2.LINE_AA)
    return np.dstack([img, alpha])


def make_background(kind, size, rng):
    if kind not in ("mat", "felt", "paper"):
        img = cv2.imread(kind)
        if img is None:
            raise RuntimeError(f"rig_sim could not read background picture {kind!r}")
        return cv2.resize(img, (size, size))
    tex = _noise_texture(size, rng, (1, 3, 12))
    colour = {"mat": (70, 95, 45), "felt": (50, 40, 120), "paper": (225, 228, 230)}[kind]
    grain = {"mat": 0.25, "felt": 0.35, "paper": 0.08}[kind]
    img = np.array(colour, np.float32)[None, None, :] * (1 - grain / 2 + grain * tex[:, :, None])
    img = img.clip(0, 255).astype(np.uint8)
    if kind == "mat":  # cutting-mat grid lines every 10 mm
        step = size // 4
        for i in range(0, size, step):
            cv2.line(img, (i, 0), (i, size), (110, 140, 90), 1)
            cv2.line(img, (0, i), (size, i), (110, 140, 90), 1)
    return img


class World:
    """The coin lying on the stage, seen through the microscope."""

    def __init__(self):
        s = SETTINGS
        self.rng = np.random.default_rng(s["seed"])
        k = s["px_per_mm"]
        coin_px = int(round(s["coin_mm"] * k))
        if s["coin"]:
            src = cv2.imread(s["coin"], cv2.IMREAD_UNCHANGED)
            if src is None:
                raise RuntimeError(f"rig_sim could not read coin picture {s['coin']!r}")
            if src.ndim == 2:
                src = cv2.cvtColor(src, cv2.COLOR_GRAY2BGR)
            if src.shape[2] == 4 and (src[:, :, 3] < 128).mean() > 0.05:
                # Cut-out coin: crop to the coin and scale it to its real size.
                ys, xs = np.nonzero(src[:, :, 3] > 128)
                src = src[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
                scale = coin_px / max(src.shape[:2])
            else:
                # Plain photo: assume the coin fills most of its shorter side.
                src = cv2.cvtColor(src[:, :, :3], cv2.COLOR_BGR2BGRA)
                src[:, :, 3] = 255
                scale = coin_px / (0.9 * min(src.shape[:2]))
            interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
            self.coin = cv2.resize(src, None, fx=scale, fy=scale, interpolation=interp)
        else:
            self.coin = make_test_coin(coin_px, self.rng)
        self.background = make_background(s["background"], int(40 * k), self.rng)  # a 40 mm square, repeating
        self.stage = None  # set when the fake serial port opens
        self.frames_rendered = 0

    @staticmethod
    def transform(stage_mm, img_shape):
        """2x3 matrix taking a picture centred on the coin (at px_per_mm) to the camera's view.
        A point p mm from the coin's centre appears at  frame centre + px_per_mm * R @ (p + stage),
        where R is the camera's rotation (and mirror)."""
        s = SETTINGS
        w, h = s["resolution"]
        a = math.radians(s["camera_angle_deg"])
        rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        if s["camera_mirrored"]:
            rot = rot @ np.array([[-1, 0], [0, 1]])
        shift = np.array([w / 2, h / 2]) + s["px_per_mm"] * rot @ (np.asarray(stage_mm) + s["coin_offset_mm"])
        ih, iw = img_shape[:2]
        return np.hstack([rot, (shift - rot @ np.array([iw / 2, ih / 2]))[:, None]])

    def render(self, stage_mm, since_move_s=None):
        """The picture the microscope sees with the stage at stage_mm (stage coordinates)."""
        s = SETTINGS
        w, h = s["resolution"]

        def warp(img, border):
            return cv2.warpAffine(img, self.transform(stage_mm, img.shape), (w, h), flags=cv2.INTER_LINEAR,
                                  borderMode=border, borderValue=0)

        bg = warp(self.background, cv2.BORDER_WRAP).astype(np.float32)
        coin = warp(self.coin, cv2.BORDER_CONSTANT).astype(np.float32)
        alpha = coin[:, :, 3:] / 255
        frame = coin[:, :, :3] * alpha + bg * (1 - alpha)

        # Focus: the coin is tilted, so the height under the lens changes as the stage moves.
        # The mat is lower than the coin's face by the coin's thickness (~1.5 mm) but a wide
        # flat area, so only the coin's own focus is modelled.
        p = -np.asarray(stage_mm)  # the stage point under the lens
        height = s["coin_tilt_mm"] * p[0] / max(s["coin_mm"], 1) + s["focus_error_mm"]
        sigma = 1.2 * abs(height) / max(s["depth_of_field_mm"], 1e-6)
        if sigma > 0.3:
            frame = cv2.GaussianBlur(frame, (0, 0), sigma)

        # Wobble: a photo taken right after a move is smeared along the move.
        if since_move_s is not None and since_move_s < s["settle_time_s"]:
            length = int(2 + 12 * (1 - since_move_s / s["settle_time_s"]))
            kernel = np.zeros((length, length), np.float32)
            kernel[length // 2, :] = 1 / length
            frame = cv2.filter2D(frame, -1, kernel)

        # Ring light: brighter in the middle.
        if s["vignette"]:
            yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
            r2 = ((xx - w / 2) ** 2 + (yy - h / 2) ** 2) / ((w / 2) ** 2 + (h / 2) ** 2)
            frame *= (1 - s["vignette"] * r2)[:, :, None]
        if s["noise"]:
            frame += self.rng.normal(0, s["noise"], frame.shape).astype(np.float32)
        self.frames_rendered += 1
        return frame.clip(0, 255).astype(np.uint8)

    # -- ground truth, for tests and marking
    def truth(self):
        s = SETTINGS
        return {"coin_mm": s["coin_mm"], "px_per_mm": s["px_per_mm"], "camera_angle_deg": s["camera_angle_deg"],
                "camera_mirrored": s["camera_mirrored"], "coin_offset_mm": list(s["coin_offset_mm"])}

    def coin_mask(self, stage_mm):
        """Where the coin really is in a frame taken with the stage at stage_mm (255 = coin)."""
        w, h = SETTINGS["resolution"]
        return cv2.warpAffine(self.coin[:, :, 3], self.transform(stage_mm, self.coin.shape), (w, h),
                              flags=cv2.INTER_LINEAR)


_world = None


def world():
    global _world
    if _world is None:
        _world = World()
    return _world


class FakeVideoCapture:
    """Stands in for cv2.VideoCapture. Camera 0 and 1 are the simulated microscope."""

    def __init__(self, index=0, api_preference=None, *args):
        self.index = index
        self._open = isinstance(index, int) and index in (0, 1)
        self.requested = [None, None]
        self._recent = collections.deque()

    def isOpened(self):
        return self._open

    def set(self, prop, value):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            self.requested[0] = value
        elif prop == cv2.CAP_PROP_FRAME_HEIGHT:
            self.requested[1] = value
        return self._open

    def get(self, prop):
        w, h = SETTINGS["resolution"]
        return {cv2.CAP_PROP_FRAME_WIDTH: float(w), cv2.CAP_PROP_FRAME_HEIGHT: float(h),
                cv2.CAP_PROP_FPS: 30.0}.get(prop, 0.0)

    def read(self):
        if not self._open:
            return False, None
        wd = world()
        stage = wd.stage
        pos = stage.carriage_mm() if stage else np.zeros(2)
        since = time.monotonic() - stage.last_move_end if stage and stage.moves else None
        # The camera hands out frames it took a little while ago: the oldest one in its buffer.
        self._recent.append((pos.copy(), since))
        while len(self._recent) > SETTINGS["buffered_frames"] + 1:
            self._recent.popleft()
        old_pos, old_since = self._recent[0]
        wd.last_frame_at_mm = old_pos
        return True, wd.render(old_pos, old_since)

    def grab(self):
        return self._open

    def retrieve(self):
        return self.read()

    def release(self):
        self._open = False


# ---------------------------------------------------------------- switching it on

def install():
    """Swap pyserial and the camera for the simulated ones. Done automatically on import."""
    serial_mod = types.ModuleType("serial")
    serial_mod.Serial = FakeSerial
    serial_mod.SerialException = SerialException
    serial_mod.SerialTimeoutException = SerialException
    serial_mod.__rig_sim__ = True
    tools = types.ModuleType("serial.tools")
    list_ports = types.ModuleType("serial.tools.list_ports")

    class _Port:
        def __init__(self, device):
            self.device, self.description = device, "USB-SERIAL CH340 (simulated by rig_sim)"

    list_ports.comports = lambda: [_Port(p) for p in SETTINGS["ports"][:1]]
    tools.list_ports = list_ports
    serial_mod.tools = tools
    sys.modules["serial"] = serial_mod
    sys.modules["serial.tools"] = tools
    sys.modules["serial.tools.list_ports"] = list_ports
    if not getattr(cv2.VideoCapture, "__rig_sim__", False):
        cv2.RealVideoCapture = cv2.VideoCapture
        FakeVideoCapture.__rig_sim__ = True
        cv2.VideoCapture = FakeVideoCapture


def _summary():
    wd = _world
    if SETTINGS["quiet"] or wd is None or wd.stage is None or not wd.stage.moves:
        return
    st = wd.stage
    steps = st.x.steps_taken + st.y.steps_taken
    print(f"[rig_sim] {st.moves} moves, {steps} motor steps, {wd.frames_rendered} frames. "
          f"The real motors would have taken about {st.busy_s:.0f} s.")


_load_settings_file()
install()
atexit.register(_summary)


def main(argv=None):
    """python rig_sim.py <coin_scan command> [--sim-<setting> value ...]"""
    argv = list(sys.argv[1:] if argv is None else argv)
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--sim-"):
            key = a[6:].replace("-", "_")
            if key not in SETTINGS:
                raise SystemExit(f"rig_sim has no setting {key!r}. It has: {', '.join(SETTINGS)}")
            default = SETTINGS[key]
            if isinstance(default, list):
                n = len(default)
                configure(**{key: [type(default[0])(v) for v in argv[i + 1:i + 1 + n]]})
                i += 1 + n
                continue
            value = argv[i + 1]
            if isinstance(default, bool):
                value = value.lower() in ("1", "true", "yes", "on")
            elif isinstance(default, float):
                value = float(value)
            elif isinstance(default, int):
                value = int(value)
            configure(**{key: value})
            i += 2
        else:
            rest.append(a)
            i += 1
    if not rest:
        print(__doc__)
        return
    sys.path.insert(0, HERE)
    import coin_scan
    sys.argv = ["coin_scan.py"] + rest
    coin_scan.main()


if __name__ == "__main__":
    main()
