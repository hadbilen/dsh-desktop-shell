#!/usr/bin/env bash
# Open DeepSeek Harness in its own application window using an ISOLATED Chrome
# profile. (No browser bar or tabs; cookies, history and sessions are not
# shared with your main browser profile, so it behaves like a separate app.)
#
# Plain flow:
#   - Normal start : the plain URL is opened; the journal and token search are
#                    never touched.
#   - First run    : the isolated profile has no session cookie yet, so the
#                    launch token the service writes to the journal is located
#                    and appended to the address.
#   - Cookie life  : the DSH cookie is valid for about 30 days; if the marker
#                    goes stale before that, the token is tried once more.
#   - No token     : falls back to the plain URL and reports the reason; nothing
#                    breaks.
#
# Configurable:
#   DSH_WEB_URL      address     (default http://127.0.0.1:3080)
#   DSH_APP_PROFILE  profile dir (default ~/.local/share/dsh-app)
#   DSH_CHROME       browser     (default google-chrome)
#
# If the session drops (401 screen): rm ~/.local/share/dsh-app/.dsh-bootstrapped
# and click the shortcut again.

set -u

URL="${DSH_WEB_URL:-http://127.0.0.1:3080}"
PROFILE="${DSH_APP_PROFILE:-$HOME/.local/share/dsh-app}"
find_browser() {
  if [ -n "${DSH_CHROME:-}" ] && command -v "$DSH_CHROME" >/dev/null 2>&1; then
    printf '%s' "$DSH_CHROME"
    return 0
  fi
  for cand in google-chrome google-chrome-stable chromium chromium-browser brave-browser brave microsoft-edge-stable microsoft-edge; do
    if command -v "$cand" >/dev/null 2>&1; then
      printf '%s' "$cand"
      return 0
    fi
  done
  return 1
}

if ! CHROME="$(find_browser)"; then
  cat >&2 <<EOF
dsh: no supported Chromium-based browser found.
  Install Chrome, Chromium, or Brave, or set DSH_CHROME to your browser executable.
  Example: sudo apt install chromium-browser
EOF
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -a DSH -u critical "DSH: browser not found" \
      "Install Chrome, Chromium, or Brave, or set DSH_CHROME." 2>/dev/null || true
  fi
  exit 1
fi

MARKER="$PROFILE/.dsh-bootstrapped"
REBOOTSTRAP_DAYS=25   # DSH cookie lasts 30 days; 5 days of safety margin

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$1" 2>/dev/null; }

# Is the service responding? A 401 also means "up".
up() {
  local c
  c=$(http_code "$URL/")
  [ -n "$c" ] && [ "$c" != "000" ]
}

for _ in $(seq 1 20); do
  up && break
  sleep 0.5
done

if ! up; then
  systemctl --user start dsh-web.service 2>/dev/null
  for _ in $(seq 1 40); do
    up && break
    sleep 0.5
  done
fi

# If the service is still not responding, do not silently open an empty window:
# tell the user what happened and where to look. (Previously Chrome opened a
# blank/401 window with no explanation at all.)
if ! up; then
  cat >&2 <<EOF
dsh: the DSH Web service is not responding ($URL).

  Status : systemctl --user status dsh-web.service
  Log    : journalctl --user -u dsh-web.service -n 50
  Start  : systemctl --user start dsh-web.service

Click this shortcut again once the service is up.
EOF
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -a DSH -u critical "DSH failed to start" \
      "dsh-web.service is not responding. Details: journalctl --user -u dsh-web.service -n 50" \
      2>/dev/null || true
  fi
fi

# Is bootstrapping needed? Yes when the profile was never set up, or the marker
# has gone stale.
needs_bootstrap() {
  [ -f "$MARKER" ] || return 0
  [ -n "$(find "$MARKER" -mtime +"$REBOOTSTRAP_DAYS" 2>/dev/null)" ]
}

# Find the current launch token. Tokens from earlier starts are invalid, so
# every candidate is verified individually. If none works the caller falls back
# to the plain URL and reports why.
find_token() {
  local cand code
  # Note: journalctl's `-g` filter behaves differently across versions when
  # combined with `-n`, and it is tied to the exact upstream log text. So the
  # raw records are fetched and filtered here. The NEWEST token is tried first.
  while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    code=$(http_code "$URL/?token=$cand")
    case "$code" in
      ''|000|401) continue ;;
    esac
    printf '%s' "$cand"
    return 0
  done < <(journalctl --user -u dsh-web.service --no-pager -n 200 --output=cat 2>/dev/null \
           | LC_ALL=C grep 'dsh web:' \
           | grep -oE '[?&]token=[A-Za-z0-9_.-]+' \
           | sed 's/.*token=//' \
           | tac | awk '!seen[$0]++')
  return 1
}

TARGET="$URL/"
BOOTSTRAPPED=0

if needs_bootstrap; then
  if TOKEN=$(find_token); then
    TARGET="$URL/?token=$TOKEN"
    BOOTSTRAPPED=1
  else
    printf 'dsh: no session token found; trying the plain URL (expect a 401).\n' >&2
    printf '     Log   : journalctl --user -u dsh-web.service -n 50\n' >&2
    printf '     Fix   : systemctl --user restart dsh-web.service\n' >&2
  fi
fi

mkdir -p "$PROFILE" 2>/dev/null
if [ "$BOOTSTRAPPED" = 1 ]; then
  : > "$MARKER" 2>/dev/null
fi

# Window start mode (see dsh-desktop-launch.sh). Maximizing a Chrome app window
# is best-effort; the real shell is QtWebEngine.
WINDOW_ARGS=()
case "${DSH_TRAY_WINDOW:-maximized}" in
  fullscreen) WINDOW_ARGS+=(--start-fullscreen) ;;
  normal)     ;;
  *)          WINDOW_ARGS+=(--start-maximized) ;;
esac

exec "$CHROME" \
  --app="$TARGET" \
  --class=DSH-Desktop \
  --user-data-dir="$PROFILE" \
  --password-store=basic \
  --no-first-run \
  --no-default-browser-check \
  "${WINDOW_ARGS[@]}" \
  "$@"
