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
# Newest backup of a file, empty when there is none. The `|| true` is required:
# under `set -o pipefail` a failing `ls` (no match) would otherwise abort the
# script through the enclosing assignment.
latest_backup() {
  ls -t "$1".bak.* 2>/dev/null | head -n1 || true
}
# Validate a profile patch. 0 = valid (or no validator available), 1 = broken.
patch_is_valid() {
  local checker="$REPO_DIR/bin/dsh-yaml-check.py"
  if [ ! -f "$checker" ]; then
    say "! YAML validator not found ($checker); skipping the check"
    return 0
  fi
  "$PYTHON" "$checker" "$1"
}

PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  echo "ERROR: python3 not found." >&2
  exit 1
fi
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
if [ -L "$DESKTOP_DIR/dsh-desktop.desktop" ] || [ -f "$DESKTOP_DIR/dsh-desktop.desktop" ]; then
  run rm -f "$DESKTOP_DIR/dsh-desktop.desktop" 2>/dev/null || true
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
    # Atomic rewrite: an interrupted write must not truncate the profile
    # manifest, which is what makes the profile boot at all.
    "$PYTHON" - "$PROFILE_DIR/package.json" <<'PY'
import json, os, sys, tempfile
p = sys.argv[1]
with open(p, encoding="utf-8") as f:
    data = json.load(f)
deps = data.get("dependencies", {})
if "dsh-notify" in deps:
    deps.pop("dsh-notify")
    if not deps:
        data.pop("dependencies", None)
    mode = os.stat(p).st_mode & 0o777
    with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(p), delete=False,
                                     encoding="utf-8") as tf:
        json.dump(data, tf, indent=2)
        tf.write("\n")
        tmp = tf.name
    os.chmod(tmp, mode)
    os.replace(tmp, p)
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
      cp -p "$PATCH" "$PATCH.bak.$(date +%Y%m%d-%H%M%S)"
      # Atomic rewrite that keeps the original mode. The notify entry is removed
      # whole — its id line plus every deeper-indented line belonging to it
      # (`name`, and the `config:` block the settings page writes) — and an
      # `- insert:` line is dropped only when nothing is left under it. Removing
      # just the first two lines used to leave orphan rows whenever the entry
      # carried configuration, and orphan rows make the patch unparseable, which
      # is fatal for the whole profile.
      "$PYTHON" - "$PATCH" <<'PY'
import os, re, sys, tempfile
p = sys.argv[1]
with open(p, encoding="utf-8") as f:
    text = f.read()

# 1) The notify entry and everything nested under it.
lines = text.split("\n")
kept = []
index = 0
while index < len(lines):
    line = lines[index]
    if line.strip().startswith("- id: notify"):
        indent = len(line) - len(line.lstrip())
        index += 1
        while index < len(lines):
            follower = lines[index]
            if not follower.strip():
                index += 1
                continue
            if (len(follower) - len(follower.lstrip())) > indent:
                index += 1
                continue
            break
        continue
    kept.append(line)
    index += 1
text = "\n".join(kept)
text = re.sub(r"(?m)^# dsh-notify[^\n]*\n", "", text)

# 2) An `- insert:` line that no longer has any child row.
lines = text.split("\n")
kept = []
for index, line in enumerate(lines):
    if line.strip() != "- insert:":
        kept.append(line)
        continue
    indent = len(line) - len(line.lstrip())
    has_child = False
    for follower in lines[index + 1:]:
        if not follower.strip() or follower.lstrip().startswith("#"):
            continue
        has_child = (len(follower) - len(follower.lstrip())) > indent
        break
    if has_child:
        kept.append(line)
text = "\n".join(kept)

mode = os.stat(p).st_mode & 0o777
with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(p), delete=False,
                                 encoding="utf-8") as tf:
    tf.write(text)
    tmp = tf.name
os.chmod(tmp, mode)
os.replace(tmp, p)
print("  cordis.patch.yml updated")
PY
      if grep -q "id: notify" "$PATCH"; then
        say "! the notify entry could not be removed automatically; edit by hand:"
        say "  $PATCH"
      fi
      # A patch that does not parse is fatal for the whole profile, so the edit
      # is validated (the checker accepts DSH's `!!js` tags) and rolled back on
      # failure — install.sh has always done this, this script did not.
      if ! patch_is_valid "$PATCH"; then
        LATEST_BAK="$(latest_backup "$PATCH")"
        if [ -n "$LATEST_BAK" ]; then
          cp -p "$LATEST_BAK" "$PATCH"
          say "! the edited cordis.patch.yml did not validate; restored the backup"
        else
          say "! the edited cordis.patch.yml did not validate and no backup was found"
          say "  Repair it by hand before starting DSH: $PATCH"
        fi
      fi
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
  run rm -rf "$PROFILE_DIR"/cordis.patch.yml.bak.* 2>/dev/null || true
  run rm -rf "$HOME/.cache/dsh-update" 2>/dev/null || true
  run rm -rf "$DATA_DIR/dsh-tray" 2>/dev/null || true
  run rm -rf "$DATA_DIR/dsh-app" 2>/dev/null || true
  run rm -f "$CONF_DIR/dsh/proxy.env" 2>/dev/null || true
  [ -d "$CONF_DIR/dsh" ] && rmdir "$CONF_DIR/dsh" 2>/dev/null || true
  say "local state, proxy configuration, and backups removed"
  say "(versions, sessions and the profile were preserved)"
fi

step "Done"
say "DSH itself and your session data were preserved."
exit 0
