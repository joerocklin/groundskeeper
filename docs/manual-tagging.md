# Tagging a Stage 1 trial

How to capture a debug recording that an agent can read with confidence. Used for the Stage 1 "Shadow" trial (#44), where the Mammotion app mows and the director only watches.

You do not need to stand by the Mower. Start it from the app, tap a few **Tags** from your phone when you happen to look, and pull the recording afterwards.

## Why tags

The debug recording holds what Home Assistant saw. It cannot hold what you did in the Mammotion app: which Zone you picked, the blade height, whether edge laps were on, or whether the app showed the Mower moving while Home Assistant's numbers were frozen. A **Tag** is a timestamped note in the recording that says so. It is record-only: the director never reads a Tag, and a Tag never commands the Mower.

The first 16-day recording had no labels, so it could not answer the #44 questions:

- Do progress, area and position advance during edge laps? (tunes the stall window)
- Does the lift reach its target height per the readback? (gates Stage 2)
- Are job and done detection correct for mows started in the app?

## What is captured without you

After this version is installed, the recording also carries:

| Record | What it gives |
|---|---|
| `signal` | Everything the adapter tells the core (job started, Zone complete or aborted, new error, rain stop), which was never recorded while the Schedule was idle. |
| `job_settings` | The running job's settings as Mammotion-HA's data holds them: `knife_height`, `edge_mode` (the perimeter laps), `zone_hashs`, path mode and angle. Written when they change. |
| `meta.source_version` | The installed Mammotion-HA version, so a recording says which upstream produced it. |

`job_settings` is read from another integration's private data (PyMammotion's `work` for Mammotion-HA v0.6.18). Whether it is filled in for a mow started from the app is **not yet verified**. After Mow A below, check whether the recording has `job_settings` records. If it does, your `job_start` tags are a cross-check. If it does not, your tags are the only record of the settings, so make them accurate.

Telemetry silences, job start and finish, recharge trips and errors need no tag: they are already in the `snapshot` and `raw_state` records.

## What only you can say

- The settings you chose in the app, if `job_settings` turns out to be empty.
- Whether the Mower was really moving in the app while Home Assistant's progress sat still. In the first recording, job telemetry went silent for 10 to 56 minutes while the Mower was mowing, and Home Assistant's data looks the same whether the Mower stopped or the numbers froze.
- Anything else worth remembering: a gate opened, an obstruction, the app being closed.

## Setup, once

1. Install the version of the integration that has the `groundskeeper.add_tag` action and reload it. Confirm the Schedule is `idle` and debug recording is on (the toggle in the setup banner). Do not reload the integration during the trial.
2. Try the action in **Developer Tools → Actions** (YAML mode):

   ```yaml
   action: groundskeeper.add_tag
   data:
     label: note
     note: "test tag"
   ```

   With "Return response" shown, you should see `recorded: true`. If it says `false`, the recording is off, has stopped after a write error, is paused for low disk space, or its queue is full.

3. Put the tags you will use on a phone dashboard so each is one tap. A button card per tag works; this is a sketch, not exercised against a live dashboard:

   ```yaml
   type: button
   name: Started in the app (edge laps on, 60 mm)
   icon: mdi:play
   tap_action:
     action: perform-action
     perform_action: groundskeeper.add_tag
     data:
       label: job_start
       edge_laps: true
       laps: 2
       height_mm: 60
   ```

   Make similar buttons for `moving`, `stopped` and `progress_check`. A tag's `minutes_ago` backdates it, up to 24 hours, so a tag entered later still lands at the right time. Use Developer Tools when you need a backdated or custom one.

## The tags

| `label` | Meaning | Useful fields |
|---|---|---|
| `job_start` | You started a mow in the app | `zone`, `height_mm`, `edge_laps`, `laps` |
| `edge_laps_begin`, `edge_laps_end` | The Mower started or finished its perimeter laps | |
| `progress_check` | You looked at the app | `app_progress_pct` |
| `moving`, `stopped` | The app shows the Mower moving or stopped | |
| `note` | Anything else | `note` (up to 200 characters) |

Every tag also stores Home Assistant's own view at the moment you tapped (activity, progress, battery, blade height), so "the app says 40% and Home Assistant says 12%" is answerable from one record. Keep serial numbers and locations out of `note`. At most 30 tags per minute are accepted.

## Mow A: edge laps on, 60 mm

These are mows you start in the Mammotion app, not Runs: the Schedule stays idle, so nothing here is a Run in the glossary's sense.

Edge laps are on by default in your app, usually 2 laps and sometimes 3.

