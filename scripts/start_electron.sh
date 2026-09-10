#!/usr/bin/env bash
set -e

# Change to repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

echo "=========================================================="
echo "🚀 Launching Blackmagic Converter v5.1 (Electron Forge)"
echo "=========================================================="

npm start
