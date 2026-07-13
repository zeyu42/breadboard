"""Spawn and manage a per-session Breadboard JVM subprocess.

The MCP server can fire up a fresh Breadboard instance with its own port,
H2 database, and dev/ directory so multiple Claude Code sessions (or
worktrees) don't race on a single shared Breadboard. Termination is
automatic (atexit) plus exposed as an explicit tool.

Per-session layout (under ~/.breadboard-mcp/sessions/<id>/):
    db/                  -- session-private H2 file
    dev/                 -- session-private file-mode experiment dir
    logs/                -- session-private Play logs
    RUNNING_PID          -- written by Play
    stdout.log           -- combined stdout/stderr of the JVM

JVM overrides:
    -Dhttp.port=<picked>
    -Dpidfile.path=<workdir>/RUNNING_PID
    -Ddb.default.url=jdbc:h2:file:<workdir>/db/breadboard;MODE=MYSQL
    -Duser.dir=<workdir>             (overrides the script's own user.dir)
    -DapplyEvolutions.default=true   (auto-apply evolutions in PROD mode)
    -Dmcp.enabled=true               (unlock /debug/* routes — off by default)
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


_IS_WINDOWS = sys.platform == "win32"


_DEFAULT_SESSION_PARENT = Path.home() / ".breadboard-mcp" / "sessions"
_READY_TIMEOUT = 90.0
_DEFAULT_ADMIN_EMAIL = "admin@example.com"
_DEFAULT_ADMIN_PASSWORD = "admin123"

# Single in-process spawned instance. One MCP server -> one Breadboard.
_spawned: dict[str, Any] | None = None


# ---------------------------------------------------------------- helpers

def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _repo_root_default() -> Path:
    # .../breadboard/mcp-server/src/breadboard_mcp/spawner.py -> breadboard/
    return Path(__file__).resolve().parents[3]


def _staged_binary(repo_root: Path) -> Path:
    override = os.environ.get("BREADBOARD_STAGED_BIN")
    if override:
        return Path(override)
    # Play's `sbt stage` produces a Unix shell script + a `.bat` for Windows.
    name = "breadboard.bat" if _IS_WINDOWS else "breadboard"
    candidate = repo_root / "target" / "universal" / "stage" / "bin" / name
    if not candidate.exists():
        raise FileNotFoundError(
            f"Staged Breadboard binary not found at {candidate}. "
            "Run `sbt stage` in the repo root first, or set BREADBOARD_STAGED_BIN."
        )
    return candidate


def _create_workdir(repo_root: Path) -> Path:
    parent = Path(os.environ.get("BREADBOARD_MCP_SESSION_DIR",
                                 str(_DEFAULT_SESSION_PARENT)))
    workdir = parent / uuid.uuid4().hex[:8]
    for sub in ("db", "dev", "logs"):
        (workdir / sub).mkdir(parents=True, exist_ok=True)

    # Several pieces of Breadboard and individual experiments read files
    # from paths relative to `user.dir` (which we override to the session
    # workdir). Link (or copy on Windows) the repo-root dirs they might
    # need so those reads succeed:
    #   groovy/      - ScriptBoard.resetEngine reads the bundled scripts
    #                  (util/timer/graph/...) from `<user.dir>/groovy/`.
    #   data/        - Experiment-specific data files (e.g. CSV-driven
    #                  treatment pools loaded at engine startup).
    # On Windows we copy instead of symlinking — Windows symlinks require
    # admin or Developer Mode, but junctions would require shelling out to
    # `mklink /J`. Copy is simpler. Tradeoff: edits to the source dirs
    # made mid-session won't be picked up until respawn.
    for name in ("groovy", "data"):
        src = repo_root / name
        dst = workdir / name
        if not src.is_dir() or dst.exists():
            continue
        if _IS_WINDOWS:
            shutil.copytree(src, dst)
        else:
            dst.symlink_to(src)
    return workdir


def _wait_for_ready(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=2.0, follow_redirects=False)
            # Anything that's not a connection failure or 5xx means the
            # server is up. Play returns 303 -> /login for unauthenticated
            # GET /.
            if r.status_code < 500:
                return
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            last_err = e
        time.sleep(0.5)
    raise TimeoutError(
        f"Breadboard at {url} did not become ready within {timeout:.0f}s "
        f"(last connect error: {last_err!r})"
    )


def _bootstrap_schema(url: str) -> None:
    """Work around an upstream Breadboard bug: evolution 28 creates an empty
    breadboard_version table which short-circuits Global.onStart()'s schema
    upgrade path, so the `experiments.file_mode` column never gets added on a
    fresh DB. The /debug/bootstrap-schema endpoint runs the missing ALTER
    idempotently."""
    r = httpx.post(f"{url}/debug/bootstrap-schema", timeout=10.0)
    if r.status_code != 200:
        raise RuntimeError(
            f"POST /debug/bootstrap-schema failed ({r.status_code}): {r.text[:300]}"
        )


def _find_english_id(url: str) -> int:
    """Look up the seeded English language id (or fall back to the first
    language). Locale order from JVM is not stable, so we can't assume id=1."""
    r = httpx.get(f"{url}/languages", timeout=10.0)
    r.raise_for_status()
    langs = r.json().get("languages", [])
    for lang in langs:
        if lang.get("code") == "eng":
            return int(lang["id"])
    if langs:
        return int(langs[0]["id"])
    raise RuntimeError("Breadboard /languages returned no languages")


