#!/bin/bash
# Install Mashup Studio on a Mac (Apple Silicon, macOS 14 or later), from the folder you cloned:
#
#   packaging/install.sh
#
# It checks for ffmpeg and Rubber Band (and installs them with Homebrew when Homebrew is there), makes a Python 3.12
# venv in ~/Library/Application Support/Mashup Studio/venv with uv (installing uv first if it is missing), installs
# this folder into it, and writes "Mashup Studio.app" into ~/Applications. Run it again after an update; it keeps
# your settings and catalog. The separation and beat models download on first use (htdemucs is about 80 MB).
#   UV   the uv program to use (default: uv on PATH, else Astral's installer puts one in ~/.local/bin)
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
support="$HOME/Library/Application Support/Mashup Studio"
venv="$support/venv"
app="$HOME/Applications/Mashup Studio.app"

formulae=()
command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || formulae+=(ffmpeg)
command -v rubberband >/dev/null || formulae+=(rubberband)
if [ ${#formulae[@]} -gt 0 ]; then
  if ! command -v brew >/dev/null; then
    echo "Mashup Studio needs ffmpeg and Rubber Band. Install Homebrew (https://brew.sh), then run:" >&2
    echo "  brew install ${formulae[*]}" >&2
    echo "and this installer again." >&2
    exit 1
  fi
  echo "Installing ${formulae[*]} with Homebrew"
  brew install "${formulae[@]}"
fi

uv="${UV:-$(command -v uv || true)}"
if [ -z "$uv" ]; then
  echo "Installing uv, Astral's Python package manager, into ~/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  uv="$HOME/.local/bin/uv"
fi

mkdir -p "$support"
[ -x "$venv/bin/python" ] || "$uv" venv --python 3.12 "$venv"
"$uv" pip install --python "$venv/bin/python" -e "$here"

mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$here/packaging/launcher/Info.plist" "$app/Contents/Info.plist"
cp "$here/packaging/launcher/mashup-studio" "$app/Contents/MacOS/mashup-studio"
cp "$here/packaging/launcher/AppIcon.icns" "$app/Contents/Resources/AppIcon.icns"
cp "$here/packaging/launcher/Mashup Studio.command" "$support/Mashup Studio.command"
printf '%s\n' "$here/mf" > "$support/mf-path"
chmod +x "$app/Contents/MacOS/mashup-studio" "$support/Mashup Studio.command" "$here/mf"

MF_PYTHON="$venv/bin/python" "$here/mf" --help >/dev/null
echo "Installed. Open Mashup Studio from ~/Applications (or run $here/mf app)."
