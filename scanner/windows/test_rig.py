"""Automated checks for the coin scanner, run against the simulated rig.

    python test_rig.py            (or: python -m pytest test_rig.py)

The simulator knows exactly where the coin is, so these tests can check that
coin_scan.py's answers are right, not just that it ran without crashing.
The stitching test is skipped if Hugin isn't installed.
"""

import csv
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rig_sim  # noqa: E402  (must come before the hardware is opened)
import cv2  # noqa: E402
import numpy as np  # noqa: E402
import coin_scan  # noqa: E402

# The tests use the standard simulated rig, whatever rig_sim.json says.
rig_sim.SETTINGS.update(json.loads(json.dumps(rig_sim.DEFAULT_SETTINGS)))

COIN_MM = 16.0


def run(*argv, config=None):
    """Run coin_scan.py with these arguments, the way it runs from the command line."""
    sys.argv = ["coin_scan.py"] + [str(a) for a in argv]
    out = io.StringIO()
    with redirect_stdout(out):
        coin_scan.main()
    return out.getvalue()


class TempFolder(unittest.TestCase):
    """Every test runs in its own empty folder with its own config.json."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rig_test_")
        self.old_cwd = os.getcwd()
        os.chdir(self.dir)
        self.old_config = coin_scan.CONFIG_PATH
        coin_scan.CONFIG_PATH = os.path.join(self.dir, "config.json")
        with open(coin_scan.CONFIG_PATH, "w") as f:
            json.dump({"settle_s": 0.3}, f)
        self.old_settings = json.loads(json.dumps(rig_sim.SETTINGS))
        rig_sim.configure(coin_mm=COIN_MM, quiet=True)

    def tearDown(self):
        os.chdir(self.old_cwd)
        coin_scan.CONFIG_PATH = self.old_config
        rig_sim.SETTINGS.update(self.old_settings)
        rig_sim.configure()
        shutil.rmtree(self.dir, ignore_errors=True)

    def config(self):
        with open(coin_scan.CONFIG_PATH) as f:
            return json.load(f)


# ---------------------------------------------------------------- the Arduino

class FirmwareTests(unittest.TestCase):
    """The fake Nano must answer exactly like coin_stage.ino does."""

    def setUp(self):
        import serial
        rig_sim.configure()
        self.port = serial.Serial("COM3", 115200, timeout=1)
        self.assertEqual(self.port.readline(), b"OK coin_stage ready\r\n")

    def ask(self, line):
        self.port.write((line + "\n").encode())
        return self.port.readline().decode().strip()

    def test_commands(self):
        self.assertEqual(self.ask("PING"), "OK coin_stage")
        self.assertEqual(self.ask("move 1.5 -2"), "OK 1.500 -2.000")
        self.assertEqual(self.ask("JOG 0.5 0.5"), "OK 2.000 -1.500")
        self.assertEqual(self.ask("POS"), "OK 2.000 -1.500")
        self.assertEqual(self.ask("HOME"), "OK 0.000 0.000")
        self.assertEqual(self.ask("OFF"), "OK off")
        self.assertEqual(self.ask("FLY 1 2"), "ERR unknown command")

    def test_soft_limits(self):
        self.assertEqual(self.ask("MOVE 24 -24"), "OK 24.000 -24.000")
        self.assertEqual(self.ask("MOVE 24.1 0"), "ERR outside limits")
        self.assertEqual(self.ask("POS"), "OK 24.000 -24.000")  # didn't move

    def test_positions_round_to_whole_steps(self):
        steps_per_mm = 4096 / rig_sim.read_firmware_settings()["LEAD_MM"]
        reply = self.ask("MOVE 0.0001 0")
        self.assertEqual(reply, f"OK {round(0.0001 * steps_per_mm) / steps_per_mm:.3f} 0.000")

    def test_backlash_overshoot_lands_in_the_same_place(self):
        """Coming from either side, the carriage must end up at the same spot. This is what
        BACKLASH_MM in the .ino is for; without it the gear play would show up as an error."""
        rig_sim.configure(step_error_mm=0.0)
        self.ask("MOVE 3 3")
        from_below = self.port.firmware.carriage_mm().copy()
        self.ask("MOVE 6 6")
        self.ask("MOVE 3 3")
        from_above = self.port.firmware.carriage_mm()
        np.testing.assert_allclose(from_below, from_above, atol=1e-3)

    def test_long_lines_are_cut_like_the_arduino(self):
        # The Arduino keeps only the first 40 characters, so the 2 at the end is lost.
        line = "MOVE 1 ".ljust(40) + "2"
        self.assertEqual(self.ask(line), "OK 1.000 0.000")

    def test_half_steps_round_like_lround(self):
        steps_per_mm = 4096 / rig_sim.read_firmware_settings()["LEAD_MM"]
        # 2.5 steps: C's lround gives 3, Python's round would give 2.
        self.assertEqual(self.ask(f"MOVE {2.5 / steps_per_mm:.10f} 0"), f"OK {3 / steps_per_mm:.3f} 0.000")

    def test_wrong_baud_rate_gives_gibberish(self):
        import serial
        bad = serial.Serial("COM3", 9600, timeout=1)
        self.assertFalse(bad.readline().startswith(b"OK"))

    def test_missing_port(self):
        import serial
        with self.assertRaises(serial.SerialException):
            serial.Serial("COM99", 115200, timeout=1)

    def test_firmware_settings_come_from_the_ino(self):
        ino = os.path.join(HERE, "..", "arduino", "coin_stage", "coin_stage.ino")
        with open(ino) as f:
            text = f.read()
        # Change every setting away from its default and check the simulator picks the new values up.
        changed = (text.replace("LEAD_MM = 8.0;", "LEAD_MM = 2.0;")
                       .replace("LIMIT_MM = 24.0;", "LIMIT_MM = 15.5;")
                       .replace("BACKLASH_MM = 0.3;", "BACKLASH_MM = 0.45;")
                       .replace("INVERT_Y = false;", "INVERT_Y = true;"))
        self.assertNotEqual(text, changed, "coin_stage.ino's settings lines have changed; update this test")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "coin_stage.ino")
            with open(path, "w") as f:
                f.write(changed)
            fw = rig_sim.read_firmware_settings(path)
        self.assertEqual((fw["LEAD_MM"], fw["LIMIT_MM"], fw["BACKLASH_MM"], fw["INVERT_Y"]), (2.0, 15.5, 0.45, True))


# ---------------------------------------------------------------- calibrate, scan, crop

class PipelineTests(TempFolder):

    def test_calibrate_finds_scale_and_camera_angle(self):
        run("calibrate")
        cfg = self.config()
        true_k = rig_sim.SETTINGS["px_per_mm"]
        self.assertAlmostEqual(cfg["px_per_mm"], true_k, delta=0.02 * true_k)
        a = np.radians(rig_sim.SETTINGS["camera_angle_deg"])
        angle_err = np.degrees(np.arccos(np.clip(np.dot(cfg["x_vec"], [np.cos(a), np.sin(a)]), -1, 1)))
        self.assertLess(angle_err, 2.0, "camera direction is off by more than 2 degrees")

    def test_calibrate_handles_a_mirrored_camera(self):
        rig_sim.configure(camera_mirrored=True)
        run("calibrate")
        cfg = self.config()
        self.assertLess(cfg["x_vec"][0], -0.9, "a mirrored camera should make +X move the picture left")

    def test_scan_masks_the_background_without_cutting_the_coin(self):
        run("calibrate")
        run("scan", COIN_MM, "--out", "scan")
        stats = self.compare_masks("scan")
        self.assertGreater(stats["tiles"], 10)
        self.assertGreater(stats["coin_kept"], 0.98, "the mask cut away part of the coin")
        self.assertGreater(stats["background_removed"], 0.99, "background was left on the edge tiles")

    def test_off_centre_coin_is_still_masked_by_croptiles(self):
        """croptiles finds the background from the photos alone, so it must cope even when the coin
        wasn't centred before HOME (which throws off the stage-based mask)."""
        rig_sim.configure(coin_offset_mm=[0.8, -0.5])
        run("calibrate")
        run("scan", COIN_MM, "--out", "raw", "--no-mask")
        run("croptiles", "raw", "--out", "raw_cropped")
        stats = self.compare_masks("raw", masks_from="raw_cropped")
        self.assertGreater(stats["coin_kept"], 0.95)
        self.assertGreater(stats["background_removed"], 0.95)

    @unittest.skipUnless(shutil.which("pto_gen") or os.path.exists(r"C:\Program Files\Hugin\bin\pto_gen.exe"),
                         "Hugin is not installed")
    def test_stitch_and_final_crop_give_the_whole_coin(self):
        run("calibrate")
        run("scan", COIN_MM, "--out", "scan")
        run("stitch", "scan")
        self.assertTrue(os.path.exists("scan/coin.tif"), "Hugin made no picture")
        run("cropfinal", "scan/coin.tif")
        img = cv2.imread("scan/coin_cropped.png", cv2.IMREAD_UNCHANGED)
        expected = (COIN_MM - 2 * 0.2) * rig_sim.SETTINGS["px_per_mm"]  # 0.2 mm edge trim each side
        for size in img.shape[:2]:
            self.assertAlmostEqual(size, expected, delta=0.03 * expected,
                                   msg="the stitched coin is the wrong size or was cut off")
        alpha = img[:, :, 3] > 0
        self.assertGreater(alpha.mean(), 0.7, "the stitched coin has holes in it")

    # -- helper
    def compare_masks(self, folder, masks_from=None):
        """Compare each saved tile's transparency with where the coin really was."""
        world = rig_sim.world()
        k = rig_sim.SETTINGS["px_per_mm"]
        kept = total_coin = removed = total_bg = tiles = 0
        with open(os.path.join(folder, "positions.csv")) as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            path = os.path.join(masks_from or folder, row["file"])
            if not os.path.exists(path):  # croptiles leaves out photos with no coin in them
                continue
            img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
            alpha = img[:, :, 3] if img.ndim == 3 and img.shape[2] == 4 else np.full(img.shape[:2], 255, np.uint8)
            truth = world.coin_mask((float(row["x_mm"]), float(row["y_mm"])))
            # Ignore a thin band at the rim: the scan trims 0.2 mm off on purpose, and the stage
            # lands within a few hundredths of a mm of where it was sent.
            band = int(0.35 * k)
            kernel = np.ones((2 * band + 1, 2 * band + 1), np.uint8)
            sure_coin = cv2.erode((truth > 127).astype(np.uint8), kernel) > 0
            sure_bg = cv2.dilate((truth > 127).astype(np.uint8), kernel) == 0
            kept += np.count_nonzero(alpha[sure_coin] > 127)
            total_coin += np.count_nonzero(sure_coin)
            removed += np.count_nonzero(alpha[sure_bg] <= 127)
            total_bg += np.count_nonzero(sure_bg)
            tiles += 1
        return {"tiles": tiles, "coin_kept": kept / max(total_coin, 1), "background_removed": removed / max(total_bg, 1)}


if __name__ == "__main__":
    unittest.main(verbosity=2)
