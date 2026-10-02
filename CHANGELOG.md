# Changelog

## 0.0.5 - 2026-10-02

Everything below landed between v0.0.4 and this release: update-path hardening, an
editable notification plugin, and a friendlier desktop launch.

### Added
- **Desktop notifications are editable from the interface.** DSH 0.2 renders no generic
  form for a plugin namespace — the host sends an `autoGenerate` flag that no client
  bundle consumes, and "Built-in plugins" is a read-only inventory — so every
  first-party plugin draws its own row. `dsh-notify` now draws one too: a
  **Notifications** row on the General settings page, one labelled switch per
  preference, registered into `settings.general.item` and writing through
  `ctx.configForms.get("notify")`. The row is always registered and reports its own
  readiness ("waiting for the settings document" / "this deployment does not serve the
  notify namespace"), and it falls back to native checkboxes when the DSH `Switch`
  primitive is unavailable — a row that silently disappears cannot be diagnosed.
- **The host half declares the six preferences as volatile fields**
  (`z.boolean().default(...).volatile()`, the shape DSH's own `ui-theme` and
  `product-analytics` use), which is what makes `dsh-settings` serve the namespace at
  all; without it the entry is skipped and nothing can be written.
- **The client half reads the settings document** through the shared `configForms`
  service (a boot-mounted client module is created with only its id, so `ctx.config` is
  always empty) and subscribes for live changes. Every failure path — no settings
  provider, unreadable namespace, wrong-typed value, a volatile field that resolves to
  `{}` — falls back to `DEFAULTS`, so a settings problem can never break notifications.
- **Toggles apply immediately:** the three event handlers are registered
  unconditionally and check their own flag when they fire, so switching a preference
  off takes effect without a reload. The approval waterfall calls `next()` in every
  case — a missing `next()` would deadlock the approval flow.

### Fixed
- **The settings row no longer crashes its slot entry.** The notice selector returned
  the whole store state, and that object was handed to React as a child (React error
  #31); the slot framework then dropped the row silently — no row, no message.
  A regression test renders the row *with a notice set* and requires every child in the
  tree to be a string, number, boolean, array or element, never a plain object.
- **The switches have visible labels.** The DSH `Switch` primitive uses its `label`
  prop as an `aria-label` only; each preference now draws its own text next to the
  switch, the way DSH's own settings rows do.
- **`uninstall.sh` removes the whole notify entry** — its id line plus every
  deeper-indented line (`name`, and the `config:` block the settings page writes).
  Removing just the first two lines left orphan rows whenever the entry carried
  configuration, which is invalid YAML and fatal for the whole profile.
- **A successful update is no longer reported as a failure:** `dsh-update` exits `2`
  when the new version IS installed but leftover package copies remain; the tray dialog
  only accepted `0`, so it showed "Update failed with exit code 2", offered "Retry" and
  skipped the reload.
- **The old package tree is deleted with verification:** `apply` used
  `shutil.rmtree(..., ignore_errors=True)`, which does nothing — silently — when a
  directory lacks the owner's write bit; the following `bun add` then installed on top
  of the old packages and left two versions behind, the exact state this tool exists to
  prevent. The delete retries after restoring write permission, reports failure, and
  restores the backup instead of installing on a half-deleted tree (`rmtree_verified`).
- **The tree is never replaced under a running server:** the result of
  `systemctl --user stop` is no longer ignored; the service is asked directly
  (`service_down()`) and the run stops before touching anything if it still answers.
- **A failed update leaves a log:** every `apply`/`prune`/`full` run mirrors stdout and
  stderr into `~/.cache/dsh-update/logs/<command>-<stamp>.log` (newest five kept), the
  full `bun` output is captured there, and `check` prints the newest log path.
- **Leftover counts are real file counts:** `deep_scan` promised hardlink handling but
  deduplicated symlinks only; bun hardlinks package files out of its download cache, so
  the "379 package copies" in a real report were 71 files.
- **An unfinished previous update is reported:** `check` warns when the `tree-backup`
  of an aborted run is still on disk (164 MB in the report that motivated this work)
  instead of silently replacing it on the next run.
- **Cache pruning matches what `plan` promises:** entries of the same core version
  (`0.2.0-rc.1` while installing `0.2.0-rc.2`) are deleted like any other stale entry;
  before they were kept forever and the cache grew with every release.
- **The notification plugin is actually linked on a fresh profile:** `install.sh` wrote
  the profile manifest but never installed the profile's dependency tree (DSH does that
  only through `dsh plugin --profile <name> install`), so on a clean profile the import
  check failed and the plugin was rolled back. The installer now runs that install
  (falling back to `pnpm install` inside the profile), which also replaces a leftover
  development symlink into the git working tree with the installed copy.
- **`!!js` patches are no longer rejected:** the validator used `yaml.safe_load`, which
  has no constructor for DSH's `!!js` expressions, so a valid patch was reported as
  broken and the plugin install rolled back. Validation moved to
  `bin/dsh-yaml-check.py`, which accepts DSH's tags and — when PyYAML is absent — runs
  an honest structural check instead of the hand-rolled parser that rejected valid
  block scalars.
- **Profile patch edits are atomic and validated:** both scripts rewrite
  `cordis.patch.yml` through a temp file + `os.replace` and keep the file mode (0600);
  `uninstall.sh` validates the result and restores the backup on failure.
- **`set -o pipefail` no longer aborts the installer:**
  `LATEST_BAK="$(ls -t ... | head -n1)"` killed the script when no backup matched; both
  scripts now use a `latest_backup` helper that cannot fail.
- **Smaller fixes:** dead branches removed from `newest_installable`; the tray docstring
  no longer claims updates are manual; a newly created patch file is 0600.

### Changed
- **The desktop shortcut reopens the last session** instead of forcing a clean one:
  `DSH_NEW_CHAT` defaults to `0` in the tray. `DSH_NEW_CHAT=1` / `--new-chat` still
  start a fresh session, `--resume` forces the restore, and the Chrome fallback window
  always resumed — the tray was the only piece forcing a new session.

### Documentation
- README: the notification settings are documented where they actually live — the
  profile patch (`~/.dsh/profiles/web/cordis.patch.yml`), because DSH 0.2's settings
  document *is* that patch (`ConfigEditor.documentPath = profileContext.patchPath`);
  `settings.yaml` is legacy and renamed `.imported` at boot. Troubleshooting covers
  reloading the interface in the tray shell, clearing the shell's HTTP cache when a
  plugin update does not arrive, and reading the row's own status line. The update
  section documents the run log and the exit codes; `curl` is documented as a
  requirement of the browser-window fallback; `uninstall.sh --purge` is documented as
  removing the shell's own browser profiles (which logs the web UI out).
- `bin/dsh-yaml-check.py` added (installer helper; it is not copied to `~/.local/bin`).

### Verified
- **In the real interface**, not only in unit tests: the DSH UI was driven headlessly
  (Chrome DevTools Protocol, this profile and its auth) into Settings → General. The
  page lists **Notifications** with six labelled switches — "Notify when a reply
  completes" … "Play the notification sound" — none of them disabled, console clean.
- 38 client checks against the real module source: row registration, write-through and
  refused-write notice, rendered switch tree, visible labels, aria-labels, notice as a
  string, no object children, native-checkbox fallback, both status lines, no row
  without the settings services, and missing React only skipping the row.
- Host schema (all six fields volatile, defaults and types matching `DEFAULTS`),
  33 update-path tests, 34 installer/uninstaller snippet checks, launch-mode checks
  (default resume, `DSH_NEW_CHAT=1`/`0`, flags still parsed), `install.sh --dry-run`,
  and `import('dsh-notify')` from the web profile.

## 0.0.4 - 2026-09-27

### Fixed & Improved
- **GUI Non-Interactive Update:** Added `--yes` flag to `dsh-update apply` execution in `dsh-tray.py`, allowing GUI one-click updates to run without terminal prompt hangs.
- **Tailscale Reverse Proxy Robustness & Security:**
  - Authenticated query parameter (`?token=...`) is stripped before forwarding upstream, eliminating collision with DSH internal `processLaunchToken` (resolving 401 errors).
  - Issued `Set-Cookie` (`HttpOnly; SameSite=Lax`) upon token verification so subsequent browser asset requests and WebSocket upgrades remain authenticated seamlessly.
  - Cleaned up stream listeners (`proxyRes.removeListener`) upon exceeding the 4MB HTML buffer limit to prevent duplicate chunks and double `res.end()` crashes.
  - Normalized Host header to upstream loopback while preserving original client host in `X-Forwarded-Host`.
- **Browser Window Guard:** Added immediate exit in `dsh-app-window.sh` when `dsh-web.service` is unreachable, preventing browser launch against a dead service.
- **Update Tooling & Service Compatibility:**
  - Dynamically resolved `DSH_WEB_URL` for status checks and service connectivity in `dsh-update.py`.
  - Returned exit code `0` in `dsh-update notify` when desktop notification daemon or session is missing, eliminating spurious systemd timer failure states.
- **Installer & Runtime Hardening:**
  - Removed PyYAML requirement in `install.sh`; implemented safe standard-library validation for `cordis.patch.yml`.
  - Added atomic manifest updates and guaranteed rollback of `package.json` if YAML validation or plugin import fails.
  - Added Node.js V8 engine validation guard (`process.versions.v8`) in `dsh-launcher.sh` and removed Bun masquerading as node.
  - Templated binary directories (`__BIN_DIR__`) in `dsh-proxy.service` and `dsh-update-check.service` to support custom installation directories.
- **Tray & Desktop Integration Polish:**
  - Migrated IPC socket from `/tmp/dsh-tray-ipc` to user's `XDG_RUNTIME_DIR` (`dsh-tray-<uid>.sock`).
  - Created `proxy.env` with `0o600` permissions directly via `os.open` to eliminate TOCTOU security races.
  - Added interactive error guidance and troubleshooting actions in `dsh-tray.py` when service fails to respond within timeout.
  - Stopped `killer` timer in `UpdateChecker` upon process termination.
  - Cached `is_proxy_active()` status checks to eliminate UI stutter when opening the tray context menu.
  - Aligned `StartupWMClass` and application name to `"DSH-Desktop"` across `.desktop` entry and Qt application.
- **Uninstaller Purge Cleanup:** `uninstall.sh --purge` now completely cleans `~/.config/dsh/proxy.env` and the Chrome profile directory (`dsh-app`).

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
