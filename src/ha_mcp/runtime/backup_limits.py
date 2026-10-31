"""Full archive job windows shared by native owners and remote polling."""

# Core's frontend waits without a timeout; tools keep a generous finite budget.
# Large safety archives on constrained hardware can exceed five minutes. A short
# deadline loses a completed archive's identity and tempts duplicate creation.
# Client idle timeouts are handled separately through progress heartbeats.
BACKUP_MAX_WAIT_S = 1800
SAFETY_BACKUP_MAX_WAIT_S = BACKUP_MAX_WAIT_S