def _seed_admin(url: str, email: str, password: str, language_id: int) -> None:
    """POST /createFirstUser. Idempotent: 400 means the user table is
    already non-empty, which is fine for our purposes."""
    r = httpx.post(
        f"{url}/createFirstUser",
        json={"email": email, "password": password,
              "defaultLanguageId": language_id},
        timeout=15.0,
    )
    if r.status_code not in (200, 400):
        raise RuntimeError(
            f"POST /createFirstUser failed ({r.status_code}): {r.text[:300]}"
        )


def _public(state: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state.items() if not k.startswith("_")}


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. Uses kill(pid, 0) — sends
    no signal, just checks existence."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user. From our POV, alive.
        return True


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def list_alive_spawns() -> list[dict[str, Any]]:
    """Discover Breadboards spawned by any MCP process (this one or
    siblings) that are currently alive. Reads `metadata.json` from each
    session workdir and filters to those whose `RUNNING_PID` is alive.
    Each entry contains the same fields as `spawn_breadboard()` returns,
    plus an `owner_alive` boolean: True if the spawning MCP process is
    still running, False if it's an orphan (use cleanup_orphans to reap).
    """
    parent = Path(os.environ.get("BREADBOARD_MCP_SESSION_DIR",
                                 str(_DEFAULT_SESSION_PARENT)))
    if not parent.is_dir():
        return []
    alive: list[dict[str, Any]] = []
    for workdir in parent.iterdir():
        if not workdir.is_dir():
            continue
        meta_path = workdir / "metadata.json"
        jvm_pid = _read_pid(workdir / "RUNNING_PID")
        if not meta_path.exists() or jvm_pid is None or not _pid_alive(jvm_pid):
            continue
        try:
            entry = json.loads(meta_path.read_text())
        except (ValueError, OSError):
            continue
        owner_pid = _read_pid(workdir / "owner.pid")
        entry["owner_pid"] = owner_pid
        entry["owner_alive"] = (owner_pid is not None and _pid_alive(owner_pid))
        alive.append(entry)
    return alive


