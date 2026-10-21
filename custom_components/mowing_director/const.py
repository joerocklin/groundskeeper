"""Constants for Mowing Director."""

DOMAIN = "mowing_director"

# Adapter registry: integrations whose lawn_mower devices the director can
# drive. Phase 1: mammotion only (spec §4.1).
ADAPTER_DOMAINS: tuple[str, ...] = ("mammotion",)

# Panel (spec §16.1, §2.3): one sidebar panel per HA instance, shared by all
# Mowers.
PANEL_URL_PATH = "mowing-director"
PANEL_ELEMENT = "mowing-director-panel"
PANEL_STATIC_URL = "/mowing_director/panel"
PANEL_JS = "mowing-director-panel.js"
PANEL_JS_MIN = "mowing-director-panel.min.js"

# The one bus event every Mowing Director automation hook listens to (spec §13.4).
EVENT_TYPE = "mowing_director_event"
