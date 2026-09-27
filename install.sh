#!/usr/bin/env bash
# DSH Desktop Shell — installer.
#
# What it does:
#   1. Copies the scripts into ~/.local/bin
#   2. Installs desktop entries and icons into the XDG directories
#   3. Renders and enables the systemd --user units from templates
#   4. Links the notification plugin into the DSH web profile (optional)
#
# Use --dry-run to see each step without changing anything.
#
# Options:
#   --no-services     skip the systemd units
#   --no-plugin       skip the notification plugin
#   --with-proxy      also enable the remote-access proxy
#   --dry-run         print what would happen; change nothing
#   -h, --help        this help

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="$CONF_DIR/systemd/user"
APP_DIR="$DATA_DIR/applications"
ICON_DIR="$DATA_DIR/icons"

WITH_SERVICES=1
WITH_PLUGIN=1
WITH_PROXY=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --no-services) WITH_SERVICES=0 ;;
    --no-plugin)   WITH_PLUGIN=0 ;;
    --with-proxy)  WITH_PROXY=1 ;;
    --dry-run)     DRY_RUN=1 ;;
    -h|--help)     sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s ==\n' "$*"; }
run() {
  if [ "$DRY_RUN" = 1 ]; then say "[dry-run] $*"; else "$@"; fi
}

# --------------------------------------------------------------------------- #
# 0) Check prerequisites
# --------------------------------------------------------------------------- #
step "Prerequisites"

NODE=""
for cand in "${DSH_NODE:-}" "$(command -v node || true)" \
            /usr/bin/node /usr/local/bin/node "$HOME/.local/bin/node"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then
    # Verify that the binary is a real Node.js with V8 engine (not Bun masquerading as node)
    if "$cand" -e 'process.exit(process.versions.v8 ? 0 : 1)' >/dev/null 2>&1; then
      NODE="$cand"
      break
    fi
  fi
done

if [ -z "$NODE" ]; then
  RAW_NODE="$(command -v node || true)"
  if [ -n "$RAW_NODE" ]; then
    echo "ERROR: $RAW_NODE was found, but it is not a standard Node.js runtime with V8 (e.g. Bun or wrapper)." >&2
    echo "       DeepSeek Harness native modules require standard Node.js (v20+ with V8 engine)." >&2
    echo "       Install Node.js: sudo apt install nodejs (or via nvm / fnm) or set DSH_NODE=/path/to/node." >&2
  else
    echo "ERROR: Node.js with V8 engine not found. Set DSH_NODE to its absolute path." >&2
    echo "       Install Node.js: sudo apt install nodejs (or via nvm / fnm)." >&2
  fi
  exit 1
fi
say "node        : $NODE"

# An explicit setting (DSH_BIN) always wins over auto-detection: the user knows
# which installation to use, a heuristic search does not. That is why the
# environment variable is NOT reset here.
if [ -n "${DSH_BIN:-}" ] && [ -f "${DSH_BIN}" ]; then
  say "dsh entry   : $DSH_BIN (DSH_BIN)"
else
  DSH_BIN=""
  for cand in \
    "${BUN_INSTALL:-$HOME/.bun}/install/global/node_modules/@deepseek-ai/dsh/lib/bin.js" \
    "$HOME/.local/share/pnpm/global/5/node_modules/@deepseek-ai/dsh/lib/bin.js" \
    /usr/lib/node_modules/@deepseek-ai/dsh/lib/bin.js \
    /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js
  do
    [ -f "$cand" ] && { DSH_BIN="$cand"; break; }
  done
  if [ -z "$DSH_BIN" ]; then
    echo "WARNING: no @deepseek-ai/dsh installation found." >&2
    echo "         Install it: bun add -g @deepseek-ai/dsh" >&2
    echo "         Or give the absolute path: DSH_BIN=/path/lib/bin.js ./install.sh" >&2
    [ "$WITH_SERVICES" = 1 ] && exit 1
  fi
  say "dsh entry   : $DSH_BIN"
