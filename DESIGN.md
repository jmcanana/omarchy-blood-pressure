# Pressure

A local Omarchy bar plugin for monitors that speak the Bluetooth Blood Pressure Profile. It syncs stored readings, keeps them by patient, and draws trends with the active Omarchy theme. It does not upload anything.

## What the cuff actually does

Measured on one UA-810CNBLE (advertises as UA-810BLE, firmware `PUA0810_1.01`) on 17 and 18 October 2026, with btmon on the 18th.

- **A measurement ends in a 60-second window.** After START the cuff counts down and measures, once or three times (60 s apart), then flashes its Bluetooth light for 60 s and switches off. The light goes solid while a computer is connected.
- **Of the packets decoded on 18 October, only the window is connectable.** The cuff sends four packet kinds. BlueZ keeps the last value of each manufacturer field, so one callback can pair a fresh field with a stale one. (On 17 October a link also formed after a company-105 `03…` advert; that packet was never decoded.)
  - The START burst, company 105 `01…`: `ADV_SCAN_IND`, never connectable.
  - The idle iBeacon, Apple company 76 with tail `32010040c3`: `ADV_NONCONN_IND`.
  - The transfer iBeacon, tail `a1030001c3`: `ADV_NONCONN_IND`, at the start of the window.
  - The window, company 105 `02…`: `ADV_IND`, the only connectable advert decoded.
  The fourth company-105 byte varies (`42`, `64`). It matched the battery percentage twice and contradicted it once, so it is ignored.
- **The cuff sends its oldest unsent record first, and never resends one.** Records arrive on the standard `0x2A35` or on the A&D indicate `11127003`; on every multi-record link the next record came on the characteristic subscribed most recently (the same flags-`0x1e` layout plus a tail that is stored and not decoded). Mean arterial pressure is the SFLOAT `0x07FE` (+infinity), not a parser failure.
- **Some requests between records stop the cuff.** A feature-characteristic read between records was followed by no further record on 5 of 6 links; the 17 October 14:13 drain continued after one. Three of the five also wrote Current Time after that read, so the clock write is not isolated. Subscriptions between records (6 of 6) and one `11127001` read (n = 1) did not stop it. Four links that only subscribed until the records stopped got the whole backlog (5, 3, 1 and 3 records), each within 2 s. The cuff hangs up 5.2–5.8 s after its last record.
- **A connect started at the START burst times out** (three times on 18 October, about 6 s each). Connect by address string: bleak rediscovers, then BlueZ connects on the next connectable packet. The link formed about 30 ms after that packet, and Connected came in 3.7–4.1 s with encryption and the cached GATT.

## Shape

- `service` keeps a Python sidecar alive. `bar-widget` opens the panel. They share nothing but files under `~/.local/state/jmcanana.pressure` and the database under `~/.local/share/jmcanana.pressure`, both mode `0700` / `0600`.
- The sidecar is the only BlueZ client. Quickshell's Bluetooth type can pair a device and cannot read Blood Pressure Measurement.
- A new monitor is not connected until the panel trusts it. The panel sees a name, a beacon class, and the last four hex digits. The full address stays in the database.
- Readings bind to a patient by cuff user-id byte. The person shown on screen is a separate choice, so looking at one patient does not claim the next packet.
- The bar never shows a number, a name, or a serial. It shows `BP`, `Sync`, or `New`. The theme urgent colour means AHA stage 2 or severe, ESC hypertension (from 140/90), any reading above the AHA crisis line (higher than 180 and/or 120, at any age), an irregular-pulse flag, or a sidecar error. AHA stage 1 and ESC elevated stay as the category word inside the panel. The colour follows the strongest stored reading, including ones not yet bound to a person. Changing who is on screen does not change it.
- The panel is for a household at home: numbers and patient names show without a toggle. Readings, names and the share image stay on this computer (database 0600 in a 0700 directory) and never enter the repository; tests and mockups use synthetic numbers only.
- The panel (design direction B, "Map") draws only; `pressure/view.py` computes every position, so the bands it draws are the bands it classifies with:
  - the latest reading on the systolic-by-diastolic map (nested L regions, previous ten readings, crosshairs on the latest);
  - systolic, diastolic and pulse trends on equal-height band lanes over a day-slot time axis, each with a plain comparison sentence over the last 10 readings;
  - a 24-hour dial whose spokes span each hour's lowest to highest systolic and diastolic, with rings at 80, 120, 140 and 180 mmHg;
  - severe guidance, notices, the trust card, the patient editor (name, age group, cuff user), the device strip, and "Copy image", which puts a PNG of the panel on the clipboard.
- Colours, type, spacing, and corner radius come from `Color` and `Style`. There is no private palette, so a theme change restyles the panel.

## Categories

Default is the American Heart Association table ("Understanding Blood Pressure Readings", page updated August 2025):

