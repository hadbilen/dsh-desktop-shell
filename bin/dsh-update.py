#!/usr/bin/env python3
"""DSH update tool — "single version" model.

Goal: this machine should hold EXACTLY ONE version of DSH, and it should be the
newest rc release of the official channel. After an update, the old version and its leftovers are removed.

Commands:
  dsh-update full        ONE COMMAND: timer + leftover cleanup + newest rc + verification
  dsh-update check       Status: installed version(s), newest rc, other copies on disk
  dsh-update ghcheck     Looks only at the official GitHub repo (0 up to date / 10 new / 1 error)
  dsh-update notify      Desktop notification if a new rc exists (the systemd timer calls this)
  dsh-update plan        Prints what would be done; changes NOTHING
  dsh-update apply       Approved update: switches to the newest rc, deletes everything else
  dsh-update prune       Cleanup without changing versions: leaves a single copy of the current version
  dsh-update extras      Lists/deletes DSH assets outside the main trees

Before the destructive step the whole @deepseek-ai tree is copied aside
(hardlinked when possible) and restored automatically when the install fails,
times out, or is interrupted. An existing backup from an earlier unfinished run
is never overwritten: it is kept as `tree-backup.<stamp>`.

Where versions live (measured on this machine):
  ~/.bun/install/global/node_modules/@deepseek-ai/   ← the ONE real copy (the CLI tree)
  ~/.dsh/profiles/node_modules/@deepseek-ai/         ← symlinks to it (not a copy)
  ~/.bun/install/cache/@deepseek-ai/                 ← download cache
  no DSH in the npm cache (verified)

Why the service must be stopped:
  The DSH web UI serves from disk on each request; a running old process
  immediately starts serving the new client bundle, and because the old
  hash-named assets were deleted, an open page gets a 404 while the new client
  talks to the old server. Besides, the service is the running agent sessions
  themselves: DSH goes down while this command runs.

Diagnostics:
  Every apply/prune/full run mirrors its output into
  ~/.cache/dsh-update/logs/<command>-<stamp>.log (the newest five are kept), and
  the complete `bun` output is captured there. `check` prints the newest log path
  and warns when an earlier run left a tree backup behind.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
bun_env = os.environ.get("BUN_INSTALL")
BUN_DIR = Path(bun_env) if bun_env else HOME / ".bun"
_bun_cand = BUN_DIR / "bin/bun"
if _bun_cand.is_file() and os.access(_bun_cand, os.X_OK):
    BUN = _bun_cand
else:
    _which_bun = shutil.which("bun")
    BUN = Path(_which_bun) if _which_bun else _bun_cand
GLOBAL = BUN_DIR / "install/global"
GLOBAL_NM = GLOBAL / "node_modules"
# XDG base directories, resolved exactly as install.sh resolves them: a custom
# XDG_DATA_HOME/XDG_CONFIG_HOME/DSH_HOME must not split the installation across
# two sets of paths (that is how a token ends up in a file no unit reads).
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
DSH_HOME = Path(os.environ.get("DSH_HOME") or HOME / ".dsh")
PROFILE_NM = DSH_HOME / "profiles/node_modules"
CACHE_SCOPE = BUN_DIR / "install/cache/@deepseek-ai"
SERVICE = "dsh-web.service"
URL = os.environ.get("DSH_WEB_URL", "http://127.0.0.1:3080").rstrip("/")
# Where the shell's own scripts live: install.sh honours XDG_BIN_HOME, the
# launcher honours DSH_BIN_DIR — this tool must look in the same place.
BIN_DIR = Path(os.environ.get("DSH_BIN_DIR")
               or os.environ.get("XDG_BIN_HOME")
               or HOME / ".local/bin")
UNIT = CONFIG_HOME / "systemd/user/dsh-web.service"
STATE_DIR = HOME / ".cache/dsh-update"
STATE = STATE_DIR / "state.json"
ICON = DATA_HOME / "icons/dsh-desktop.png"
REGISTRY = "https://registry.npmjs.org/@deepseek-ai%2Fdsh"
PKG = "@deepseek-ai/dsh"
# The tray honours DSH_TRAY_DATA/DSH_APP_PROFILE and the XDG data directory; the
# markers must be looked for in the same places, otherwise an update clears a
# marker nobody reads and the stale one keeps a dead session cookie in use.
TRAY_DATA = Path(os.environ.get("DSH_TRAY_DATA") or DATA_HOME / "dsh-tray")
APP_DATA = Path(os.environ.get("DSH_APP_PROFILE") or DATA_HOME / "dsh-app")
BOOTSTRAP_MARKERS = (TRAY_DATA / ".bootstrapped",
                     APP_DATA / ".dsh-bootstrapped")
# Commands that must never hang forever (a stalled bun would hold the lock and
# leave the service stopped).
BUN_TIMEOUT = float(os.environ.get("DSH_UPDATE_BUN_TIMEOUT", "900"))
SYSTEMCTL_TIMEOUT = 60.0


# --------------------------------------------------------------------------- #
# version comparison
# --------------------------------------------------------------------------- #

def vkey(v: str):
    """Semver-like comparison key (prerelease aware)."""
    core, _, pre = v.split("+")[0].partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in core.split("."))
    nums = nums + (0,) * max(0, 3 - len(nums))
    if not pre:
        return (nums, (1,))
    parts = tuple((1, int(p), "") if p.isdigit() else (0, 0, p)
                  for p in pre.split("."))
    return (nums, (0, parts))


def newer(a: str, b: str) -> bool:
    try:
        return vkey(a) > vkey(b)
    except Exception:
        return False


def is_rc(v: str) -> bool:
    """Is this an official rc version? (0.1.7-rc.2 yes, 0.1.7-alpha.2 no)"""
    _, _, pre = v.split("+")[0].partition("-")
    return bool(pre) and pre.split(".")[0].lower() == "rc"


# --------------------------------------------------------------------------- #
# disk inventory
# --------------------------------------------------------------------------- #

def deep_scan(roots: tuple[Path, ...]) -> dict[str, int]:
    """Counts DSH package files in the given trees, grouped by version.

    One package counts ONCE PER FILE: entries that are the same file through a
    symlink OR a hardlink are deduplicated. Bun hardlinks package files out of
    its download cache, so the same `package.json` can sit at dozens of paths;
    counting paths instead of files inflated a real report roughly fivefold
    (379 paths, 71 files), which made the tool look like it had found far more
    leftovers than actually existed.

    `f.stat()` (not `lstat`) follows symlinks, so the profile tree — which is
    symlinks into the global tree — is not counted as a second copy either.
    """
    counts: dict[str, int] = {}
    seen: set[tuple[int, int]] = set()
    for root in roots:
        base = root / "@deepseek-ai"
        if not base.is_dir():
            continue
        for f in base.rglob("package.json"):
            name = f.parent.name
            if name != "dsh" and not name.startswith("dsh-"):
                continue
            try:
                st = f.stat()
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                v = json.loads(f.read_text())["version"]
            except Exception:
                continue
            counts[v] = counts.get(v, 0) + 1
    return counts


def is_dsh_package(name: str) -> bool:
    """Does the package name belong to the DSH family?

    The `@deepseek-ai` scope is shared: this cache also holds non-DSH packages
    (cordis, schemastery, cosmokit, node-addon-system, libreoffice-kit...).
    Only `dsh` and `dsh-*` may be pruned; otherwise others' download cache goes too.

    The same criterion is used in `deep_scan` when it groups files by version.
    """
    return name == "dsh" or name.startswith("dsh-")


def cache_entries() -> list[tuple[str, str, Path]]:
    """ONLY the DSH-family entries in the bun download cache.

    The returned path may be a symlink (that is how the bun cache works); the
    deletion side must account for this — see `remove_cache_entry`.

    A package directory whose own name carries a version (`name@1.2.3@@@1`) is
    the REAL directory the version symlinks point at. Its children are package
    contents (`lib`, `src`, ...), not versions, so it must never be enumerated:
    treating `lib` as a version would delete files out of the download cache.
    """
    out = []
    if not CACHE_SCOPE.is_dir():
        return out
    for pkg in sorted(CACHE_SCOPE.iterdir()):
        if not pkg.is_dir():
            continue
        if not is_dsh_package(pkg.name):
            continue  # do not touch non-DSH packages in the shared scope
        if "@" in pkg.name:
            continue  # a real version directory, not a version index
        for ver in sorted(pkg.iterdir()):
            if ver.is_dir():
                out.append((pkg.name, ver.name.split("@@@")[0], ver))
    return out


def rmtree_verified(path: Path) -> bool:
    """Delete a directory tree and CONFIRM that it is really gone.

    `shutil.rmtree(..., ignore_errors=True)` is not enough on its own: a
    directory that lacks the owner's write bit makes rmtree fail on its children
    and it returns silently, leaving the tree in place. A silent failure here is
    the worst possible outcome for an updater, because the `bun add` that follows
    then installs on top of the old packages and the machine ends up holding two
    versions — exactly the state this tool exists to prevent.

    Strategy: delete, then (only if something survived) make every directory
    writable and delete again, then report the truth to the caller.

    @param path - the tree to remove.
    @returns True when the path no longer exists.
    """
    if not path.exists() and not path.is_symlink():
        return True

    # A symlink or a plain file is not a tree. `shutil.rmtree` refuses symlinks
    # (the error is swallowed by ignore_errors) and the retry below would then
    # walk the LINK TARGET and chmod its directories, leaving the link in place
    # and the target's permissions changed. Unlink instead.
    if path.is_symlink() or not path.is_dir():
        try:
            path.unlink()
        except OSError:
            pass
        return not path.exists() and not path.is_symlink()

    shutil.rmtree(path, ignore_errors=True)
    if not path.exists():
        return True

    # Retry: rmtree cannot unlink entries from a directory the owner cannot write.
    for root, dirs, _files in os.walk(path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), 0o700)
            except OSError:
                pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def remove_cache_entry(path: Path, link_counts: dict[str, int] | None = None) -> int:
    """ACTUALLY deletes a single cache entry; returns the bytes freed.

    Critical detail: in the bun cache an entry is usually a symlink
    (`<pkg>/<ver>@@@n -> <pkg>@<ver>@@@n`), and `shutil.rmtree(symlink)` silently
    does nothing. Unlinking the symlink alone is not enough either: the real
    directory then has no link left to enumerate it, so it leaks forever and the
    reported "freed" size is a lie. The target is removed too, unless another
    cache symlink still points at it (in which case nothing was really freed).

    @param path - the cache entry (symlink or real directory).
    @param link_counts - optional map of resolved target -> number of links.
    @returns the number of bytes really deleted; 0 if nothing was freed.
    """
    if path.is_symlink():
        try:
            target = Path(os.path.realpath(path))
        except OSError:
            target = None
        size = dir_size(path)
        try:
            path.unlink()
        except OSError:
            return 0

        # Only the last link to a target may remove it, and only inside the cache
        # scope (never a path that resolves outside it).
        if target is None or not _under_cache_scope(target):
            return 0
        key = str(target)
        remaining = (link_counts or {}).get(key, 1) - 1
        if link_counts is not None and remaining > 0:
            return 0
        return size if rmtree_verified(target) else 0

    if not path.exists():
        return 0
    size = dir_size(path)
    # Verified delete: a read-only subdirectory must not look like a success.
    return size if rmtree_verified(path) else 0


def _under_cache_scope(path: Path) -> bool:
    """Is `path` really inside the DSH download cache?"""
    try:
        path.resolve().relative_to(CACHE_SCOPE.resolve())
        return True
    except (OSError, ValueError):
        return False


def dir_size(path: Path) -> int:
    """Total file size of a directory tree; 0 on errors."""
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total



def dangling_profile_links() -> list[Path]:
    base = PROFILE_NM / "@deepseek-ai"
    if not base.is_dir():
        return []
    return [p for p in base.iterdir() if p.is_symlink() and not p.exists()]


# Roots to search for DSH traces outside the two main trees (bounded, fast)
EXTRA_ROOTS = ("/opt", "/usr/local", "/srv",
               str(HOME / ".local"), str(HOME / ".cache"), str(HOME / ".harness-desktop"))
# Third-party package names that older versions of this tool removed. That was
# wrong for two reasons: the names belong to someone else's npm scope, and a
# public tool must never delete packages it does not own. The list is kept
# empty on purpose; `--with-extras` now only reports, never removes.
EXTRA_NPM: tuple[str, ...] = ()


def _find(args: list[str], timeout: float = 60.0) -> list[Path]:
    try:
        r = subprocess.run(["find", *args], capture_output=True, text=True,
                           timeout=timeout)
        return [Path(x) for x in r.stdout.splitlines() if x.strip()]
    except Exception:
        return []


def npm_name(p: Path) -> str:
    """Builds the npm name from a package directory (@scope/name or name)."""
    return f"{p.parent.name}/{p.name}" if p.parent.name.startswith("@") else p.name


# Directories whose contents may be treated as disposable download artifacts.
# Anything else is reported but never deleted: a file called `dsh-tool.AppImage`
# inside a project directory is the user's file, not a leftover.
ARCHIVE_DIRS = (HOME / "Downloads", HOME / "Desktop", HOME / "Documents",
                Path("/tmp"), Path("/opt"))


def _deletable_archive(path: Path) -> bool:
    """Is this artifact in a place where deleting it is safe?

    @param path - candidate archive file.
    @returns true for the home root and the download-like directories only.
    """
    if path.parent == HOME:
        return True
    for base in ARCHIVE_DIRS:
        try:
            path.relative_to(base)
            return True
        except ValueError:
            continue
    return False


def find_other_installs() -> list[tuple[str, str, Path]]:
    """(kind, version/tag, path) — DSH installs and leftovers outside the main trees."""
    out: list[tuple[str, str, Path]] = []
    known = {str(GLOBAL_NM), str(PROFILE_NM)}

    # 1) other @deepseek-ai/dsh package trees
    roots = [r for r in EXTRA_ROOTS if Path(r).is_dir()]
    if roots:
        for p in _find([*roots, "-maxdepth", "8",
                        "-path", "*@deepseek-ai/dsh/package.json", "-print"]):
            if any(k in str(p) for k in known) or ".bun/install" in str(p):
                continue
            try:
                v = json.loads(p.read_text())["version"]
            except Exception:
                v = "?"
            out.append(("install", v, p.parent.parent.parent))

    # 2) AppImage / archive files
    for p in _find([str(HOME), "/opt", "/tmp", "-maxdepth", "5",
                    "(", "-iname", "*.AppImage", "-o", "-iname", "*.dmg",
                    "-o", "-iname", "*.exe", ")", "-print"]):
        low = p.name.lower()
        if any(k in low for k in ("harness", "dsh", "deepseek")):
            try:
                size = human(p.stat().st_size)
            except OSError:
                size = "?"
            out.append(("archive", size, p))

    # 3) DSH-related global npm packages NOT tied to the profiles
    for name in EXTRA_NPM:
        p = GLOBAL_NM / name / "package.json"
        if p.exists():
            try:
                v = json.loads(p.read_text())["version"]
            except Exception:
                v = "?"
            out.append(("npm package", v, p.parent))
    return out


def gather() -> dict:
    g = deep_scan((GLOBAL_NM, PROFILE_NM))
    installed = None
    p = GLOBAL_NM / "@deepseek-ai/dsh/package.json"
    if p.exists():
        try:
            installed = json.loads(p.read_text())["version"]
        except Exception:
            pass
    entries = cache_entries()
    return {
        "installed": installed,
        "versions": g,
        "other_versions": {v: n for v, n in g.items() if v != installed},
        "cache": entries,
        "cache_size": sum(dir_size(d) for _, _, d in entries) if entries else 0,
        "dangling": dangling_profile_links(),
    }


# --------------------------------------------------------------------------- #
# remote versions
# --------------------------------------------------------------------------- #

def fetch_registry(timeout: float = 20.0) -> dict:
    try:
        req = urllib.request.Request(REGISTRY, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def newest_rc(data: dict) -> str | None:
    """The newest (non-deprecated) rc version on npm."""
    vers = data.get("versions", {})
    rcs = [v for v, meta in vers.items()
           if is_rc(v) and not meta.get("deprecated")]
    return max(rcs, key=vkey) if rcs else None


def newest_installable(data: dict) -> str | None:
    """The NEWEST installable version on npm.

    Why this is needed: `newest_rc` only looks at rc versions. When DSH publishes
    a stable release, the rc filter does not see it, the tool says
    "up to date", and the user misses the update.

    Policy: the newest version wins. If the same core has both an rc and a
    stable (e.g. 0.1.7-rc.2 and 0.1.7), the STABLE one is preferred — the stable
    release is that core's final state, preserving the "single, newest" goal.

    @param data - the npm registry response.
    @returns the newest version to install, or None.
    """
    vers = data.get("versions", {})
    rc = newest_rc(data)

    # Stable (no prerelease) and non-deprecated versions.
    stable = [v for v, meta in vers.items()
              if not v.split("+")[0].partition("-")[2]  # no prerelease
              and not meta.get("deprecated")]

    candidates = []
    if rc:
        candidates.append(rc)
    if stable:
        candidates.append(max(stable, key=vkey))
    if not candidates:
        return None

    newest = max(candidates, key=vkey)
    # A stable release sorts above its own rc (see `vkey`), so "the newest wins"
    # already implements "within one core, stable supersedes the rc".
    return newest




# --------------------------------------------------------------------------- #
# official GitHub repo (deepseek-ai/deepseek-harness)
#
# npm is the installation channel (bun pulls the package from there), but the
# official announcement of "a new version is out" is GitHub releases. The two
# can diverge: the `latest` tag on npm may point at a version whose release was
# never opened. So both are read together.
# --------------------------------------------------------------------------- #

GH_RELEASES = "https://api.github.com/repos/deepseek-ai/deepseek-harness/releases"
GH_TAG_PREFIX = "dsh-v"


def fetch_github(timeout: float = 20.0) -> list | dict:
    """Reads the release list (newest to oldest); on error returns {"error": ...}.

    The anonymous GitHub API is limited to 60 requests per hour; this usage (a
    few times a day) stays far below the limit, so no token is needed.
    """
    try:
        req = urllib.request.Request(
            GH_RELEASES + "?per_page=100",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "dsh-update"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        if not isinstance(data, list):
            return {"error": "unexpected GitHub response"}
        return [rel for rel in data if isinstance(rel, dict)]
    except Exception as e:
        return {"error": str(e)}


def gh_versions(releases: list) -> list[tuple[str, str]]:
    """Turns `dsh-v*` tags into (version, release date) pairs.

    The monorepo may also tag other packages; only the DSH CLI's tags
    (`dsh-v...`) are taken into account.
    """
    out: list[tuple[str, str]] = []
    for rel in releases:
        tag = str(rel.get("tag_name") or "")
        if not tag.startswith(GH_TAG_PREFIX):
            continue
        ver = tag[len(GH_TAG_PREFIX):]
        if ver:
            out.append((ver, str(rel.get("published_at") or "")[:10]))
    return out


def newest_gh_rc(releases: list) -> tuple[str | None, str]:
    """Selects the newest rc version (version, date) among the releases.

    It only looks at rc versions; it answers the "newest rc" question. For a
    decision that also covers stable versions, use `newest_gh_installable`.
    """
    rcs = [vd for vd in gh_versions(releases) if is_rc(vd[0])]
    if not rcs:
        return None, ""
    return max(rcs, key=lambda vd: vkey(vd[0]))


def newest_gh_installable(releases: list) -> tuple[str | None, str]:
    """Selects the NEWEST installable version (version, date) among the releases.

    Because `newest_gh_rc` only looks at rcs, when the official channel announced
    a stable release the check could not see it and said "up to
    date". This applies the same policy as `newest_installable` on the npm side:
    the newest version wins; within the same core, stable supersedes the rc.
    """
    allv = gh_versions(releases)
    if not allv:
        return None, ""

    rc = newest_gh_rc(releases)
    stable = [vd for vd in allv if not vd[0].split("+")[0].partition("-")[2]]

    candidates = []
    if rc[0]:
        candidates.append(rc)
    if stable:
        candidates.append(max(stable, key=lambda vd: vkey(vd[0])))
    if not candidates:
        return None, ""

    newest = max(candidates, key=lambda vd: vkey(vd[0]))
    return newest



# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #

def systemctl(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        return subprocess.run(["systemctl", "--user", *args], env=env,
                              capture_output=True, text=True,
                              timeout=SYSTEMCTL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["systemctl", "--user", *args], 1, "",
            f"systemctl --user {' '.join(args)} timed out after {SYSTEMCTL_TIMEOUT:.0f}s")
    except OSError as e:
        return subprocess.CompletedProcess(
            ["systemctl", "--user", *args], 1, "", f"systemctl unavailable: {e}")


def service_up(timeout: float = 2.0) -> bool:
    import http.client
    import urllib.parse
    try:
        target = os.environ.get("DSH_WEB_URL", URL)
        p = urllib.parse.urlsplit(target)
        host = p.hostname or "127.0.0.1"
        port = p.port or (443 if p.scheme == "https" else 80)
        conn_cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
        c = conn_cls(host, port, timeout=timeout)
        c.request("GET", p.path or "/")
        c.getresponse().status          # even 401 means "up"
        c.close()
        return True
    except Exception:
        return False


def wait_service(seconds: int = 90) -> bool:
    for _ in range(seconds):
        if service_up():
            return True
        time.sleep(1)
    return False


def service_down(timeout: float = 10.0) -> bool:
    """Wait until the DSH web service stops answering.

    Used after `systemctl stop`: the tree must never be replaced under a server
    that is still running (an open page would then mix old and new assets).
    A service that was not running at all also counts as down, so this does not
    turn "nothing to stop" into an error.

    @param timeout - how long to wait for the port to go quiet.
    @returns True when the service no longer answers.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not service_up():
            return True
        time.sleep(0.5)
    return not service_up()


