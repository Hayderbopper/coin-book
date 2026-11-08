# Coin scanner setup guide

This folder has everything except the 3D models, which come once the stand is measured.

- `arduino/coin_stage/coin_stage.ino` is the motor code for the Arduino Nano.
- `windows/coin_scan.py` is the Windows program that moves the stage, takes the photos, masks the background, runs Hugin and crops the final image.
- `windows/rig_sim.py` simulates the Arduino and the microscope, so everything runs with no hardware (see "Run it with no hardware" below), and `windows/test_rig.py` tests the whole thing against it.
- `simulator.ipynb` runs the simulator online in Google Colab.

## 1. Install the software (once, about 20 minutes)

1. **Python:** download Python 3.12 or newer from python.org. In the installer, tick **"Add python.exe to PATH"** before clicking Install.
2. **Python libraries:** open the Start menu, type `cmd`, open Command Prompt, and run:
   ```
   pip install opencv-python pyserial numpy
   ```
3. **Hugin:** download and install it from hugin.sourceforge.io. Keep the default install folder.
4. **Arduino IDE:** download Arduino IDE 2 from arduino.cc/en/software and install it.
5. **CH340 driver:** only if Windows doesn't recognise the Nano in step 2.2. Search "CH340 driver Windows" and install it from the chip maker, WCH (wch-ic.com).

Copy this whole `coin-scanner` folder somewhere easy, like `Documents\coin-scanner`.

## 2. Load the motor code onto the Nano

1. Plug the Nano into the PC with its USB cable.
2. Open `arduino\coin_stage\coin_stage.ino` in the Arduino IDE.
3. At the top, check `LEAD_MM`. Leave it at `8.0` if you bought "lead 8mm" screws, or change it to `2.0` for "lead 2mm".
4. Choose **Tools > Board > Arduino Nano**, then **Tools > Port** and pick the COM port that appeared when you plugged it in.
5. If the upload fails, set **Tools > Processor > ATmega328P (Old Bootloader)** and try again. Most cheap clones need this.
6. Click **Upload** (the arrow button). Open **Tools > Serial Monitor**, set it to **115200 baud**, type `PING` and press Enter. It should answer `OK coin_stage`.

## 3. Wire the motors

Unplug USB before wiring. Use female-to-female jumper wires.

| ULN2003 board pin | X motor board goes to Nano | Y motor board goes to Nano |
| --- | --- | --- |
| IN1 | D8 | D4 |
| IN2 | D9 | D5 |
| IN3 | D10 | D6 |
| IN4 | D11 | D7 |
| + (5-12V) | 5V | 5V |
| - | GND | GND |

Plug each motor's white connector into its driver board. The boards' little LEDs blink when the motors move.

## 4. Tell the program which ports to use

1. Plug in the Nano and the microscope (in PC mode), then in Command Prompt run:
   ```
   cd Documents\coin-scanner\windows
   python coin_scan.py devices
   ```
2. It lists the serial ports and cameras. Create a file called `config.json` in the `windows` folder with the Nano's port and the microscope's camera number:
   ```
   {
     "port": "COM5",
     "camera_index": 1,
     "width": 1920,
     "height": 1080
   }
   ```
   The width and height are what the program asks the microscope for. It prints what it actually got when it starts.

## 5. Scan a coin

1. **Centre the coin.** Put the coin in the cup and run `python coin_scan.py jog`. A live view opens. Move the stage with the arrow keys or W A S D (+ and - change the step size) until the coin's centre is on the crosshair. Focus the microscope, then press **H** to set that spot as home, and **Q** to quit.
2. **Calibrate (once per zoom setting).** Run `python coin_scan.py calibrate`. The stage moves 1 mm each way and the program measures how far the picture moved. That gives it the scale and the camera's direction, so no ruler is needed. Run it again whenever you change the zoom.
3. **Scan.** Run `python coin_scan.py scan 31.8` with your coin's diameter in mm (a 2 sen is 31.8 mm). Add `--out dragon_2sen` to name the folder. The tiles on the coin's edge are saved with the background made transparent.
4. **Stitch.** Run `python coin_scan.py stitch dragon_2sen`. This runs Hugin automatically and makes `coin.tif` in that folder. It can take a while for big scans. If any step fails, open `coin.pto` from that folder in the Hugin window and use Align, then Stitch.
5. **Final crop.** Run `python coin_scan.py cropfinal dragon_2sen\coin.tif`. It finds the coin's circle, removes everything outside it, and saves `coin_cropped.png`.

## Cropping photos you took by hand