| Band | Rule |
|---|---|
| Normal | under 120 and under 80 |
| Elevated | 120–129 and under 80 |
| Stage 1 | 130–139 or 80–89 |
| Stage 2 | 140 or higher, or 90 or higher |
| Severe | higher than 180 and/or higher than 120 |

The ESC 2024 switch uses the higher of the two sides: non-elevated under 120 and under 70, elevated 120–139 or 70–89, hypertension 140 or higher or 90 or higher. The panel names which table is on. Neither table is a diagnosis. A reading above the AHA crisis line shows the AHA symptom list under either table and at any age: call emergency services if those symptoms are present; with none, wait five minutes and measure again, then contact a clinician if it is still that high.

Patients have an age group, "13 or older" (the default) or "under 13". The AAP 2017 guideline uses the adult cut-offs from 13; under 13 it uses percentiles for age, sex and height, which this plugin does not hold, so an under-13 patient's readings are shown without bands or a category. No age or birth date is stored.

Pulse uses the AHA usual adult resting range of 60–100 only as a label. The sentence next to it says a cuff pulse is taken during the measurement and is not a resting pulse. The irregular-pulse bit is the cuff's own flag.

Morning is 4:00–9:59 and evening is 18:00–1:59 on the cuff clock. Those are the windows the monitor itself uses for its AM and PM averages. The hour row is drawn from that clock. A morning-versus-evening sentence waits until a sync has read the cuff clock within two minutes of this computer, and then only when each side has at least four readings. "Wider" means the systolic medians differ by 10 mmHg or more. That threshold is a display rule.

## Deliberate limits

- No network, no account, no legal-name field, no date of birth.
- Vendor characteristic `11127001` is neither read nor written. `11127002` is not subscribed.
- Intermediate cuff pressure `0x2A36` is telemetry, not a stored reading.
- The Apple and company-105 bytes are not decoded beyond the classes above: `transfer`, `start`, `idle`, `aandd-other`, `generic`.
- Generic Blood Pressure adverts (service `0x1810`, no company-105 payload) can be trusted and synced. Any other company-105 payload is ignored.
- A START burst or window from a trusted cuff shows `ready` in the status. A START that follows idle beacons can merge with the stale idle tail and show as `idle` instead; that affects the display only. Only the window takes the link: company 105 starting `02` with Apple tail `a1030001c3`. The fourth byte is ignored. Discovery stops for that connect only, by address: bleak rediscovers, then connects, each stage with six seconds, so up to about 12 s. The sidecar calls `pair()` after connect; an unimplemented stack and an existing bond are not errors. A pairing prompt that nobody answers gives up after 30 seconds.
- The drain subscribes to `0x2A35`, `0x2A36` and `11127003`, and sends nothing more until no record has arrived for 2 s. It then reads the battery and the cuff clock every time. Until model, firmware and feature bits are stored, it also reads those, then manufacturer and serial. That is seven reads at most, about 2.8 s, inside the cuff's hang-up delay; the first sync has the least margin. It waits for the cuff to hang up, releasing the link itself after 10 s quiet, or 8 s with no record at all. If the reads cut a backlog short, the rest stays on the cuff for the next sync.
- A cuff trusted from the window connects only on the window afterwards. A monitor trusted as generic connects only on a generic advert. A window tail without a company-105 payload, or a payload starting `03`, leaves discovery running.
- A pending sighting has its own token. Trust applies to that token. A later sighting invalidates it. A generic Blood Pressure advert needs a separate confirm; it is not the same action as trusting this cuff.
- Binding a cuff user moves readings already stored for that monitor and user byte. The control names the monitor by its last four hex digits. Readings with no user byte have their own bind. The panel says when it has classed a START burst and sync is waiting for the window.
- Morning and evening are not compared until the cuff clock has been shown to match this computer. The hour row stays, labelled as the cuff clock.
- Every sync reads the cuff clock and records whether it is within two minutes of this computer; nothing is written. This read has run only against the test fake; no link on 18 October read `0x2A2B`. When it is not (the cuff does not follow daylight saving, so expect this after 1 November 2026), the panel gives the offset in minutes and asks for the cuff's clock to be set from its own menu. Arming a clock write from the panel still exists, one shot, after the quiet gap. The cuff hangs up first on every link seen, so in practice it never fires.
- Device detail shows manufacturer, model, battery and firmware. The full address stays in the database. A pending monitor's public name has that suffix removed.
- `keepLoaded` is set. A change to the service needs `omarchy restart shell` before the new sidecar code is the one running. Do not restart the shell as part of evaluation unless that is the point of the trial.

## Questions for review

Systems: what should the bar be allowed to disclose in a shared room, and is "trust once, then sync on every transfer beacon" the right default for a cuff that also advertises at power-on? Reviewer: does the code match the capture, and which tests are still missing? Adversarial: where does a number, a name, or a medical claim leak, and which beacon will make the scan go deaf?
