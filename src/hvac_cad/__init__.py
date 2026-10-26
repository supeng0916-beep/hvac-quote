"""Local CAD reader. Public HTTP contracts remain owned by @hvac/contracts."""

TOOL_VERSION = "hvac-cad-0.1.7"
# Proofs select the exact frozen index version; new inspections only write TOOL_VERSION.
REPLAY_READER_VERSIONS = frozenset({"hvac-cad-0.1.4", TOOL_VERSION})