`croptiles` masks the background using only the pictures, with no stage positions needed. It works on any folder of overlapping coin photos with a plain background:

```
python coin_scan.py croptiles my_photos
```

It writes the results to `my_photos_cropped`. Photos that are all coin are copied unchanged. On the others it fits a circle to the coin's edge, using the same radius for every photo because they all show the same coin. Add `--outline` to follow the coin's real edge instead, which suits uneven ancient coins. It works on plain, coloured, speckled and unevenly lit backgrounds, and on a coin sitting on paper on top of a mat. Lower `--tolerance` (default 20 for hand-taken photos) if bits of background are left, or raise it if parts of the coin go missing. If it picks the wrong colour as the background, take one photo of just the empty background and pass it with `--background-photo empty.png`.

When the folder came from `scan` (it has `positions.csv` and `scan_info.json`), it uses the stage positions and the coin size you gave `scan` to find the background: the corners of the photos outside the coin are always background, whatever colour the coin is. It also sets the tolerance itself, from 8 on a clean, even background up to 20 on a speckled or patchy one, so a coin that is nearly the background's colour is still told apart. It also fits one circle to the coin's edge across all the photos and uses it as a guide, which makes the edges much more even. `--band` (default 0.06) sets how far, as a share of the radius, a photo may follow its own edge away from that circle. Add `--circle` to cut exactly on the circle instead. It then lays all the photos out together, finds the coin's edge in every direction around it, and gives every photo that same smooth outline, so the cut matches from photo to photo with no rough or patchy edges. Slightly oval coins are handled automatically: the cut is kept within `--dent` (default 0.02) inside and `--bulge` (default 0.005) outside a clean oval fitted to the coin, as fractions of its radius. Raise them for very irregular ancient coins. When most of the edge is already round, the cut is made an exact circle or oval: wobble under 0.8% of the radius is removed, while real shapes like a scalloped 20 cent edge are kept. The coin size given to `scan` is also used to ignore stray edges, such as the grid lines on a cutting mat. A dark shadow the coin casts on the background, and any thin fringe of background colour, are cut off; add `--keep-shadow` to keep them. A shadow keeps the background's own tint, so a dark copper or tarnished rim is not mistaken for one.

`tile_crop_test.jpg` and `tile_test_stitched.jpg` in this folder show a test on a white background, `blue_tile_crop_test.jpg` and `blue_tile_test_stitched.jpg` the same test on a speckled blue background, and `purple_tile_crop_test.jpg` and `purple_tile_test_stitched.jpg` on an unevenly lit purple background. In the first, a silver coin photo on a white background was cut into 77 overlapping tiles. 19 of the 21 tiles that were fully on the coin were kept unchanged (2 had a shiny corner mistaken for background, which the overlapping neighbours cover), the edge tiles were masked, and Hugin stitched them back together.

`ten_photo_crop_test.jpg` is a bigger test on 10 museum and book photos of coins on black cloth, yellow card, white paper and lined paper. Each was cut into overlapping tiles and cropped. Every tile that was fully on the coin was kept whole in all 10, and the edge was typically within 0.4% to 3% of the true outline. Dark shadows the coin casts on light paper (photos 4 to 6) are removed, and dark print near the rim is no longer mistaken for shadow. Photo 9's faint top edge is kept in full. Photo 10's flat right side is real: the coin runs off the edge of the printed picture there.

`ten_colours_crop_test.jpg` is a second test, with 10 real photos of modern coins from a collector's catalogue: pink, dark and corroded copper, bright and worn gold, bimetal and worn nickel. They were placed on wood, black velvet, red felt, yellow card, a green cutting mat, grey card, navy fabric, white paper, dark grey card and kraft paper, some with a soft shadow, and cut into tiles the same way. All 10 came out with every full tile kept whole and the edge within 0.1% to 1.1% of the true outline. The scalloped 20 cent coin (photo 5) is recognised as a shaped coin from its evenly spaced notches and traced closely instead of being made round; its edge is within about 1.2 px of the true outline on average (`scalloped_edge_closeup.jpg`), including the notch under the added shadow, since a soft shadow (the background's own colour, only darker) counts as background. The background's tint is also taken back out of the coin's outermost pixels, so a coloured background leaves no thin coloured line round the coin. `edge_closeup.jpg` shows the edges of four of them at full size.

## Run it with no hardware (the simulated rig)

`windows/rig_sim.py` is a pretend rig. Put it next to `coin_scan.py` and run commands through it instead:

```
python rig_sim.py calibrate
python rig_sim.py scan 16 --out test
python rig_sim.py stitch test
python rig_sim.py cropfinal test\coin.tif
```

