# Changelog

## 0.0.3 - 2026-09-27

### Added
- **Tray Tailscale Remote Access Management:** Added checkable `Remote access (Tailscale)` action directly in the system tray menu to toggle `dsh-proxy.service` on and off on demand, eliminating the need to use the terminal.
- **One-Click Remote Link Copy:** Added `Copy remote link` action in the tray menu. Automatically generates a cryptographically secure token, detects the active Tailscale IP (`tailscale ip -4`), copies the full authentication link (`http://<tailscale-ip>:3000/?token=...`) to clipboard, and delivers desktop notifications.
- **Startup New Chat Mode:** By default (`DSH_NEW_CHAT=1`), launching the shell automatically opens a fresh, blank session ready for prompt input while retaining previous sessions in the sidebar. Configurable via `DSH_NEW_CHAT=0`, `--new-chat`, or `--resume`.
- **Node.js V8 Engine Validation:** Added defensive verification in `install.sh` (`process.versions.v8`) to detect and reject Bun or non-V8 runtimes masquerading as `node`, preventing recursive service crash loops.

## 0.0.2 - 2026-09-27

### Added
- **In-App One-Click Update Tooling:** The update check dialog in `dsh-tray.py` now provides an interactive "Apply Update" action with confirmation prompt, executing `dsh-update apply` via a subprocess with live output log streaming and auto-reloading the UI upon success.
- **Single-Instance IPC Management:** Implemented `QLocalServer`/`QLocalSocket` IPC (`dsh-tray-ipc`) so subsequent launcher invocations (`dsh-desktop-launch.sh` or `--update`) seamlessly restore and focus the active window and trigger update checks without duplicate processes.
- **Desktop Notification Action Integration:** Enhanced `dsh-update.py notify` to include a clickable "View Update" action (`notify-send -A`), allowing users to jump directly from a system update notification into the in-app update dialog.

## 0.0.1 - 2026-09-27

### Initial Public Release
- **Desktop Launcher & Tray Shell:** QtWebEngine-based application window with system tray integration (`dsh-tray.py`). Closing window (X) hides to tray without interrupting the session.
- **Multi-Browser Fallback:** Automated detection across `google-chrome`, `google-chrome-stable`, `chromium`, `chromium-browser`, `brave-browser`, `brave`, `microsoft-edge-stable`, and `microsoft-edge` with desktop notifications (`dsh-app-window.sh`).
- **Desktop Notifications:** Host/client Cordis plugin (`dsh-notify`) for turn completion, input/approval requests, and agent errors, suppressed while focused and excluding subagents by default.
- **Service Management:** Seamless `systemd --user` units (`dsh-web.service`, `dsh-update-check.service`, `dsh-update-check.timer`) bound to `timers.target` for universal desktop environment compatibility.
- **Update Tooling (`dsh-update`):** Automated official release tracking, single-version integrity model, dynamic Bun resolution, automated rollback, and download cache pruning.
- **Authenticated Proxy:** Tailscale reverse proxy (`dsh-tailscale-proxy.mjs`) with constant-time token verification, loopback protection, and client polyfills.
- **Installation Automation:** `install.sh` and `uninstall.sh` supporting `--dry-run`, `--no-services`, `--no-plugin`, `--with-proxy`, and `--purge`.
