# DSH Desktop Shell

Tray-capable desktop shell, desktop shortcut, and update tooling for
**[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)** (DSH).

Built on DeepSeek Harness. Not affiliated with or endorsed by DeepSeek.

---

## What it does

DSH ships as a browser UI. This project makes it behave like a desktop
application on Linux:

| Feature | What you get |
|---|---|
| **Desktop shortcut** | A launcher entry with icon — no terminal needed |
| **Tray shell** | QtWebEngine window; closing it (X) hides to the tray instead of quitting |
| **Tray menu** | Show/hide, reload, service status, in-app updates, Tailscale remote toggle & link copy, real quit |
| **Session restore** | The shortcut reopens the last session; `DSH_NEW_CHAT=1` (or `--new-chat`) starts a clean prompt session with autofocus instead. The clean-session switch needs the tray shell — the Chrome fallback window always resumes |
| **systemd service** | `dsh-web.service` keeps DSH running independently of any window |
| **Desktop notifications** | System notifications for turn, question, and error events |
| **In-app updates** | One-click update check and apply directly from the GUI or notification, with live log stream and auto-reload |
| **Update CLI** | `dsh-update` — npm + GitHub release channels, automatic backup & rollback, version cleanup |
| **Tailscale remote access** | On-demand authenticated reverse proxy with one-click tray toggle and link sharing |

### Desktop notifications

The `dsh-notify` plugin (in `notify/`) sends a system notification when:

- **A reply completes** — the agent finishes a turn
- **Input is needed** — the agent asks a question or waits for approval
- **A run fails** — the agent ends with an error

Notifications are suppressed while the window is focused, and subagent
sessions are excluded by default so a long task does not spam you. Clicking a
notification brings the window to the front.

The toggles are edited on the **General settings page**, in a **Notifications** row
with one switch per preference. (DSH 0.2 renders no generic form for a plugin
namespace, so the plugin draws its own row — the same way DSH's own Appearance and
session-log rows work.) A change takes effect immediately, without reloading the page.
Until a value is stored, the default below stays in force:

| Setting | Default | Meaning |
|---|---|---|
| `onComplete` | `true` | Notify when a reply completes |
| `onQuestion` | `true` | Notify when input is needed |
| `onError` | `true` | Notify on failure |
| `onlyWhenHidden` | `true` | Stay quiet while the window is focused |
| `includeSubagents` | `false` | Also notify for subagent sessions |
| `sound` | `false` | Play the system notification sound |

Stored values live in the profile patch
(`~/.dsh/profiles/web/cordis.patch.yml`) as the entry's `config:` block — in DSH 0.2
that file *is* the settings document. Editing it by hand works but needs a service
restart; the settings row applies changes live. Settings are persisted per profile
while the page is served over loopback: a browser that reaches DSH through the optional
Tailscale proxy gets a memory-only settings scope, so changes made from a remote device
are not written to the profile.

---

## Requirements

- Linux with a systemd **user** session
- `node` (standard Node.js v20+ with V8 engine; note that Bun masquerading as node is not supported by DSH native modules) and a DSH install (`@deepseek-ai/dsh`)
- `python3` 3.10+
- **Optional but recommended:** `python3-pyqt6.qtwebengine` for the tray shell.
  Without it the launcher falls back to a plain browser app window.
- **Optional:** `pnpm` — needed only for the notification plugin and `dsh plugin`
- **Optional:** `curl` — only the browser-window fallback (`dsh-app-window.sh`) uses
  it to probe the service before opening a window; the tray shell does not need it

```bash
sudo apt install python3-pyqt6.qtwebengine
bun add -g pnpm          # or: npm i -g pnpm
```

---

## Install

```bash
git clone https://github.com/hadbilen/dsh-desktop-shell.git
cd dsh-desktop-shell
./install.sh
```

Useful flags:

