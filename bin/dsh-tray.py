#!/usr/bin/env python3
"""DSH desktop shell: QtWebEngine window + system tray.

Behaviour:
  - The window close button (X) does NOT close the window; it hides it to the tray.
  - Left click on the tray icon: show/hide the window.
  - Tray menu: update check, reload, service status, really quit.
  - "Check for updates" looks at the official GitHub repo (dsh-update ghcheck);
    the check runs in the background, the window does not freeze and the result
    is shown in a separate window. If a new version exists, installing it is
    still manual: `dsh-update apply`.
  - "Quit" only closes this shell; dsh-web.service (the agent) keeps running.

Note: The DSH Web interface requires authentication on loopback. When a new
profile is opened for the first time, the launch token the service writes to
the journal is found and appended to the address; therefore a ".bootstrapped"
marker is kept in the persistent profile directory.

Test: QT_QPA_PLATFORM=offscreen python3 dsh-tray.py --selftest
      python3 dsh-tray.py --check-once     (headless update check)
"""

from __future__ import annotations

import fcntl
import http.client
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from PyQt6.QtCore import QObject, QProcess, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QGuiApplication, QIcon, QTextCursor
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
)
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView


def _get_ipc_socket() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, f"dsh-tray-{os.getuid()}.sock")
    candidate = f"/run/user/{os.getuid()}"
    if os.path.isdir(candidate):
        return os.path.join(candidate, f"dsh-tray-{os.getuid()}.sock")
    return os.path.join(tempfile.gettempdir(), f"dsh-tray-{os.getuid()}.sock")


IPC_NAME = _get_ipc_socket()

APP_NAME = "DSH-Desktop"
ICON = Path.home() / ".local/share/icons/dsh-desktop.png"
DATA = Path(os.environ.get("DSH_TRAY_DATA", Path.home() / ".local/share/dsh-tray"))
LOCK = DATA / ".lock"
MARKER = DATA / ".bootstrapped"
URL = os.environ.get("DSH_WEB_URL", "http://127.0.0.1:3080").rstrip("/")
REBOOTSTRAP_DAYS = 25  # DSH cookie lasts 30 days; safety margin
UPDATE_BIN = Path.home() / ".local/bin/dsh-update"
UPDATE_PY = Path.home() / ".local/bin/dsh-update.py"
CHECK_TIMEOUT = 180  # seconds; the tray must not wait forever when there is no network

CONFIG_DIR = Path.home() / ".config/dsh"
PROXY_ENV = CONFIG_DIR / "proxy.env"
PROXY_SERVICE = "dsh-proxy.service"

# Startup window mode: "maximized" (default), "fullscreen" or "normal".
# To change it, edit the DSH_TRAY_WINDOW line in
# ~/.local/bin/dsh-desktop-launch.sh (the shortcut uses this value on every launch).
WINDOW_MODE = os.environ.get("DSH_TRAY_WINDOW", "maximized").strip().lower()
if WINDOW_MODE not in ("maximized", "fullscreen", "normal"):
    print(f"dsh-tray: unknown DSH_TRAY_WINDOW={WINDOW_MODE!r}; "
          "using 'maximized'.", file=sys.stderr)
    WINDOW_MODE = "maximized"

# Startup session mode: 1 / "true" starts with a clean new session; 0 / "false" restores last session.
# Configurable via DSH_NEW_CHAT or CLI flags (--new-chat / --resume).
NEW_CHAT_DEFAULT = os.environ.get("DSH_NEW_CHAT", "1").strip().lower() in ("1", "true", "yes", "on")

# Desktop environment, used only to produce a useful diagnostic. Nothing in
# this script behaves differently per environment: the tray is probed at
# runtime, so GNOME, KDE, XFCE, MATE, Cinnamon and tiling setups all take the
# same code path. The session type matters more than the name, because a tray
# on Wayland needs an XDG/StatusNotifier host.
DESKTOP = os.environ.get("XDG_CURRENT_DESKTOP", "").strip()
SESSION_TYPE = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()


