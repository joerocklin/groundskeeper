# Mowing Director

A Home Assistant integration that directs a robot mower through multi-height step-down mowing across zones, sequencing work the mower's own app cannot express.

## Language

**Mower**:
One physical robot mower, with its own Zones, device-wide settings, and Schedule.
_Avoid_: Device, robot

**Zone**:
A mowable area known to the Mower.
_Avoid_: Area, yard, region

**Zone bearing**:
The compass bearing of a Zone's long axis, against which Run path angles are expressed.
_Avoid_: Orientation, heading

**Run**:
One full mow of a Zone at a specific blade height, path angle (relative to the Zone bearing), speed, and other mow settings; it carries its own status: pending, on hold, done, failed, skipped, or cancelled.
_Avoid_: Pass, job, task, attempt

**On hold**:
The status of a Run that is still alive on the Mower but cannot continue until its cause clears or the user acts; no clock fails it.
_Avoid_: Paused, suspended, waiting

**Plan**:
An ordered set of Runs for one Zone, stepping blade height down from a start height to an end height; a Plan whose start and end heights are equal is a single maintenance Run.
_Avoid_: Scenario, recipe, program, template

**Schedule**:
The order in which a Mower performs Runs, possibly interleaving Runs from several Plans; Runs within a Plan always keep the Plan's order. A Schedule says what comes next, never when.
_Avoid_: Queue, sequence, task (Mammotion's term for its own app-side jobs)

**Recurrence**:
A rule that repeatedly adds a Plan to a Mower's Schedule over time, such as weekly maintenance.
_Avoid_: Recurring schedule, repeat, cron

**Gate**:
A condition that must hold before the next Run may start, such as sufficient battery, work hours, or dry grass.
_Avoid_: Precondition, check, hold

**Anomaly**:
A detected fault fingerprint in Mower behaviour or telemetry.
_Avoid_: Fault, issue
