"""Constants for Groundskeeper."""

DOMAIN = "groundskeeper"

# Adapter registry: integrations whose lawn_mower devices the director can
# drive. Phase 1: mammotion only (spec §4.1).
ADAPTER_DOMAINS: tuple[str, ...] = ("mammotion",)

# Panel (spec §16.1, §2.3): one sidebar panel per HA instance, shared by all
# Mowers.
PANEL_URL_PATH = "groundskeeper"
PANEL_ELEMENT = "groundskeeper-panel"
PANEL_STATIC_URL = "/groundskeeper/panel"
PANEL_JS = "groundskeeper-panel.js"
PANEL_JS_MIN = "groundskeeper-panel.min.js"

# The one bus event every Groundskeeper automation hook listens to (spec §13.4).
EVENT_TYPE = "groundskeeper_event"
