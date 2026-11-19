# Panel websocket pattern

How later panel tickets add backend features. Code: `custom_components/groundskeeper/websocket_api.py`.

- **One panel, many Mowers.** The sidebar panel is registered once per HA instance (`panel.py`) and removed when the last entry unloads. The panel picks a Mower with `entry_id`.
- **Commands.** `groundskeeper/<verb>`, decorated `@websocket_api.require_admin` and `@websocket_api.websocket_command({...})`, taking `entry_id` for per-Mower work. Resolve it with `_loaded_entry`; reply `ERR_NOT_FOUND` when unknown. Register in `async_register_commands` (called from `async_setup`, never per entry).
- **Reads.** Add fields to `build_snapshot`. The panel receives it via `get_state` and via the subscription.
- **Pushes.** After any change the panel shows, call `async_notify_state_changed(hass, entry_id)`. Each `groundskeeper/subscribe` connection gets a fresh snapshot (and one immediately on subscribe). Unsubscribing is core's `unsubscribe_events`; the dispatcher unsub lives in `connection.subscriptions`.
- **Fast streams get their own subscription.** `subscribe` and `subscribe_now` push on every change, so something that updates about once a second (the Mower position, `groundskeeper/subscribe_mower_position`) does not ride on them. Give it a dedicated admin-only subscription that reads on demand with `async_track_time_interval`, pushes once at once and then only on change (at most every 1.5 s), sends ages as durations, and stores its unsubscribe in `connection.subscriptions`. The panel opens it only while the tab that shows it is open, and updates the DOM in place rather than re-rendering.
- **Frontend.** `frontend/groundskeeper-panel.js` (plain `HTMLElement`, shadow DOM). Use `hass.connection.sendMessagePromise` for commands and `subscribeMessage` for the subscription. After editing it run `node devtools/build_panel.mjs` and commit the `.min.js` and `.sha1`; a test fails if they are stale. The `?v=` cache-buster is a content hash, so no manual bump.
- **Tests.** Use `hass_ws_client` with fakes; never call the live Mower.