# --------------------------------------------------------------------------- #
# run log
#
# A destructive run must leave a trace. Before v0.0.5 the installer output went
# to the terminal only, so a failed update could not be diagnosed afterwards
# (the only surviving evidence was rollback.json). Every apply/prune/full run now
# mirrors stdout+stderr into ~/.cache/dsh-update/logs/<command>-<stamp>.log and
# keeps the newest few.
# --------------------------------------------------------------------------- #

LOG_DIR = STATE_DIR / "logs"
LOG_KEEP = 5
_LOG_HANDLE = None


class _Tee:
    """Minimal stream duplicator: writes to the console AND the log file."""

    def __init__(self, *streams) -> None:
        self.streams = [s for s in streams if s is not None]

    def write(self, data: str) -> int:
        for s in self.streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self) -> None:
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False


# Streams replaced by start_log(), so stop_log() can put them back.
_LOG_ORIGINALS: dict = {}


def _prune_logs() -> None:
    """Keep only the newest LOG_KEEP logs."""
    try:
        logs = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in logs[LOG_KEEP:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def start_log(command: str, header: str = "") -> Path | None:
    """Mirror stdout/stderr into a timestamped log file.

    @param command - subcommand name, used in the file name.
    @param header - extra first lines (version, target) for context.
    @returns the log path, or None when logging is not possible.
    """
    global _LOG_HANDLE
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = LOG_DIR / f"{command}-{stamp}.log"
        handle = open(path, "a", encoding="utf-8")
        handle.write(f"# dsh-update {command} — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        if header:
            handle.write(f"# {header}\n")
        handle.flush()
        _LOG_HANDLE = handle
        _LOG_ORIGINALS["stdout"] = sys.__stdout__
        _LOG_ORIGINALS["stderr"] = sys.__stderr__
        sys.stdout = _Tee(sys.__stdout__, handle)
        sys.stderr = _Tee(sys.__stderr__, handle)
        _prune_logs()
        return path
    except Exception:
        return None


def stop_log() -> None:
    """Restore stdout/stderr and close the run log.

    The handle used to stay open and sys.stdout kept pointing at the tee for the
    rest of the process, so the log file was only flushed by interpreter exit.
    """
    global _LOG_HANDLE
    if _LOG_ORIGINALS.get("stdout") is not None:
        sys.stdout = _LOG_ORIGINALS["stdout"]
    if _LOG_ORIGINALS.get("stderr") is not None:
        sys.stderr = _LOG_ORIGINALS["stderr"]
    if _LOG_HANDLE is not None:
        try:
            _LOG_HANDLE.flush()
            _LOG_HANDLE.close()
        except Exception:
            pass
        _LOG_HANDLE = None


def log_raw(text: str) -> None:
    """Write text to the log file only (for output that is too long to print)."""
    if _LOG_HANDLE is None:
        return
    try:
        _LOG_HANDLE.write(text)
        if not text.endswith("\n"):
            _LOG_HANDLE.write("\n")
        _LOG_HANDLE.flush()
    except Exception:
        pass


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- #
# desktop notification (environment agnostic)
#
# Sending a notification needs three things, and none of them can be assumed:
#   1. a session bus address,
#   2. a notification daemon listening on it,
#   3. a `notify-send` binary.
#
# The original code hardcoded `DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus` in
# the unit, which is correct on most systems but wrong wherever the bus lives
# elsewhere (some XFCE/MATE setups, containerised sessions, systems using
# dbus-broker with a non-standard path). The helpers below discover the bus
# instead of assuming it, and report precisely which step failed so a missing
# notification is never a silent mystery.
# --------------------------------------------------------------------------- #

def session_bus_address() -> str | None:
    """Locate the session D-Bus address without assuming a fixed path.

    Resolution order:
      1. An already exported DBUS_SESSION_BUS_ADDRESS (the common case).
      2. $XDG_RUNTIME_DIR/bus, the XDG convention.
      3. /run/user/<uid>/bus, the conventional path when XDG_RUNTIME_DIR is
         unset (for example in some cron/systemd contexts).

    @returns the bus address, or None when no candidate exists.
    """
    env = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if env:
        return env

    candidates = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        candidates.append(Path(runtime) / "bus")
    candidates.append(Path(f"/run/user/{os.getuid()}/bus"))

    for cand in candidates:
        try:
            if cand.exists():
                return f"unix:path={cand}"
        except OSError:
            continue
    return None


def have_notify_send() -> bool:
    """Is a `notify-send` binary present on this system?"""
    return shutil.which("notify-send") is not None


def notify_send(title: str, body: str, urgency: str = "normal",
                action: tuple[str, str] | None = None) -> tuple[bool, str, str]:
    """Deliver a desktop notification; return (delivered, diagnostic, action_chosen).

    Works with any freedesktop-compliant notification daemon, which covers
    GNOME (including the AppIndicator extension), KDE Plasma, XFCE, MATE,
    Cinnamon, Budgie and most tiling setups. Where no daemon is running this
    returns (False, reason, "") instead of failing silently.

    @param title - notification title.
    @param body - notification body.
    @param urgency - "low", "normal" or "critical".
    @param action - optional (key, label) e.g. ("update", "View Update").
    @returns (True, "", action_chosen) on success, otherwise (False, reason, "").
    """
    if not have_notify_send():
        return False, "notify-send is not installed (package: libnotify-bin)", ""

    bus = session_bus_address()
    if bus is None:
        return False, ("no D-Bus session bus found; not running inside a "
                       "graphical session?"), ""

    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = bus

    args = ["notify-send", "-a", "DSH", "-u", urgency]
    if ICON.exists():
        args += ["-i", str(ICON)]
    if action is not None:
        key, label = action
        args += ["-t", "15000", "-A", f"{key}={label}"]
    args += [title, body]

    try:
        r = subprocess.run(args, capture_output=True, text=True, env=env)
    except OSError as e:
        return False, f"could not run notify-send: {e}", ""

    # If -A failed (e.g. older notify-send or daemon rejecting actions), retry without action
    if r.returncode != 0 and action is not None:
        fallback_args = ["notify-send", "-a", "DSH", "-u", urgency]
        if ICON.exists():
            fallback_args += ["-i", str(ICON)]
        fallback_args += [title, body]
        try:
            r = subprocess.run(fallback_args, capture_output=True, text=True, env=env)
        except OSError as e:
            return False, f"could not run notify-send: {e}", ""

    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip() or f"exit code {r.returncode}"
        return False, f"notify-send failed: {detail}", ""

    action_chosen = (r.stdout or "").strip()
    return True, "", action_chosen


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #

def service_bin_mismatch() -> str | None:
    """Return the service's DSH entry when it is not the tree this tool updates.

    `install.sh` may bind the unit to a pnpm/npm copy while this tool only updates
    the Bun tree; without a warning the service would keep running the old one.

    @returns the ExecStart entry, or None when it matches (or cannot be read).
    """
    try:
        text = UNIT.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"(?m)^ExecStart=\S+\s+(\S+)", text)
    if m is None:
        return None
    entry = m.group(1)
    return None if str(GLOBAL_NM) in entry else entry


def cmd_check() -> int:
    st = gather()
    data = fetch_registry()
    rc = newest_rc(data) if "error" not in data else None

    print("== Installed ==")
    print(f"  version in use   : {st['installed'] or 'unreadable'}")
    if st["versions"]:
        for v, n in sorted(st["versions"].items(), key=lambda kv: vkey(kv[0])):
            tag = "  ← in use" if v == st["installed"] else "  ← UNWANTED"
            print(f"    {v:16s} {n} package files{tag}")
    if st["dangling"]:
        print(f"  dangling symlinks: {len(st['dangling'])} (profile tree)")
    print(f"  download cache   : {len(st['cache'])} entries, {human(st['cache_size'])}")
    mismatch = service_bin_mismatch()
    if mismatch is not None:
        print(f"  ! {SERVICE} runs a DSH outside this tree:")
        print(f"      {mismatch}")
        print(f"      updates here would not reach it (expected under {GLOBAL_NM})")
    logs = (sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)
            if LOG_DIR.is_dir() else [])
    if logs:
        print(f"  last run log     : {logs[-1]}")
    backup = STATE_DIR / "tree-backup"
    if backup.is_dir():
        print(f"  unfinished update: backup still on disk ({human(dir_size(backup))})")
    kept_backups = sorted(STATE_DIR.glob("tree-backup.*")) if STATE_DIR.is_dir() else []
    if kept_backups:
        print(f"  kept backups     : {len(kept_backups)} from earlier unfinished updates")
        for old_backup in kept_backups:
            print(f"      {old_backup} ({human(dir_size(old_backup))})")
    others = find_other_installs()
    if others:
        print("  other DSH assets (outside the main trees):")
        for kind, desc, path in others:
            print(f"    [{kind:10s}] {desc:10s} {path}")
    print()

    print("== npm (official channel) ==")
    if "error" in data:
        print(f"  registry unreadable: {data['error']}")
        return 1
    tags = data.get("dist-tags", {})
    times = data.get("time", {})
    for tag, ver in tags.items():
        mark = "  ← installed" if ver == st["installed"] else ""
        print(f"  {tag:6s} {ver:16s} {(times.get(ver) or '')[:10]}{mark}")
    print(f"  newest rc version: {rc or 'not found'}")
    inst = newest_installable(data)
    if inst and inst != rc:
        print(f"  version to install: {inst}   (stable release is newer than the rc)")
    print()

    print("== GitHub (official repo: deepseek-ai/deepseek-harness) ==")
    releases = fetch_github()
    if "error" in releases:
        gh, gh_date = None, ""
        print(f"  release list unreadable: {releases['error']}")
    else:
        gh, gh_date = newest_gh_installable(releases)
        if gh:
            print(f"  installable      : {gh:16s} {gh_date}")
        else:
            print("  newest rc        : not found")
    print()

    problems = []
    if rc and st["installed"] and newer(rc, st["installed"]):
        problems.append(f"Newest rc is not installed: {st['installed']} → {rc}")
    if gh and st["installed"] and newer(gh, st["installed"]):
        problems.append(f"A newer version exists on the official GitHub: {st['installed']} → {gh}")
    if gh and "error" not in data:
        versions = data.get("versions", {})
        if newer(gh, st["installed"] or "0") and gh not in versions:
            problems.append(f"GitHub has announced {gh} but it is not on npm yet; "
                            f"it cannot be installed until the package is published (newest rc: {rc})")
    if st["other_versions"]:
        total = sum(st["other_versions"].values())
        problems.append(f"Other versions are on disk: {total} package files "
                        f"({', '.join(sorted(st['other_versions']))})")
    if st["dangling"]:
        problems.append(f"there are {len(st['dangling'])} dangling profile symlinks")
    if backup.is_dir():
        problems.append(f"a previous update did not finish: its backup is still on disk "
                        f"({human(dir_size(backup))}); the next apply keeps it aside as "
                        f"tree-backup.<stamp> and removes it only after a successful run")
    if kept_backups:
        problems.append(f"{len(kept_backups)} backup(s) from earlier unfinished updates are kept "
                        f"({human(sum(dir_size(b) for b in kept_backups))}); "
                        f"remove them once the current version is verified")
    if others:
        problems.append(f"{len(others)} DSH assets sit outside the main trees "
                        f"(see above; dsh-update extras)")
    if problems:
        for p in problems:
            print(f"  ! {p}")
        print("\n  To fix: dsh-update apply   (or without changing version: dsh-update prune)")
    else:
        print("  Clean: there is a single version and it is the newest rc.")
    return 0


