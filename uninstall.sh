#!/usr/bin/env bash
# DSH Desktop Shell — uninstaller.
#
# Reverses everything the installer did:
#   - stops and removes the systemd units
#   - removes the scripts, icons and desktop entries
#   - unlinks the notification plugin from the profile
#
# It does NOT touch user data (versions, sessions, profiles, caches).
#
# Options:
#   --keep-plugin   leave the plugin link in the profile
#   --purge         also remove backups and local state
#   --dry-run       print what would happen; change nothing

set -euo pipefail

BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="$CONF_DIR/systemd/user"
APP_DIR="$DATA_DIR/applications"
ICON_DIR="$DATA_DIR/icons"
PROFILE_DIR="${DSH_HOME:-$HOME/.dsh}/profiles/web"

KEEP_PLUGIN=0
PURGE=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --keep-plugin) KEEP_PLUGIN=1 ;;
    --purge)       PURGE=1 ;;
    --dry-run)     DRY_RUN=1 ;;
    -h|--help)     sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s ==\n' "$*"; }
run() {
  if [ "$DRY_RUN" = 1 ]; then say "[dry-run] $*"; else "$@"; fi
}

step "systemd units"
if command -v systemctl >/dev/null 2>&1; then
  for u in dsh-proxy.service dsh-update-check.timer dsh-update-check.service \
           dsh-web.service; do
    run systemctl --user disable --now "$u" 2>/dev/null || true
  done
  say "stopped and disabled"
fi

for u in dsh-web.service dsh-proxy.service dsh-update-check.service \
         dsh-update-check.timer; do
  if [ -f "$UNIT_DIR/$u" ]; then
    run rm -f "$UNIT_DIR/$u"
    say "removed: $u"
  fi
done
if command -v systemctl >/dev/null 2>&1; then
  run systemctl --user daemon-reload 2>/dev/null || true
  run systemctl --user reset-failed 2>/dev/null || true
fi

step "Scripts"
for f in dsh-tray.py dsh-update.py dsh-update dsh-desktop-launch.sh \
         dsh-app-window.sh dsh-launcher.sh dsh-tailscale-proxy.mjs; do
  if [ -f "$BIN_DIR/$f" ]; then
    run rm -f "$BIN_DIR/$f"
    say "removed: $f"
  fi
done

step "Desktop entries and icons"
# Both the current names and the legacy names from older releases are cleaned.
for f in dsh-desktop.desktop dsh-desktop-browser.desktop \
         dsh-desktop-chrome.desktop deepseek-harness-web.desktop; do
  [ -f "$APP_DIR/$f" ] && { run rm -f "$APP_DIR/$f"; say "removed: $f"; }
done

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -L "$DESKTOP_DIR/dsh-desktop.desktop" ]; then
  run rm -f "$DESKTOP_DIR/dsh-desktop.desktop"
  say "shortcut removed"
fi

run rm -f "$ICON_DIR/dsh-desktop.png" "$ICON_DIR/dsh-desktop.svg"
for sz in 32x32 48x48 64x64 128x128 256x256; do
  f="$ICON_DIR/hicolor/$sz/apps/dsh-desktop.png"
  [ -f "$f" ] && run rm -f "$f"
done
say "icons removed"

if command -v update-desktop-database >/dev/null 2>&1; then
  run update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

step "Notification plugin"
if [ "$KEEP_PLUGIN" = 1 ]; then
  say "skipped (--keep-plugin)"
elif [ -f "$PROFILE_DIR/package.json" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    say "[dry-run] the plugin link would be removed from the profile"
  else
    python3 - "$PROFILE_DIR/package.json" <<'PY'
import json, sys
p = sys.argv[1]
with open(p) as f:
    data = json.load(f)
deps = data.get("dependencies", {})
if "dsh-notify" in deps:
    deps.pop("dsh-notify")
    if not deps:
        data.pop("dependencies", None)
    with open(p, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print("  package.json updated")
else:
    print("  link already absent")
PY
  fi
  PATCH="$PROFILE_DIR/cordis.patch.yml"
  if [ -f "$PATCH" ] && grep -q "id: notify" "$PATCH"; then
    if [ "$DRY_RUN" = 1 ]; then
      say "[dry-run] the notify entry would be removed from cordis.patch.yml"
    else
      cp "$PATCH" "$PATCH.bak.$(date +%Y%m%d-%H%M%S)"
      python3 - "$PATCH" <<'PY'
import re, sys
p = sys.argv[1]
text = open(p).read()
# Remove the notify rows inside the insert block, and the block itself when empty.
text = re.sub(r"# dsh-notify[^\n]*\n", "", text)
text = re.sub(r"- insert:\n(?:[ \t]+- id: notify\n[ \t]+name: 'dsh-notify'\n)+", "", text)
open(p, "w").write(text)
print("  cordis.patch.yml updated")
PY
    fi
  fi
else
  say "profile not found; skipped"
fi

if [ "$KEEP_PLUGIN" = 0 ] && [ -d "$DATA_DIR/dsh-desktop" ]; then
  run rm -rf "$DATA_DIR/dsh-desktop"
  say "removed: $DATA_DIR/dsh-desktop"
fi

if [ "$PURGE" = 1 ]; then
  step "Extra cleanup (--purge)"
  run rm -rf "$PROFILE_DIR"/cordis.patch.yml.bak.*
  run rm -rf "$HOME/.cache/dsh-update"
  run rm -rf "$DATA_DIR/dsh-tray"
  say "local state and backups removed"
  say "(versions, sessions and the profile were preserved)"
fi

step "Done"
say "DSH itself and your session data were preserved."
exit 0
