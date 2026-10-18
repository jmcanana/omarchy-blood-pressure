# Pressure

Local blood-pressure sync for the Omarchy bar. Monitors that advertise the Blood Pressure service can be trusted and drained. Readings stay on this computer, split by patient, and drawn with the active theme.

This repository is not installed. Copying it into `~/.config/omarchy/plugins/` or running `omarchy plugin enable` loads it into the live shell.

## Disclaimer

- **Not a medical device.** This is a personal hobby project for keeping home readings. It is not a medical device, has not been reviewed or approved by any regulator, and does not diagnose, treat or monitor any condition.
- **Not medical advice.** The bands, sentences and guidance it shows restate published tables (AHA, ESC 2024, AAP 2017) for information only. Do not make medical decisions from it; talk to a qualified clinician. In an emergency, call your local emergency number.
- **No warranty, no liability.** The software is provided "as is", without warranty of any kind, and the authors are not liable for any claim, damages or other liability arising from its use (see `LICENSE`). Readings can be missed, delayed, mis-parsed or mis-classified; you use it at your own risk.
- **Tested narrowly.** It was built against one A&D UA-810 on one Omarchy machine. Other monitors, firmware or Bluetooth stacks may behave differently.
- **Your data is yours to protect.** Readings and patient names are stored unencrypted in your home directory (files mode 0600). Securing that computer, and anything you share with "Copy image", is up to you.
- **No affiliation.** Not affiliated with or endorsed by A&D Company / A&D Medical, the American Heart Association, the European Society of Cardiology, the American Academy of Pediatrics, or the Omarchy project. Product names are trademarks of their owners.

## Evaluate without installing

On an Omarchy machine, from a checkout of this repo (not from `~/.config/omarchy/plugins`):

```sh
python3 -m unittest discover -s tests -v
python3 pressure/sidecar.py --self-check --data /tmp/pressure-self/data --state /tmp/pressure-self/state
python3 pressure/sidecar.py --demo --data /tmp/pressure-demo/data --state /tmp/pressure-demo/state
omarchy plugin validate .
```

`SELF_CHECK_OK` means a fake link stored two synthetic readings, wrote no clock, and the status file has no blood-pressure numbers. `DEMO_OK` writes a synthetic household (Alex, Sam under 13, Jordan) into that `/tmp` database only. The unit tests do not use a radio and do not read captured logs.

QML, still outside the live plugin directory:

```sh
mkdir -p /tmp/qs-shadow/qs
ln -sfn /usr/share/omarchy/shell/Ui /tmp/qs-shadow/qs/Ui
ln -sfn /usr/share/omarchy/shell/Commons /tmp/qs-shadow/qs/Commons
/usr/lib/qt6/bin/qmllint -I /tmp/qs-shadow Panel.qml MapChart.qml LaneStrip.qml HourDial.qml SyncPill.qml Segmented.qml ActionButton.qml
```

Lint copies of `BarWidget.qml` and `Service.qml` under other filenames. A file that is itself named `BarWidget.qml` makes qmllint report an inheritance cycle against the shell type of the same name.

## Radio, when you want a live trial

The sidecar needs bleak in a private venv. This does not need root.

```sh
python3 -m venv ~/.local/share/jmcanana.pressure/venv
~/.local/share/jmcanana.pressure/venv/bin/pip install bleak
~/.local/share/jmcanana.pressure/venv/bin/python pressure/sidecar.py
```

Stop any other collector first. The cuff allows one connection. Leave the phone's Bluetooth off. The first time this computer pairs, hold START until `Pr` and accept the desktop pairing prompt.

The process prints `ready` and then nothing. Status is `~/.local/state/jmcanana.pressure/status.json` (state, beacon class, counts, no readings). Trust a monitor from the panel, or insert a trusted row only after you have decided the address is yours. Until a monitor is trusted, a transfer beacon is recorded as `seen` and is not connected.

Take a measurement. When the cuff's Bluetooth light flashes, the sidecar connects and takes the stored readings, oldest first. While the measurement runs, the beacon class in the status file is `start` and the scan keeps running.

## Screenshots

Every number and name below is synthetic, seeded by `pressure/demo.py`; none is a real measurement.

| Adult, AHA table | Same reading, ESC 2024 table |
|---|---|
| ![Alex, AHA](docs/screenshots/alex-aha.png) | ![Alex, ESC](docs/screenshots/alex-esc.png) |
| **Under 13: numbers without bands** | **Above the crisis line: severe guidance** |
| ![Sam, under 13](docs/screenshots/sam-under13.png) | ![Jordan, severe](docs/screenshots/jordan-severe.png) |

## Install, only when you want the live shell to load it

`omarchy plugin add` clones a git URL. This checkout has no remote yet. A local load is a copy, not a symlink (the validator rejects symlinks), then a rescan, then enable. Enable writes `shell.json` and the shell starts the service.

```sh
rsync -a --delete --exclude .git ./ ~/.config/omarchy/plugins/jmcanana.pressure/
omarchy-shell shell rescanPlugins
omarchy plugin enable jmcanana.pressure --section right
```

Click `BP` in the bar. Escape closes the panel. Tap a patient to switch to them; tap the active patient, or `+`, to edit or add one. "Copy image" at the bottom puts a PNG of the panel on the clipboard.

Remove it with:

```sh
omarchy plugin disable jmcanana.pressure
omarchy plugin remove jmcanana.pressure
```

The database in `~/.local/share/jmcanana.pressure/` is not deleted by remove.

A change under the plugin directory hot-reloads widget code. `keepLoaded` is set, so a change to the sidecar is picked up after `omarchy restart shell`.

## What you should see

- Bar text is `BP`, `Sync`, or `New`. No reading, no name.
- Urgent colour on the bar for AHA stage 2 or severe, ESC hypertension, a reading above the crisis line, an irregular-pulse flag, or a sidecar error. AHA stage 1 stays a word in the panel. Changing the person on screen does not change that colour.
- The latest reading on the AHA (or ESC) map, systolic/diastolic/pulse trends against their bands, and the 24-hour range dial.
- A trust card that shows the monitor name and four hex digits, not the full address.
- Bind cuff user 1 or 2 to the person on screen. Each sync reads the cuff clock and only compares it; arming a clock write from the panel rarely fires, because the cuff hangs up first.
- Two patients in the demo database.

## Sources

- American Heart Association, Understanding Blood Pressure Readings, page updated August 2025. https://www.heart.org/en/health-topics/high-blood-pressure/understanding-blood-pressure-readings
- American Heart Association resting pulse range 60–100, used here only as a label beside a cuff pulse.
- ESC 2024 Guidelines for the management of elevated blood pressure and hypertension: non-elevated, elevated, hypertension, as listed in DESIGN.md.
- American Academy of Pediatrics 2017 clinical practice guideline: adult cut-offs from age 13.

This is not a medical device and it does not diagnose.