# --------------------------------------------------------------------------- #
# ghcheck — only the official GitHub repo (the tray calls this)
# --------------------------------------------------------------------------- #

def cmd_ghcheck() -> int:
    """Answers a single question: is there a new installable version on the official channel?

    Exit codes (the tray menu decides based on these):
      0  = up to date; nothing new to install (a release may be announced but
           not published on npm, and that also means "nothing to install")
      10 = a new rc version is published on npm and can be installed
      1  = the check could not complete (network, GitHub or npm unreadable)
    """
    st = gather()
    installed = st["installed"]

    releases = fetch_github()
    if "error" in releases:
        print(f"GitHub unreadable: {releases['error']}")
        return 1
    gh, gh_date = newest_gh_installable(releases)
    if not gh:
        print("no dsh-v* version found in the GitHub releases")
        return 1

    data = fetch_registry()
    npm_ok = "error" not in data
    npm_rc = newest_rc(data) if npm_ok else None
    npm_has_gh = npm_ok and gh in data.get("versions", {})

    print("== Official GitHub: deepseek-ai/deepseek-harness ==")
    print(f"  installed version: {installed or 'unreadable'}")
    print(f"  newest rc      : {gh}" + (f"   ({gh_date})" if gh_date else ""))
    if not npm_ok:
        print(f"  npm check      : failed ({data.get('error')})")
    elif npm_has_gh:
        print(f"  npm check      : {gh} is published on npm")
    else:
        print(f"  npm check      : {gh} is NOT on npm (newest rc: {npm_rc or 'none'})")

    if not installed:
        print("\n  The installed version could not be read; comparison is not possible.")
        return 1

    if not newer(gh, installed):
        print(f"\n  Up to date: {installed} is the newest official rc version.")
        return 0

    if not npm_ok:
        print(f"\n  A new version exists ({installed} → {gh}) but npm could not be verified; "
              f"the update cannot be installed right now.")
        return 1

    if not npm_has_gh:
        print(f"\n  GitHub has announced {gh} but the package is not on npm yet; "
              f"it can be updated once published. For now there is nothing to install.")
        return 0

    print(f"\n  A new version exists: {installed} → {gh}")
    print("  To install: dsh-update apply   (dsh-web.service is stopped)")
    return 10


