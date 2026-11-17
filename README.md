# Book Inspector

Two Raspberry Pi stations that share one codebase.

| Station | Config | What it checks |
|---|---|---|
| **Conveyor** | `config.yaml` | Every book on the conveyor is photographed when the photo sensor sees it. A **different title** in the running lot raises an alert. **Without cover** mode photographs the first page after the cover; **with cover** mode photographs the front cover. |
| **Packing** | `config.packing.yaml` | The **box label** must be in the job's label list from the ERP. The **books put in the box** must be that label's titles and quantities. |

## Conveyor station

### Wiring the sensor (Autonics BJ, PNP, 24 V)

Never connect the sensor's black wire to the Pi. The PC817 optocoupler keeps the 24 V side and the Pi side apart.

| From | To |
|---|---|
| Sensor brown | +24 V |
| Sensor blue | 0 V of the 24 V supply |
| Sensor black (output) | Optocoupler IN1 |
| Optocoupler input common (−) | 0 V of the 24 V supply |
| Optocoupler VCC | Pi 3.3 V (pin 1) |
| Optocoupler GND | Pi GND (pin 6) |
| Optocoupler OUT1 | Pi GPIO17 (pin 11) |

Before connecting the optocoupler:
1. Power the sensor and measure black→blue with the multimeter. It should switch between about 24 V and 0 V as a book passes.
2. Measure the resistor on the optocoupler input. About 1 kΩ is fine at 24 V. If it is 470 Ω or less, add a 2.2 kΩ resistor in series with the black wire.
3. Do not join the 24 V 0 V to the Pi GND.

**The 15 mm gap:** mount the sensor close to the books (30–50 mm), with nothing shiny behind the gap, so it switches off cleanly in every gap. Set sensitivity with `scripts/bench_sensor.py` running. If the sensor ever sees two books as one, the app raises a "No gap between books" alert.

### Speed (5000–6000 books per hour)

| | Value |
|---|---|
| Time per book | 600 ms |
| Gap passes the sensor in | about 30–40 ms |
| Sensor response | about 1 ms |
| Camera | 120 fps, so the photo is at most 8 ms late |
| Exposure | 400 µs, so the book moves under 0.2 mm during the photo |
| Comparison | about 50 ms per book on a PC; measure it on the Pi (the screen shows ms per book) |

### Daily use

1. **Start lot:** choose Without cover / With cover, type part of the job number on the keypad, tap **Find**, pick the job, and tap **Start**.
2. **New job:** the first 5 books teach the title, so they must be good books of that job. Their photos are saved and reused the next time the same job runs in the same mode.
3. **Different book:** the screen turns red and shows the odd book next to the reference. Remove the book, then tap **OK, checked**.
4. **Re-teach:** use it when the reference was taught badly or the job's print changed.
5. **History:** shows every lot with its counts and alert photos.

## Packing station

### How it works

1. **Start packing:** pick the job on the keypad, as at the conveyor. The station loads **every packing-slip label of that job** from the ERP (`PackingSlipBarcodeEntry`), with every title in each box and its quantity per box.
   - Example: job J03704 has 426 labels, and each box holds 6 "Meus amigos" titles × 12 books.
2. **Show a box label to the camera.** The camera reads the barcode (no scanner).
   - If the label is not in the list: "Wrong box label".
   - If the box was already packed: "Box already done", with an **Open box again** button.
   - A label printed after the list was loaded is looked up in the ERP once.
3. **Pass each book under the camera** at the marked spot; the gate sensor takes the photo. The book is matched only against **this box's titles** and counted on screen.
4. **Alerts:**
   - **Wrong book?** The book is not one of this box's titles.
   - **Not sure** which of two titles: show the book again.
   - **Too many of one title.**
   - **No box open:** a book was shown before any label.
5. **Box complete** when every title reaches its quantity. Showing the next label while a box is short logs it as **short** and shows what is missing. **Boxes** lists complete and short boxes.

### Teaching titles at packing

Each title needs 3 reference photos from the packing camera. If a title has none, its row shows **Teach**: tap it, then pass 3 books of that title. They are counted into the box too.