def http_code(url: str, timeout: float = 2.0) -> str:
    """Return the raw HTTP status code; '000' if the connection cannot be made.

    Redirects are deliberately NOT followed: the token exchange returns 303,
    and going to the / address without carrying the cookie yields 401. To avoid
    mistakenly treating a valid token as invalid, the raw code must be
    inspected. (The `curl` call in the shell version also does not use -L.)
    """
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
    conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                else http.client.HTTPConnection)
    try:
        conn = conn_cls(parts.hostname, parts.port or 80, timeout=timeout)
        conn.request("GET", path)
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        return str(status)
    except Exception:
        return "000"


def service_up() -> bool:
    """A 401 also means 'the service is up'."""
    return http_code(f"{URL}/") not in ("000",)


def find_launch_token() -> str | None:
    """Find the current launch token the service writes to the journal.

    Tokens from older startups are invalid, so candidates are verified one by
    one. Python's re module is locale-independent; the problem where the [a-z]
    range excludes the letter 'i' in the Turkish locale does not arise here.

    Why `-g` is not used: journalctl's `-g` filter behaves differently from
    version to version when combined with the `-n` window, and it is also tied
    exactly to upstream's log text. If upstream changes that text (prefix,
    logger, localisation), the token search would fail SILENTLY and the user
    would see an unexplained 401 screen. Therefore raw records are fetched and
    matching is done here, over a wide window.
    """
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", "dsh-web.service", "--no-pager",
             "-n", "200", "--output=cat"],
            capture_output=True, text=True, timeout=8,
        ).stdout
    except Exception:
        return None

    # Only tokens on "dsh web: http..." lines are candidates.
    candidates: list[str] = []
    for line in out.splitlines():
        if "dsh web:" not in line or "http" not in line:
            continue
        for m in re.finditer(r"[?&]token=([A-Za-z0-9_.-]+)", line):
            if m.group(1) not in candidates:
                candidates.append(m.group(1))

    # The newest token is last; try that one first.
    for cand in reversed(candidates):
        code = http_code(f"{URL}/?token={cand}", timeout=3.0)
        if code not in ("000", "401"):
            return cand
    return None



def needs_bootstrap() -> bool:
    if not MARKER.exists():
        return True
    return (time.time() - MARKER.stat().st_mtime) > REBOOTSTRAP_DAYS * 86400


def target_url() -> str:
    """Return the address to load; append the bootstrap token if needed."""
    if not needs_bootstrap():
        return f"{URL}/"
    token = find_launch_token()
    if token:
        try:
            MARKER.parent.mkdir(parents=True, exist_ok=True)
            MARKER.touch()
        except OSError:
            pass
        return f"{URL}/?token={token}"

    # Token not found: silently falling back to the plain address leaves the
    # user facing an unexplained 401 screen. Report the cause and what to do.
    print(
        "dsh-tray: session token not found; trying the plain address.\n"
        "  This usually means a 401 (authentication) screen.\n"
        "  Service log     : journalctl --user -u dsh-web.service -n 50\n"
        "  Fix             : systemctl --user restart dsh-web.service\n"
        "                    then restart this shell.\n"
        f"  Marker file     : {MARKER}",
        file=sys.stderr,
    )
    return f"{URL}/"


def get_tailscale_ip() -> str | None:
    """Get active IPv4 address from tailscale daemon, or None if unavailable."""
    try:
        r = subprocess.run(["tailscale", "ip", "-4"],
                           capture_output=True, text=True, timeout=2.0)
        if r.returncode == 0:
            lines = r.stdout.strip().splitlines()
            if lines and lines[0] and not lines[0].startswith("127."):
                return lines[0].strip()
    except Exception:
        pass
    return None


def get_proxy_config() -> tuple[str | None, str, int]:
    """Read (token, bind_addr, port) from proxy.env if present."""
    token: str | None = None
    bind = os.environ.get("DSH_PROXY_BIND", "127.0.0.1")
    port = int(os.environ.get("DSH_PROXY_PORT", "3000"))

    if PROXY_ENV.is_file():
        try:
            for line in PROXY_ENV.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k == "DSH_PROXY_TOKEN":
                    token = v
                elif k == "DSH_PROXY_BIND":
                    bind = v
                elif k == "DSH_PROXY_PORT" and v.isdigit():
                    port = int(v)
        except Exception:
            pass
    return token, bind, port


