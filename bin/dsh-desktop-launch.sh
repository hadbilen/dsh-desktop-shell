#!/usr/bin/env bash
# DSH desktop launcher.
#
# Starts the QtWebEngine tray shell (dsh-tray.py) when available; falls back to
# a Chrome application window when PyQt6 WebEngine is not installed yet. That
# way the shortcut keeps working without `python3-pyqt6.qtwebengine`.
#
# Paths resolve through $HOME; the script carries no hardcoded user name so it
# stays portable.
#
# Configurable:
#   DSH_TRAY_WINDOW   maximized (default) | fullscreen | normal
#   DSH_BIN_DIR       directory holding the scripts (default ~/.local/bin)
#   DSH_PYTHON        python3 interpreter to use

set -u

BIN_DIR="${DSH_BIN_DIR:-$HOME/.local/bin}"
TRAY="$BIN_DIR/dsh-tray.py"
CHROME="$BIN_DIR/dsh-app-window.sh"

# Window start mode: the shortcut applies this on every launch.
export DSH_TRAY_WINDOW="${DSH_TRAY_WINDOW:-maximized}"

# Note: --password-store=basic keeps QtWebEngine from stalling on a password
# prompt in environments without KDE/KWallet integration. Be aware it stores
# passwords in plain text; it is kept inside this project's own profile
# directory.
export QTWEBENGINE_CHROMIUM_FLAGS="${QTWEBENGINE_CHROMIUM_FLAGS:+$QTWEBENGINE_CHROMIUM_FLAGS }--password-store=basic"

# Probe PyQt6 WebEngine with the interpreter we would actually use; report the
# reason instead of swallowing it.
PY="${DSH_PYTHON:-$(command -v python3 || true)}"
if [ -n "$PY" ] && [ -x "$TRAY" ]; then
  if probe_err=$("$PY" -c 'from PyQt6.QtWebEngineWidgets import QWebEngineView' 2>&1); then
    exec "$PY" "$TRAY" "$@"
  elif [ -n "$probe_err" ]; then
    # Rather than silently falling back to Chrome, say why: the user may be one
    # package away from the real shell.
    printf 'dsh: PyQt6 WebEngine unavailable, falling back to a Chrome window.\n' >&2
    printf '     Reason: %s\n' "$(printf '%s' "$probe_err" | head -n 1)" >&2
    printf '     Install: sudo apt install python3-pyqt6.qtwebengine\n' >&2
  fi
fi

exec "$CHROME" "$@"