| Flag | Effect |
|---|---|
| `--dry-run` | Print what would happen; change nothing |
| `--no-services` | Skip the systemd units |
| `--no-plugin` | Skip the notification plugin |
| `--with-proxy` | Also enable the remote-access proxy (generates a token) |

Every consumer resolves the same XDG variables the installer does
(`XDG_BIN_HOME`, `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `DSH_HOME`), so a custom
layout never splits the installation across two sets of paths. The installer
prints an explicit reminder when a unit that is already running still holds the
previously loaded code: `enable --now` cannot restart it, so run
`systemctl --user restart dsh-web.service dsh-proxy.service` (restarting
`dsh-web.service` ends any active agent turn).

The installer copies files into XDG directories, fills the systemd unit
templates with your real paths, enables the services, and links the notification
plugin into your `web` profile — including installing the profile's dependency
tree (`dsh plugin --profile web install`), which is what actually materializes the
link, and validating the profile patch it edits.

### Uninstall

```bash
./uninstall.sh              # removes this project's files
./uninstall.sh --purge      # also removes local state and backups
./uninstall.sh --keep-plugin
```

Neither touches your DSH versions, sessions, or DSH profiles. `--purge` does
additionally remove this project's own state: the update logs and backups
(`~/.cache/dsh-update`), the proxy token (`~/.config/dsh/proxy.env`), and the two
browser profiles the shell creates (`~/.local/share/dsh-tray`,
`~/.local/share/dsh-app`) — removing those logs the web UI out, so you will have
to authenticate once on the next launch.

---

## The update tool

```bash
dsh-update check      # status: installed version, newest rc, stray copies
dsh-update ghcheck    # official channels only; exit 0 current / 10 update / 1 error
dsh-update plan       # describe what apply would do; changes nothing
dsh-update apply      # move to the newest version, remove other copies
dsh-update prune      # clean up without changing version
dsh-update extras     # list DSH artifacts outside the main trees
dsh-update notify     # desktop notification if an update exists (timer calls this)
dsh-update full       # timer + cleanup + newest version + verify
```

`apply` backs up the whole `@deepseek-ai` package tree before touching it and
**restores it automatically** if the install fails, so a failed update never
leaves you without DSH.

The friendly default is a `systemd --user` timer that checks twice a day and
notifies you when a new version exists.

Exit codes of `apply`: `0` = installed and clean, `2` = installed but leftover
package copies remain (a warning), `1` = failed (the previous version was restored).

Every `apply`, `prune` and `full` run writes a full log to
`~/.cache/dsh-update/logs/<command>-<timestamp>.log` (the newest five are kept,
including the complete `bun` output). `dsh-update check` prints the newest log
path and warns when a previous run left a backup behind — that is the first thing
to look at when an update did not go as expected.

### In-app updates

No terminal is required to check or install updates:
- **Interactive Tray Dialog:** Select **Check for updates** in the tray menu. If an update is available, an **Apply Update** button appears.
- **Notification Action:** Desktop notifications include a **View Update** button that opens the update dialog directly.
- **Live Output Stream:** Before updating, an explicit prompt confirms that active agent turns will stop. Once confirmed, `dsh-update apply` runs with live stdout/stderr logging, and the window automatically reloads 3 seconds after a successful update.

---

## Remote access (optional)

`bin/dsh-tailscale-proxy.mjs` exposes the loopback-only DSH UI over a private
network such as Tailscale.

### Managing remote access from the tray

No manual terminal configuration is needed:
1. Open the tray menu and check **Remote access (Tailscale)**.
2. If `~/.config/dsh/proxy.env` does not exist yet, a secure 32-byte token is automatically generated and bound to your active Tailscale IP (`tailscale ip -4`).
3. The ready-to-use URL (`http://<tailscale-ip>:3000/?token=...`) is automatically copied to your clipboard. On first visit the proxy verifies that token, reads DSH's current launch token from the service journal, exchanges it with DSH **server-side**, and returns a `303` carrying both cookies (its own `dsh_proxy_token` and DSH's session cookie) — so a browser that has never seen DSH still lands on a logged-in page. The DSH launch token never reaches the browser and never appears in a URL you can copy. The proxy's own token is percent-encoded in the link, so a token containing `+` or `/` works.
4. Click **Copy remote link** in the tray menu anytime you need the link on your phone or remote browser.
5. Unchecking the option immediately stops `dsh-proxy.service` and returns DSH to loopback-only isolation.

### Manual terminal configuration (alternative)

```bash
mkdir -p ~/.config/dsh
echo "DSH_PROXY_TOKEN=$(head -c 32 /dev/urandom | base64)" > ~/.config/dsh/proxy.env
chmod 600 ~/.config/dsh/proxy.env
# Edit the unit to set DSH_PROXY_BIND to your tailscale IP:
#   Environment=DSH_PROXY_BIND=100.x.y.z
systemctl --user enable --now dsh-proxy.service
```

Then visit `http://<tailscale-ip>:3000/?token=<your-token>`.

### Security and networking notes

- **Authentication & session persistence:** Access requires the shared secret via `Bearer <token>`, `?token=<secret>`, or the `dsh_proxy_token` cookie. The proxy strips the token parameter before forwarding requests upstream to avoid collisions with DSH's internal `processLaunchToken`, and it never forwards its own bearer token, cookie or a token-bearing `Referer` to DSH.
- **Host and Origin normalization:** The proxy normalizes incoming `Host` and `Origin` headers to the upstream loopback address so DSH accepts reverse-proxied requests and WebSocket upgrades behind authentication, while preserving the original host in `X-Forwarded-Host`.
- **Token in the URL:** the link the tray copies carries the *proxy* token in `?token=`; the proxy turns it into an HttpOnly `SameSite=Lax` cookie and strips it before forwarding upstream. The trade-off is that the token appears in the browser history and the clipboard — treat the copied link like a password. (Tailscale itself is WireGuard-encrypted, so the token is not exposed to the local network.) If the browser cannot be logged in automatically (for example the service was restarted and the journal no longer holds the launch token), the proxy falls back to forwarding the request, and DSH shows its own "authentication required" page.
- **Client safety:** The proxy deliberately does **not** inject `window.__DSH_TRANSPORT__ = { ownsHost: true }` — that upstream flag tells the DSH client it is running on the host machine, which would grant remote browsers the host's settings-write path.

---

## Files

```
bin/            scripts installed to ~/.local/bin
bin/dsh-yaml-check.py   profile patch validator used by both installers (not installed)
bin/dsh-launcher.sh     standalone CLI launcher (dsh web in a terminal; not wired
                        into any unit or desktop entry — the shortcuts use
                        dsh-desktop-launch.sh)
units/          systemd user unit templates (filled in by install.sh)
applications/   .desktop entries
icons/          app icon (png + svg + hicolor sizes)
notify/         dsh-notify plugin (host + browser halves)
install.sh      installer
uninstall.sh    uninstaller
```

Installed locations:

| From | To |
|---|---|
| `bin/*` (except `dsh-yaml-check.py`) | `~/.local/bin/` (`$XDG_BIN_HOME` when set) |
| `units/*` | `~/.config/systemd/user/` |
| `applications/*` | `~/.local/share/applications/` |
| `icons/*` | `~/.local/share/icons/` |
| `notify/` | `~/.local/share/dsh-desktop/notify` (linked by DSH `web` profile) |

---

## Configuration

All scripts read environment variables; nothing hardcodes a username.

| Variable | Used by | Default |
|---|---|---|
| `DSH_WEB_URL` | tray, browser window, proxy, updater | `http://127.0.0.1:3080` |
| `DSH_TRAY_WINDOW` | tray launcher | `maximized` (`fullscreen`/`normal`) |
| `DSH_NEW_CHAT` | tray launcher | `0` (default: restore the last session) / `1` (clean new chat) |
| `DSH_CHROME` | browser window fallback | auto-detected (Chrome, Chromium, Brave, Edge) |
| `DSH_NODE` | launcher, installer | auto-detected |
| `DSH_BIN` | launcher, installer | auto-detected |
| `DSH_HOME` | plugin install | `~/.dsh` |
| `BUN_INSTALL` | updater, installer | `~/.bun` (or PATH) |
| `DSH_PROXY_TOKEN` | proxy | empty → loopback only |
| `DSH_PROXY_BIND` | proxy | `127.0.0.1` |
| `DSH_PROXY_PORT` | proxy | `3000` |
| `DSH_PROXY_UPSTREAM_TIMEOUT_MS` | proxy | `30000` (response **headers** only; streams are never cut off) |
| `DSH_UPDATE_BUN_TIMEOUT` | updater | `900` seconds for `bun add`/`bun install` |
| `XDG_BIN_HOME` | installer, launcher, tray, updater | `~/.local/bin` |
| `XDG_DATA_HOME` | installer, tray, updater | `~/.local/share` |
| `XDG_CONFIG_HOME` | installer, tray, updater (proxy.env, units) | `~/.config` |

To change the window mode, edit `DSH_TRAY_WINDOW` in
`~/.local/bin/dsh-desktop-launch.sh`.

---

## Desktop environment support

**The project is desktop-environment agnostic.** No code path is selected by
the name of your desktop: the system tray and the D-Bus session bus are probed
at runtime, and everything else (window handling, notifications, services) uses
freedesktop and systemd interfaces that all major environments share. The
installer reads `XDG_CURRENT_DESKTOP` only to print environment-specific advice.

| Environment | Session | Tray icon | Notes |
|---|---|---|---|
| **KDE Plasma** | X11 / Wayland | ✅ built in | Works out of the box |
| **GNOME** | X11 / Wayland | ⚠️ needs extension | Install `gnome-shell-extension-appindicator` (or the [AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/)). Without it the window works; only "hide to tray" is lost |
| **XFCE** | X11 | ⚠️ needs plugin | Add the **Status Tray Plugin** to a panel |
| **MATE** | X11 | ⚠️ needs applet | Add the **Notification Area** applet |
| **Cinnamon** | X11 / Wayland | ✅ built in | Enable **XApp Status Applet** if missing |
| **Budgie** | X11 / Wayland | ✅ built in | — |
| **Sway / Hyprland / i3** | Wayland / X11 | ⚠️ needs a bar | Use a StatusNotifier host, e.g. `waybar` with the `tray` module |
| **LXQt** | X11 | ✅ built in | — |

The shell detects a missing tray and tells you exactly what to install for your
desktop, then keeps working: the window simply closes instead of hiding.

### What differs between environments, and why it does not break anything

| Concern | How it is handled |
|---|---|
| **Tray availability** | Probed with `QSystemTrayIcon.isSystemTrayAvailable()` plus a live-tray check; a watchdog restores the window if the tray vanishes mid-session |
| **Notification delivery** | `dsh-update notify` *discovers* the session bus (`DBUS_SESSION_BUS_ADDRESS`, then `$XDG_RUNTIME_DIR/bus`, then `/run/user/<uid>/bus`) instead of assuming it; a failure reports the exact reason. The update-check unit exports `XDG_RUNTIME_DIR=%t` only — the script derives the bus address from it. `dsh-web.service` is the unit that pins `DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus` |
| **Service startup** | Units bind to `default.target`/`timers.target`, which exist on every systemd install. `graphical-session.target` is deliberately avoided because XFCE/MATE/Cinnamon sessions do not always pull it in, which would leave the timer permanently inactive |
| **Desktop shortcut location** | Resolved via `xdg-user-dir DESKTOP`, falling back to `~/Desktop` |
| **Menu / icon caches** | `update-desktop-database` and `gtk-update-icon-cache` are called only when present |
| **Window matching** | `StartupWMClass` on X11; on Wayland the Qt shell sets its app id directly |
| **Password store** | `--password-store=basic` avoids a keyring stall in environments without KWallet/GNOME Keyring integration. Passwords are stored in plain text inside this project's own profile directory |

### Wayland and X11 notes

- `StartupWMClass` only works on X11; on Wayland the Qt shell sets its app id
  directly, so window matching still works for the tray shell.
- `showMaximized` state is owned by the compositor on Wayland. The tray stores
  your window *intent* separately and falls back to it, so hiding and restoring
  does not shrink your window.
- If the tray itself disappears mid-session, a watchdog brings the window back
  rather than leaving an invisible, unkillable process behind.

---

## Troubleshooting

**The window never appears / "already running"**
The tray shell holds a lock file. If a previous run is stuck:
`pkill -f dsh-tray.py`, then relaunch.

**Service startup timeout / "Starting DeepSeek Harness…"**
If `dsh-web.service` does not become reachable within 30 seconds, the shell displays an interactive diagnostics screen showing the service URL, relevant `journalctl` inspection commands, and a direct "Retry Connection" button.

**Chrome window instead of the tray shell**
PyQt6 WebEngine is missing: `sudo apt install python3-pyqt6.qtwebengine`.
The launcher now prints the exact reason when it falls back.

**401 screen / login loop**
Delete the bootstrap marker and relaunch:
`rm ~/.local/share/dsh-tray/.bootstrapped`
The shell automatically finds the launch token; if it cannot, it now says so
instead of failing silently.

**No notifications**
1. Check browser notification permission for the DSH page.
2. Confirm the plugin loaded: `dsh --profile web --dump-config | grep notify`
3. Restart DSH after installing the plugin — the client roster is built at boot.
4. Subagent-only activity does not notify by default (`includeSubagents`).

**Notifications never appear, although the switches are on**

The row says "Notifications are blocked by the browser" when the OS/browser
permission was denied. Allow notifications for the page (QtWebEngine and Chrome
each keep their own permission store), then reload. The permission is requested
when you interact with any switch in the row; the request made at attach time has
no user gesture and is usually denied.

**The Notifications row is missing from the General settings page**
The client bundle is fetched when the page loads, so reload the interface first —
close and reopen the window or tab (in the tray shell the *Reload* menu item does the
same). If a plugin update still does not arrive, clear the shell's HTTP cache; the
session cookies live in `profile/` and stay untouched:

```bash
pkill -f dsh-tray.py; sleep 1; rm -rf ~/.local/share/dsh-tray/cache
```

If the row is there but its switches are disabled, the row says why: the settings
document is still loading, or the deployment does not serve the `notify` namespace —
`dsh --profile web --dump-config | grep -A5 notify` then shows whether the entry is
composed at all.

**Update check says "up to date" but a release exists**
The tool tracks rc releases and also picks up newer stable releases. If only a
GitHub release exists and npm has not published it yet, there is nothing
installable — `dsh-update check` says so explicitly.

**An update failed, or "leftovers remain"**
Every `apply`/`prune`/`full` run writes a log to `~/.cache/dsh-update/logs/`;
`dsh-update check` prints the newest one and warns when an aborted run left a
backup behind (the next `apply` replaces and removes it). A failed run restores
the previous version automatically and prints the manual rollback command at the
end of the log. Leftovers (`exit code 2`) mean the new version is installed and
only stale package files remain — `dsh-update apply` removes them.

---

## License

MIT. See [LICENSE](LICENSE).

"DeepSeek Harness" is a trademark of DeepSeek. This project uses the
abbreviated "DSH" in its name per the official
[brand guidelines](https://github.com/deepseek-ai/deepseek-harness/blob/master/BRAND_GUIDELINES.md),
and is an independent community project.