def ensure_proxy_config() -> tuple[str, str, int]:
    """Ensure proxy.env has a secure token and the best available bind address."""
    token, bind, port = get_proxy_config()
    ts_ip = get_tailscale_ip()

    if ts_ip and (bind == "127.0.0.1" or not bind):
        bind = ts_ip

    if not token:
        token = secrets.token_urlsafe(32)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    content = (
        f"# DeepSeek Harness remote-access proxy configuration\n"
        f"DSH_PROXY_TOKEN={token}\n"
        f"DSH_PROXY_BIND={bind}\n"
        f"DSH_PROXY_PORT={port}\n"
    )
    # Open directly with 0o600 to prevent TOCTOU race
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(PROXY_ENV, flags, 0o600)
    with open(fd, "w", encoding="utf-8") as f:
        f.write(content)

    return token, bind, port


_last_proxy_check = 0.0
_cached_proxy_active = False


def is_proxy_active(force: bool = False) -> bool:
    """Return True if dsh-proxy.service is active, using short TTL cache to avoid blocking GUI."""
    global _last_proxy_check, _cached_proxy_active
    now = time.monotonic()
    if not force and (now - _last_proxy_check < 2.0):
        return _cached_proxy_active
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", PROXY_SERVICE],
                           capture_output=True, text=True, timeout=0.8)
        _cached_proxy_active = (r.stdout.strip() == "active")
        _last_proxy_check = now
    except Exception:
        _cached_proxy_active = False
    return _cached_proxy_active


def get_remote_url() -> str | None:
    """Build the full remote URL with token if configured."""
    token, bind, port = get_proxy_config()
    if not token:
        return None
    return f"http://{bind}:{port}/?token={token}"


def update_command() -> list[str]:
    """The `dsh-update` invocation: the wrapper if present, else python3 + script.

    The GUI session's PATH may not include ~/.local/bin; therefore an absolute
    path is used and, if the wrapper is missing, it falls back to the script.
    The interpreter is not hardcoded but taken from the running interpreter
    (sys.executable); virtual environments and different distributions also
    work.
    """
    if UPDATE_BIN.is_file() and os.access(UPDATE_BIN, os.X_OK):
        return [str(UPDATE_BIN)]
    return [sys.executable or "python3", str(UPDATE_PY)]



def update_check() -> tuple[int, str]:
    """Run the update check and return (exit code, output).

    0 = up to date, 10 = a new installable version is available, 1 = the check
    could not be performed. Network errors are caught here too; the caller
    never sees an exception.
    """
    try:
        r = subprocess.run([*update_command(), "ghcheck"],
                           capture_output=True, text=True, timeout=CHECK_TIMEOUT)
        out = "\n".join(p for p in (r.stdout.strip(), r.stderr.strip()) if p)
        return r.returncode, out or "(no output)"
    except subprocess.TimeoutExpired:
        return 1, f"dsh-update: did not respond within {CHECK_TIMEOUT} s"
    except OSError as e:
        return 1, f"could not run dsh-update: {e}"