# --------------------------------------------------------------------------- #
# notify
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _tray_socket() -> Path | None:
    """Path of the running shell's IPC socket, when it exists.

    The candidate order must match dsh-tray.py's `_get_ipc_socket`: with
    XDG_RUNTIME_DIR unset the tray uses /run/user/<uid> while this tool used to
    look only in /tmp, so "open the updater in the running shell" silently did
    nothing.
    """
    candidates = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        candidates.append(Path(runtime))
    conventional = Path(f"/run/user/{os.getuid()}")
    if conventional.is_dir():
        candidates.append(conventional)
    candidates.append(Path(tempfile.gettempdir()))
    for base in candidates:
        sock = base / f"dsh-tray-{os.getuid()}.sock"
        if sock.exists():
            return sock
    return None


def _ask_tray(cmd: str) -> bool:
    """Hand a request to the running tray shell.

    The tray lives in the graphical session, so it can open the updater window
    even when this process runs from a systemd service without DISPLAY.

    @param cmd - "update" or "show".
    @returns true when the request was written to the socket.
    """
    sock = _tray_socket()
    if sock is None:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1.0)
            client.connect(str(sock))
            client.sendall(cmd.encode("utf-8"))
        return True
    except OSError:
        return False


def cmd_notify() -> int:
    # `apply`/`full`/`prune` hold this lock; a notify run during one of them can
    # read a half-swapped tree and report (or act on) a version that is being
    # replaced. Skipping quietly is correct: the next timer run covers it.
    lock = _acquire_lock(quiet=True)
    if lock is None:
        print("dsh-update: an update is running; the check is skipped.")
        return 0
    st = gather()
    data = fetch_registry()
    if "error" in data or not st["installed"]:
        print(f"dsh-update: skipped ({data.get('error', 'installed version unreadable')})",
              file=sys.stderr)
        return 0

    rc = newest_installable(data)
    if not rc:
        print("dsh-update: no installable version found", file=sys.stderr)
        return 0

    state = load_state()
    installed = st["installed"]

    if not newer(rc, installed):
        print(f"dsh-update: the newest rc is installed ({installed})")
        return 0
    if state.get("notified_rc") == rc:
        print(f"dsh-update: {rc} was already notified")
        return 0

    extra = ""
    if st["other_versions"]:
        extra = (f"\nThere are also {sum(st['other_versions'].values())} unwanted package files on disk; "
                 f"the update deletes them.")
    body = (f"Installed: {installed}\nNewest rc: {rc}{extra}\n\n"
            f"Click 'View Update' to inspect and apply within DeepSeek Harness,\n"
            f"or in a terminal run: dsh-update apply")

    delivered, reason, action_taken = notify_send(
        "DSH: new rc version", body, action=("update", "View Update")
    )
    if delivered:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state["notified_rc"] = rc
        STATE.write_text(json.dumps(state, indent=2) + "\n")
        print(f"dsh-update: notified → {rc}")
        if action_taken == "update":
            if _ask_tray("update"):
                # Preferred: the running shell already owns the graphical session.
                print("dsh-update: the running shell will open the updater")
            else:
                launcher = BIN_DIR / "dsh-desktop-launch.sh"
                if not launcher.is_file():
                    candidate = Path(__file__).resolve().parent / "dsh-desktop-launch.sh"
                    if candidate.is_file():
                        launcher = candidate
                if launcher.is_file():
                    try:
                        subprocess.Popen([str(launcher), "--update"], start_new_session=True)
                    except Exception as e:
                        print(f"dsh-update: could not launch GUI: {e}", file=sys.stderr)
        return 0

    # Not delivered (no bus, no daemon, no notify-send). The state file is NOT
    # updated so a later run retries. The reason is printed verbatim: a missing
    # notification must never be a silent mystery.
    print(f"dsh-update: notification not delivered — {reason}", file=sys.stderr)
    print(f"dsh-update: new version {rc} is available (installed: {installed})")
    return 0