1. Dock the Mower with the battery at 80% or more and nothing running.
2. In the app, pick one Zone and use your usual settings: 60 mm, edge laps on.
3. Press start, then tap `job_start` (with `zone` and `laps` if the button does not carry them).
4. Walk away. Whenever you do look at the app, tap one of `progress_check` (with the app's number), `moving`, or `stopped`. One or two taps in the first half hour is plenty. A long gap with Home Assistant's progress frozen is where one `moving` or `stopped` tap is worth most.
5. If you notice the perimeter laps starting or ending, tap `edge_laps_begin` or `edge_laps_end`. If you miss them, backdate the tag later with `minutes_ago`.

## Mow B: edge laps off, 70 mm

1. After Mow A has finished and the job has cleared, set the same Zone to 70 mm with edge laps off.
2. Press start and tap `job_start` with `height_mm: 70`, `edge_laps: false`.
3. Tap `progress_check` and `moving` or `stopped` whenever you look.

Why 70 mm: in the first recording, mowing readbacks sat at 60 mm, and 80, 90 and 100 mm appeared as travel and charging heights. A mow at 80 mm would be hard to tell from the Mower travelling. 70 mm showed up only briefly in height ramps, so a long plateau there can only be the mow. Same Zone for both mows, so the two differ only in the two settings under test.

## Pull the data

Look the pod up each time, since it changes on restart:

```
POD=$(k get pods -n apps --no-headers -o custom-columns=:metadata.name | grep '^home-assistant-')
```

Make a consistent copy with SQLite's online backup API. This is safe while Home Assistant is writing and needs no pause of the recording:

```
k exec -n apps "$POD" -c home-assistant -- python3 -c "import glob,os,sqlite3; s=sqlite3.connect(max(glob.glob('/config/groundskeeper/recording_*.db'),key=os.path.getsize)); d=sqlite3.connect('/tmp/stage1b.db'); s.backup(d); d.close(); s.close()"
```

The command takes the largest `recording_*.db`, because the folder can also hold a zero-byte `recording_<entry_id>.db` (a literal placeholder name, seen on the live install, origin unknown) and a bare `[0]` may pick it. Do not copy the live `.db` file directly. In WAL mode a plain copy can miss recent writes.

Compress it, hash it, and copy it. `kubectl cp` has failed on large files before (`unexpected EOF`, zero retries by default), so compress and retry:

```
k exec -n apps "$POD" -c home-assistant -- sh -c 'gzip -kf /tmp/stage1b.db && sha256sum /tmp/stage1b.db'
k cp -n apps -c home-assistant --retries=-1 "$POD":/tmp/stage1b.db.gz ~/groundskeeper-data/stage1b.db.gz
gunzip -f ~/groundskeeper-data/stage1b.db.gz && sha256sum ~/groundskeeper-data/stage1b.db
```

The two hashes must match. Then clean up the pod, because `/tmp` counts against the free-space floor that pauses recording:

```
k exec -n apps "$POD" -c home-assistant -- rm -f /tmp/stage1b.db /tmp/stage1b.db.gz
```

The recording holds real GPS data for your property (see #49), so keep the file outside the repo, `~/groundskeeper-data/`, and do not attach it anywhere. Open it read-only with `sqlite3 "file:stage1b.db?immutable=1"`, which creates no `-wal` or `-shm` files.

## What the labels answer

| #44 criterion | Answered by |
|---|---|
| Two mows started in the app, one with edge laps, one at a different height | `job_start` tags, or `job_settings` records |
| Zero director commands | count of `command` records over the window |
| Progress, area and position during edge laps | `edge_laps_begin`/`edge_laps_end` tags if you made them; otherwise whatever differs between Mow A and Mow B |
| Telemetry freezes while the Mower works | `moving`/`stopped` tags and `progress_check` against Home Assistant's own values |
| Lift reaches target per the readback | `blade_height` readback after each `job_start` |
| Recording growth rate | record count and payload bytes per hour inside the two mows |

## The first recording

The 2026-10-24 to 2026-11-09 recording is unlabeled. It supports facts that need no labels, such as zero commands and the size of the lift ramps. Edge laps are on by default, so each fresh start there opens with them, but telemetry freezes overlap the same minutes. Use it for hints, not for tuning the stall window.

## Known issues that touch this trial

- #80: the recording leaks the device serial in lowercase entity IDs. Do not attach a recording to a public issue until it is fixed. Free-text notes pass through the same case-sensitive redaction, so keep them free of identifiers.
- #49: the recording and diagnostics carry real GPS position, which is a separate reason not to share one.