class UpdateChecker(QObject):
    """Runs `dsh-update ghcheck` via QProcess.

    QProcess instead of a separate thread: the interface is never blocked, the
    check can be terminated silently on exit and the output is collected once
    it finishes.
    """

    finished = pyqtSignal(int, str)  # (exit code, output)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.proc: QProcess | None = None
        self.killer: QTimer | None = None

    def running(self) -> bool:
        return self.proc is not None

    def start(self) -> None:
        if self.proc is not None:
            return
        cmd = [*update_command(), "ghcheck"]
        proc = QProcess(self)
        self.proc = proc
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        killer = QTimer(self)  # the check must not wait forever when there is no network
        self.killer = killer
        killer.setSingleShot(True)
        killer.setInterval(CHECK_TIMEOUT * 1000)
        killer.timeout.connect(proc.kill)
        killer.start()
        proc.start(cmd[0], cmd[1:])

    def abort(self) -> None:
        """Silently kill the running check on exit (emits no signal)."""
        if self.killer is not None:
            self.killer.stop()
            self.killer = None
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        proc.finished.disconnect()
        proc.kill()
        proc.waitForFinished(1000)

    def _report(self) -> str:
        proc = self.proc
        if proc is None:
            return "(no output)"
        out = bytes(proc.readAllStandardOutput()).decode(errors="replace").strip()
        err = bytes(proc.readAllStandardError()).decode(errors="replace").strip()
        return "\n".join(p for p in (out, err) if p) or "(no output)"

    def _on_finished(self, code: int, status) -> None:
        if self.killer is not None:
            self.killer.stop()
            self.killer = None
        if self.proc is None:
            return
        report = self._report()
        if status != QProcess.ExitStatus.NormalExit:
            code = 1
            report = f"{report}\n\ndsh-update terminated unexpectedly."
        self.proc = None
        self.finished.emit(code, report)

    def _on_error(self, error) -> None:
        if self.killer is not None:
            self.killer.stop()
            self.killer = None
        # In the Crashed case finished() also arrives; _on_finished handles it.
        if self.proc is None or error == QProcess.ProcessError.Crashed:
            return
        self.proc = None
        self.finished.emit(1, f"could not start dsh-update: {error}")


def update_summary(code: int) -> str:
    if code == 10:
        return ("A new official version is available to install.\n"
                "Click 'Apply Update' below to install it now, or run 'dsh-update apply' in a terminal.\n"
                "(The update stops dsh-web.service; any active agent turn will end.)")
    if code == 0:
        return "Installed version is up to date: there is no new rc release on the official channel."
    return "The check could not be completed (network, GitHub or npm could not be read)."