fi

PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  echo "ERROR: python3 not found." >&2
  exit 1
fi
say "python3     : $PYTHON"

# Detect the desktop environment and session type. Nothing here changes which
# code is installed -- the scripts are environment agnostic and probe the tray
# and the session bus at runtime. This is purely so the installer can warn
# about the one thing that genuinely differs: whether a system tray exists.
DESKTOP="${XDG_CURRENT_DESKTOP:-unknown}"
SESSION_TYPE="${XDG_SESSION_TYPE:-unknown}"
say "desktop     : $DESKTOP ($SESSION_TYPE)"

# PyQt6 WebEngine is recommended for the tray shell (not required: falls back to Chrome).
if "$PYTHON" -c 'from PyQt6.QtWebEngineWidgets import QWebEngineView' 2>/dev/null; then
  say "PyQt6 WebEngine: present (tray shell enabled)"
else
  say "PyQt6 WebEngine: MISSING - will fall back to a Chrome window"
  say "                 Install: sudo apt install python3-pyqt6.qtwebengine"
fi

# Warn when the tray will not be usable, with advice for this desktop. The
# shell still runs; only "hide to tray" is lost.
DESKTOP_UC="$(printf '%s' "$DESKTOP" | tr '[:lower:]' '[:upper:]')"
case "$DESKTOP_UC" in
  *GNOME*)
    say "note        : GNOME 3.26+ ships no tray. For the tray icon install:"
    say "              sudo apt install gnome-shell-extension-appindicator"
    ;;
  *XFCE*)
    say "note        : on XFCE add the 'Status Tray Plugin' to a panel for the tray icon."
    ;;
  *MATE*)
    say "note        : on MATE the tray needs the 'Notification Area' panel applet."
    ;;
  *CINNAMON*)
    say "note        : Cinnamon ships a tray by default; if missing, enable the"
    say "              'XApp Status Applet' in Panel settings."
    ;;
  *)
    if [ "$SESSION_TYPE" = "wayland" ]; then
      say "note        : Wayland sessions need a StatusNotifier host for the tray icon"
      say "              (GNOME: AppIndicator extension; wlroots: waybar's tray module)."
    fi
    ;;
esac

# --------------------------------------------------------------------------- #
# 1) Scripts
# --------------------------------------------------------------------------- #
step "Scripts -> $BIN_DIR"
run mkdir -p "$BIN_DIR"
for f in dsh-tray.py dsh-update.py dsh-update dsh-desktop-launch.sh \
         dsh-app-window.sh dsh-launcher.sh dsh-tailscale-proxy.mjs; do
  run install -m 0755 "$REPO_DIR/bin/$f" "$BIN_DIR/$f"
  say "$f"
done

# --------------------------------------------------------------------------- #
# 2) Icons and desktop entries
# --------------------------------------------------------------------------- #
step "Icons -> $ICON_DIR"
# Create the directory if missing: a fresh account may not have
# ~/.local/share/icons yet, and `install` fails when the target directory
# does not exist.
run mkdir -p "$ICON_DIR"
run install -m 0644 "$REPO_DIR/icons/dsh-desktop.png" "$ICON_DIR/dsh-desktop.png"
run install -m 0644 "$REPO_DIR/icons/dsh-desktop.svg" "$ICON_DIR/dsh-desktop.svg"
for sz in 32x32 48x48 64x64 128x128 256x256; do
  src="$REPO_DIR/icons/hicolor/$sz/apps/dsh-desktop.png"
  if [ -f "$src" ]; then
    run mkdir -p "$ICON_DIR/hicolor/$sz/apps"
    run install -m 0644 "$src" "$ICON_DIR/hicolor/$sz/apps/dsh-desktop.png"
    say "hicolor/$sz"
  fi
done