Alternatively, set `packing.conveyor_url` to reuse the conveyor's **With cover** photos. Teaching at packing is more reliable, because the camera and light are different there.

### Hardware for packing (still to buy)

- A second Pi (Pi 4; a Pi 5 is about 2–3× faster for the title check)
- Raspberry Pi **Camera Module 3** (autofocus, 12 MP). A USB webcam also works: `camera.type: usb`.
- A second Autonics sensor plus optocoupler channel, mounted as a gate the books pass through
- An LED light and a screen

### Label reading

The barcode reader needs about **2 pixels per thinnest bar**. With a ~50 mm wide barcode at 2304 px across the view, show the label within roughly 30 cm of the camera.

### Speed

Checking one book against 6 titles took about 320 ms on a PC. Expect around 1 s on a Pi 4, which suits hand packing. The screen shows ms per book. If it is too slow, use a Pi 5.

## Setup on the Pi

1. Flash Raspberry Pi OS Bookworm 64-bit. Connect the camera ribbon and the touchscreen.
2. Get the code from GitHub (see "Updating the Pi" below for the read-only key), then run:
   - conveyor Pi: `bash scripts/install.sh`
   - packing Pi: `bash scripts/install.sh packing`
3. Edit `.env` and enter the **read-only** ERP login.
4. Reboot. The screen opens the station by itself. From the office, open `http://<pi-address>:8080`.

### Updating the Pi

The code lives in a private GitHub repository. Change it on the PC and push; the Pi pulls it.

**On the PC, after each change:**
```bash
git add -A
git commit -m "what changed"
git push
```

**On the Pi:** `bash scripts/update.sh`. It pulls the new code and restarts the station. It also tells you when to run `install.sh` again or to reboot for new screen pages.

**What an update never touches:** `.env` (ERP login), `data/` (photos, history, references, touchscreen settings) and `config.local.yaml`. Put settings that belong to this Pi only in `config.local.yaml`, for example:
```yaml
camera:
  rotate: 180
  crop: [100, 0, 1080, 800]
```
Don't edit `config.yaml` on the Pi: `update.sh` stops if it finds edited files.

**One-time setup on the Pi (read-only access to the repository):**
1. On the Pi: `ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519`, then `cat ~/.ssh/id_ed25519.pub`.
2. On GitHub: repository → Settings → Deploy keys → Add deploy key. Paste the key and leave "Allow write access" off.
3. On the Pi: `git clone git@github.com:eacdc/AI-Packing-PI.git ~/book-inspector`.

For a Pi that already has a copied folder: move `.env` and `data/` into the cloned folder, and run `install.sh` from the clone so the service points there.

### First-time tuning (conveyor)

1. **Camera check:** run `python3 scripts/bench_camera.py --exposure 400 --gain 4`. It should report about 120 fps and a brightness of 80–200. If it's too dark, add light rather than raising the exposure.
2. **Sensor check:** stop the service (`sudo systemctl stop book-inspector`), then run `python3 scripts/bench_sensor.py` with the line running. Every book should print once, with gaps above 20 ms.
3. **On the screen:** Settings → Take test photo to aim and focus. Then run books and adjust **Photo delay after sensor** until the page is centred in "Last book".
4. If the photo includes belt edges or the machine frame, set `camera.crop` in `config.yaml`.

## Trials: record, then count false alarms and missed books

For test runs (at home or on the line), record every photo. Then replay the recording with any settings and count the mistakes, without running books again.

**1. Record.** Start the station with `--record`, or set `trial: {record: true}` in the config. The screen shows **● REC**. Each lot (or packing session) is saved to `data/trials/<date>_<job>_<mode>/`:

| File | Contents |
|---|---|
| `photos/000123.png` | Every book photo, lossless, named by the sensor count |
| `log.jsonl` | What the station decided for each photo; gap alarms, dropped photos, noise blips, labels, boxes |
| `refs/` | The reference photos each check used |
| `trial.json` | Job and the settings in use |

Size: about 0.7 MB per photo, so about 4 GB per hour at 6000 books/hr. Use a fast SD card or a USB drive. If the card can't keep up, photos are skipped (the report shows "Not saved"), and the station itself never slows down.

