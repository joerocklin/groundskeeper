# One Store per Mower holds all configuration; no options flow

Everything the user configures (settings, Zones, Gates, Checks, entity and service references) and all runtime state live in one HA JSON `Store` per Mower, edited in the panel. `entry.data` holds only the Mower's `device_id`, `entry.options` is unused, and there is no options flow, so HA shows no Configure button. This departs from both the HA convention and ha_washdata, which this integration otherwise follows. We chose it because every `entry.options` write fires the update listener, which reloads the entry and runs restart reconciliation on a live Mower. We also chose it because a single atomically written file makes write-ahead persistence trivial, including moving a finished Run from `state` to `history`. Moving data back into `entry.options` later would mean migrating across two storage mechanisms.

## Considered Options

- **Panel writes `entry.options` (the WashData pattern):** rejected, because every edit would reload the entry and reconcile it.
- **Options flow for entity references only:** rejected, because it gave two places to edit settings and two export paths, and bought nothing.