class UpdateDialog(QDialog):
    """Shows the update report with an optional 'Apply Update' action and live log output."""

    def __init__(self, parent: DshWindow, code: int, report: str) -> None:
        super().__init__(parent)
        self.parent_win = parent
        self.code = code
        self.proc: QProcess | None = None

        self.setWindowTitle("DSH update check")
        self.resize(720, 480)
        lay = QVBoxLayout(self)

        self.head = QLabel(update_summary(code))
        self.head.setWordWrap(True)
        lay.addWidget(self.head)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlainText(report)
        self.view.setStyleSheet("font-family:monospace;")
        lay.addWidget(self.view)

        self.buttons = QDialogButtonBox()
        self.btn_close = self.buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.btn_close.clicked.connect(self.close)

        if code == 10:
            self.btn_apply = QPushButton("Apply Update")
            self.btn_apply.setStyleSheet("font-weight: bold;")
            self.btn_apply.clicked.connect(self.on_apply)
            self.buttons.addButton(self.btn_apply, QDialogButtonBox.ButtonRole.ActionRole)
        else:
            self.btn_apply = None

        lay.addWidget(self.buttons)

    def on_apply(self) -> None:
        if self.btn_apply is None:
            return

        confirm = QMessageBox.question(
            self,
            "Confirm Update",
            "DeepSeek Harness service (dsh-web.service) will be stopped and updated.\n"
            "Any in-progress agent turn or session will end.\n\n"
            "Do you want to proceed with the update?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.btn_apply.setEnabled(False)
        self.btn_apply.setText("Updating…")
        self.btn_close.setEnabled(False)

        self.view.appendPlainText("\n" + "=" * 60 + "\nStarting update: dsh-update apply --yes\n" + "=" * 60 + "\n")
        self.view.moveCursor(QTextCursor.MoveOperation.End)

        cmd = [*update_command(), "apply", "--yes"]
        proc = QProcess(self)
        self.proc = proc
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(self._on_output)
        proc.finished.connect(self._on_apply_finished)
        proc.start(cmd[0], cmd[1:])

    def _on_output(self) -> None:
        if self.proc is None:
            return
        text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self.view.insertPlainText(text)
        self.view.ensureCursorVisible()

    def _on_apply_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self.proc = None
        self.btn_close.setEnabled(True)

        if code == 0 and status == QProcess.ExitStatus.NormalExit:
            self.view.appendPlainText("\n" + "=" * 60 + "\n✓ Update completed successfully!\n"
                                     "Reloading interface in 3 seconds…\n" + "=" * 60)
            self.view.moveCursor(QTextCursor.MoveOperation.End)
            if self.btn_apply:
                self.btn_apply.setText("Updated ✓")
            QTimer.singleShot(3000, self._finish_reload)
        else:
            self.view.appendPlainText(f"\n! Update failed with exit code {code}.\n"
                                     "See log above for details or rollback information.")
            self.view.moveCursor(QTextCursor.MoveOperation.End)
            if self.btn_apply:
                self.btn_apply.setEnabled(True)
                self.btn_apply.setText("Retry Update")

    def _finish_reload(self) -> None:
        try:
            self.parent_win.view.reload()
        except Exception:
            pass
        self.accept()

    def closeEvent(self, event) -> None:
        if self.proc is not None:
            event.ignore()
            return
        event.accept()


def show_update_report(parent, code: int, report: str) -> None:
    """Show the check result in a readable window with selectable text."""
    dlg = UpdateDialog(parent, code, report)
    dlg.exec()


class DshWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.quitting = False
        self._tray: QSystemTrayIcon | None = None
        self.setWindowTitle("DeepSeek Harness")
        if ICON.exists():
            self.setWindowIcon(QIcon(str(ICON)))
        # Window state before hiding (None on first launch).
        self._state_before_hide: Qt.WindowState | None = None
        # This size is only visible in "normal" mode; the default startup
        # window is maximized.
        self.resize(1280, 860)

        DATA.mkdir(parents=True, exist_ok=True)
        profile = QWebEngineProfile(APP_NAME, self)
        profile.setPersistentStoragePath(str(DATA / "profile"))
        profile.setCachePath(str(DATA / "cache"))
        profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )

        self.view = QWebEngineView(self)
        self.profile = profile  # keep a reference to the profile on the Python side too
        self.view.setPage(QWebEnginePage(profile, self.view))
        self.setCentralWidget(self.view)
        self.view.setHtml(
            "<body style='background:#171513;color:#eee;font:14px sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:95vh'>"
            "<div>Starting DeepSeek Harness…</div></body>"
        )

        self.start_new_chat = NEW_CHAT_DEFAULT
        if "--new-chat" in sys.argv:
            self.start_new_chat = True
        elif "--resume" in sys.argv:
            self.start_new_chat = False

        self._new_chat_triggered = False
        self.view.loadFinished.connect(self._on_load_finished)

        QTimer.singleShot(100, self.load_when_ready)
        self._tries = 0

    def _on_load_finished(self, ok: bool) -> None:
        if not ok or self._new_chat_triggered or not self.start_new_chat:
            return
        url_str = self.view.url().toString()
        if not url_str.startswith("http"):
            return
        self._new_chat_triggered = True
        # Allow client-side rendering/hydration to settle before triggering action
        QTimer.singleShot(600, self._trigger_new_chat)

    def _trigger_new_chat(self) -> None:
        js = """
        (function() {
            var btn = document.querySelector('button[aria-label="New session"], button[aria-label="新建会话"]') ||
                      document.querySelector('button[aria-keyshortcuts*="KeyN"]') ||
                      Array.from(document.querySelectorAll('button')).find(function(b) {
                          return b.textContent && (b.textContent.includes('New Session') || b.textContent.includes('新会话'));
                      });
            if (btn) {
                btn.click();
            } else {
                window.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'n',
                    code: 'KeyN',
                    keyCode: 78,
                    which: 78,
                    ctrlKey: true,
                    bubbles: true
                }));
            }
            setTimeout(function() {
                var ta = document.querySelector('textarea, [contenteditable="true"]');
                if (ta) ta.focus();
            }, 300);
        })();
        """
        try:
            self.view.page().runJavaScript(js)
        except Exception:
            pass

    def load_when_ready(self) -> None:
        if service_up():
            self.view.setUrl(QUrl(target_url()))
            return
        self._tries += 1
        if self._tries == 1:
            subprocess.run(["systemctl", "--user", "start", "dsh-web.service"],
                           capture_output=True)
        if self._tries < 60:
            QTimer.singleShot(500, self.load_when_ready)
        else:
            error_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>DeepSeek Harness</title><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: sans-serif; text-align: center; padding: 40px; background: #0f172a; color: #f8fafc;">
  <h2>DeepSeek Harness failed to start</h2>
  <p>The DSH Web service ({URL}) is not responding after 30 seconds.</p>
  <div style="background: #1e293b; padding: 15px; border-radius: 8px; display: inline-block; text-align: left; margin: 20px auto; font-family: monospace; font-size: 13px;">
    <div><strong>Status:</strong> systemctl --user status dsh-web.service</div>
    <div><strong>Log:</strong> journalctl --user -u dsh-web.service -n 50</div>
    <div><strong>Start:</strong> systemctl --user start dsh-web.service</div>
  </div>
  <p><button onclick="window.location.href = '{URL}'" style="background: #3b82f6; color: white; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; font-size: 14px;">Retry Connection</button></p>
