"""Coin scanner: drives the XY stage, photographs the coin in overlapping tiles,
masks out the background, and prepares the tiles for Hugin.

Commands (run "python coin_scan.py <command> -h" for options):
  devices     list serial ports and cameras
  jog         live view; move the stage with the keyboard and set HOME on the coin centre
  calibrate   measure pixels per mm and the camera direction automatically
  scan        photograph the whole coin
  stitch      run Hugin's command-line tools on a scan folder
  cropfinal   find the coin in a stitched image and make the background transparent
  croptiles   mask the background in a folder of photos using only the pictures
              (for hand-taken photos, or a scan done with --no-mask)

Add --simulate to jog/calibrate/scan to test everything without hardware,
using any coin photo as a fake microscope view.
"""

import argparse
import csv
import json
import math
import warnings
import os
import shutil
import subprocess
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    "port": "COM3",
    "camera_index": 0,
    "width": 1920,
    "height": 1080,
    "px_per_mm": None,      # set by calibrate
    "x_vec": [1.0, 0.0],    # image shift (unit vector) when the stage moves +X; set by calibrate
    "y_vec": [0.0, 1.0],    # image shift (unit vector) when the stage moves +Y; set by calibrate
    "overlap": 0.35,
    "settle_s": 0.5,
    "simulate_image": "",
    "simulate_px_per_mm": 60.0,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"Saved settings to {CONFIG_PATH}")


# ---------------------------------------------------------------- hardware

class Stage:
    """Talks to coin_stage.ino over USB serial."""

    def __init__(self, port):
        import serial
        self.ser = serial.Serial(port, 115200, timeout=120)
        time.sleep(2.0)  # the Nano resets when the port opens
        self.ser.reset_input_buffer()
        self._cmd("PING")

    def _cmd(self, text):
        self.ser.write((text + "\n").encode())
        reply = self.ser.readline().decode(errors="replace").strip()
        if not reply.startswith("OK"):
            raise RuntimeError(f"Stage said {reply!r} to {text!r}")
        return reply

    def move(self, x, y):
        self._cmd(f"MOVE {x:.3f} {y:.3f}")
        self.pos = (x, y)

    def jog(self, dx, dy):
        parts = self._cmd(f"JOG {dx:.3f} {dy:.3f}").split()
        self.pos = (float(parts[1]), float(parts[2]))

    def home(self):
        self._cmd("HOME")
        self.pos = (0.0, 0.0)

    def off(self):
        self._cmd("OFF")

    def close(self):
        try:
            self.off()
        finally:
            self.ser.close()


class Camera:
    def __init__(self, index, width, height):
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        self.cap = cv2.VideoCapture(index, backend)
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open camera {index}. Try 'devices' to find the right one.")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("Camera opened but returned no picture.")
        print(f"Camera resolution: {frame.shape[1]} x {frame.shape[0]}")

    def grab(self):
        # Throw away buffered frames so the photo is from after the stage stopped.
        for _ in range(5):
            self.cap.read()
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("Camera stopped returning pictures.")
        return frame

    def preview(self):
        ok, frame = self.cap.read()
        return frame if ok else None

    def close(self):
        self.cap.release()


class SimWorld:
    """Fake stage + camera: crops a window out of a big coin photo."""

    def __init__(self, image_path, px_per_mm, width, height):
        img = cv2.imread(image_path)
        if img is None:
            raise RuntimeError(f"Could not read simulate image {image_path!r}")
        self.big = img
        self.px_per_mm = px_per_mm
        self.w, self.h = min(width, 640), min(height, 480)
        self.pos = (0.0, 0.0)

    # stage interface
    def move(self, x, y):
        self.pos = (x, y)

    def jog(self, dx, dy):
        self.pos = (self.pos[0] + dx, self.pos[1] + dy)

    def home(self):
        self.pos = (0.0, 0.0)

    def off(self):
        pass

    # camera interface
    def grab(self):
        bh, bw = self.big.shape[:2]
        # Moving the stage +X moves the coin +X in the picture.
        cx = bw / 2 - self.pos[0] * self.px_per_mm
        cy = bh / 2 - self.pos[1] * self.px_per_mm
        m = np.float32([[1, 0, self.w / 2 - cx], [0, 1, self.h / 2 - cy]])
        return cv2.warpAffine(self.big, m, (self.w, self.h), borderValue=(40, 40, 40))

    preview = grab

    def close(self):
        pass


def open_hardware(cfg, simulate):
    if simulate:
        path = cfg.get("simulate_image") or ""
        if not path:
            raise SystemExit("Set \"simulate_image\" in config.json to a coin photo, or pass --image.")
        world = SimWorld(path, cfg["simulate_px_per_mm"], cfg["width"], cfg["height"])
        return world, world
    stage = Stage(cfg["port"])
    camera = Camera(cfg["camera_index"], cfg["width"], cfg["height"])
    return stage, camera


# ---------------------------------------------------------------- commands

def cmd_devices(cfg, args):
    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        print("Serial ports:")
        for p in ports:
            print(f"  {p.device}  {p.description}")
        if not ports:
            print("  none found (is the Nano plugged in and the CH340 driver installed?)")
    except ImportError:
        print("pyserial is not installed.")
    print("Cameras:")
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    for i in range(6):
        cap = cv2.VideoCapture(i, backend)
        if cap.isOpened():
            ok, frame = cap.read()
            if ok:
                print(f"  index {i}: {frame.shape[1]} x {frame.shape[0]}")
        cap.release()


KEYS = {
    # Windows arrow-key codes from cv2.waitKeyEx, then WASD
    2424832: (-1, 0), 2555904: (1, 0), 2490368: (0, -1), 2621440: (0, 1),
    ord("a"): (-1, 0), ord("d"): (1, 0), ord("w"): (0, -1), ord("s"): (0, 1),
}


