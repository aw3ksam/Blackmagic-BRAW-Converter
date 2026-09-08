#!/usr/bin/env bash
# Start BRAW Ingest Hot Folder Watcher Daemon

cd "$(dirname "$0")/.." || exit 1

export RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
export RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
export PYTHONPATH="$PYTHONPATH:$RESOLVE_SCRIPT_API/Modules/"

python3 -m src.cli watch --config config/config.yaml