This runs the real `coin_scan.py`, unchanged. Only the two pieces of hardware are swapped out:

- **The Arduino.** `serial.Serial` is replaced by a fake USB port that runs a Python copy of `coin_stage.ino`. It answers the same `PING`, `MOVE`, `JOG`, `HOME`, `POS` and `OFF` commands with the same replies, counts in the same half-steps, keeps the same 24 mm limits and does the same backlash overshoot. It reads `LEAD_MM`, `LIMIT_MM` and the other settings straight from the `.ino`, so changing the firmware changes the simulation.
- **The microscope.** `cv2.VideoCapture` is replaced by a fake camera looking down at a coin on a cutting mat. It shows what the real one would see at the stage's actual position, including the things that make the real rig hard: play in the motor gears, a camera turned 3° on its mount, a coin that isn't quite flat (so the focus drifts), uneven ring light, sensor noise, old frames left in the camera's buffer, and a smeared picture if a photo is taken before the stage stops wobbling.

You can also `import rig_sim` at the top of your own script. From then on, anything that opens a serial port or a camera gets the simulated ones.

**Choosing the coin.** With no settings it draws a test coin. To use a real one, pass a transparent cut-out PNG, like the ones the coin book makes, and its real diameter:

```
python rig_sim.py scan 16 --out bulgaria --sim-coin ..\..\images\bulgaria\1-stotinka\madara-rider\1999\obverse.png --sim-coin-mm 16
```

Any setting in the `SETTINGS` list at the top of `rig_sim.py` can be given as `--sim-<name>`, or saved in a `rig_sim.json` file next to it so every command uses it. For example `{"coin_offset_mm": [0.8, -0.5]}` puts the coin down off-centre, and `{"camera_mirrored": true}` flips the picture.

**Automated tests.** `python test_rig.py` runs 14 checks against the simulated rig. The simulator knows exactly where the coin is, so the tests check that the answers are right, not just that nothing crashed:

- The fake Nano answers every command exactly as `coin_stage.ino` does, including the limits and the backlash overshoot.
- `calibrate` finds the true scale within 2% and the camera's angle within 2°, including with a mirrored camera.
- `scan` removes at least 99% of the background on the edge photos and keeps at least 98% of the coin.
- `croptiles` still finds the coin from the pictures alone when the coin was put down off-centre.
- `stitch` and `cropfinal` give the whole coin at the right size. This one is skipped if Hugin isn't installed.

They take about 4 minutes. The simulator already found one real bug: `stitch` told Hugin to crop to the largest rectangle covered by photos, which cut the top and bottom off the coin. It now keeps everything (`--crop=AUTOOUTSIDE`).

**Online.** `simulator.ipynb` runs all of this in Google Colab with nothing to install: open it, choose Runtime > Run all, and it installs Hugin, runs the tests, scans the Bulgarian 1 stotinka and shows the photos and the finished coin.

The older `--simulate --image some_coin_photo.png` option inside `coin_scan.py` still works too. It is quicker, but it skips the Arduino code and makes a perfect, noise-free camera.

### The quick built-in simulation

Every step except `devices` can also run with `coin_scan.py` on its own. Add `--simulate --image some_coin_photo.png` and the program pretends a big coin photo is the microscope view:

```
python coin_scan.py calibrate --simulate --image my_coin.png
python coin_scan.py scan 31.8 --simulate --image my_coin.png --out test
python coin_scan.py stitch test
python coin_scan.py cropfinal test\coin.tif
```

Use a photo at least 2000 pixels across. `simulation_test_result.png` in this folder is what this produced from a 600-pixel 2 sen photo enlarged 4 times: 77 tiles, stitched by Hugin and cropped. In simulation, 75 pixels equal 1 mm (change `simulate_px_per_mm` in config.json). Run `calibrate` again without `--simulate` once the real hardware is connected.

## If something goes wrong

| Problem | Fix |
| --- | --- |
| A motor hums but doesn't turn, or skips | Raise `STEP_US` in the .ino to 1500 or 2000 and upload again |
| An axis moves the wrong way | It doesn't matter for scanning, because `calibrate` measures the direction. For the jog keys, flip `INVERT_X` or `INVERT_Y` |
| "outside limits" | The stage can only go 24 mm from home. Re-centre the coin and press H again |
| Camera shows black or the wrong camera | Change `camera_index` in config.json; check the microscope is in PC or webcam mode |
| Calibrate says match quality is low | Focus on the coin, and make sure there's detail in view (not a blank area) |
| Seams or a lit edge on every tile | Tape a paper diffuser around the microscope's light |
