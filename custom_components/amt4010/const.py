"""Constants for the Intelbras AMT 4010 integration."""
DOMAIN = "amt4010"
DEFAULT_PORT = 9009

CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 5
MIN_SCAN_INTERVAL = 2
MAX_SCAN_INTERVAL = 60

# Installed read-only: arm/disarm stays off until the partitions have been
# mapped with the owner at the keypad.
CONF_ENABLE_COMMANDS = "enable_commands"
DEFAULT_ENABLE_COMMANDS = False
CONF_CODE_ARM_REQUIRED = "code_arm_required"
DEFAULT_CODE_ARM_REQUIRED = True

# Partitions the panel really uses. The status says which partitions are armed
# but not which ones exist, and "everything armed" needs to know.
CONF_PARTITIONS = "partitions"
DEFAULT_PARTITIONS = ["A", "B"]

# Zones with an entity: empty = the zones with a name programmed in the panel.
CONF_ZONES = "zones"

# Time for the panel to settle before the strict read after a command.
SETTLE_SECONDS = 1.0
FAILURE_CYCLES_BEFORE_UNAVAILABLE = 3
NAME_RETRY_SECONDS = 300.0
NAME_MAX_ATTEMPTS = 6

EVENT_ALARM_TRIGGERED = f"{DOMAIN}_alarm_triggered"
# Per-entry state that must outlive a coordinator (reloads) — a refused password
# and the alarm tracker. The Store carries it across restarts.
DATA_STATE = f"{DOMAIN}_state"
