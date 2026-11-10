# Manual tagging for live trial Stage 1

How to capture a debug recording that an agent can read with confidence. Used for the Stage 1 "Shadow" trial (#44), where the Mammotion app mows and the director only watches.

## Why tag by hand

The debug recording holds what Home Assistant saw: `snapshot` and `raw_state` records, with no commands in Stage 1. It cannot hold what you did in the Mammotion app. It does not know when you pressed start, which Zone you picked, whether edge laps were on, or which blade height you chose. Those are exactly the labels the #44 acceptance criteria turn on:

- Do progress, area and position advance during edge laps? (tunes the stall window)
- Does the lift reach its target height per the readback? (gates Stage 2)
- Are job and done detection correct for app-started Runs?

A 16-day recording taken without labels could not answer these, because nobody remembered what happened on which day. The fix is a timestamped log written while you mow, plus two short, deliberately different Runs.

What the first recording did show, and why the protocol below looks the way it does:

- Job telemetry (`progress`, `area`, task timers) can freeze for tens of minutes while the Mower works. Step 5 of each Run catches whether that happens, by comparing the app against HA.
- Blade height changes to or from 80 mm pass through 90 mm on the readback (for example 60 to 80 reads `65, 70, 75, 80, 85, 90, 85, 80`). Run B uses 80 mm to capture this cleanly.
- `snapshot.movement.position`, `zone` and `job_ref` were null on every snapshot. The `device_tracker` entity in `raw_state` is where position lives today.

## What you need

- A terminal you can reach while the Mower is mowing.
- `kubectl` access to the Home Assistant pod (the commands use `k` as the alias, in namespace `apps`).
- Somewhere outside the repo for the files, here `~/groundskeeper-data/`. The recording holds real GPS data for your property (see #49 and the README's section on sharing your map), so it must not be committed or attached anywhere.

## Once, before you start

1. Define the logger. Every line gets a UTC timestamp to the second:

   ```
   mkdir -p ~/groundskeeper-data
   mowlog() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> ~/groundskeeper-data/mowlog.txt; }
   ```

   A phone notes app works too, if each line carries a time to the second and the timezone.

2. Compare clocks. Run `date -u` locally and `k exec -n apps "$POD" -c home-assistant -- date -u` (see "Pull the data" for `POD`). If they differ by more than about 5 seconds, run `mowlog "clock offset: HA is +Ns"`.

3. In the panel, confirm the Schedule is `idle` and debug recording is on (the toggle in the setup banner).

4. Do not reload or restart the integration during the test. If you must, run `mowlog "reloaded integration"`; each reload shows up in the recording as `unload`, `start` and `reconciled` lifecycle records.

## Run A: edge laps on, 60 mm

1. Dock the Mower with battery at 80% or more and nothing running. Run `mowlog "A prep: battery NN%, blade readback XX mm"`.
2. In the Mammotion app, pick one Zone. Set blade height to 60 mm and turn edge laps on. Run `mowlog "A settings: zone=<name> height=60 edge_laps=ON"`.
3. Press start in the app and run `mowlog "A START"`.
4. Watch the start of the Run. When the Mower begins the perimeter, run `mowlog "A edge laps began"`. When it switches to stripes, run `mowlog "A edge laps ended"`. Accuracy to about a minute is enough.
5. About every 10 minutes for the first half hour, compare the app's progress with HA's progress entity and run `mowlog "A app=NN% HA=NN%"`. If HA stays put while the app moves, that is the telemetry freeze, and it is the most useful thing to catch.
6. Do not pause, skip, or change the height mid-Run. Log anything unusual in one line: a recharge trip, an obstruction, the app being opened or closed, a gate being opened. When the Mower docks, run `mowlog "A finished"`.

## Run B: edge laps off, 80 mm

1. Wait for Run A to finish and the job to clear. Run `mowlog "B prep: readback XX mm"`.
2. In the app, set the same Zone to 80 mm with edge laps off. Run `mowlog "B settings: zone=<same name> height=80 edge_laps=OFF"`.
3. Press start and run `mowlog "B START"`. Repeat the 10-minute progress checks from Run A. Run `mowlog "B finished"` when it docks.

Run B changes the height from the previous Run (satisfying the #44 criterion) and runs without edge laps, so the two Runs differ in only the two settings under test.

## Pull the data

Look the pod up each time, since it changes on restart:

```
POD=$(k get pods -n apps --no-headers -o custom-columns=:metadata.name | grep '^home-assistant-')
```

Make a consistent copy with SQLite's online backup API. This is safe while HA is writing, and it needs no pause of the recording:

```
k exec -n apps "$POD" -c home-assistant -- python3 -c "import glob,sqlite3; s=sqlite3.connect(glob.glob('/config/groundskeeper/recording_*.db')[0]); d=sqlite3.connect('/tmp/stage1b.db'); s.backup(d); d.close(); s.close()"
```

Do not copy the live `.db` file directly. In WAL mode a plain copy can miss recent writes.

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

## What to hand over

Two files, both outside the repo:

- `~/groundskeeper-data/stage1b.db`
- `~/groundskeeper-data/mowlog.txt`

An agent can then open the database read-only (`sqlite3 "file:stage1b.db?immutable=1"`, which creates no `-wal` or `-shm` files) and answer the #44 criteria against your timestamps. Agents never command the Mower in this stage.

## What the labels answer

| #44 criterion | Answered by |
|---|---|
| Two app-started Runs, one with edge laps, one at a different height | `A settings` and `B settings` lines |
| Zero director commands | count of `command` records over the window |
| Progress, area and position during edge laps | records between `A edge laps began` and `A edge laps ended`, against the same measures in stripes |
| Telemetry freezes while the Mower works | the `app=NN% HA=NN%` lines against HA's own values |
| Lift reaches target per the readback | `blade_height` readback after each `settings` line and each START |
| Recording growth rate | record count and payload bytes per hour inside the two Runs |

## Recordings from before this protocol

The first recording (2026-10-24 to 2026-11-09) is unlabeled. It supports facts that need no labels, such as zero commands and the size of the lift ramps. It does not support conclusions about edge laps or any specific Run, and none should be drawn from it.

## Known issues that touch this trial

- #80: the recording leaks the device serial in lowercase entity IDs. Do not attach a recording to a public issue until this is fixed, and the position data is a separate reason (#49).