def cmd_jog(cfg, args):
    stage, camera = open_hardware(cfg, args.simulate)
    step = 1.0
    print("Arrows or WASD move the stage. + / - change step size. "
          "H = set HOME here (put the coin centre on the crosshair first). P = save a photo. Q = quit.")
    try:
        while True:
            frame = camera.preview()
            if frame is not None:
                view = frame.copy()
                h, w = view.shape[:2]
                cv2.line(view, (w // 2 - 30, h // 2), (w // 2 + 30, h // 2), (0, 255, 255), 1)
                cv2.line(view, (w // 2, h // 2 - 30), (w // 2, h // 2 + 30), (0, 255, 255), 1)
                cv2.putText(view, f"step {step:g} mm", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
                cv2.imshow("coin scanner - jog", view)
            key = cv2.waitKeyEx(30)
            if key == -1:
                continue
            if key in KEYS:
                dx, dy = KEYS[key]
                stage.jog(dx * step, dy * step)
            elif key in (ord("+"), ord("=")):
                step = min(step * 2, 8.0)
            elif key in (ord("-"), ord("_")):
                step = max(step / 2, 0.125)
            elif key in (ord("h"), ord("H")):
                stage.home()
                print("HOME set.")
            elif key in (ord("p"), ord("P")):
                name = time.strftime("photo_%Y%m%d_%H%M%S.png")
                cv2.imwrite(name, camera.grab())
                print(f"Saved {name}")
            elif key in (ord("q"), ord("Q"), 27):
                break
    finally:
        cv2.destroyAllWindows()
        stage.off()
        camera.close()


def measure_shift(img_a, img_b):
    a = cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY).astype(np.float32)
    b = cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY).astype(np.float32)
    window = cv2.createHanningWindow(a.shape[::-1], cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(a, b, window)
    return np.array([dx, dy]), response


def cmd_calibrate(cfg, args):
    """Moves the stage a known distance and measures how far the picture shifts."""
    stage, camera = open_hardware(cfg, args.simulate)
    dist = args.distance
    try:
        stage.move(0, 0)
        time.sleep(cfg["settle_s"])
        base = camera.grab()
        vecs = []
        for axis in ("X", "Y"):
            stage.move(dist if axis == "X" else 0, dist if axis == "Y" else 0)
            time.sleep(cfg["settle_s"])
            shifted = camera.grab()
            shift, response = measure_shift(base, shifted)
            print(f"Stage +{dist} mm in {axis}: picture moved {shift[0]:.1f}, {shift[1]:.1f} px "
                  f"(match quality {response:.2f})")
            if response < 0.05:
                print("  Match quality is low. Make sure the coin is in focus and has detail in view.")
            vecs.append(shift / dist)
            stage.move(0, 0)
            time.sleep(cfg["settle_s"])
    finally:
        stage.off()
        camera.close()

    px_per_mm = float((np.linalg.norm(vecs[0]) + np.linalg.norm(vecs[1])) / 2)
    cfg["px_per_mm"] = round(px_per_mm, 3)
    cfg["x_vec"] = [round(float(v), 4) for v in vecs[0] / np.linalg.norm(vecs[0])]
    cfg["y_vec"] = [round(float(v), 4) for v in vecs[1] / np.linalg.norm(vecs[1])]
    h, w = base.shape[:2]
    print(f"{px_per_mm:.1f} px per mm. Field of view {w / px_per_mm:.2f} x {h / px_per_mm:.2f} mm.")
    save_config(cfg)


def plan_tiles(diameter, fov_w, fov_h, overlap):
    """Serpentine grid of stage positions covering a coin centred at 0,0."""
    step = min(fov_w, fov_h) * (1 - overlap)
    radius = diameter / 2
    half_diag = math.hypot(fov_w, fov_h) / 2
    n = math.ceil(radius / step)
    tiles = []
    for row, j in enumerate(range(-n, n + 1)):
        cols = list(range(-n, n + 1))
        if row % 2:
            cols.reverse()
        for i in cols:
            x, y = i * step, j * step
            dist = math.hypot(x, y)
            if dist - half_diag > radius:
                continue  # tile sees no coin at all
            inside = dist + half_diag < radius
            tiles.append((x, y, inside))
    return tiles, step


def coin_mask(shape, cfg, x, y, radius_mm):
    """Alpha mask for a tile: 255 on the coin, 0 on the background."""
    h, w = shape[:2]
    p = cfg["px_per_mm"]
    xv, yv = np.array(cfg["x_vec"]), np.array(cfg["y_vec"])
    # At HOME the coin centre is in the middle of the picture; moving the stage
    # by (x, y) moves it by x*xv + y*yv mm in picture directions.
    centre = np.array([w / 2, h / 2]) + (x * xv + y * yv) * p
    mask = np.zeros((h, w), np.uint8)
    cv2.circle(mask, (int(round(centre[0])), int(round(centre[1]))), int(round(radius_mm * p)), 255, -1,
               lineType=cv2.LINE_AA)
    return mask


def cmd_scan(cfg, args):
    if not cfg.get("px_per_mm"):
        raise SystemExit("Run 'calibrate' first.")
    out = args.out or time.strftime("scan_%Y%m%d_%H%M%S")
    os.makedirs(out, exist_ok=True)
    stage, camera = open_hardware(cfg, args.simulate)
    try:
        first = camera.grab()
        h, w = first.shape[:2]
        fov_w, fov_h = w / cfg["px_per_mm"], h / cfg["px_per_mm"]
        # Shrink the circle slightly so the rim edge isn't left with a background halo.
        radius = args.diameter / 2 - args.edge_trim
        tiles, step = plan_tiles(args.diameter, fov_w, fov_h, cfg["overlap"])
        print(f"{len(tiles)} photos, {step:.2f} mm apart. Field of view {fov_w:.2f} x {fov_h:.2f} mm.")
        rows = []
        start = time.time()
        for n, (x, y, inside) in enumerate(tiles, 1):
            stage.move(x, y)
            time.sleep(cfg["settle_s"])
            frame = camera.grab()
            name = f"tile_{n:03d}.png"
            if inside or args.no_mask:
                img = frame
            else:
                img = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
                img[:, :, 3] = coin_mask(frame.shape, cfg, x, y, radius)
            cv2.imwrite(os.path.join(out, name), img)
            rows.append({"file": name, "x_mm": round(x, 3), "y_mm": round(y, 3), "masked": not inside})
            print(f"  {n}/{len(tiles)}  {name}  ({x:+.2f}, {y:+.2f}) {'' if inside else 'edge'}")
        stage.move(0, 0)
        with open(os.path.join(out, "positions.csv"), "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["file", "x_mm", "y_mm", "masked"])
            writer.writeheader()
            writer.writerows(rows)
        # Keep the calibration with the photos, so stitching still works after a recalibrate.
        with open(os.path.join(out, "scan_info.json"), "w") as f:
            json.dump({"px_per_mm": cfg["px_per_mm"], "x_vec": cfg["x_vec"], "y_vec": cfg["y_vec"],
                       "width": w, "height": h, "diameter": args.diameter}, f, indent=2)
        print(f"Done in {time.time() - start:.0f} s. Photos are in {os.path.abspath(out)}")
        print(f"Next: python coin_scan.py stitch {out}")
    finally:
        stage.off()
        camera.close()


def find_hugin_tool(name):
    found = shutil.which(name)
    if found:
        return found
    for base in (r"C:\Program Files\Hugin\bin", r"C:\Program Files (x86)\Hugin\bin"):
        path = os.path.join(base, name + ".exe")
        if os.path.exists(path):
            return path
    raise SystemExit(f"Could not find Hugin's {name}. Install Hugin from hugin.sourceforge.io.")


def start_positions(folder, tiles):
    """Hugin yaw/pitch for each tile, worked out from where the stage was.

    The field of view is tiny, so a flat mosaic behaves like a very narrow
    panorama: sliding a tile sideways is the same as turning a camera slightly.
    """
    with open(os.path.join(folder, "scan_info.json")) as f:
        info = json.load(f)
    with open(os.path.join(folder, "positions.csv")) as f:
        pos = {r["file"]: (float(r["x_mm"]), float(r["y_mm"])) for r in csv.DictReader(f)}
    xv, yv = np.array(info["x_vec"]), np.array(info["y_vec"])
    deg_per_px = HUGIN_FOV / info["width"]
    sets = []
    for i, name in enumerate(tiles):
        x, y = pos[name]
        # The tile shows the coin shifted by this many pixels from centre.
        dx, dy = (x * xv + y * yv) * info["px_per_mm"]
        sets.append(f"y{i}={-dx * deg_per_px:.5f},p{i}={dy * deg_per_px:.5f},r{i}=0")
    return ",".join(sets)


def centre_tile(folder, tiles):
    with open(os.path.join(folder, "positions.csv")) as f:
        pos = {r["file"]: math.hypot(float(r["x_mm"]), float(r["y_mm"])) for r in csv.DictReader(f)}
    return min(range(len(tiles)), key=lambda i: pos[tiles[i]])


HUGIN_FOV = 1.0  # lens angle Hugin is told; kept tiny so the flat mosaic is not distorted


def cmd_stitch(cfg, args):
    """Runs Hugin's command-line tools, starting every tile at the spot the
    stage put it, so Hugin only has to fine-tune."""
    folder = os.path.abspath(args.folder)
    tiles = sorted(f for f in os.listdir(folder) if f.startswith("tile_") and f.endswith(".png"))
    # Edge photos with only a sliver of coin add nothing the neighbours don't
    # already cover, and they confuse Hugin's blending, so leave them out.
    kept = []
    for t in tiles:
        img = cv2.imread(os.path.join(folder, t), cv2.IMREAD_UNCHANGED)
        if img is not None and (img.ndim < 3 or img.shape[2] < 4 or (img[:, :, 3] > 0).mean() >= 0.05):
            kept.append(t)
    if len(kept) < len(tiles):
        print(f"Leaving out {len(tiles) - len(kept)} photos that are almost all background.")
    tiles = kept
    if not tiles:
        raise SystemExit("No tile_*.png files in that folder.")
    pto = os.path.join(folder, "coin.pto")

    def run(cmd):
        print(">", os.path.basename(cmd[0]), " ".join(cmd[1:4]), "...")
        if subprocess.run(cmd).returncode != 0:
            print(f"{os.path.basename(cmd[0])} failed. You can open {pto} in the Hugin window and "
                  "finish there (Align, then Stitch).")
            return False
        return True

    steps = [
        [find_hugin_tool("pto_gen"), "-p", "0", "-f", str(HUGIN_FOV), "-o", pto]
        + [os.path.join(folder, t) for t in tiles],
        [find_hugin_tool("pto_var"), "--set", start_positions(folder, tiles), "-o", pto, pto],
        [find_hugin_tool("cpfind"), "--prealigned", "-o", pto, pto],
        [find_hugin_tool("cpclean"), "-o", pto, pto],
    ]
    if not all(run(cmd) for cmd in steps):
        return

    # Only yaw and pitch are optimised: the stage slides tiles, it never rotates them.
    # The centre tile stays fixed as the anchor, since it overlaps the most others.
    # Photos with too few matches keep the position the stage gave them.
    points = [0] * len(tiles)
    with open(pto) as f:
        for line in f:
            if line.startswith("c "):
                for key in ("n", "N"):
                    i = int(line.split(f" {key}")[1].split()[0])
                    points[i] += 1
    anchor = centre_tile(folder, tiles)
    opt = ",".join(f"y{i},p{i}" for i in range(len(tiles)) if i != anchor and points[i] >= 4)
    steps = [
        [find_hugin_tool("pto_var"), "--opt", opt, "-o", pto, pto],
        [find_hugin_tool("autooptimiser"), "-n", "-o", pto, pto],
        [find_hugin_tool("pano_modify"), "--projection=0", "--fov=AUTO", "--canvas=AUTO",
         "--crop=AUTOOUTSIDE", "--output-type=NORMAL", "--ldr-file=TIF", "-o", pto, pto],
        [find_hugin_tool("hugin_executor"), "--stitching", "--prefix=" + os.path.join(folder, "coin"), pto],
    ]
    if not all(run(cmd) for cmd in steps):
        return
    print(f"Stitched image: {os.path.join(folder, 'coin.tif')}")
    print(f"Next: python coin_scan.py cropfinal {os.path.join(folder, 'coin.tif')}")


def cmd_cropfinal(cfg, args):
    img = cv2.imread(args.image, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SystemExit(f"Could not read {args.image}")
    if img.dtype != np.uint8:
        img = (img / 256).astype(np.uint8)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    elif img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    h, w = img.shape[:2]
    # Work on a small copy so circle finding is quick.
    scale = 800 / max(h, w)
    small = cv2.resize(img[:, :, :3], None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.medianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), 7)
    alpha_small = cv2.resize(img[:, :, 3], (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST)
    if args.center and args.radius:
        cx, cy, r = args.center[0], args.center[1], args.radius
    elif (alpha_small == 0).mean() > 0.05:
        # Hugin kept the transparent background from the masked tiles, so the
        # coin is simply the biggest solid shape.
        contours, _ = cv2.findContours((alpha_small > 0).astype(np.uint8), cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_NONE)
        pts = max(contours, key=cv2.contourArea).reshape(-1, 2).astype(np.float64)
        # Fit a circle to the outline, then drop points that stick out (like
        # corners of a photo whose background was left on) and fit again.
        for _ in range(4):
            cx, cy, r, err = fit_circle(pts)
            resid = np.abs(np.hypot(*(pts - (cx, cy)).T) - r)
            pts = pts[resid <= max(2 * np.median(resid), 1.5)]
        cx, cy, r = cx / scale, cy / scale, r / scale
        print(f"Coin found at {cx:.0f}, {cy:.0f}, radius {r:.0f} px (from the transparent background)")
    else:
        min_r = int(min(small.shape[:2]) * 0.30)
        max_r = int(max(small.shape[:2]) * 0.55)
        circles = cv2.HoughCircles(gray, cv2.HOUGH_GRADIENT, dp=1.5, minDist=min(small.shape[:2]),
                                   param1=100, param2=40, minRadius=min_r, maxRadius=max_r)
        if circles is None:
            raise SystemExit("Couldn't find the coin edge. Pass --center X Y --radius R in pixels.")
        cx, cy, r = (circles[0][0] / scale).tolist()
        print(f"Coin found at {cx:.0f}, {cy:.0f}, radius {r:.0f} px")
    r = r * (1 - args.shrink)
    mask = np.zeros((h, w), np.uint8)
    cv2.circle(mask, (int(cx), int(cy)), int(r), 255, -1, lineType=cv2.LINE_AA)
    img[:, :, 3] = np.minimum(img[:, :, 3], mask)
    x0, y0 = max(int(cx - r), 0), max(int(cy - r), 0)
    x1, y1 = min(int(cx + r) + 1, w), min(int(cy + r) + 1, h)
    out = args.out or os.path.splitext(args.image)[0] + "_cropped.png"
    cv2.imwrite(out, img[y0:y1, x0:x1])
    print(f"Saved {out}")


def background_colour(images, coin_at=None, radius=None):
    """Colour and texture of the background, worked out from the photos.

    With the coin's centre in each photo known from the stage positions, the
    corners furthest from it are background, whatever colour the coin is.
    Otherwise corner patches fall into two colour groups, coin and background.
    To tell which is which, look at photos that show both: the coin's part is
    always convex (a slice of a disc), while the background's part curves around it.
    """
    means, textures, spreads, dists = [], [], [], []
    for n, img in enumerate(images):
        h, w = img.shape[:2]
        k = max(16, min(h, w) // 10)
        for y, x in ((0, 0), (0, w - k), (h - k, 0), (h - k, w - k)):
            patch = img[y:y + k, x:x + k]
            means.append(patch.reshape(-1, 3).mean(axis=0))
            textures.append(local_texture(patch).mean())
            spreads.append(cv2.GaussianBlur(patch, (0, 0), 3).reshape(-1, 3).std(axis=0).mean())
            if coin_at is not None:
                dists.append(math.hypot(x + k / 2 - coin_at[n][0], y + k / 2 - coin_at[n][1]))
    means, textures, spreads = np.float32(means), np.float32(textures), np.float32(spreads)
    if len(means) < 8:
        return means, float(np.median(textures))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 0.5)
    _, labels, centres = cv2.kmeans(means, 2, None, criteria, 5, cv2.KMEANS_PP_CENTERS)
    labels = labels.ravel()
    if coin_at is not None:
        # The corners well away from the coin are background, whatever colours
        # the coin and background are. Corners just outside the coin's edge are
        # too (a second background, like paper on a mat), if they look like the
        # far ones rather than like a shadow or the coin.
        dists = np.float32(dists)
        if radius:
            far = dists > 1.15 * radius + k
            if far.sum() < 8:
                far = dists >= np.percentile(dists, 67)
            group = labels == int(np.mean(labels[far]) > 0.5)
            outside = far | (group & (dists > radius + 0.75 * k))
        else:
            outside = dists >= np.percentile(dists, 67)
        inside = dists <= np.percentile(dists, 15)
        samples = means[outside][spreads[outside] <= 2 * np.median(spreads[outside]) + 1]
        dist = np.linalg.norm(samples - np.median(samples, axis=0), axis=1)
        odd = dist > 3 * np.median(dist) + 10
        if odd.sum() >= max(5, 0.08 * len(samples)):
            odd &= np.linalg.norm(samples - np.median(means[inside], axis=0), axis=1) < dist
        return samples[~odd], float(np.median(textures[outside]))

    solidity = [[], []]
    for img in images:
        small = cv2.GaussianBlur(cv2.resize(img, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA), (0, 0), 2)
        dist = [np.linalg.norm(small.astype(np.float32) - c, axis=2) for c in centres]
        side = (dist[1] < dist[0]).astype(np.uint8)
        values = []
        for part in (1 - side, side):
            # Only use photos split cleanly into two pieces, one of each colour.
            # Photos inside the coin, where shiny and dark areas make a patchwork,
            # say nothing about which colour is the background.
            if not 0.15 < part.mean() < 0.85:
                break
            count, lab, stats, _ = cv2.connectedComponentsWithStats(part)
            biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            if stats[biggest, cv2.CC_STAT_AREA] < 0.9 * part.sum():
                break
            contours, _ = cv2.findContours((lab == biggest).astype(np.uint8), cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_NONE)
            outline = max(contours, key=cv2.contourArea)
            values.append(cv2.contourArea(outline) / max(cv2.contourArea(cv2.convexHull(outline)), 1))
        if len(values) == 2:
            solidity[0].append(values[0])
            solidity[1].append(values[1])
    if len(solidity[0]) >= 3 and len(solidity[1]) >= 3:
        coin = int(np.median(solidity[1]) > np.median(solidity[0]))
        bg = 1 - coin
    else:
        # Not enough photos show both; fall back to the more consistent colour.
        spread = [np.median(np.linalg.norm(means[labels == i] - centres[i], axis=1))
                  if (labels == i).sum() > 2 else 1e9 for i in range(2)]
        bg = int(np.argmin(spread))
    # Keep every clean background patch, not just one average, so a background
    # that is lit unevenly (darker on one side) is still recognised everywhere.
    # Patches that straddle the coin's edge have mixed colours; drop those.
    samples = [p for p, lbl, mixed in zip(means, labels, spreads) if lbl == bg]
    mixed = np.float32([m for lbl, m in zip(labels, spreads) if lbl == bg])
    keep = mixed <= 2 * np.median(mixed) + 1
    samples = np.float32(samples)[keep]
    # Drop odd ones out (a corner that caught a shadow or a dark part of the
    # coin). Many odd ones together are a second background colour, like paper
    # on a mat, and are kept unless they look more like the coin.
    dist = np.linalg.norm(samples - np.median(samples, axis=0), axis=1)
    odd = dist > 3 * np.median(dist) + 10
    if odd.sum() >= max(5, 0.08 * len(samples)):
        odd &= np.linalg.norm(samples - centres[1 - bg], axis=1) < dist
    samples = samples[~odd]
    return samples, float(np.median(textures[labels == bg]))


def background_noise(images, centres, bg):
    """How far the background strays from its own colours, in the corners
    furthest from the coin, measured the way coin_region looks at it."""
    found = []
    for img, (cx, cy) in zip(images, centres):
        h, w = img.shape[:2]
        k = max(16, min(h, w) // 10)
        for y, x in ((0, 0), (0, w - k), (h - k, 0), (h - k, w - k)):
            found.append((math.hypot(x + k / 2 - cx, y + k / 2 - cy), img, y, x, k))
    found.sort(key=lambda f: -f[0])
    values = []
    for _, img, y, x, k in found[:max(8, len(found) // 3)]:
        patch = cv2.GaussianBlur(img, (0, 0), 3)[y:y + k, x:x + k]
        patch = cv2.resize(patch, (k // 2, k // 2), interpolation=cv2.INTER_AREA)
        values.append(background_distance(patch, bg).ravel())
    return float(np.percentile(np.concatenate(values), 99.9))


def local_texture(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mean = cv2.blur(gray, (7, 7))
    return np.sqrt(np.maximum(cv2.blur(gray * gray, (7, 7)) - mean * mean, 0))


def background_lab(bg):
    samples = np.atleast_2d(np.float32(bg))
    return cv2.cvtColor(np.uint8(np.clip(samples, 0, 255))[None], cv2.COLOR_BGR2LAB)[0].astype(np.float32)


def background_distance(img, bg):
    """How far each pixel's colour is from the nearest of the background's colours."""
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    bg_lab = background_lab(bg)
    # On an unevenly lit coloured background, judge mostly by colour so the
    # lighting doesn't count. On white, grey or black, or a coloured one lit
    # evenly, brightness counts too: a pale gold coin on yellow card differs
    # from it mainly in brightness.
    weight = np.float32([1.0, 1.0, 1.0])
    if np.median(np.hypot(bg_lab[:, 1] - 128, bg_lab[:, 2] - 128)) > 12 and bg_lab[:, 0].std() > 8:
        weight[0] = 0.3
    diff = np.full(lab.shape[:2], np.inf, np.float32)
    for c in bg_lab:
        diff = np.minimum(diff, np.linalg.norm((lab - c) * weight, axis=2))
    return diff


def shadow_tint(lab, bg_lab):
    """True where a dark pixel could be a shadow on the background: a shadow
    keeps the background's own tint, fading with its brightness. Dark copper,
    bronze and tarnish are redder or yellower than that, so they are not."""
    ref = np.median(bg_lab, axis=0)
    share = np.minimum(lab[:, :, 0] / max(float(ref[0]), 1.0), 1.0)
    redder = lab[:, :, 1] - (128 + (ref[1] - 128) * share)
    yellower = lab[:, :, 2] - (128 + (ref[2] - 128) * share)
    return (redder < 4) & (yellower < 6)


def clean_edge(img, mask, bg):
    """Takes the background's colour back out of the coin's outermost pixels.

    A camera pixel on the coin's edge sees a little of both, so a coin on a
    green mat gets a faint green line round it. Near the edge, each pixel's
    colour (not its brightness, which shading changes) is treated as a mix of
    the coin's colour just inside and the nearest background colour, and the
    background's share is taken out of its colour and how solid it is.
    """
    solid = (mask > 250).astype(np.uint8)
    core = cv2.erode(solid, np.ones((11, 11), np.uint8))
    band = (mask > 0) & ~(core > 0)
    if not band.any() or not core.any():
        return img, mask
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    # The coin's colour just inside the edge, spread outwards.
    weight = cv2.GaussianBlur(core.astype(np.float32), (0, 0), 4)
    inner = cv2.GaussianBlur(lab * core[..., None], (0, 0), 4) / np.maximum(weight, 1e-3)[..., None]
    ys, xs = np.nonzero(band)
    c, coin = lab[ys, xs, 1:], inner[ys, xs, 1:]
    samples = background_lab(bg)[:, 1:]
    back = samples[np.argmin(((c[:, None, :] - samples[None]) ** 2).sum(axis=2), axis=1)]
    span = back - coin
    length = np.linalg.norm(span, axis=1)
    share = np.clip(((c - coin) * span).sum(axis=1) / np.maximum(length ** 2, 1.0), 0, 1)
    # Only where the coin and background colours clearly differ.
    share[length < 8] = 0
    keep = np.maximum(1 - share, 0.35)[:, None]
    fixed = np.where(share[:, None] > 0.65, coin, (c - share[:, None] * back) / keep)
    lab[ys, xs, 1:] = np.clip(fixed, 0, 255)
    out = cv2.cvtColor(np.uint8(np.clip(lab, 0, 255)), cv2.COLOR_LAB2BGR)
    alpha = mask.astype(np.float32)
    # Pixels that are mostly background colour are background.
    alpha[ys, xs] *= np.where(share > 0.6, 1 - share, 1 - 0.5 * share)
    return out, np.uint8(np.clip(alpha, 0, 255))


def tidy_edge(img, mask, bg, tolerance, depth, near_edge=None, coin_l=None):
    """Peels background and the coin's cast shadow off the outside of a mask.

    Starting from the background around the mask, it eats inwards through
    pixels that look like the background, or like a shadow (much darker than
    both the background and the coin, with little colour), up to depth pixels.
    That removes the thin coloured fringe and the dark crescent a coin throws
    on paper, but never reaches into the coin itself. near_edge marks where
    the coin's outline is expected; a shadow that runs off the side of the
    photo there is peeled even if no background shows in this photo.
    """
    h, w = mask.shape
    outside = mask < 128
    if outside.all() or (not outside.any() and near_edge is None):
        return mask
    small = cv2.resize(cv2.GaussianBlur(img, (0, 0), 1.5), (w // 2, h // 2), interpolation=cv2.INTER_AREA)
    out_small = cv2.resize(outside.astype(np.uint8), (w // 2, h // 2), interpolation=cv2.INTER_NEAREST) > 0
    diff = background_distance(small, bg)
    background = (diff < tolerance).astype(np.uint8)
    # Clearly background: the paper's own colour, which a grey or brown coin's
    # pale rim never has. On a white or grey background a pale rim can match
    # it, so there only the thin fringe is peeled.
    bg_lab = background_lab(bg)
    clear = np.zeros_like(background)
    if np.median(np.hypot(bg_lab[:, 1] - 128, bg_lab[:, 2] - 128)) > 12:
        clear = (diff < 0.4 * tolerance).astype(np.uint8)
    shadow = np.zeros_like(background)
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)
    bg_l = float(np.median(bg_lab[:, 0]))
    inside = cv2.erode((~out_small).astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
    if coin_l is None and inside.sum() > 100:
        coin_l = float(np.median(lab[:, :, 0][inside]))
    if bg_l > 100 and coin_l is not None:
        # Only on a light background: on a dark one a shadow is just more background.
        chroma = np.hypot(lab[:, :, 1] - 128, lab[:, :, 2] - 128)
        # A cast shadow is very dark, colourless and flat; the dark parts of a
        # coin's rim are less dark and have scratches and texture.
        flat = cv2.GaussianBlur(local_texture(small), (0, 0), 2) < 12
        shadow = ((lab[:, :, 0] < min(0.4 * coin_l, 0.3 * bg_l)) & (chroma < 20) & flat &
                  shadow_tint(lab, bg_lab)).astype(np.uint8)
    eaten = out_small.astype(np.uint8)
    kernel = np.ones((3, 3), np.uint8)
    # A coin's edge can fade into the background colour, so only a thin fringe
    # of near-background pixels is peeled, then the shadow and whatever is
    # clearly background, then the fringe between the shadow and the coin.
    def grow(eaten, through, steps):
        for _ in range(max(1, int(steps))):
            grown = cv2.dilate(eaten, kernel) & (through | eaten)
            if np.array_equal(grown, eaten):
                break
            eaten = grown
        return eaten

    fringe = max(2, depth / 20)
    eaten = grow(eaten, background | shadow, fringe)
    before = eaten
    # The soft step from paper to shadow is neither colour, so let the peel
    # jump it once, from the background straight into the shadow.
    jump = np.ones((9, 9), np.uint8)
    eaten |= cv2.dilate(eaten, jump) & cv2.dilate(shadow, jump)
    if near_edge is not None and shadow.any():
        border = np.zeros_like(shadow)
        border[:2, :] = border[-2:, :] = border[:, :2] = border[:, -2:] = 1
        near = cv2.resize(near_edge.astype(np.uint8), (w // 2, h // 2), interpolation=cv2.INTER_NEAREST)
        eaten |= border & near & shadow
    eaten = grow(eaten, shadow | clear, 1.5 * depth)  # a cast shadow can be wide
    # Only right next to a shadow that was peeled, so a coin's pale rim is kept.
    near = cv2.dilate(eaten & ~before, np.ones((2 * int(fringe) + 1,) * 2, np.uint8))
    eaten = grow(eaten, background & near, fringe)
    eaten = cv2.resize(eaten, (w, h), interpolation=cv2.INTER_NEAREST)
    eaten = cv2.GaussianBlur(eaten.astype(np.float32), (0, 0), 1.0)
    return np.minimum(mask, np.uint8(np.clip((1 - eaten) * 255, 0, 255)))


def coin_region(img, bg, tolerance, bg_texture=0.0):
    """255 where the picture is coin, 0 where it is background."""
    # Blur first so a speckled or patterned background averages out to its colour.
    smooth = cv2.GaussianBlur(img, (0, 0), 3)
    h, w = img.shape[:2]
    small = cv2.resize(smooth, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
    diff = cv2.resize(background_distance(small, bg), (w, h), interpolation=cv2.INTER_LINEAR)
    # A shiny spot on the coin can match a plain background's colour but still
    # has scratches and texture. Only used when the background itself is smooth.
    texture = cv2.GaussianBlur(local_texture(img), (0, 0), 2)
    coin = (diff > tolerance) | (texture > max(tolerance / 4, 3 * bg_texture + 4))
    coin = coin.astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    coin = cv2.morphologyEx(coin, cv2.MORPH_CLOSE, kernel)
    coin = cv2.morphologyEx(coin, cv2.MORPH_OPEN, kernel)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(coin)
    if count <= 1:
        return np.zeros_like(coin)
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    # A coin is convex, and so is the part of it a photo can see. Filling the
    # convex hull puts back shiny or dark spots that happened to match the background.
    pts = cv2.findNonZero((labels == biggest).astype(np.uint8))
    filled = np.zeros_like(coin)
    cv2.fillConvexPoly(filled, cv2.convexHull(pts), 255)
    # The blur above spreads the coin outwards a little; pull the edge back in.
    return cv2.erode(filled, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))


def edge_points(region):
    """Points on the coin's edge, leaving out the photo's own border."""
    contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return np.empty((0, 2))
    pts = max(contours, key=len).reshape(-1, 2).astype(np.float64)
    h, w = region.shape
    keep = (pts[:, 0] > 2) & (pts[:, 0] < w - 3) & (pts[:, 1] > 2) & (pts[:, 1] < h - 3)
    return pts[keep]


def fit_circle(pts, radius=None, start=None):
    """Least-squares circle. With radius given, only the centre is fitted."""
    if radius is None:
        a = np.c_[2 * pts, np.ones(len(pts))]
        b = (pts ** 2).sum(axis=1)
        cx, cy, c = np.linalg.lstsq(a, b, rcond=None)[0]
        r = math.sqrt(max(c + cx * cx + cy * cy, 0))
    else:
        cx, cy = start
        r = radius
        for _ in range(50):
            d = pts - (cx, cy)
            dist = np.hypot(d[:, 0], d[:, 1]) + 1e-9
            jac = -d / dist[:, None]
            step = np.linalg.lstsq(jac, -(dist - r), rcond=None)[0]
            cx, cy = cx + step[0], cy + step[1]
            if abs(step).max() < 1e-3:
                break
    resid = np.abs(np.hypot(*(pts - (cx, cy)).T) - r)
    return cx, cy, r, float(np.median(resid))


def fit_circle_robust(pts, rounds=6):
    """Circle fit that ignores stray points (another coin, a paper edge, a label)."""
    for _ in range(rounds):
        cx, cy, r, err = fit_circle(pts)
        resid = np.abs(np.hypot(*(pts - (cx, cy)).T) - r)
        keep = resid <= max(2.5 * np.median(resid), 2.0)
        if keep.all() or keep.sum() < 20:
            break
        pts = pts[keep]
    return cx, cy, r, err, len(pts)


def fit_ellipse_robust(pts, rounds=6):
    """Ellipse fit that ignores stray points. Returns centre, semi-axes and angle."""
    if len(pts) < 50:
        return None
    for _ in range(rounds):
        (cx, cy), (da, db), angle = cv2.fitEllipse(np.float32(pts))
        a, b = da / 2, db / 2
        ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        d = pts - (cx, cy)
        rho = np.hypot((d[:, 0] * ca + d[:, 1] * sa) / a, (-d[:, 0] * sa + d[:, 1] * ca) / b)
        resid = np.abs(rho - 1) * (a + b) / 2
        keep = resid <= max(2.5 * np.median(resid), 2.0)
        if keep.all() or keep.sum() < 50:
            break
        pts = pts[keep]
    return cx, cy, a, b, angle


def tile_offsets(folder, names, shape):
    """Where each photo's centre sits relative to the coin's centre, in pixels,
    from the stage positions a scan saved. None for hand-taken photos."""
    pos_path = os.path.join(folder, "positions.csv")
    info_path = os.path.join(folder, "scan_info.json")
    if not (os.path.exists(pos_path) and os.path.exists(info_path)):
        return None
    with open(info_path) as f:
        info = json.load(f)
    with open(pos_path) as f:
        pos = {r["file"]: (float(r["x_mm"]), float(r["y_mm"])) for r in csv.DictReader(f)}
    xv, yv = np.array(info["x_vec"]), np.array(info["y_vec"])
    h, w = shape[:2]
    offsets = {}
    for n in names:
        if n in pos:
            x, y = pos[n]
            offsets[n] = np.array([w / 2, h / 2]) + (x * xv + y * yv) * info["px_per_mm"]
    return offsets or None


OUTLINE_STEPS = 1440  # directions around the coin, a quarter of a degree apart


def guide_rho(whole, offset, shape):
    """For each pixel of a photo, its distance from the coin's centre as a share
    of the fitted ellipse's distance in that direction: 1.0 is on the ellipse."""
    h, w = shape
    ex, ey = offset + whole[:2]
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    dx, dy = xx - ex, yy - ey
    ca, sa = math.cos(math.radians(whole[4])), math.sin(math.radians(whole[4]))
    return np.hypot((dx * ca + dy * sa) / whole[2], (-dx * sa + dy * ca) / whole[3])


def outline_mask(outline, whole, offset, shape):
    """The part of a photo inside the coin's outline, with a soft one-pixel edge."""
    h, w = shape
    ex, ey = offset + whole[:2]
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    dx, dy = xx - ex, yy - ey
    angle = (np.arctan2(dy, dx) / (2 * math.pi) * OUTLINE_STEPS) % OUTLINE_STEPS
    edge = np.interp(angle.ravel(), np.arange(OUTLINE_STEPS + 1),
                     np.append(outline, outline[0])).reshape(h, w)
    return np.uint8(np.clip(edge - np.hypot(dx, dy) + 0.5, 0, 1) * 255)


def vote_outline(images, info, offsets, whole, bg, coin_l, args):
    """The edge in each direction, from each photo masked on its own."""
    mean_r = (whole[2] + whole[3]) / 2
    edge = 1 - args.trim / mean_r
    votes = [[] for _ in range(OUTLINE_STEPS)]
    for n, img in images.items():
        if n not in offsets:
            continue
        h, w = img.shape[:2]
        rho = guide_rho(whole, offsets[n], (h, w))
        if rho.max() <= edge or rho.min() >= edge:
            continue
        # Well inside the ellipse is always coin, well outside never is
        # (another coin, a label, a paper edge); in between, the photo decides.
        inner = np.uint8(rho <= edge - args.band) * 255
        outer = np.uint8(rho <= edge + args.band) * 255
        mask = np.maximum(inner, np.minimum(outer, info[n][0]))
        if not args.keep_shadow:
            mask = tidy_edge(img, mask, bg, args.tolerance, 2 * args.band * mean_r,
                             near_edge=rho > edge - args.band / 2, coin_l=coin_l)
        pts = edge_points(np.uint8(mask > 127) * 255)
        if len(pts) == 0:
            continue
        d = pts - offsets[n] - whole[:2]
        angle = np.int32(np.arctan2(d[:, 1], d[:, 0]) / (2 * math.pi) * OUTLINE_STEPS) % OUTLINE_STEPS
        dist = np.hypot(d[:, 0], d[:, 1])
        for a in np.unique(angle):
            votes[a].append(float(np.median(dist[angle == a])))
    # Where photos disagree, lean towards the one that cut further in.
    return np.array([np.percentile(v, 30) if v else np.nan for v in votes])


def whole_outline(images, info, offsets, whole, bg, bg_texture, coin_l, args):
    """The coin's outline as a distance from its centre in every direction.

    The photos are laid out in one picture using the stage positions, then the
    edge is found by walking outwards from well inside the coin in each
    direction until the picture turns into background or the coin's shadow.
    Every photo then uses this one outline, smoothed so the cut is clean.
    """
    mean_r = (whole[2] + whole[3]) / 2
    ca, sa = math.cos(math.radians(whole[4])), math.sin(math.radians(whole[4]))
    t = (np.arange(OUTLINE_STEPS) + 0.5) / OUTLINE_STEPS * 2 * math.pi
    guide = 1 / np.hypot((np.cos(t) * ca + np.sin(t) * sa) / whole[2],
                         (-np.cos(t) * sa + np.sin(t) * ca) / whole[3])
    if args.circle:
        return guide - args.trim

    # One picture of the whole coin, shrunk to keep it quick.
    scale = min(1.0, 1200 / mean_r)
    h, w = next(iter(images.values())).shape[:2]
    corner = {n: -offsets[n] for n in images if n in offsets}
    lo = np.min(list(corner.values()), axis=0)
    hi = np.max(list(corner.values()), axis=0) + (w, h)
    size = np.int32(np.ceil((hi - lo) * scale)) + 2
    mosaic = np.zeros((size[1], size[0], 3), np.uint8)
    seen = np.zeros((size[1], size[0]), np.uint8)
    for n, c in corner.items():
        small = cv2.resize(images[n], None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        x, y = np.int32(np.round((c - lo) * scale))
        sh, sw = small.shape[:2]
        mosaic[y:y + sh, x:x + sw] = small
        seen[y:y + sh, x:x + sw] = 1
    centre = (np.array(whole[:2]) - lo) * scale

    # What each pixel looks like: coin, background, or the coin's shadow.
    # The photos' own masks (below) already handle a pale rim that fades into
    # a white or grey background, so here only clearly-background counts.
    smooth = cv2.GaussianBlur(mosaic, (0, 0), 2)
    bg_lab = background_lab(bg)
    coloured = np.median(np.hypot(bg_lab[:, 1] - 128, bg_lab[:, 2] - 128)) > 12
    bg_dist = background_distance(smooth, bg)
    background = bg_dist < args.tolerance * (1.0 if coloured else 0.25)
    texture = cv2.GaussianBlur(local_texture(mosaic), (0, 0), 2)
    coin = ~background | (texture > max(args.tolerance / 4, 3 * bg_texture + 4))
    shadow = np.zeros_like(coin)
    if not args.keep_shadow and coin_l:
        lab = cv2.cvtColor(smooth, cv2.COLOR_BGR2LAB).astype(np.float32)
        chroma = np.hypot(lab[:, :, 1] - 128, lab[:, :, 2] - 128)
        bg_l = float(np.median(bg_lab[:, 0]))
        if bg_l > 100:
            shadow = lab[:, :, 0] < min(0.4 * coin_l, 0.3 * bg_l)
        else:
            # On a dark background the shadow is simply darker still.
            shadow = lab[:, :, 0] < min(0.6 * bg_l, 0.4 * coin_l)
        shadow &= (chroma < 20) & shadow_tint(lab, bg_lab)

    # The same, unrolled around the centre: one row per direction.
    steps = int(np.ceil(guide.max() * (1 + 2 * args.band) * scale))
    radius = np.arange(steps, dtype=np.float32)
    map_x = np.float32(centre[0] + np.cos(t)[:, None] * radius[None, :])
    map_y = np.float32(centre[1] + np.sin(t)[:, None] * radius[None, :])
    known = cv2.remap(seen.astype(np.float32), map_x, map_y, cv2.INTER_NEAREST, borderValue=0)
    polar_dist = cv2.remap(bg_dist, map_x, map_y, cv2.INTER_LINEAR, borderValue=0)
    # A soft shadow on the background: the background's own colour mix
    # (chromaticity), only darker.
    ref = np.median(np.float32(bg), axis=0)
    total = smooth.astype(np.float32).sum(axis=2) + 1
    tint = np.linalg.norm(smooth.astype(np.float32) / total[..., None] - ref / (ref.sum() + 1), axis=2)
    soft_shadow = ((tint < 0.02) & (total < 0.97 * (ref.sum() + 1)) & (total > 0.4 * (ref.sum() + 1))).astype(np.float32)
    polar_soft = cv2.remap(soft_shadow, map_x, map_y, cv2.INTER_LINEAR, borderValue=0)

    def circular(values, size, how):
        pad = np.concatenate([values[-size:], values, values[:size]])
        windows = np.lib.stride_tricks.sliding_window_view(pad, 2 * size + 1)
        return how(windows, axis=1)

    def walk(coin):
        """Walk out from well inside the coin; the edge is where a stretch of
        mostly not-coin begins. A shiny spot or a dark crevice is too short."""
        polar = cv2.remap((coin & (seen > 0)).astype(np.float32), map_x, map_y, cv2.INTER_LINEAR, borderValue=0)
        run = max(3, int(0.015 * mean_r * scale))
        ahead = cv2.blur(polar, (run, 1), anchor=(0, 0), borderType=cv2.BORDER_REPLICATE)
        outline = np.full(OUTLINE_STEPS, np.nan)
        for i in range(OUTLINE_STEPS):
            start = int(guide[i] * (1 - 3 * args.band) * scale)
            stop = min(steps - run, int(guide[i] * (1 + args.band) * scale))
            if start >= stop or known[i, start:stop].min() == 0:
                continue
            row = polar[i, start:stop]
            # Mostly not-coin from here to the end of the stretch: a dark spot on
            # the coin is followed by more coin, a shadow only by background.
            rest = np.cumsum(row[::-1])[::-1] / np.arange(len(row), 0, -1)
            gone = np.nonzero((ahead[i, start:stop] < 0.3) & (rest < 0.35))[0]
            outline[i] = (start + (gone[0] if len(gone) else stop - start)) / scale
        return polar, outline

    polar, outline = walk(coin & ~shadow)
    # Second opinion: each photo masked on its own, voting on the edge. Where
    # the two disagree the one that cuts further in is kept, since the usual
    # mistake is a bit of background or shadow left on, not coin cut off.
    votes = vote_outline(images, info, offsets, whole, bg, coin_l, args)
    # A shadow runs along a stretch of the edge; a single direction that
    # stops early has usually hit a dark spot on the coin, so ignore those.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        outline = circular(outline, 12, np.nanmedian)
    if not coloured:
        # On white, grey or black there is no colour to tell a pale or dark
        # part of the coin from the background, so the walk may only trim a
        # little off what the photos themselves found.
        outline = np.fmax(outline, votes * 0.97)
    outline = np.where(np.isnan(outline), votes, np.fmin(outline, votes))
    have = ~np.isnan(outline)
    if have.sum() < OUTLINE_STEPS // 4:
        return guide - args.trim
    idx = np.arange(OUTLINE_STEPS)
    outline = np.interp(idx, idx[have], outline[have], period=OUTLINE_STEPS)

    # Remove bumps (a blob of background or shadow) that stick out from the
    # outline around them, then smooth away the pixel-sized roughness. Wide
    # dents are dealt with below, where it can be checked that they are coin.
    # Judged against the rough oval, so an oval coin's long sides don't count
    # as bumps, and over a wide stretch so a wide blob can't hide itself.
    broad = circular(outline / guide, 240, np.median) * guide
    bump = outline - broad > 0.015 * mean_r
    # A narrow notch is often noise, filled in below if it looks like coin.
    # A wide, shallow one may be a real flat side, so it is left to the oval check.
    notch = broad - outline > 0.015 * mean_r
    fill = np.zeros(OUTLINE_STEPS, bool)
    sharp = np.zeros(OUTLINE_STEPS, bool)
    if notch.any() and not notch.all():
        shift = int(np.argmin(notch))  # start the scan outside a notch
        rolled = np.roll(notch, -shift)
        edges = np.diff(np.r_[0, rolled.astype(np.int8), 0])
        for a, b in zip(np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0]):
            run = (np.arange(a, b) + shift) % OUTLINE_STEPS
            depth = (broad[run] - outline[run]).max()
            if b - a <= OUTLINE_STEPS // 6 and depth > 0.05 * mean_r:
                # A deep bite out of one side is rarely the coin's real shape
                # (a flat side or a worn edge is shallow).
                sharp[run] = True
            elif b - a <= OUTLINE_STEPS // 36:
                fill[run] = True
    outline[bump] = broad[bump]
    outline = circular(outline, 6, np.median)
    outline = circular(outline, 8, np.mean)

    def push_out(old, new, needed=0.65, tone_min=0.25):
        """Takes the new outline, except where it moves outwards over what
        doesn't look like coin: a coin with a genuinely flat or dented side
        must not get a sliver of background added."""
        looks = np.full(OUTLINE_STEPS, np.nan)
        tone = np.full(OUTLINE_STEPS, np.nan)
        for i in np.nonzero(new > old + 0.5)[0]:
            a, b = int(old[i] * scale), int(new[i] * scale) + 1
            looks[i] = polar[i, a:b].mean()
            tone[i] = np.median(polar_dist[i, a:b])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            looks = circular(looks, 10, np.nanmean)
            tone = circular(tone, 10, np.nanmean)
        # Colour that matches the background almost exactly is never coin.
        bad = (np.nan_to_num(looks, nan=1.0) < needed) | (np.nan_to_num(tone, nan=99.0) < tone_min * args.tolerance)
        result = np.where(bad, np.minimum(new, old), new)
        return circular(result, 8, np.mean)

    # A narrow notch only needs to look a little like coin to be filled in,
    # and a deep bite only needs to be not quite the background's colour.
    outline = push_out(outline, np.where(fill, np.maximum(outline, broad), outline), needed=0.4)
    outline = push_out(outline, np.where(sharp, np.maximum(outline, broad), outline), needed=0, tone_min=0.2)

    # Most coins are close to an oval. Fit one to the outline, ignoring the
    # parts that stray, and keep the outline near it: that puts back a pale
    # rim that faded into the background and cuts off what is left of a shadow.
    # The oval is fitted only to the directions where the outline agrees with
    # the first, rough oval, so a dent or bulge can't pull it. If a shadow
    # made the rough oval too big all round, agree with its shape instead.
    ratio = outline / guide
    agree = np.abs(ratio - 1) < 0.02
    if agree.mean() < 0.5:
        agree = np.abs(ratio / np.median(ratio) - 1) < 0.02
    if agree.mean() < 0.3:
        agree[:] = True
    def oval_of(pts):
        """Distance from the coin's centre to an oval fitted to pts, in each direction."""
        fit = fit_ellipse_robust(pts)
        if not fit:
            return None
        fx, fy, fa, fb, fang = fit
        c, sn = math.cos(math.radians(fang)), math.sin(math.radians(fang))
        r = np.linspace(0, 1.5 * outline.max(), 6000)
        px = np.cos(t)[:, None] * r - fx
        py = np.sin(t)[:, None] * r - fy
        rho = np.hypot((px * c + py * sn) / fa, (-px * sn + py * c) / fb)
        # Interpolate between the two samples either side of the oval.
        k = np.clip(np.argmax(rho >= 1, axis=1), 1, len(r) - 1)
        lo, hi = rho[np.arange(len(k)), k - 1], rho[np.arange(len(k)), k]
        return r[k - 1] + (1 - lo) / np.maximum(hi - lo, 1e-9) * (r[k] - r[k - 1])

    oval = oval_of(np.c_[np.cos(t) * outline, np.sin(t) * outline][agree])
    if oval is not None:
        outline = push_out(outline, np.clip(outline, oval * (1 - args.dent), oval * (1 + args.bulge)))
    # A coin is convex, so fill in any dent that is left.
    filled = outline.copy()
    hull = cv2.convexHull(np.float32(np.c_[np.cos(t) * outline, np.sin(t) * outline]))[:, 0]
    a, b = hull, np.roll(hull, -1, axis=0)
    for i in range(OUTLINE_STEPS):
        d = np.array([math.cos(t[i]), math.sin(t[i])])
        # Distance along this direction to each hull side it crosses.
        e = b - a
        den = d[0] * e[:, 1] - d[1] * e[:, 0]
        with np.errstate(divide="ignore", invalid="ignore"):
            dist = (a[:, 0] * e[:, 1] - a[:, 1] * e[:, 0]) / den
            u = (a[:, 0] * d[1] - a[:, 1] * d[0]) / den
        ok = (np.abs(den) > 1e-9) & (u >= -1e-6) & (u <= 1 + 1e-6) & (dist > 0)
        if ok.any():
            filled[i] = dist[ok].max()
    outline = push_out(outline, filled)
    # Give the cut an exactly round (or oval) edge: wobble of under 0.8% of
    # the radius around a fitted oval is removed, so a round coin gets a
    # perfect circle. Bigger departures, like an old coin's uneven edge or a
    # modern coin's scallops, are kept, just as smooth. Only for a coin whose
    # edge is mostly round already; an uneven old coin keeps its traced edge.
    oval = oval_of(np.c_[np.cos(t) * outline, np.sin(t) * outline])
    round_coin = oval is not None and (np.abs(outline - oval) < 0.006 * mean_r).mean() > 0.7
    shaped = False
    if coloured and oval is not None:
        # On a coloured background, trace the edge closely by colour: the edge
        # is where the picture turns from coin to background, within a few
        # pixels of the outline. (On white or grey a pale rim can match the
        # background, so there the smoothed outline is kept.)
        refined = outline.copy()
        for i in range(OUTLINE_STEPS):
            o = int(outline[i] * scale)
            lo, hi = max(1, o - int(0.035 * mean_r * scale)), min(polar.shape[1] - 1, o + int(0.015 * mean_r * scale))
            # Coin by colour as well as by the walk's coin/shadow test; the
            # walk's texture test alone spills a few pixels into a narrow notch.
            inside = (polar[i, lo:hi] >= 0.5) & (polar_dist[i, lo:hi] >= 0.5 * args.tolerance) & \
                (args.keep_shadow | (polar_soft[i, lo:hi] < 0.5))
            # The outermost step from coin to background with nothing but
            # background after it in the window.
            after = np.cumsum(inside[::-1])[::-1]
            steps_out = np.nonzero(inside[:-1] & ~inside[1:] & (after[1:] == 0))[0]
            if len(steps_out):
                refined[i] = (lo + steps_out[-1] + 1) / scale
        refined = circular(circular(refined, 5, np.median), 3, np.mean)
        # Notches that show the background, evenly round the coin, mean a
        # shaped edge like the scalloped 20 cent: keep the close trace.
        # Otherwise a round coin is snapped to its oval below.
        notch = oval - refined > 0.008 * mean_r
        # Count the separate notches and how evenly they are spaced.
        centres = []
        if notch.any() and not notch.all():
            shift = int(np.argmin(notch))
            rolled = np.roll(notch, -shift)
            edges = np.diff(np.r_[0, rolled.astype(np.int8), 0])
            for a, b in zip(np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0]):
                if b - a >= OUTLINE_STEPS // 360:
                    centres.append(((a + b) / 2 + shift) % OUTLINE_STEPS)
        gaps = np.diff(np.r_[np.sort(centres), np.sort(centres)[:1] + OUTLINE_STEPS]) if centres else np.zeros(0)
        # Evenly spaced: every gap is a whole number of the usual gap (a notch
        # the trace missed just makes one gap double).
        even = False
        if len(gaps) >= 5:
            steps = gaps / np.median(gaps)
            even = np.abs(steps - np.round(steps)).max() < 0.2 and np.round(steps).sum() <= len(gaps) + 2
        shaped = even or not round_coin
        print(f"Close trace on the background colour: {len(centres)} notches, "
              f"{'evenly spaced, so a shaped edge' if even else 'not a shaped edge'}")
        if shaped:
            outline = refined
    if shaped:
        pass
    elif round_coin:
        off = outline - oval
        give = 0.008 * mean_r
        # Fade from nothing at the give to the full departure at twice it,
        # so a real notch keeps its full depth.
        fade = np.clip(np.abs(off) / give - 1, 0, 1)
        off = off * fade * fade * (3 - 2 * fade)
        # On a round coin anything sticking out past the oval is left-over
        # shadow or background, not coin.
        off = np.minimum(off, args.bulge * mean_r)
        outline = oval + circular(off, 6, np.mean)
        # A dent in a round coin is filled unless the gap is the background's
        # own colour; a scalloped coin's notches show background and stay.
        outline = push_out(outline, np.maximum(outline, oval), needed=0, tone_min=0.3)
    return outline - args.trim


def cmd_croptiles(cfg, args):
    """Masks each photo's background by finding the coin edge in the picture.

    Photos that are all coin are copied unchanged. On the others a circle is
    fitted to the visible edge. Every photo shows the same coin, so the radius
    is shared: it is measured on the photos with the longest edges, then only
    the centre is fitted on each photo.
    """
    folder = os.path.abspath(args.folder)
    out = args.out or folder + "_cropped"
    os.makedirs(out, exist_ok=True)
    names = sorted(f for f in os.listdir(folder)
                   if f.lower().endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")))
    images = {n: cv2.imread(os.path.join(folder, n)) for n in names}
    images = {n: im for n, im in images.items() if im is not None}
    if not images:
        raise SystemExit("No photos in that folder.")
    if args.background_photo:
        empty = cv2.imread(args.background_photo)
        if empty is None:
            raise SystemExit(f"Could not read {args.background_photo}")
        eh, ew = empty.shape[:2]
        bg = np.float32([empty[y * eh // 6:(y + 1) * eh // 6, x * ew // 6:(x + 1) * ew // 6].reshape(-1, 3).mean(axis=0)
                         for y in range(6) for x in range(6)])
        bg_texture = float(local_texture(empty).mean())
    else:
        # Stage positions say where the coin is in every photo, which makes
        # telling the background from the coin easy even when they look alike.
        where = tile_offsets(folder, list(images), next(iter(images.values())).shape)
        centres, size = None, None
        if where and all(n in where for n in images):
            centres = [where[n] for n in images]
            with open(os.path.join(folder, "scan_info.json")) as f:
                scan = json.load(f)
            if scan.get("diameter"):
                size = scan["diameter"] / 2 * scan["px_per_mm"]
        bg, bg_texture = background_colour(list(images.values()), centres, size)
        if args.tolerance is None and centres is not None:
            # A clean, even background lets a coin of nearly the same colour be
            # told apart; a speckled or patchy one needs more leeway.
            noise = background_noise(list(images.values()), centres, bg)
            args.tolerance = float(np.clip(1.5 * noise + 4, 8, 20))
            print(f"Tolerance {args.tolerance:.0f} (background varies by up to {noise:.1f})")
    if args.tolerance is None:
        args.tolerance = 20.0
    print(f"Background colour (B, G, R): {np.round(np.median(bg, axis=0)).astype(int).tolist()} "
          f"({len(bg)} samples), texture {bg_texture:.1f}")

    info = {}
    for n, img in images.items():
        region = coin_region(img, bg, args.tolerance, bg_texture)
        bg_frac = 1 - region.mean() / 255
        pts = edge_points(region)
        info[n] = (region, bg_frac, pts)

    # How bright the coin is overall, from the photos that are all or mostly
    # coin, to tell its dark parts from a shadow in photos that show only a sliver.
    coin_l = []
    for n, (region, bg_frac, pts) in info.items():
        if bg_frac < 0.5:
            inner = cv2.erode(region, np.ones((31, 31), np.uint8)) > 0
            coin_l.append(cv2.cvtColor(images[n], cv2.COLOR_BGR2LAB)[:, :, 0][inner][::17])
    coin_l = float(np.median(np.concatenate(coin_l))) if coin_l else None

    # With stage positions from a scan, every photo's edge points can be put
    # in one picture and a single circle fitted to the whole coin. That is far
    # steadier than fitting each photo on its own.
    whole = None
    offsets = None if args.outline else tile_offsets(folder, list(images), next(iter(images.values())).shape)
    if offsets:
        allpts = [pts - offsets[n] for n, (region, bg_frac, pts) in info.items()
                  if n in offsets and bg_frac > 0.02 and len(pts) > 20]
        if allpts and sum(len(p) for p in allpts) > 200:
            allpts = np.vstack(allpts)
            if size:
                # The coin is about the size given to scan and near the middle
                # of the scan: ignore edges far from that (a pattern or lines on
                # the background), which could otherwise pull the fit.
                near = np.abs(np.hypot(allpts[:, 0], allpts[:, 1]) - size) < 0.2 * size
                if near.sum() > 200:
                    allpts = allpts[near]
            cx, cy, r, err, used = fit_circle_robust(allpts)
            whole = (cx, cy, r, r, 0.0)
            if args.radius:
                whole = (cx, cy, args.radius, args.radius, 0.0)
            elif not args.circle:
                # A coin photographed a little from the side, or an oval old
                # coin, fits an ellipse better. Keep it only if it is close to round.
                ellipse = fit_ellipse_robust(allpts[np.abs(np.hypot(*(allpts - (cx, cy)).T) - r) < 0.15 * r])
                if ellipse and max(ellipse[2:4]) < 1.3 * min(ellipse[2:4]) and \
                        not (size and max(abs(ellipse[2] - size), abs(ellipse[3] - size)) > 0.08 * size):
                    whole = ellipse
            print(f"Whole coin: {2 * whole[2]:.0f} x {2 * whole[3]:.0f} px across, "
                  f"fitted to {used} edge points from the stage positions")

    fits = []
    for n, (region, bg_frac, pts) in info.items():
        if bg_frac > 0.02 and len(pts) > 50:
            cx, cy, r, err = fit_circle(pts)
            fits.append((len(pts), r, err))
    fits.sort(reverse=True)
    good = [r for count, r, err in fits[:max(3, len(fits) // 3)] if err < 3]
    if args.radius:
        radius = args.radius
    elif good:
        radius = float(np.median(good))
    else:
        radius = None
    if radius:
        print(f"Coin radius: {radius:.0f} px")

    if whole:
        outline = whole_outline(images, info, offsets, whole, bg, bg_texture, coin_l, args)

    for n, img in images.items():
        region, bg_frac, pts = info[n]
        h, w = img.shape[:2]
        if whole and n in offsets:
            mask = outline_mask(outline, whole, offsets[n], (h, w))
            if mask.min() == 255:
                kind = "all coin, kept as is"
                cv2.imwrite(os.path.join(out, os.path.splitext(n)[0] + ".png"), img)
            elif mask.max() == 0:
                kind = "no coin, skipped"
            else:
                colour, mask = clean_edge(img, mask, bg)
                rgba = cv2.cvtColor(colour, cv2.COLOR_BGR2BGRA)
                rgba[:, :, 3] = mask
                cv2.imwrite(os.path.join(out, os.path.splitext(n)[0] + ".png"), rgba)
                kind = "edge, masked using the whole-coin outline"
            print(f"  {n}: {kind}")
            continue
        if bg_frac < 0.02:
            # Under 2% is more likely a shiny spot than real background, and a
            # sliver of real background is removed anyway by cropfinal.
            kind = "all coin, kept as is"
            cv2.imwrite(os.path.join(out, os.path.splitext(n)[0] + ".png"), img)
        elif region.max() == 0:
            kind = "no coin, skipped"
        else:
            mask = region
            kind = "edge, masked to the coin's outline"
            if radius and len(pts) > 20 and not args.outline:
                m = cv2.moments(region)
                start = (m["m10"] / m["m00"], m["m01"] / m["m00"])
                cx, cy, r, err = fit_circle(pts, radius, start)
                if err < max(4, radius * 0.01):
                    mask = np.zeros((h, w), np.uint8)
                    cv2.circle(mask, (int(round(cx)), int(round(cy))), int(round(r - args.trim)), 255, -1,
                               lineType=cv2.LINE_AA)
                    kind = f"edge, circle at {cx:.0f}, {cy:.0f}"
            if not args.keep_shadow:
                mask = tidy_edge(img, mask, bg, args.tolerance, 0.12 * (radius or min(h, w) / 2), coin_l=coin_l)
            rgba = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
            rgba[:, :, 3] = mask
            cv2.imwrite(os.path.join(out, os.path.splitext(n)[0] + ".png"), rgba)
        print(f"  {n}: {bg_frac * 100:4.1f}% background, {kind}")
    for extra in ("positions.csv", "scan_info.json"):
        if os.path.exists(os.path.join(folder, extra)):
            shutil.copy(os.path.join(folder, extra), out)
    print(f"Saved to {out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("devices")

    for name in ("jog", "calibrate", "scan"):
        p = sub.add_parser(name)
        p.add_argument("--simulate", action="store_true", help="no hardware; use a coin photo instead")
        p.add_argument("--image", help="coin photo for --simulate")
        if name == "calibrate":
            p.add_argument("--distance", type=float, default=1.0, help="mm to move for the test (default 1)")
        if name == "scan":
            p.add_argument("diameter", type=float, help="coin diameter in mm")
            p.add_argument("--out", help="folder for the photos")
            p.add_argument("--edge-trim", type=float, default=0.2, help="mm to trim off the rim edge")
            p.add_argument("--no-mask", action="store_true", help="keep the background on every tile")

    p = sub.add_parser("stitch")
    p.add_argument("folder")

    p = sub.add_parser("cropfinal")
    p.add_argument("image")
    p.add_argument("--out")
    p.add_argument("--center", type=float, nargs=2)
    p.add_argument("--radius", type=float)
    p.add_argument("--shrink", type=float, default=0.005, help="fraction to pull the circle in")

    p = sub.add_parser("croptiles")
    p.add_argument("folder")
    p.add_argument("--out", help="folder for the masked photos (default: <folder>_cropped)")
    p.add_argument("--tolerance", type=float, default=None,
                   help="how different from the background counts as coin (default: set from the background, 8 to 20)")
    p.add_argument("--radius", type=float, help="coin radius in pixels, if the automatic one is off")
    p.add_argument("--background-photo", help="a photo of just the empty background, if the automatic guess is wrong")
    p.add_argument("--trim", type=float, default=2, help="pixels to pull the circle in")
    p.add_argument("--band", type=float, default=0.06,
                   help="how far (as a fraction of the radius) the edge may stray from a circle; 0 for round modern coins")
    p.add_argument("--circle", action="store_true", help="cut exactly on the whole-coin circle (round modern coins)")
    p.add_argument("--dent", type=float, default=0.02,
                   help="how far (as a fraction of the radius) the cut may go inside the coin's oval")
    p.add_argument("--bulge", type=float, default=0.005,
                   help="how far (as a fraction of the radius) the cut may go outside the coin's oval")
    p.add_argument("--keep-shadow", action="store_true",
                   help="do not peel the shadow and background fringe off the coin's edge")
    p.add_argument("--outline", action="store_true",
                   help="follow the coin's real outline instead of a circle (for uneven ancient coins)")

    args = parser.parse_args()
    cfg = load_config()
    if getattr(args, "image", None) and getattr(args, "simulate", False):
        cfg["simulate_image"] = args.image
    {
        "devices": cmd_devices,
        "jog": cmd_jog,
        "calibrate": cmd_calibrate,
        "scan": cmd_scan,
        "stitch": cmd_stitch,
        "cropfinal": cmd_cropfinal,
        "croptiles": cmd_croptiles,
    }[args.command](cfg, args)


if __name__ == "__main__":
    main()
