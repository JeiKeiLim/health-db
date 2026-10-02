# Tenet State Snapshot

This directory is for portable Tenet SQLite snapshots that are safe to track in Git.

- Run `tenet db snapshot` to write a gzip-compressed `state-snapshot/tenet.db.gz` (use `--no-compress` for a plain `tenet.db`).
- Run `tenet db restore-snapshot` to restore live runtime state from the snapshot (auto-detects compressed or plain).
- Do not track `.tenet/.state/`; it is the live SQLite WAL database.
