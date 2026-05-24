# History of Changes

## v1.0.1
- Initial technical specification version.
- Added streaming enumeration and dual-contour logging.
- Implemented TUI for 90-character fixed width.
- Added `/SECFIX` logic for mandatory ACL synchronization.
- Implemented emergency break engine (Ctrl+C / F10).

## v1.0.2 (Current)
- Implemented the monolithic `smart_sync.py` script.
- Added `EXCLUDE_RULES` administrative dictionary for file filtering.
- Integrated `psutil` for CPU and RAM telemetry.
- Implemented `sync_history.json` for session persistence.
- Added `requirements.txt` and updated documentation.
