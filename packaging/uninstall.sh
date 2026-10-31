#!/usr/bin/env bash
# Remove an AudioCrate user-local install.
set -euo pipefail
rm -rf "$HOME/.local/share/audiocrate"
rm -rf "$HOME/.local/share/audiocrate-venv"        # the pip environment install.sh makes where no package has PySide6
rm -f  "$HOME/.local/bin/audiocrate"
rm -f  "$HOME/.local/share/applications/audiocrate.desktop"
rm -f  "$HOME/.local/share/icons/hicolor/256x256/apps/audiocrate.png"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "AudioCrate uninstalled. (Config in ~/.config/audiocrate kept: delete manually if wanted.)"
