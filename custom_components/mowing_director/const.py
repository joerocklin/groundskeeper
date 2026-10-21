"""Constants for Mowing Director."""

DOMAIN = "mowing_director"

# Adapter registry: integrations whose lawn_mower devices the director can
# drive. Phase 1: mammotion only (spec §4.1).
ADAPTER_DOMAINS: tuple[str, ...] = ("mammotion",)