**2. Write the truth list.** Make a run sheet before the test (for example "odd books at 37, 81, 150"). Then put a `truth.txt` in the trial folder. The photo numbers are the file names in `photos/`.

```
odd: 37, 81, 150-152        # conveyor: books that are NOT the lot's title (all others are)
skip: 55                    # leave out: a hand in view, two books together...
J03698_26_27: 12, 40-51     # packing: the real title of these photos
odd: 300                    # packing: a book that is no title of its box
```

If there is no `truth.txt` yet, the replay writes `truth.draft.txt` from its own decisions. Check it against the report's contact sheet, correct it and save it as `truth.txt`. Mock trials write their own `truth.txt`.

**3. Replay** (faster on a PC: copy the trial folder over):

```bash
python scripts/replay.py data/trials/<folder>                     # the recorded settings: should match the live run
python scripts/replay.py "data/trials/*" --config config.yaml       # all trials, with this station's current settings
python scripts/replay.py <folder> --set matching.inlier_ratio=0.3
python scripts/replay.py <folder> --sweep matching.diff_min_patch=50,100,150,200,300
```

It prints:
- **Conveyor:** missed odd books, false alarms, odd books caught and good books passed.
- **Packing:** books counted as the wrong title, foreign books counted, good books rejected, foreign books caught and right titles.
- **Safety margins:**
  - how close the worst good book came to the limit, and how close the nearest odd book came;
  - the range of the changed-patch limit that makes no mistake on this recording;
  - which setting controls the limit right now (`diff_min_patch` or `diff_factor`);
  - for packing, the tightest win over the runner-up title.

`report/report.html` in the trial folder has:
- the counts, speed figures (books/hr, dropped photos, gap alarms, check time) and margins;
- every mistake, with its photo;
- a contact sheet of all photos. On the conveyor station the most different books come first, so the odd ones should all be at the top. On the packing station photos are grouped by the title chosen.

**How long a sweep takes:** sweeping a limit (`diff_factor`, `diff_min_patch`, `patch_factor`, `min_patch_gap`) takes seconds, because the scores are reused. Sweeping any other setting checks every photo again, using all CPU cores.

## Run on a PC without the Pi (mock mode)

```bash
pip install -r requirements.txt
python -m app --mock --config config.mock.yaml          # conveyor, http://localhost:8080
python -m app --mock --config config.mock-packing.yaml --port 8081   # packing
```

- **Mock conveyor:** runs at 6000 books/hr and sends a different book every 25th book.
- **Mock packer:** packs 8 boxes of 6 titles × 3. Box 2 is one book short, and box 3 gets a foreign book.

Add `--record` to either command to try the trial tools. Mock trials know which books were odd, so they write their own `truth.txt`.

Tests:
```bash
python tests/test_matcher.py
python tests/test_packing.py
python tests/test_trial.py
```

## Files

| File | What it does |
|---|---|
| `app/matcher.py` | Title check: ORB print points + changed-patch check, calibrated from the reference photos |
| `app/station.py` | Shared by both stations: sensor → photo on separate threads, so the camera never waits |
| `app/inspector.py`, `app/server.py`, `static/index.html`, `static/app.js` | Conveyor station |
| `app/packing.py`, `app/packing_server.py`, `static/packing.html`, `static/packing.js` | Packing station |
| `app/barcode.py` | Reads the Code 39 box label (zxing-cpp) |
| `app/erp.py` | Read-only IndusEnterprise (Kolkata) lookups: job search, a job's label list, one label's titles |
| `app/camera.py`, `app/trigger.py`, `app/alarm.py` | Pi hardware (ribbon or USB camera, sensor input, optional buzzer) |
| `app/store.py` | SQLite log, packed boxes, alert photos, reference photos (`data/`) |
| `app/trial.py`, `app/replay.py`, `scripts/replay.py` | Trial recording, and replay with scoring against a truth list |
| `scripts/install.sh`, `scripts/update.sh` | One-time Pi setup; pull the latest code and restart |

## Not done yet

- The beep or tower light: set `alarm.buzzer_pin` once a relay is wired.
- Matching covered books against uncovered books.
- Nothing has run on the real Pi, cameras, sensor or books yet.
