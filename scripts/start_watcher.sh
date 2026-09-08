#!/usr/bin/env bash
# Start BRAW Ingest Hot Folder Watcher Daemon

cd "$(dirname "$0")/.." || exit 1

export PYTHONPATH="${PYTHONPATH:-}:$(pwd)"

python3 -u -m src.cli watch --config config/config.yaml