def cleanup_orphans(dry_run: bool = False) -> list[dict[str, Any]]:
    """Find (and by default terminate) orphaned Breadboard JVMs from
    previous MCP sessions that crashed (SIGKILL, segfault, etc.) without
    running the atexit handler.

    A session is considered orphaned when its `owner.pid` (the MCP
    Python process that spawned it) is no longer alive but the JVM's
    `RUNNING_PID` is. Returns a list of metadata for each orphan found
    (workdir, jvm_pid, owner_pid).

    Pass `dry_run=True` to just discover orphans without killing them —
    useful for "are there any orphans?" diagnostics.

    Safe to call any time; only acts on sessions whose owner has
    disappeared. Will not touch sessions belonging to a currently-running
    MCP process.
    """
    parent = Path(os.environ.get("BREADBOARD_MCP_SESSION_DIR",
                                 str(_DEFAULT_SESSION_PARENT)))
    if not parent.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for workdir in parent.iterdir():
        if not workdir.is_dir():
            continue
        owner_pid = _read_pid(workdir / "owner.pid")
        jvm_pid = _read_pid(workdir / "RUNNING_PID")
        # Skip sessions whose owner MCP is still alive — that's a healthy
        # live spawn (possibly from a sibling MCP process).
        if owner_pid is not None and _pid_alive(owner_pid):
            continue
        # Be conservative when owner.pid is missing: could be from a
        # prior-version spawner that didn't write it, or a workdir
        # mid-creation. Don't touch the JVM in that case.
        if owner_pid is None:
            if not dry_run and (jvm_pid is None or not _pid_alive(jvm_pid)):
                (workdir / "RUNNING_PID").unlink(missing_ok=True)
            continue
        # Owner is dead. If the JVM is also gone, just remove stale files.
        if jvm_pid is None or not _pid_alive(jvm_pid):
            if not dry_run:
                for f in ("owner.pid", "RUNNING_PID"):
                    (workdir / f).unlink(missing_ok=True)
            continue
        # True orphan: owner exists in record but is dead, JVM alive.
        found.append({
            "workdir": str(workdir),
            "jvm_pid": jvm_pid,
            "owner_pid": owner_pid,
        })
        if dry_run:
            continue
        # On Windows, os.kill(pid, SIGTERM) maps to TerminateProcess —
        # already a forceful kill, no SIGKILL escalation possible (or
        # needed). On Unix, follow up with SIGKILL if SIGTERM is ignored.
        try:
            os.kill(jvm_pid, signal.SIGTERM)
            for _ in range(20):
                if not _pid_alive(jvm_pid):
                    break
                time.sleep(0.25)
            else:
                if not _IS_WINDOWS:
                    os.kill(jvm_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for f in ("owner.pid", "RUNNING_PID"):
            (workdir / f).unlink(missing_ok=True)
    return found


# ----------------------------------------------------------- public API

def spawn_breadboard(
    repo_root: str | None = None,
    admin_email: str | None = None,
    admin_password: str | None = None,
) -> dict[str, Any]:
    """Start a per-session Breadboard subprocess. Returns metadata
    { url, port, pid, workdir, session_id, admin_email, admin_password }.

    If a Breadboard is already spawned and still alive, returns its
    metadata without starting another. If a previous spawn died, the dead
    state is cleared and a fresh one is started."""
    global _spawned

    if _spawned is not None:
        proc: subprocess.Popen = _spawned["_proc"]
        if proc.poll() is None:
            return _public(_spawned)
        # previous JVM died — clean up state, fall through to respawn
        _spawned["_log_fd"].close()
        _spawned = None

    # Opportunistically reap orphans from prior crashed MCP sessions.
    cleanup_orphans()

    root = Path(repo_root) if repo_root else _repo_root_default()
    binary = _staged_binary(root)
    port = _pick_free_port()
    workdir = _create_workdir(root)
    # Mark this workdir as owned by the current MCP process. cleanup_orphans
    # uses this to distinguish "live spawn from a sibling MCP" from "JVM
    # whose owner died without cleaning up."
    (workdir / "owner.pid").write_text(str(os.getpid()))
    email = admin_email or _DEFAULT_ADMIN_EMAIL
    password = admin_password or _DEFAULT_ADMIN_PASSWORD

    cmd = [
        str(binary),
        f"-Dhttp.port={port}",
        f"-Dpidfile.path={workdir}/RUNNING_PID",
        f"-Ddb.default.url=jdbc:h2:file:{workdir}/db/breadboard;MODE=MYSQL",
        f"-Duser.dir={workdir}",
        "-DapplyEvolutions.default=true",
        # /debug/* routes are gated by Global.onRequest +
        # Secured.onUnauthorized and return 404 unless this flag is set.
        # Spawn mode always needs them on.
        "-Dmcp.enabled=true",
    ]
    log_path = workdir / "stdout.log"
    log_fd = open(log_path, "w")
    proc = subprocess.Popen(
        cmd,
        cwd=str(workdir),
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        env=os.environ.copy(),
    )

    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_ready(url, _READY_TIMEOUT)
        _bootstrap_schema(url)
        language_id = _find_english_id(url)
        _seed_admin(url, email, password, language_id)
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fd.close()
        raise

    _spawned = {
        "url": url,
        "port": port,
        "pid": proc.pid,
        "workdir": str(workdir),
        "session_id": workdir.name,
        "admin_email": email,
        "admin_password": password,
        "log_file": str(log_path),
        "_proc": proc,
        "_log_fd": log_fd,
    }
    # Persist discoverable metadata so sibling MCPs can find + attach
    # to this spawn via list_alive_spawns().
    (workdir / "metadata.json").write_text(json.dumps({
        k: v for k, v in _spawned.items() if not k.startswith("_")
    }, indent=2))
    return _public(_spawned)


def terminate_breadboard(timeout: float = 10.0) -> dict[str, Any]:
    """Stop the spawned Breadboard. Returns metadata of what was stopped,
    or {"status": "no-spawn"} if nothing was running."""
    global _spawned
    if _spawned is None:
        return {"status": "no-spawn"}
    proc: subprocess.Popen = _spawned["_proc"]
    log_fd = _spawned["_log_fd"]
    info = _public(_spawned)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
    log_fd.close()
    # Clean termination - drop our ownership marker and discoverable
    # metadata so future cleanup_orphans() / list_alive_spawns() don't
    # see a ghost entry.
    workdir = Path(info["workdir"])
    (workdir / "owner.pid").unlink(missing_ok=True)
    (workdir / "metadata.json").unlink(missing_ok=True)
    info["status"] = "terminated"
    info["exit_code"] = proc.returncode
    _spawned = None
    return info


def get_spawned() -> dict[str, Any] | None:
    if _spawned is None:
        return None
    proc: subprocess.Popen = _spawned["_proc"]
    info = _public(_spawned)
    info["alive"] = proc.poll() is None
    return info


@atexit.register
def _atexit_cleanup() -> None:
    if _spawned is not None:
        try:
            terminate_breadboard(timeout=5.0)
        except Exception:
            # atexit handlers should never raise
            pass
    # Best-effort: clean up orphans from sibling MCPs that died abnormally.
    # Cheap if there are no orphans. Quiet on failure.
    try:
        cleanup_orphans()
    except Exception:
        pass