# --------------------------------------------------------------------------- #
# plan / apply / prune
# --------------------------------------------------------------------------- #

def print_plan(st: dict, target: str, mode: str) -> None:
    print(f"Mode: {mode}")
    print(f"  {st['installed'] or '?'}  →  {target}")
    print()
    print("To be done:")
    print(f"  1. {SERVICE} will be stopped")
    print("     ! DSH (and agent sessions) go down while this command runs;")
    print("       session history is preserved. Run it from a terminal.")
    print(f"  2. package.json + bun.lock will be backed up ({STATE_DIR})")
    print(f"  3. {GLOBAL_NM}/@deepseek-ai and bun.lock will be installed from scratch")
    print(f"     (`bun add --global {PKG}@{target}` — the old version is deleted at this step)")
    print(f"  4. dsh entries in the bun download cache that do not match {target} will be deleted")
    print("  5. Dangling profile symlinks will be cleaned up")
    print("  6. Verification: only ONE version must remain in the tree")
    print(f"  7. {SERVICE} will be started and an HTTP response awaited")
    print("  8. Client bootstrap markers will be reset (fresh session cookie)")
    print()
    if st["other_versions"]:
        print(f"  Other versions to delete: {', '.join(sorted(st['other_versions']))}")
    if st["cache"]:
        print(f"  Cache: {len(st['cache'])} entries / {human(st['cache_size'])} (non-matching ones are deleted)")