</body>
</html>"""
            self.view.setHtml(error_html, QUrl(URL))

    def show_startup(self) -> None:
        """Show the window according to DSH_TRAY_WINDOW mode (default: maximized)."""
        if WINDOW_MODE == "fullscreen":
            self.showFullScreen()
        elif WINDOW_MODE == "maximized":
            self.showMaximized()
        else:
            self.show()

    def ensure_startup_state(self) -> None:
        """If the compositor ignored the first maximize request, try once more.

        If the user has hidden the window and brought it back, their state is
        left alone.
        """
        if self._state_before_hide is not None:
            return
        if WINDOW_MODE == "maximized" and not self.isMaximized():
            self.showMaximized()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt signature)
        """X = hide to tray (no notification shown). A real quit is menu-only.

        If the tray has disappeared (no AppIndicator on Wayland, or the tray
        owner crashed) the window is NOT hidden: otherwise there would be no
        way to bring it back and the process would invisibly hold the flock
        lock, blocking every later launch with "already running".
        """
        tray = getattr(self, "_tray", None)
        tray_usable = (
            QSystemTrayIcon.isSystemTrayAvailable()
            and tray is not None
            and tray.isVisible()
        )
        if self.quitting or not tray_usable:
            event.accept()
            return
        event.ignore()
        self._state_before_hide = self.windowState()
        self.hide()

    def watchdog(self) -> None:
        """If neither the window nor the tray is visible, bring the window back.

        This is the last safeguard against the invisible-process situation: if
        the tray disappears mid-session, the user does not lose the window.
        """
        if self.quitting:
            return
        tray = getattr(self, "_tray", None)
        tray_visible = tray is not None and tray.isVisible()
        if not self.isVisible() and not tray_visible:
            self._state_before_hide = None
            self.show_startup()


    def _restore(self) -> None:
        """Bring the window back.

        If it was hidden before, its pre-hide state (maximized/normal) is
        preserved; on the window's first show, the DSH_TRAY_WINDOW mode is
        applied.

        Wayland note: the compositor manages window state itself and
        `windowState()` may return `WindowNoState` at hide time. In that case
        (masked state 0), if the mode is "maximized" it still opens maximized;
        otherwise the window would remain permanently small.
        """
        full = Qt.WindowState.WindowFullScreen
        maxi = Qt.WindowState.WindowMaximized
        if self._state_before_hide is None:
            state = {"fullscreen": full, "maximized": maxi}.get(
                WINDOW_MODE, Qt.WindowState.WindowNoState
            )
        else:
            state = self._state_before_hide & (full | maxi)
            # If the compositor did not report a state, fall back to the intent.
            if not state and WINDOW_MODE == "maximized":
                state = maxi

        if state & full:
            self.showFullScreen()
        elif state & maxi:
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def toggle(self) -> None:
        """Tray icon click: show/hide.

        A minimized window counts as "visible but hidden"; in that case it is
        restored instead of hidden. Otherwise, on X11 the first click hid the
        window and the second click lost the maximized state.
        """
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self._restore()



def main() -> int:
    # Headless check: runs the same command without setting up Qt at all (test/shortcut).
    if "--check-once" in sys.argv:
        code, out = update_check()
        print(out)
        print(f"\nexit code: {code}  ({update_summary(code).splitlines()[0]})")
        return code

    QGuiApplication.setDesktopFileName("dsh-desktop")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("dsh")
    app.setQuitOnLastWindowClosed(False)  # required for the tray

    if "--selftest" in sys.argv:
        DATA.mkdir(parents=True, exist_ok=True)
        w = DshWindow()
        print(f"selftest: window={w.windowTitle()!r} tray_support="
              f"{QSystemTrayIcon.isSystemTrayAvailable()} target={target_url()}")
        QTimer.singleShot(2500, app.quit)
        return app.exec()

    DATA.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sock = QLocalSocket()
        sock.connectToServer(IPC_NAME)
        if sock.waitForConnected(800):
            if "--update" in sys.argv:
                cmd = "update"
            elif "--new-chat" in sys.argv:
                cmd = "new-chat"
            else:
                cmd = "show"
            sock.write(cmd.encode("utf-8"))
            sock.waitForBytesWritten(800)
            sock.disconnectFromServer()
        else:
            print("dsh-tray: already running.", file=sys.stderr)
        return 0

    win = DshWindow()

    tray = QSystemTrayIcon(QIcon(str(ICON)) if ICON.exists() else app.windowIcon())
    tray.setToolTip("DeepSeek Harness")
    menu = QMenu()

    act_toggle = QAction("Show / Hide", menu)
    act_toggle.triggered.connect(win.toggle)
    act_reload = QAction("Reload", menu)
    act_reload.triggered.connect(win.view.reload)
    act_status = QAction("Service status", menu)
    act_status.triggered.connect(lambda: tray.showMessage(
        "dsh-web.service",
        subprocess.run(["systemctl", "--user", "is-active", "dsh-web.service"],
                       capture_output=True, text=True).stdout.strip() or "unknown",
        QSystemTrayIcon.MessageIcon.Information, 3000,
    ))

    # Update check: in a separate process so the tray does not lock up while waiting on the network.
    act_update = QAction("Check for updates", menu)
    checker = UpdateChecker(win)

    def finish_check(code: int, report: str) -> None:
        act_update.setText("Check for updates")
        act_update.setEnabled(True)
        show_update_report(win, code, report)

    def start_check() -> None:
        if checker.running():
            return  # a check is already in progress
        act_update.setEnabled(False)
        act_update.setText("Checking for updates…")
        checker.start()

    checker.finished.connect(finish_check)
    act_update.triggered.connect(start_check)

    # IPC Server for single-instance communication
    ipc_server = QLocalServer(win)
    QLocalServer.removeServer(IPC_NAME)
    if ipc_server.listen(IPC_NAME):
        def handle_ipc() -> None:
            client = ipc_server.nextPendingConnection()
            if not client:
                return
            if client.waitForReadyRead(800):
                msg = bytes(client.readAll()).decode("utf-8").strip()
                if msg == "update":
                    win._restore()
                    start_check()
                elif msg == "new-chat":
                    win._restore()
                    win._trigger_new_chat()
                elif msg == "show":
                    win._restore()
            client.disconnectFromServer()

        ipc_server.newConnection.connect(handle_ipc)
        win._ipc_server = ipc_server

    def real_quit() -> None:
        """Really close the shell (the service keeps running).

        The `win.close()` call is essential: because
        `setQuitOnLastWindowClosed(False)` is set, if the window stayed open
        the process could remain alive and keep holding the flock lock.
        """
        checker.abort()  # kill the process if there is a half-finished check
        win.quitting = True
        try:
            win.close()
        except Exception:
            pass
        tray.hide()
        app.quit()

    act_quit = QAction("Quit shell (service keeps running)", menu)
    act_quit.triggered.connect(real_quit)

    # Tailscale Remote Access Toggle & Link Copy
    act_proxy = QAction("Remote access (Tailscale)", menu)
    act_proxy.setCheckable(True)
    act_copy_url = QAction("Copy remote link", menu)

    def on_copy_url() -> None:
        url = get_remote_url()
        if url:
            cb = QGuiApplication.clipboard()
            if cb:
                cb.setText(url)
            tray.showMessage(
                "Remote Access",
                f"Link copied to clipboard:\n{url}",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )
        else:
            tray.showMessage(
                "Remote Access",
                "Proxy token is not configured yet.",
                QSystemTrayIcon.MessageIcon.Warning,
                3000,
            )

    act_copy_url.triggered.connect(on_copy_url)

    def on_toggle_proxy() -> None:
        if is_proxy_active(force=True):
            subprocess.run(["systemctl", "--user", "stop", PROXY_SERVICE],
                           capture_output=True)
            is_proxy_active(force=True)
            act_proxy.setChecked(False)
            act_copy_url.setEnabled(False)
            tray.showMessage(
                "Remote Access Disabled",
                "dsh-proxy stopped. DSH is now loopback-only.",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
        else:
            token, bind, port = ensure_proxy_config()
            subprocess.run(["systemctl", "--user", "start", PROXY_SERVICE],
                           capture_output=True)
            active = is_proxy_active(force=True)
            act_proxy.setChecked(active)
            act_copy_url.setEnabled(active)
            if active:
                url = f"http://{bind}:{port}/?token={token}"
                cb = QGuiApplication.clipboard()
                if cb:
                    cb.setText(url)
                tray.showMessage(
                    "Remote Access Enabled",
                    f"Listening on {bind}:{port}\nLink copied to clipboard!",
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )
            else:
                tray.showMessage(
                    "Remote Access Error",
                    "Could not start dsh-proxy.service. Check journalctl.",
                    QSystemTrayIcon.MessageIcon.Critical,
                    4000,
                )

    act_proxy.triggered.connect(on_toggle_proxy)

    def update_menu_states() -> None:
        proxy_up = is_proxy_active()
        act_proxy.setChecked(proxy_up)
        act_copy_url.setEnabled(proxy_up)

    menu.aboutToShow.connect(update_menu_states)

    for a in (act_toggle, act_reload, act_status):
        menu.addAction(a)
    menu.addSeparator()
    menu.addAction(act_proxy)
    menu.addAction(act_copy_url)
    menu.addSeparator()
    menu.addAction(act_update)
    menu.addSeparator()
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    win._tray = tray

    def on_tray(reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            win.toggle()

    tray.activated.connect(on_tray)
    tray.show()
    win.show_startup()
    # Some compositors (Wayland) can swallow the first maximize request; if it
    # is still not maximized shortly after, ask once more.
    QTimer.singleShot(400, win.ensure_startup_state)
    if "--update" in sys.argv:
        QTimer.singleShot(600, start_check)

    # Report a missing tray once, with advice specific to this desktop. Every
    # environment takes the same code path -- the tray is probed, never assumed
    # -- but the fix differs, and a user on GNOME would otherwise wonder why
    # the icon never appears.
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print(
            "dsh-tray: no system tray is available on this desktop.\n"
            "  The window still works; only 'hide to tray' is unavailable,\n"
            "  so closing the window quits the shell.\n"
            f"  Desktop: {DESKTOP or 'unknown'}"
            f"   Session: {SESSION_TYPE or 'unknown'}",
            file=sys.stderr,
        )
        if "GNOME" in DESKTOP.upper():
            print(
                "  GNOME 3.26+ has no built-in tray. Install the extension:\n"
                "    https://extensions.gnome.org/extension/615/appindicator-support/\n"
                "    (package name is often 'gnome-shell-extension-appindicator')",
                file=sys.stderr,
            )
        elif "XFCE" in DESKTOP.upper():
            print(
                "  On XFCE, add the 'Status Tray Plugin' to a panel.",
                file=sys.stderr,
            )
        elif SESSION_TYPE == "wayland":
            print(
                "  On Wayland the tray needs a StatusNotifier host.\n"
                "  On GNOME install the AppIndicator extension; on wlroots\n"
                "  compositors run a bar such as waybar with the tray module.",
                file=sys.stderr,
            )

    # Watchdog: if neither the window nor the tray is visible, bring the window
    # back. If the tray disappears mid-session (Wayland/AppIndicator, tray
    # crash), the user should not be left with an inaccessible process.
    guard = QTimer(win)
    guard.setInterval(2000)
    guard.timeout.connect(win.watchdog)
    guard.start()
    win._guard = guard  # keep the reference alive

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
