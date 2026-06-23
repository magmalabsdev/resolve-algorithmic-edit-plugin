#!/usr/bin/env bash
#
# Installs the Trim Timelapse Silence plugin into DaVinci Resolve's user Scripts
# folder by symlinking it (so edits to this repo take effect immediately). The
# menu label in Resolve is the destination filename without its extension.
#
# Usage:  ./install.sh          # install / update
#         ./install.sh --remove # uninstall
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SRC_DIR/trim_timelapse_silence.py"
DEST_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit"
DEST="$DEST_DIR/Trim Timelapse Silence.py"

if [[ "${1:-}" == "--remove" ]]; then
  rm -f "$DEST"
  echo "Removed: $DEST"
  exit 0
fi

if [[ ! -f "$SRC" ]]; then
  echo "error: cannot find $SRC" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
ln -sf "$SRC" "$DEST"
echo "Installed: $DEST"
echo

if ! command -v ffmpeg >/dev/null 2>&1 \
    && [[ ! -x /opt/homebrew/bin/ffmpeg ]] && [[ ! -x /usr/local/bin/ffmpeg ]]; then
  echo "NOTE: ffmpeg was not found. Install it before using the plugin:"
  echo "  brew install ffmpeg"
  echo
fi

echo "In DaVinci Resolve, run it from:  Workspace > Scripts > Edit > Trim Timelapse Silence"
echo "(If Resolve is already running, restart it so the script appears.)"