step "Desktop entries -> $APP_DIR"
run mkdir -p "$APP_DIR"
for f in "$REPO_DIR"/applications/*.desktop; do
  name="$(basename "$f")"
  # Substitute the Exec/Icon paths with this installation's real paths.
  if [ "$DRY_RUN" = 1 ]; then
    say "[dry-run] $name (Exec paths will be written for $BIN_DIR)"
  else
    sed -e "s|@BIN_DIR@|$BIN_DIR|g" \
        -e "s|@ICON_DIR@|$ICON_DIR|g" \
        "$f" > "$APP_DIR/$name"
    chmod 0644 "$APP_DIR/$name"
    say "$name"
  fi
done

# Desktop shortcut (a symlink in the desktop directory, when it exists).
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ] && [ -f "$APP_DIR/dsh-desktop.desktop" ]; then
  run ln -sf "$APP_DIR/dsh-desktop.desktop" "$DESKTOP_DIR/dsh-desktop.desktop"
  say "shortcut: $DESKTOP_DIR/dsh-desktop.desktop"
fi

# Refresh the menu database.
if command -v update-desktop-database >/dev/null 2>&1; then
  run update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -d "$ICON_DIR/hicolor" ]; then
  run gtk-update-icon-cache -f -t "$ICON_DIR/hicolor" >/dev/null 2>&1 || true
fi

# --------------------------------------------------------------------------- #
# 3) systemd units
# --------------------------------------------------------------------------- #
if [ "$WITH_SERVICES" = 1 ]; then
  step "systemd units -> $UNIT_DIR"
  run mkdir -p "$UNIT_DIR"

  for unit in dsh-web.service dsh-proxy.service dsh-update-check.service; do
    if [ "$DRY_RUN" = 1 ]; then
      say "[dry-run] $unit (template will be rendered)"
    else
      sed -e "s|__NODE__|$NODE|g" \
          -e "s|__DSH_BIN__|$DSH_BIN|g" \
          -e "s|__PYTHON__|$PYTHON|g" \
          -e "s|__BIN_DIR__|$BIN_DIR|g" \
          -e "s|@BIN_DIR@|$BIN_DIR|g" \
          "$REPO_DIR/units/$unit" > "$UNIT_DIR/$unit"
      chmod 0644 "$UNIT_DIR/$unit"
      say "$unit"
    fi
  done
  run install -m 0644 "$REPO_DIR/units/dsh-update-check.timer" "$UNIT_DIR/dsh-update-check.timer"
  say "dsh-update-check.timer"

  if [ "$DRY_RUN" = 0 ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable --now dsh-web.service 2>/dev/null \
      && say "dsh-web.service enabled" \
      || say "! could not enable dsh-web.service (normal without a session bus)"
    systemctl --user enable --now dsh-update-check.timer 2>/dev/null \
      && say "dsh-update-check.timer enabled" \
      || say "! could not enable the timer"

    if [ "$WITH_PROXY" = 1 ]; then
      mkdir -p "$CONF_DIR/dsh"
      if [ ! -f "$CONF_DIR/dsh/proxy.env" ]; then
        TOKEN="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
        printf 'DSH_PROXY_TOKEN=%s\n' "$TOKEN" > "$CONF_DIR/dsh/proxy.env"
        chmod 0600 "$CONF_DIR/dsh/proxy.env"
        say "proxy token generated: $CONF_DIR/dsh/proxy.env"
        say "For Tailscale, set DSH_PROXY_BIND to your tailscale IP."
      fi
      systemctl --user enable --now dsh-proxy.service 2>/dev/null \
        && say "dsh-proxy.service enabled" \
        || say "! could not enable the proxy"
    fi
  fi
else
  step "systemd units skipped (--no-services)"
fi

# --------------------------------------------------------------------------- #
# 4) Notification plugin
# --------------------------------------------------------------------------- #
if [ "$WITH_PLUGIN" = 1 ]; then
  step "Notification plugin (dsh-notify)"
  PROFILE_DIR="${DSH_HOME:-$HOME/.dsh}/profiles/web"
  PLUGIN_INSTALL_DIR="$DATA_DIR/dsh-desktop/notify"
  run mkdir -p "$DATA_DIR/dsh-desktop"
  if [ "$DRY_RUN" = 1 ]; then
    say "[dry-run] notify plugin would be copied to $PLUGIN_INSTALL_DIR"
  else
    rm -rf "$PLUGIN_INSTALL_DIR"
    cp -a "$REPO_DIR/notify" "$PLUGIN_INSTALL_DIR"
    say "plugin copied -> $PLUGIN_INSTALL_DIR"
  fi
  PLUGIN_DIR="$PLUGIN_INSTALL_DIR"

  if [ ! -d "$PROFILE_DIR" ]; then
    say "! web profile not found: $PROFILE_DIR"
    say "  Run DSH once to create the profile, then repeat this step."
  elif [ "$DRY_RUN" = 1 ]; then
    say "[dry-run] the plugin would be added to the profile"
  else
    # Install the plugin's dependencies FIRST. Without node_modules the host
    # half cannot import `@deepseek-ai/schemastery`; DSH then logs
    # "failed to import" and the profile fails to boot. So the profile is only
    # touched after this succeeds.
    PLUGIN_DEPS_OK=0
    if command -v pnpm >/dev/null 2>&1; then
      # pnpm may need node on PATH (its native binary is not always installed).
      # Pass the node we already resolved so the install works in a clean
      # desktop session too.
      PNPM_PATH="$(dirname "$NODE"):$PATH"
      if (cd "$PLUGIN_DIR" && PATH="$PNPM_PATH" pnpm install --silent 2>/dev/null); then
        PLUGIN_DEPS_OK=1
        say "plugin dependencies installed"
      else
        say "! pnpm install failed; run manually: cd $PLUGIN_DIR && pnpm install"
      fi
    else
      say "! pnpm not found (required for the plugin dependencies)"
      say "  Install it: bun add -g pnpm"
    fi

    if [ "$PLUGIN_DEPS_OK" != 1 ]; then
      say "! plugin NOT linked: dependencies are missing."
      say "  Fix with: cd $PLUGIN_DIR && pnpm install && ./install.sh"
    else
      remove_plugin_from_manifest() {
        "$PYTHON" - "$PROFILE_DIR/package.json" <<'PY'
import json, os, sys, tempfile
p = sys.argv[1]
try:
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    deps = data.get("dependencies", {})
    if "dsh-notify" in deps:
        deps.pop("dsh-notify", None)
        if not deps:
            data.pop("dependencies", None)
        dir_name = os.path.dirname(p)
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
            json.dump(data, tf, indent=2)
            tf.write("\n")
            temp_name = tf.name
        os.replace(temp_name, p)
except Exception as e:
    sys.stderr.write(f"Failed to remove dsh-notify from package.json: {e}\n")
PY
      }

      # 1) Link the plugin into the profile manifest atomically.
      "$PYTHON" - "$PROFILE_DIR/package.json" "$PLUGIN_DIR" <<'PY'
import json, os, sys, tempfile
manifest, plugin_dir = sys.argv[1], sys.argv[2]
with open(manifest, "r", encoding="utf-8") as f:
    data = json.load(f)
data.setdefault("dependencies", {})["dsh-notify"] = f"link:{plugin_dir}"
dir_name = os.path.dirname(manifest)
with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
    json.dump(data, tf, indent=2)
    tf.write("\n")
    temp_name = tf.name
os.replace(temp_name, manifest)
print("  package.json updated")
PY

      # 2) Add the insert block to cordis.patch.yml.
      #
      # Important: in most profiles the file root is an empty list (`[]`).
      # Prepending the insert block produces two root values and invalid YAML
      # ("could not find expected ':'"). So the empty root is dropped.
      PATCH="$PROFILE_DIR/cordis.patch.yml"
      if [ ! -f "$PATCH" ]; then
        if [ "$DRY_RUN" = 1 ]; then
          say "[dry-run] cordis.patch.yml would be created with dsh-notify"
        else
          printf -- "# dsh-notify - reply completion / question / error notifications\n- insert:\n    - id: notify\n      name: 'dsh-notify'\n" > "$PATCH"
          say "cordis.patch.yml created with dsh-notify"
        fi
      elif ! grep -q "id: notify" "$PATCH"; then
        cp "$PATCH" "$PATCH.bak.$(date +%Y%m%d-%H%M%S)"
        "$PYTHON" - "$PATCH" <<'PY'
import sys
p = sys.argv[1]
lines = [ln for ln in open(p).read().splitlines() if ln.strip() != "[]"]
body = "\n".join(lines).strip()
entry = (
    "# dsh-notify - reply completion / question / error notifications\n"
    "- insert:\n"
    "    - id: notify\n"
    "      name: 'dsh-notify'\n"
)
open(p, "w").write(entry + ("\n" + body + "\n" if body else ""))
PY
        # Validate the result using safe standard-library or non-PyYAML parser; roll back if we broke it.
        if "$PYTHON" - "$PATCH" <<'PY'
import sys
p = sys.argv[1]

# Try PyYAML if present
try:
    import yaml
    with open(p, "r", encoding="utf-8") as f:
        yaml.safe_load(f)
    sys.exit(0)
except ImportError:
    pass
except Exception:
    sys.exit(1)

# Safe standard-library YAML structure validator
try:
    with open(p, "r", encoding="utf-8") as f:
        text = f.read()

    lines = text.splitlines()
    in_single = False
    in_double = False
    escape = False
    stack = []

    for idx, line in enumerate(lines, 1):
        leading_ws = line[:len(line) - len(line.lstrip())]
        if "\t" in leading_ws:
            sys.exit(1)

        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue

        for ch in line:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                if ch == "#":
                    break
                if ch in "([{":
                    stack.append(ch)
                elif ch in ")]}":
                    if not stack:
                        sys.exit(1)
                    top = stack.pop()
                    if (top == "(" and ch != ")") or (top == "[" and ch != "]") or (top == "{" and ch != "}"):
                        sys.exit(1)
        escape = False

    if in_single or in_double or stack:
        sys.exit(1)
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
        then
          :  # valid YAML
        else
          LATEST_BAK="$(ls -t "$PATCH".bak.* 2>/dev/null | head -n1)"
          [ -n "$LATEST_BAK" ] && cp "$LATEST_BAK" "$PATCH"
          remove_plugin_from_manifest
          say "! invalid YAML generated; restored cordis.patch.yml and rolled back package.json"
          PLUGIN_DEPS_OK=0
        fi
      else
        say "cordis.patch.yml already linked"
      fi

      # 3) Verify the plugin really imports from the profile's perspective.
      if [ "$PLUGIN_DEPS_OK" = 1 ]; then
        if (cd "$PROFILE_DIR" && "$NODE" -e "import('dsh-notify')" >/dev/null 2>&1); then
          say "plugin imports cleanly"
        else
          say "! plugin does not import; removing it from the profile"
          remove_plugin_from_manifest
          LATEST_BAK="$(ls -t "$PATCH".bak.* 2>/dev/null | head -n1)"
          [ -n "$LATEST_BAK" ] && cp "$LATEST_BAK" "$PATCH"
          say "  Profile restored. Check: cd $PLUGIN_DIR && pnpm install"
        fi
      fi
      say "Restart DSH for it to take effect (service restart or page reload)."
    fi
  fi
else
  step "Notification plugin skipped (--no-plugin)"
fi

# --------------------------------------------------------------------------- #
step "Done"
say "You can now start it from the desktop menu as 'DSH Desktop'."
[ "$DRY_RUN" = 1 ] && say "(dry-run: nothing was changed)"
exit 0
