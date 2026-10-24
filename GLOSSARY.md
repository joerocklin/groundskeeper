# Groundskeeper

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

**Channel**:
A route the Mower travels between two Zones, or between a Zone and its dock, outside any Zone's mowing area.
_Avoid_: Path (the stripes of a mow), passage, corridor, route

**Gate**:
A physical barrier on one or more Channels that must be open for the Mower to pass; a person opens it, or Home Assistant does where it is automated, and it is presumed closed unless known to be open.
_Avoid_: Door, passage, fence

**Run**:
One full mow of a Zone at a specific blade height, path angle (relative to the Zone bearing), speed, and other mow settings; it carries its own status: pending, on hold, done, failed, skipped, or cancelled.
_Avoid_: Pass, job, task, attempt

**On hold**:
The status of a Run that is still alive on the Mower but cannot continue until its cause clears or the user acts; no clock fails it.
_Avoid_: Paused, suspended, waiting

**Plan**:
An ordered set of Runs for one Zone, stepping blade height down from a start height to an end height; a Plan whose start and end heights are equal is a single maintenance Run.
_Avoid_: Scenario, recipe, program, template

**Max cut**:
The largest share of blade height a single step of a Plan may remove relative to the previous Run, which determines how many Runs a Plan derives.
_Avoid_: Step size, cut ratio

**Finishing Run**:
The last Run of a multi-Run Plan, given slower or tighter settings to tidy blades the earlier Runs missed.
_Avoid_: Final pass, cleanup pass

**Base angle**:
The path angle, relative to the Zone bearing, on which a Plan's final Run lies when the user fixes it; otherwise the final Run's angle is drawn like the others.
_Avoid_: Default angle, main angle

**Allowed angle range**:
The span of path angles, relative to the Zone bearing, that a Zone can be mown at without undue extra time; Run angles are drawn from it.
_Avoid_: Angle window, angle limits

**Crossing angle**:
The minimum angle between the paths of one Run and the next, so successive Runs cut across each other's tracks.
_Avoid_: Included angle, double-grid angle (Mammotion's second-pass angle within one Run)

**Schedule**:
The order in which a Mower performs Runs, possibly interleaving Runs from several Plans; Runs within a Plan always keep the Plan's order. A Schedule says what comes next, never when.
_Avoid_: Queue, sequence, task (Mammotion's term for its own app-side jobs)

**Recurrence**:
A rule that repeatedly adds a Plan to a Mower's Schedule over time, such as weekly maintenance.
_Avoid_: Recurring schedule, repeat, cron

**Check**:
A condition that must hold before the next Run may start, such as sufficient battery, work hours, or dry grass.
_Avoid_: Gate (a physical barrier), precondition, self-check (the Mower's own diagnostic)

**Anomaly**:
A detected fault fingerprint in Mower behaviour or telemetry. An Anomaly only alerts the user (or asks the Schedule to pause after the current Run); it never fails a Run or commands the Mower.
_Avoid_: Fault, issue

**Prompt**:
A question the director puts to the user that waits for an answer; it stays pending until answered or superseded, and no clock answers it. Plain notices, such as a finished Schedule, are not Prompts.
_Avoid_: Alert, notification (the delivery, not the question)