def _backup() -> dict:
    """Backs up package.json + bun.lock; returns which files were backed up.

    The returned dictionary prevents the rollback message from referring to a
    backup that does not exist (previously, if `bun.lock` was missing, copying
    it was still suggested and failed with `cp: cannot stat`).
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    saved: dict[str, bool] = {}
    for f in ("package.json", "bun.lock"):
        src = GLOBAL / f
        ok = False
        if src.exists():
            try:
                shutil.copy2(src, STATE_DIR / f"{f}.bak")
                ok = True
            except OSError:
                ok = False
        saved[f] = ok
    return saved


def _backup_tree() -> Path | None:
    """Temporarily backs up the installed @deepseek-ai tree.

    Because the `apply` step deletes the tree BEFORE installing, there must be
    something to fall back to when `bun add` fails. Previously only
    package.json/bun.lock were backed up and the tree could not be restored:
    the user lost 273 packages and had to re-download all of them.

    An existing `tree-backup` means an earlier run did not finish. That copy is
    the only known-good one (the current tree may be half-installed), so it is
    renamed aside and KEPT instead of being deleted and replaced — deleting it
    first destroyed the last good version exactly when it was needed.

    @returns the backup directory or None.
    """
    src = GLOBAL_NM / "@deepseek-ai"
    if not src.is_dir():
        return None
    dest = STATE_DIR / "tree-backup"
    if dest.exists():
        keep = dest.with_name(f"tree-backup.{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            dest.rename(keep)
            print(f"      note: an earlier unfinished update left a backup; kept as {keep}")
            print(f"            (remove it yourself once this run succeeds: rm -rf {keep})")
        except OSError as e:
            print(f"      ! the leftover backup at {dest} could not be moved aside: {e}",
                  file=sys.stderr)
            return None
    try:
        # Try hardlinks first: nearly free on the same filesystem.
        shutil.copytree(src, dest, symlinks=True,
                        copy_function=os.link)
    except (OSError, shutil.Error):
        rmtree_verified(dest)
        try:
            shutil.copytree(src, dest, symlinks=True)
        except (OSError, shutil.Error) as e:
            print(f"      ! tree could not be backed up: {e}", file=sys.stderr)
            return None
    return dest


def _restore_tree(backup: Path) -> bool:
    """Restores the @deepseek-ai tree from the backup."""
    dest = GLOBAL_NM / "@deepseek-ai"
    try:
        # A silent failure here would make the copy below raise FileExistsError
        # and hide the real reason, so the delete is verified first.
        if not rmtree_verified(dest):
            print(f"      ! {dest} could not be cleared before the restore", file=sys.stderr)
            return False
        if backup.is_dir():
            # Copy the hardlinked backup back (so the backup is not corrupted).
            shutil.copytree(backup, dest, symlinks=True,
                            copy_function=os.link if _hardlink_ok(backup) else shutil.copy2)
        return dest.is_dir()
    except (OSError, shutil.Error) as e:
        # The hardlink route can fail (EXDEV, EPERM, a filesystem without links).
        # Fall back to a plain copy before giving up: without it the tree stays
        # deleted and the user has to run the printed `cp -a` by hand.
        print(f"      ! hardlinked restore failed ({e}); retrying with a plain copy",
              file=sys.stderr)
        try:
            if not rmtree_verified(dest):
                return False
            if backup.is_dir():
                shutil.copytree(backup, dest, symlinks=True, copy_function=shutil.copy2)
            return dest.is_dir()
        except (OSError, shutil.Error) as e2:
            print(f"      ! tree could not be restored: {e2}", file=sys.stderr)
            return False


def _hardlink_ok(path: Path) -> bool:
    """Were the files in the backup created with hardlinks (rough check)?"""
    try:
        for f in path.rglob("*"):
            if f.is_file() and f.stat().st_nlink > 1:
                return True
            if f.is_file():
                return False
    except OSError:
        pass
    return False



def _drop_extra_deps() -> list[str]:
    """Removes non-DSH desktop packages from the global package.json."""
    p = GLOBAL / "package.json"
    try:
        data = json.loads(p.read_text())
    except Exception:
        return []
    deps = data.get("dependencies", {})
    dropped = [n for n in EXTRA_NPM if n in deps]
    for n in dropped:
        deps.pop(n)
    if dropped:
        p.write_text(json.dumps(data, indent=2) + "\n")
    return dropped


def cache_version_matches(ver: str, target: str, path: Path | None = None) -> bool:
    """Does the cache entry represent the target version?

    Critical detail: the version string in bun cache directory names is an
    UNRELIABLE placeholder — it has the form
    `<major>.<minor>.<patch>-<commit-hash>` and hides the real version:

        0.1.7-3c59afc92f94c87b   →  real version 0.1.7-rc.2
        0.1.5-740e203097086e5e   →  real version 0.1.5-rc.3

    That is why `ver.startswith(target)` never works correctly. If the entry
    directory is given, the real version is read from `package.json`; otherwise
    it falls back to a cautious prefix comparison on the string.

    Only the exact target version is kept. Older builds of the same core
    (`0.2.0-rc.1` while the target is `0.2.0-rc.2`) are deleted like any other
    stale entry: keeping them contradicted what `plan` promises and left the
    download cache growing with every release. The cache is only a download
    cache — a re-download restores anything that is needed later.

    @param ver - the version string extracted from the cache directory name.
    @param target - the version to keep (e.g. "0.1.7-rc.2").
    @param path - the entry directory (if present, the real version is read from here).
    @returns True if it is the same version.
    """
    if ver == target:
        return True

    # 1) Read the real version from package.json (the actual source of truth).
    if path is not None:
        try:
            real = json.loads((path / "package.json").read_text()).get("version")
        except Exception:
            real = None
        if isinstance(real, str) and real:
            return real == target

    # 2) package.json could not be read: cautious comparison on the string.
    core_target = target.split("+")[0].partition("-")[0]
    if ver.split("+")[0].partition("-")[0] == core_target:
        return True
    return ver == target



def _prune_cache(target: str) -> tuple[int, int]:
    """Deletes DSH cache entries that do not match target. Returns (count, bytes).

    Only entries of `dsh` and `dsh-*` packages are pruned; other packages in
    the shared `@deepseek-ai` scope are left untouched (see
    `cache_entries`/`is_dsh_package`).

    Deletion goes through `remove_cache_entry`: symlink entries are not
    silently skipped by `shutil.rmtree`.
    """
    removed = freed = 0
    touched_pkgs: set[Path] = set()

    entries = cache_entries()
    # How many version symlinks point at each real cache directory? The last one
    # removed also removes the directory (see `remove_cache_entry`).
    link_counts: dict[str, int] = {}
    for _pkg, _ver, entry in entries:
        if not entry.is_symlink():
            continue
        try:
            key = str(Path(os.path.realpath(entry)))
        except OSError:
            continue
        link_counts[key] = link_counts.get(key, 0) + 1

    for _pkg, ver, path in entries:
        if cache_version_matches(ver, target, path):
            continue
        size = remove_cache_entry(path, link_counts)
        touched_pkgs.add(path.parent)
        removed += 1
        freed += size

    # Remove only the TOUCHED package directories, and only if they became empty.
    # (Previously all of CACHE_SCOPE was walked: every empty
    #  @deepseek-ai/foo/ directory of the user was silently deleted.)
    for pkg in touched_pkgs:
        try:
            if pkg.is_dir() and not any(pkg.iterdir()):
                pkg.rmdir()
        except OSError:
            pass
    return removed, freed



def _prune_dangling() -> int:
    n = 0
    for link in dangling_profile_links():
        link.unlink(missing_ok=True)
        n += 1
    return n


def core_version_copies() -> dict[str, int]:
    """Version -> copies of the CORE `dsh` package (inode-deduplicated).

    `deep_scan` groups every `dsh`/`dsh-*` file by version, which mixes the
    package itself with its dependencies. Only the core package is what
    `bun add -g @deepseek-ai/dsh@<version>` controls, so only its copies can be
    treated as removable leftovers.

    @returns version -> number of distinct copies.
    """
    counts: dict[str, int] = {}
    seen: set[tuple[int, int]] = set()
    for root in (GLOBAL_NM, PROFILE_NM):
        manifest = root / "@deepseek-ai/dsh/package.json"
        try:
            stat_result = manifest.stat()
        except OSError:
            continue
        key = (stat_result.st_dev, stat_result.st_ino)
        if key in seen:
            continue
        seen.add(key)
        try:
            version = json.loads(manifest.read_text()).get("version")
        except Exception:
            continue
        if isinstance(version, str) and version:
            counts[version] = counts.get(version, 0) + 1
    return counts


def _verify(target: str) -> bool:
    """Is the installation healthy: the target version, and nothing else?"""
    st = gather()
    ok = True
    print(f"      version in use   : {st['installed']}")
    if st["installed"] != target:
        print(f"      ! expected {target}, installed {st['installed']}")
        ok = False
    copies = core_version_copies()
    for v, n in sorted(copies.items(), key=lambda kv: vkey(kv[0])):
        if v != target:
            print(f"      ! another copy of the dsh package is at {v} ({n} path(s))")
            ok = False
    # Versions that belong to `dsh-*` dependencies are reported but are NOT a
    # failure: `bun add -g @deepseek-ai/dsh@<target>` cannot change how a
    # dependency resolves, so failing here made every apply exit 2 forever while
    # printing a fix command that could not work.
    if not any(v != target for v in copies):
        other = {v: n for v, n in st["versions"].items() if v != target}
        if other:
            summary = ", ".join(f"{v} ({n} files)"
                                for v, n in sorted(other.items(), key=lambda kv: vkey(kv[0])))
            print(f"      note: dsh-* dependencies resolve at other versions: {summary}")
            print("            (not removable by this tool; reported for information)")
    if st["dangling"]:
        print(f"      ! {len(st['dangling'])} dangling symlinks remain")
        ok = False
    if ok:
        print(f"      single version: {target} ✓")
    return ok


def _rollback(tree_backup: Path | None, saved: dict, previous: str | None) -> bool:
    """Restore the previous tree, the manifests and the service.

    Used by every failure path (failed install, timeout, interrupt, service that
    does not come up). Returns True when the tree was restored.

    @param tree_backup - the backup taken before the destructive step.
    @param saved - which of package.json/bun.lock were backed up.
    @param previous - the version that was installed before.
    """
    restored = False
    if tree_backup is not None and _restore_tree(tree_backup):
        restored = True
        if saved.get("bun.lock"):
            shutil.copy2(STATE_DIR / "bun.lock.bak", GLOBAL / "bun.lock")
        if saved.get("package.json"):
            # Without this the restored tree keeps the NEW manifest and the
            # `bun install` below re-installs the version we just rolled back.
            shutil.copy2(STATE_DIR / "package.json.bak", GLOBAL / "package.json")
        try:
            subprocess.run([str(BUN), "install"], cwd=str(GLOBAL),
                           capture_output=True, text=True, timeout=BUN_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"      ! dependencies could not be re-linked ({e}); manually: "
                  f"cd {GLOBAL} && {BUN} install", file=sys.stderr)
    else:
        print(f"      Manual rollback: cp -a {tree_backup} {GLOBAL_NM}/@deepseek-ai",
              file=sys.stderr)
        if saved.get("package.json"):
            print(f"        cp {STATE_DIR}/package.json.bak {GLOBAL}/package.json",
                  file=sys.stderr)
    systemctl("start", SERVICE)
    if restored and previous:
        print(f"      restored version: {previous}", file=sys.stderr)
    return restored


def _install_interrupt_guards() -> dict:
    """Turn SIGTERM/SIGHUP into KeyboardInterrupt during the destructive window.

    Python's default SIGTERM disposition kills the process without running any
    handler, which would skip the rollback exactly when it matters. Only the
    destructive window is guarded; the old dispositions are restored afterwards.

    @returns the previous handlers, for `_restore_signal_handlers`.
    """
    def _interrupt(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _interrupt)
        except (OSError, ValueError):
            continue
    return previous


def _restore_signal_handlers(previous: dict) -> None:
    """Restore the signal dispositions replaced by `_install_interrupt_guards`."""
    for sig, handler in (previous or {}).items():
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):
            continue


def _run_apply(target: str, mode: str, with_extras: bool = False,
               keep_cache: bool = False, keep_backup: bool = False) -> int:
    st = gather()
    if st["installed"] == target:
        print(f"{target} is already installed.")
        if not st["other_versions"] and not st["dangling"]:
            print("The tree is clean; there is nothing to do.")
            return 0
        print("However there are leftovers; they will be cleaned up.")

    if not BUN.is_file() or not os.access(BUN, os.X_OK):
        print(f"Error: bun executable not found or not executable at '{BUN}'.\n"
              "Please install Bun or set the BUN_INSTALL environment variable.",
              file=sys.stderr)
        return 1

    log_path = start_log(mode, f"installed={st['installed']} target={target} mode={mode}")
    if log_path is not None:
        print(f"Run log: {log_path}")

    saved = _backup()
    # `EXTRA_NPM` is intentionally empty (see its definition), so there is nothing
    # to drop from the global manifest: the old --with-extras branches were
    # unreachable and are gone.
    _drop_extra_deps() if with_extras else None

    print("\n[0/6] backing up the tree…")
    stale = STATE_DIR / "tree-backup"
    if stale.is_dir():
        print(f"      note: a previous run left a backup ({human(dir_size(stale))}); "
              f"it is replaced now")
    tree_backup = _backup_tree()
    if tree_backup is None:
        print("      ! The tree could not be backed up; not proceeding to the destructive step.", file=sys.stderr)
        print("        Free up the required space and try again.", file=sys.stderr)
        return 1
    print(f"      backup ready: {tree_backup}")

    (STATE_DIR / "rollback.json").write_text(json.dumps({
        "previous_dsh": st["installed"], "target": target, "mode": mode, "at": time.time(),
        "tree_backup": str(tree_backup), "files_backed_up": saved,
    }, indent=2) + "\n")

    # Everything from here on is destructive: the tree is deleted before the new
    # version is installed. Any escape — Ctrl-C, SIGTERM/SIGHUP (logout, stopping
    # the user manager), an unexpected OSError — must restore what was there
    # instead of leaving the machine without DSH and with the service stopped.
    old_handlers = _install_interrupt_guards()
    try:
        print(f"\n[1/6] stopping {SERVICE}…")
        r = systemctl("stop", SERVICE)
        stop_out = (r.stdout + r.stderr).strip()
        if stop_out:
            print("      " + stop_out)
        # The return code alone is not decisive (stopping an inactive unit is not
        # an error), so the service is asked directly. Replacing the tree under a
        # server that is still serving would leave open pages mixing old and new
        # assets.
        if not service_down():
            print(f"      ! {SERVICE} is still answering on {URL}.", file=sys.stderr)
            print("        The package tree was NOT touched.", file=sys.stderr)
            print(f"        Stop it and retry: systemctl --user stop {SERVICE}", file=sys.stderr)
            stop_log()
            return 1

        print("[2/6] deleting the old tree…")
        if not rmtree_verified(GLOBAL_NM / "@deepseek-ai"):
            print("      ! the old tree could not be fully deleted.", file=sys.stderr)
            print("        Restoring the previous tree from the backup…", file=sys.stderr)
            if _restore_tree(tree_backup):
                print("        previous tree restored; nothing was installed.", file=sys.stderr)
            else:
                print(f"        Manual: cp -a {tree_backup} {GLOBAL_NM}/@deepseek-ai", file=sys.stderr)
            systemctl("start", SERVICE)
            stop_log()
            return 1
        (GLOBAL / "bun.lock").unlink(missing_ok=True)

        print(f"[3/6] installing: {PKG}@{target}")
        try:
            r = subprocess.run([str(BUN), "add", "--global", f"{PKG}@{target}"],
                               capture_output=True, text=True, timeout=BUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            print(f"\n      INSTALLATION TIMED OUT after {BUN_TIMEOUT:.0f}s — "
                  "automatic rollback will be attempted.", file=sys.stderr)
            _rollback(tree_backup, saved, st["installed"])
            stop_log()
            return 1
        out = (r.stdout + r.stderr).strip()
        # The full output goes to the run log; the terminal only needs the tail.
        log_raw(f"\n--- bun add --global {PKG}@{target} (exit {r.returncode}) ---\n{out}\n")
        if len(out) > 1200:
            print("      " + out[-1200:])
            print("      (full bun output is in the run log)")
        else:
            print("      " + out)
        if r.returncode != 0:
            print("\n      INSTALLATION FAILED — automatic rollback will be attempted.", file=sys.stderr)
            if _rollback(tree_backup, saved, st["installed"]):
                print(f"      tree restored ({st['installed']})", file=sys.stderr)
            stop_log()
            return 1

        print("[4/6] cache cleanup…")
        if with_extras:
            # Kept for CLI compatibility: the list of third-party packages this
            # tool used to delete is empty on purpose (a public tool must not
            # remove packages it does not own).
            print("      no third-party packages are removed")
        if keep_cache:
            print(f"      skipped (--keep-cache) — cache kept at {len(cache_entries())} entries")
        else:
            n, freed = _prune_cache(target)
            print(f"      {n} entries deleted, {human(freed)} freed")
        d = _prune_dangling()
        if d:
            print(f"      {d} dangling profile symlinks cleaned up")

        print("[5/6] verification…")
        ok = _verify(target)

        print(f"[6/6] starting {SERVICE}…")
        systemctl("start", SERVICE)
        if not wait_service(90):
            print("      THE SERVICE DID NOT COME UP.", file=sys.stderr)
            # If the service does not come up, the installation is not healthy:
            # go back to the previous version.
            print("      Reverting to the previous version…", file=sys.stderr)
            if _rollback(tree_backup, saved, st["installed"]) and wait_service(60):
                print(f"      rolled back and the service is up ({st['installed']})",
                      file=sys.stderr)
                stop_log()
                return 1
            print(f"      Manually: cp -a {tree_backup} {GLOBAL_NM}/@deepseek-ai && "
                  f"systemctl --user restart {SERVICE}", file=sys.stderr)
            print(f"      Diagnostics: journalctl --user -u {SERVICE} -n 50", file=sys.stderr)
            stop_log()
            return 1
        print(f"      service is up ({URL})")

        for marker in BOOTSTRAP_MARKERS:
            if marker.exists():
                marker.unlink()
                print(f"      bootstrap reset: {marker}")

        # Successful installation: drop the temporary tree backup (disk space).
        # If the service is up, no rollback is needed; it can be kept with
        # `--keep-backup`.
        if tree_backup is not None and tree_backup.is_dir() and not keep_backup:
            if rmtree_verified(tree_backup):
                print(f"      temporary backup deleted: {tree_backup}")
            else:
                print(f"      ! the temporary backup could not be deleted "
                      f"({human(dir_size(tree_backup))}): {tree_backup}", file=sys.stderr)
                print(f"        Remove it manually: rm -rf {tree_backup}", file=sys.stderr)

        final = gather()
        if final["installed"] != target:
            # Exit code 2 means "installed, leftovers remain" and the GUI reports
            # it as a warning. Returning it for a target that is NOT installed
            # made a failed update look successful, so a missing target is a hard
            # failure (1).
            print(f"\nFAILED: {target} is not the version in use "
                  f"({final['installed'] or '?'}).", file=sys.stderr)
            print("Run 'dsh-update check' to see what is on disk.", file=sys.stderr)
            if log_path is not None:
                print(f"Full log: {log_path}")
            stop_log()
            return 1
    except BaseException as e:  # noqa: BLE001 - interrupts must roll back too
        print(f"\n      ! the update was interrupted or failed: "
              f"{e.__class__.__name__}: {e}", file=sys.stderr)
        print("        Restoring the previous version…", file=sys.stderr)
        if _rollback(tree_backup, saved, st["installed"]):
            print(f"        previous version restored ({st['installed']}).", file=sys.stderr)
        else:
            print("        The previous tree could NOT be restored automatically.",
                  file=sys.stderr)
            print(f"        Manual: cp -a {tree_backup} {GLOBAL_NM}/@deepseek-ai && "
                  f"systemctl --user restart {SERVICE}", file=sys.stderr)
        if log_path is not None:
            print(f"Full log: {log_path}")
        return 1
    finally:
        _restore_signal_handlers(old_handlers)
        stop_log()

    if ok:
        print("\nDone.")
    else:
        # Exit code 2: the new version IS installed, only leftover copies remain.
        print("\nDone: the new version is installed, but some leftovers remain.")
        print("Run 'dsh-update check' to see them; the next apply removes them.")
    print("Refresh the clients: 'Refresh' in the DSH shell, reload the page in the browser.")
    if log_path is not None:
        print(f"Full log: {log_path}")
    return 0 if ok else 2


def cmd_plan() -> int:
    st = gather()
    data = fetch_registry()
    if "error" in data:
        print(f"registry unreadable: {data['error']}")
        return 1
    rc = newest_installable(data)
    if not rc:
        print("no installable version found")
        return 1
    print_plan(st, rc, "update to the newest rc + delete all other versions")
    return 0


def cmd_apply(argv: list[str]) -> int:
    yes = "--yes" in argv
    with_extras = "--with-extras" in argv
    keep_cache = "--keep-cache" in argv
    keep_backup = "--keep-backup" in argv
    data = fetch_registry()
    if "error" in data:
        print(f"registry unreadable: {data['error']}")
        return 1
    rc = newest_installable(data)
    if not rc:
        print("no installable version found")
        return 1

    st = gather()
    # Guard against a silent downgrade: if the installed version is newer than
    # anything npm still offers (an rc can be deprecated or withdrawn after
    # publication), `ghcheck`/`notify` report "up to date" while `apply` would
    # happily move the machine backwards.
    if (st["installed"] and rc != st["installed"]
            and not newer(rc, st["installed"])
            and "--allow-downgrade" not in argv):
        print(f"Refusing to install {rc}: the installed version {st['installed']} is newer.")
        print("This happens when the newer version was deprecated or withdrawn on npm.")
        print("Re-run with --allow-downgrade if you really want to move backwards.")
        return 1
    print_plan(st, rc, "update to the newest rc + delete all other versions")
    others = find_other_installs()
    files = [p for k, _d, p in others if k == "archive"]
    if files and not with_extras:
        print(f"\n  Note: {len(files)} DSH archives are also present (archives are not deleted by this command):")
        for p in files:
            print(f"      {p}")
        print("      To delete: dsh-update extras --remove --yes")
    print()
    if not yes:
        if not sys.stdin.isatty():
            print("--yes is required for confirmation (no interactive terminal).")
            return 1
        if input("Continue? type 'yes': ").strip().lower() != "yes":
            print("Cancelled.")
            return 1
    return _run_apply(rc, "apply", with_extras=with_extras,
                      keep_cache=keep_cache, keep_backup=keep_backup)


def cmd_prune(argv: list[str]) -> int:
    """Cleanup without changing versions: leaves a single copy of the current version."""
    yes = "--yes" in argv
    st = gather()
    if not st["installed"]:
        print("The installed version could not be read.")
        return 1
    print_plan(st, st["installed"], "cleanup (version does not change)")
    print()
    if not yes:
        if not sys.stdin.isatty():
            print("--yes is required for confirmation.")
            return 1
        if input("Continue? type 'yes': ").strip().lower() != "yes":
            print("Cancelled.")
            return 1
    return _run_apply(st["installed"], "prune")


def cmd_full(argv: list[str]) -> int:
    """ONE COMMAND: set up the timer + delete leftovers + update to the newest rc + verify.

    Options:
      --yes         do not ask for confirmation
      --no-extras   do not touch non-DSH archives/npm packages
      --no-timer    do not touch the systemd timer
      --keep-cache  skip pruning the download cache
    """
    yes = "--yes" in argv
    keep_cache = "--keep-cache" in argv
    keep_backup = "--keep-backup" in argv
    no_extras = "--no-extras" in argv
    no_timer = "--no-timer" in argv

    print("=" * 70)
    print("DSH SINGLE STEP: update to the newest rc + delete leftovers + set up the timer")
    print("=" * 70)

    data = fetch_registry()
    if "error" in data:
        print(f"\nregistry unreadable: {data['error']}")
        return 1
    rc = newest_installable(data)
    if not rc:
        print("\nno installable version found")
        return 1

    st = gather()
    others = find_other_installs()
    appimages = [p for k, _d, p in others if k == "archive"]
    npmx = [npm_name(p) for k, _d, p in others if k == "npm package"]
    trees = [p for k, _d, p in others if k == "install"]

    print("\nTo be done:")
    n = 0

    def step(text: str) -> None:
        nonlocal n
        n += 1
        print(f"  {n}. {text}")

    if not no_timer:
        step("the systemd timer will be installed/enabled (notification at startup + twice a day)")
    if appimages and not no_extras:
        for p in appimages:
            if _deletable_archive(p):
                step(f"archive to delete: {p}")
            else:
                step(f"archive KEPT (not a download location): {p}")
    if npmx and not no_extras:
        step("global packages to remove: " + "  ".join(npmx))
    step(f"DSH will be updated: {st['installed'] or '?'} → {rc}")
    if st["other_versions"]:
        step("old package versions to delete: "
             + ", ".join(sorted(st["other_versions"])))
    if st["dangling"]:
        step(f"dangling symlinks to clean up: {len(st['dangling'])}")
    if st["cache"] and not keep_cache:
        step(f"download cache to prune: {len(st['cache'])} entries / {human(st['cache_size'])}")
    step("the service will be restarted and verified")

    if trees:
        print("\n  ! There is ANOTHER DSH INSTALL TREE; this command does not touch it, inspect it manually:")
        for p in trees:
            print(f"      {p}")
    print("\n  ! dsh-web.service will stop: DSH goes down while this command runs.")
    print("    Session history is preserved. Run it from a terminal and wait for it to finish.")

    if not yes:
        if not sys.stdin.isatty():
            print("\n--yes is required for confirmation (no interactive terminal).")
            return 1
        if input("\nContinue? type 'yes': ").strip().lower() != "yes":
            print("Cancelled.")
            return 1

    if not no_timer:
        print("\n[1/3] systemd timer…")
        r = systemctl("daemon-reload")
        if r.returncode != 0 and (r.stderr or r.stdout).strip():
            print("      " + (r.stdout + r.stderr).strip())
        r = systemctl("enable", "--now", "dsh-update-check.timer")
        print("      " + ((r.stdout + r.stderr).strip() or "ok"))
        if r.returncode != 0:
            print("      ! the timer could not be set up; continuing with the installation.")

    if appimages and not no_extras:
        print("\n[2/3] deleting DSH archives…")
        kept = 0
        for p in appimages:
            # Same rule as `extras --remove`: only download-like locations are
            # disposable. Without this filter `full` deleted files that merely
            # looked like installers inside project directories, which is exactly
            # what the narrower scope was introduced to prevent.
            if not _deletable_archive(p):
                kept += 1
                print(f"      kept (not a download location): {p}")
                continue
            try:
                size = p.stat().st_size
                p.unlink()
                print(f"      deleted: {p} ({human(size)})")
            except OSError as e:
                print(f"      could not delete: {p} ({e})")
        if kept:
            print(f"      {kept} file(s) kept: delete them by hand if they really are leftovers")
        for d in {p.parent for p in appimages if _deletable_archive(p)}:
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
                    print(f"      empty directory removed: {d}")
            except OSError:
                pass
    else:
        print("\n[2/3] archive/npm leftovers skipped"
              + (" (--no-extras)" if no_extras else " (none found)"))

    print("\n[3/3] update…")
    rc_result = _run_apply(rc, "full", with_extras=not no_extras,
                           keep_cache=keep_cache, keep_backup=keep_backup)
    if rc_result == 1:
        print("\nThe update FAILED; see the rollback command above.",
              file=sys.stderr)
        return 1
    if rc_result == 2:
        print("\nNote: some leftovers remain; look at the '!' lines in the report below.")

    print("\n" + "=" * 70)
    print("FINAL STATE")
    print("=" * 70)
    check_code = cmd_check()
    if check_code != 0:
        print("\n! the final check could not be completed; run 'dsh-update check' later.",
              file=sys.stderr)

    print("\n" + "=" * 70)
    print("EQUIVALENT OF THIS COMMAND (if you want to type it out step by step)")
    print("=" * 70)
    if not no_timer:
        print("  systemctl --user daemon-reload && "
              "systemctl --user enable --now dsh-update-check.timer")
    if not no_extras:
        print("  dsh-update extras --remove --yes")
    print("  dsh-update apply --with-extras" + ("  --keep-cache" if keep_cache else ""))
    print("  dsh-update check")
    print("\n  (Note: `plan` and `apply` now find the target version themselves; "
          "the old `apply latest --clean` / `plan next` patterns are unnecessary.)")

    st2 = gather()
    body = (f"Installed version: {st2['installed']}\n"
            f"Other version copies: {sum(st2['other_versions'].values())}\n"
            f"Refresh the clients: 'Refresh' in the shell, reload the page in the browser.")
    delivered, reason, _ = notify_send("DSH updated", body)
    if delivered:
        print("\nThe completion notification was sent (the desktop notification path works).")
    else:
        print(f"\n! The completion notification could not be sent: {reason}")
    # Propagate the honest code: 2 means "installed, leftovers remain". Returning
    # 0 here made a run with leftovers indistinguishable from a clean one.
    return 2 if rc_result == 2 else 0


def cmd_extras(argv: list[str]) -> int:
    """Lists DSH assets outside the main trees; with --remove it deletes archives.

    It only deletes FILES. It does not touch package trees or npm packages; for
    those it suggests a command (tree changes need the service stopped).
    """
    remove = "--remove" in argv and "--yes" in argv
    others = find_other_installs()
    if not others:
        print("No DSH assets found outside the main trees.")
        return 0

    print("DSH assets outside the main trees:")
    for kind, desc, path in others:
        print(f"  [{kind:10s}] {desc:10s} {path}")

    files = [(d, p) for k, d, p in others if k == "archive"]
    trees = [p for k, _d, p in others if k == "install"]
    pkgs = [npm_name(p) for k, _d, p in others if k == "npm package"]
    print()

    if trees:
        print("  ! ANOTHER DSH INSTALL TREE was found. Do not delete it without manual inspection:")
        for p in trees:
            print(f"      {p}")
    if pkgs:
        print("  Global npm packages (not tied to the profiles, sitting idle):")
        print("      " + "  ".join(pkgs))
        print("      With the service stopped: bun remove --global " + " ".join(pkgs))

    if not remove:
        if files:
            print(f"\n  {len(files)} archive files can be deleted: dsh-update extras --remove --yes")
        return 0

    total = 0
    kept = 0
    for _d, p in files:
        if not _deletable_archive(p):
            kept += 1
            print(f"  kept (not a download location): {p}")
            continue
        try:
            size = p.stat().st_size
            p.unlink()
            total += size
            print(f"  deleted: {p}")
        except OSError as e:
            print(f"  could not delete: {p} ({e})", file=sys.stderr)
    if kept:
        print(f"  {kept} file(s) kept: delete them by hand if they really are leftovers")
    print(f"  {human(total)} freed")
    return 0


# --------------------------------------------------------------------------- #

def _acquire_lock(quiet: bool = False):
    """Prevent two updates from running at the same time (so the tree is not corrupted).

    The returned file object must be kept alive; closing it releases the lock.
    With `quiet` the caller handles the message itself (the timer's notify run is
    expected to be skipped silently while an update is in progress).

    @param quiet - do not print the "another dsh-update is running" message.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = open(STATE_DIR / ".lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        if not quiet:
            print("dsh-update: another dsh-update is running; try again when it finishes.",
                  file=sys.stderr)
        return None
    return f


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "check"
    rest = args[1:]
    if cmd in ("check", "--check", "-c"):
        return cmd_check()
    if cmd in ("ghcheck", "gh-check", "--ghcheck", "github"):
        return cmd_ghcheck()
    if cmd in ("notify", "--notify"):
        return cmd_notify()
    if cmd in ("plan", "--plan"):
        return cmd_plan()
    if cmd in ("extras", "--extras"):
        return cmd_extras(rest)
    # Commands that change the tree run under the lock
    if cmd in ("full", "--full", "all", "--all", "setup"):
        lock = _acquire_lock()
        return 1 if lock is None else cmd_full(rest)
    if cmd in ("apply", "--apply"):
        lock = _acquire_lock()
        return 1 if lock is None else cmd_apply(rest)
    if cmd in ("prune", "--prune"):
        lock = _acquire_lock()
        return 1 if lock is None else cmd_prune(rest)
    # 2 means "installed, leftovers remain" and the tray treats it as success.
    # A usage error must not borrow that meaning: use 1 (and 0 for an explicit
    # help request).
    if cmd in ("help", "--help", "-h"):
        print(__doc__)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
