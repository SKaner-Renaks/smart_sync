# Smart Sync Utility

High-performance, fault-tolerant Python script for mirror directory synchronization with ACL preservation.

## Features
- **Multithreaded Copying:** Fast data transfer using a configurable worker pool.
- **Mirror Mode:** Ensures the destination exactly matches the source, including deletions.
- **ACL Synchronization (`/SECFIX`):** Updates security descriptors even if file content hasn't changed.
- **Streaming Enumeration:** Starts copying immediately as files are found.
- **Advanced TUI:** Real-time progress monitoring with a fixed 90-character width and ANSI colors.
- **Administrative Exclusions:** Global filtering by extensions, prefixes, or exact filenames.
- **Telemetry:** Tracks CPU, RAM, and I/O performance (min/avg/max).
- **Emergency Break:** Instant stop using `Ctrl+C` or `F10`.

## Installation
Ensure Python 3.14+ is installed.
```bash
pip install -r requirements.txt
```

## Usage
Edit the constant block in `smart_sync.py` to set the `SOURCE_PATH` and `DEST_PATH`, then run:
```bash
python smart_sync.py
```

## Logs and History
- `sync_main.log`: Successful copy/ACL/delete operations.
- `sync_errors.log`: Critical errors and retry failures.
- `sync_history.json`: Session metadata for progress tracking.
- `history.md`: Project change log.
