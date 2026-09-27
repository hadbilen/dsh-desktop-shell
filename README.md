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
| **Tray menu** | Show/hide, reload, service status, in-app update check & apply, real quit |
| **systemd service** | `dsh-web.service` keeps DSH running independently of any window |
| **Desktop notifications** | System notifications for turn, question, and error events |
| **In-app updates** | One-click update check and apply directly from the GUI or notification, with live log stream and auto-reload |
| **Update CLI** | `dsh-update` — npm + GitHub release channels, automatic backup & rollback, version cleanup |
| **Optional remote access** | Authenticated reverse proxy for Tailscale-style private networks |

### Desktop notifications

The `dsh-notify` plugin (in `notify/`) sends a system notification when:

- **A reply completes** — the agent finishes a turn
- **Input is needed** — the agent asks a question or waits for approval
- **A run fails** — the agent ends with an error

Notifications are suppressed while the window is focused, and subagent
sessions are excluded by default so a long task does not spam you. Clicking a
notification brings the window to the front.

Settings live in the DSH settings document under the `notify` namespace:

| Setting | Default | Meaning |
|---|---|---|
| `onComplete` | `true` | Notify when a reply completes |
| `onQuestion` | `true` | Notify when input is needed |
| `onError` | `true` | Notify on failure |
| `onlyWhenHidden` | `true` | Stay quiet while the window is focused |
| `includeSubagents` | `false` | Also notify for subagent sessions |
| `sound` | `false` | Play the system notification sound |

---

## Requirements

- Linux with a systemd **user** session
- `node` (standard Node.js v20+ with V8 engine; note that Bun masquerading as node is not supported by DSH native modules) and a DSH install (`@deepseek-ai/dsh`)
- `python3` 3.10+
- **Optional but recommended:** `python3-pyqt6.qtwebengine` for the tray shell.
  Without it the launcher falls back to a plain browser app window.
- **Optional:** `pnpm` — needed only for the notification plugin and `dsh plugin`

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

The installer copies files into XDG directories, fills the systemd unit
templates with your real paths, enables the services, and links the
notification plugin into your `web` profile.

### Uninstall

```bash
./uninstall.sh              # removes this project's files
./uninstall.sh --purge      # also removes local state and backups
./uninstall.sh --keep-plugin
```

Neither touches your DSH versions, sessions, or profiles.

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

### In-app updates

No terminal is required to check or install updates:
- **Interactive Tray Dialog:** Select **Check for updates** in the tray menu. If an update is available, an **Apply Update** button appears.
- **Notification Action:** Desktop notifications include a **View Update** button that opens the update dialog directly.
- **Live Output Stream:** Before updating, an explicit prompt confirms that active agent turns will stop. Once confirmed, `dsh-update apply` runs with live stdout/stderr logging, and the window automatically reloads 3 seconds after a successful update.

---

## Remote access (optional)

`bin/dsh-tailscale-proxy.mjs` exposes the loopback-only DSH UI over a private
network such as Tailscale.

**It is off by default and refuses to bind beyond loopback without a token.**
To enable it:

```bash
mkdir -p ~/.config/dsh
echo "DSH_PROXY_TOKEN=$(head -c 32 /dev/urandom | base64)" > ~/.config/dsh/proxy.env
chmod 600 ~/.config/dsh/proxy.env
# Edit the unit to set DSH_PROXY_BIND to your tailscale IP:
#   Environment=DSH_PROXY_BIND=100.x.y.z
systemctl --user enable --now dsh-proxy.service
```

Then visit `http://<tailscale-ip>:3000/?token=<your-token>`.

### Security notes

The proxy deliberately does **not**:

- inject `window.__DSH_TRANSPORT__ = { ownsHost: true }` — that upstream flag
  tells the DSH client it is running on the host machine, which grants remote
  browsers the host's settings-write path
- rewrite `Host`, `Origin`, or `Sec-Fetch-Site` — DSH's trust fence decides
  whether a request is loopback-trusted from exactly those headers

Earlier versions did both. If you run an older copy, update.

For LAN trust without a proxy, prefer DSH's own supported mechanism
(`dsh web --trusted-host <name>`) over header rewriting.

---

## Files

```
bin/            scripts installed to ~/.local/bin
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
| `bin/*` | `~/.local/bin/` |
| `units/*` | `~/.config/systemd/user/` |
| `applications/*` | `~/.local/share/applications/` |
| `icons/*` | `~/.local/share/icons/` |
| `notify/` | `~/.local/share/dsh-desktop/notify` (linked by DSH `web` profile) |

---

## Configuration

All scripts read environment variables; nothing hardcodes a username.

| Variable | Used by | Default |
|---|---|---|
| `DSH_WEB_URL` | tray, browser window, proxy | `http://127.0.0.1:3080` |
| `DSH_TRAY_WINDOW` | tray launcher | `maximized` (`fullscreen`/`normal`) |
| `DSH_NEW_CHAT` | tray launcher | `1` (clean new chat) / `0` (restore last) |
| `DSH_CHROME` | browser window fallback | auto-detected (Chrome, Chromium, Brave, Edge) |
| `DSH_NODE` | launcher, installer | auto-detected |
| `DSH_BIN` | launcher, installer | auto-detected |
| `DSH_HOME` | plugin install | `~/.dsh` |
| `BUN_INSTALL` | updater, installer | `~/.bun` (or PATH) |
| `DSH_PROXY_TOKEN` | proxy | empty → loopback only |
| `DSH_PROXY_BIND` | proxy | `127.0.0.1` |
| `DSH_PROXY_PORT` | proxy | `3000` |

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
| **Notification delivery** | The session bus is *discovered* (`DBUS_SESSION_BUS_ADDRESS`, then `$XDG_RUNTIME_DIR/bus`, then `/run/user/<uid>/bus`) instead of assumed; a failure reports the exact reason |
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

**Update check says "up to date" but a release exists**
The tool tracks rc releases and also picks up newer stable releases. If only a
GitHub release exists and npm has not published it yet, there is nothing
installable — `dsh-update check` says so explicitly.

---

## License

MIT. See [LICENSE](LICENSE).

"DeepSeek Harness" is a trademark of DeepSeek. This project uses the
abbreviated "DSH" in its name per the official
[brand guidelines](https://github.com/deepseek-ai/deepseek-harness/blob/master/BRAND_GUIDELINES.md),
and is an independent community project.
