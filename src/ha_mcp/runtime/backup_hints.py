"""Operator backup guidance used by both native and remote descriptions."""

BACKUP_HINTS = {
    "strong": "Run this backup before the FIRST modification of the day/session. This is usually not required since most operations can be rolled back (the model fetches definitions before modifying). Users with daily backups configured should use 'normal' or 'weak' instead.",
    "normal": "Run before operations that CANNOT be undone (e.g., deleting devices). If the current definition was fetched or can be fetched, this tool is usually not needed.",
    "weak": "Backups are usually not required for configuration changes since most operations can be manually undone. Only run this if specifically requested or before irreversible system operations.",
    "auto": "Run before operations that CANNOT be undone (e.g., deleting devices). If the current definition was fetched or can be fetched, this tool is usually not needed.",
}
