"""Breadboard MCP server.

Exposes a small set of tools an LLM agent can use to inspect and debug
Breadboard experiments running on a Breadboard server (default
http://localhost:9000). The server authenticates as an admin user using
credentials from environment variables and holds the Play session cookie
for the lifetime of the process.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

from . import spawner
from .client import BreadboardClient, from_env

log = logging.getLogger("breadboard_mcp")

mcp = FastMCP("breadboard")

# A single, lazily-initialized client lives for the life of the server
# process so we don't re-login on every tool call.
_client: BreadboardClient | None = None

# An explicit attach (via attach_breadboard) wins over the spawned
# instance and over auto-spawn. Cleared by detach_breadboard.
_attached: dict[str, str] | None = None


def _bb() -> BreadboardClient:
    """Return the active BreadboardClient.

    Resolution order:
      1. An explicit attach (set via attach_breadboard tool) — used if present.
      2. The current spawn (from spawn_breadboard) — used if present.
      3. Otherwise: auto-spawn a fresh session-private Breadboard.

    Auto-spawn is the default to make the tool work out-of-the-box with
    no prior setup. To talk to an already-running Breadboard instead,
    call attach_breadboard(url=...).
    """
    global _client
    if _attached:
        target_url = _attached["url"]
        target_email = _attached["email"]
        target_password = _attached["password"]
    else:
        spawn = spawner.get_spawned()
        if spawn is None:
            # default behavior: auto-spawn
            spawn = spawner.spawn_breadboard()
        target_url = spawn["url"]
        target_email = spawn["admin_email"]
        target_password = spawn["admin_password"]

    if _client is None or _client.base_url != target_url.rstrip("/"):
        if _client is not None:
            _client.close()
        _client = BreadboardClient(target_url, target_email, target_password)
        _client.login()
    return _client


def _reset_client() -> None:
    """Drop the cached client so the next _bb() call re-resolves and
    re-logs in. Use after changing attach/spawn state."""
    global _client
    if _client is not None:
        _client.close()
        _client = None


# ------------------------------------------------------- attach exclusivity

_ATTACH_LOCK_DIR = Path.home() / ".breadboard-mcp" / "attach-locks"


def _normalize_url(url: str) -> str:
    """Canonical form for keying locks. Collapses localhost/127.0.0.1,
    forces an explicit port, drops trailing path / query."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "localhost":
        host = "127.0.0.1"
    scheme = (parts.scheme or "http").lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return f"{scheme}://{host}:{port}"


def _attach_lock_path(url: str) -> Path:
    _ATTACH_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(_normalize_url(url).encode()).hexdigest()[:16]
    return _ATTACH_LOCK_DIR / f"{h}.lock"


def _try_acquire_attach_lock(url: str) -> dict[str, Any]:
    """Try to claim exclusive attach to `url`. Returns:
        {"acquired": True}                                     on success
        {"acquired": False, "holder_pid": X, "kind": "spawn"}  url is a
                                              sibling MCP's spawn target
        {"acquired": False, "holder_pid": X, "kind": "attach"} another
                                              MCP already attached
    Stale locks (dead holder) are cleaned up and retried automatically."""
    norm = _normalize_url(url)
    # 1. Refuse if a sibling MCP owns this URL as a spawn.
    for sp in spawner.list_alive_spawns():
        if _normalize_url(sp["url"]) != norm:
            continue
        if sp.get("owner_alive") and sp.get("owner_pid") != os.getpid():
            return {"acquired": False,
                    "holder_pid": sp.get("owner_pid"),
                    "kind": "spawn"}
    # 2. Try to create the attach lock file exclusively.
    lock_path = _attach_lock_path(url)
    while True:
        try:
            fd = os.open(str(lock_path),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, json.dumps({
                    "mcp_pid": os.getpid(), "url": url,
                }).encode())
            finally:
                os.close(fd)
            return {"acquired": True}
        except FileExistsError:
            holder_pid = None
            try:
                holder_pid = json.loads(lock_path.read_text()).get("mcp_pid")
            except (FileNotFoundError, ValueError):
                pass
            if holder_pid == os.getpid():
                return {"acquired": True}  # idempotent re-acquire
            if holder_pid and spawner._pid_alive(holder_pid):
                return {"acquired": False,
                        "holder_pid": holder_pid,
                        "kind": "attach"}
            # stale lock — owner dead. Remove and retry.
            lock_path.unlink(missing_ok=True)


def _release_attach_lock(url: str | None) -> None:
    if not url:
        return
    lock = _attach_lock_path(url)
    try:
        data = json.loads(lock.read_text())
        if data.get("mcp_pid") == os.getpid():
            lock.unlink()
    except (FileNotFoundError, ValueError):
        pass


@atexit.register
def _release_held_attach_lock_on_exit() -> None:
    if _attached:
        try:
            _release_attach_lock(_attached["url"])
        except Exception:
            pass


def _pretty(value: Any) -> str:
    """Tools must return text. Render structured values as compact JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, default=str, sort_keys=False)


# -------------------------------------------------------------------- tools

@mcp.tool()
def list_experiments() -> str:
    """List all experiments owned by the configured admin user.

    Returns an array of { id, name, uid, fileMode, instanceCount }.
    """
    return _pretty(_bb().list_experiments())


@mcp.tool()
def get_experiment(experiment_id: int) -> str:
    """Get full metadata for one experiment: steps (with Groovy source),
    content, parameters, languages, instance stubs, and styling.

    Use this to read step source code or content before deciding what to
    inspect at runtime.
    """
    return _pretty(_bb().get_experiment(experiment_id))


@mcp.tool()
def list_instances(experiment_id: int) -> str:
    """List all instances (runs) of an experiment.

    Each stub includes id, name, status (RUNNING / TESTING / STOPPED /
    FINISHED / ARCHIVED), creationTime, AMT hit info, and the per-instance
    Data values (parameters at run time).
    """
    return _pretty(_bb().list_instances(experiment_id))


@mcp.tool()
def get_instance(instance_id: int) -> str:
    """Get one experiment instance by id (stub form: no event list)."""
    return _pretty(_bb().get_instance(instance_id))


@mcp.tool()
def get_instance_events(
    instance_id: int,
    limit: int = 200,
    offset: int = 0,
    name_filter: str | None = None,
) -> str:
    """Get the event log for an instance, ordered by datetime.

    Use limit/offset to page through long logs (default limit 200). Pass
    name_filter to substring-match on event name (e.g. "Choice", "Step",
    "Joined").
    """
    return _pretty(_bb().get_instance_events(instance_id, limit, offset, name_filter))


@mcp.tool()
def create_experiment(name: str, copy_experiment_id: int | None = None) -> str:
    """Create a new experiment owned by the configured admin user. Returns
    the created experiment as JSON (use the `id` field for follow-up calls).

    If `copy_experiment_id` is given, the new experiment is initialized as a
    copy of an existing one; otherwise it starts blank.
    """
    return _pretty(_bb().create_experiment(name, copy_experiment_id))


@mcp.tool()
def get_experiment_paths(experiment_id: int) -> str:
    """Return the on-disk paths Breadboard uses for this experiment's
    file-mode directory (devDirectory, stepsDir, contentDir, parametersFile,
    clientHtmlFile, clientGraphFile, styleFile). Useful for verifying where
    `sync_experiment_files` will write to.
    """
    return _pretty(_bb().get_experiment_paths(experiment_id))


@mcp.tool()
def set_file_mode(experiment_id: int, enabled: bool | None = None) -> str:
    """Turn file mode on or off for an experiment. When turning on,
    Breadboard exports the current experiment into `dev/<directoryName>/`.
    When turning off, it re-imports from there. Pass `enabled` to set a
    specific value, or omit to toggle.

    Call this before `sync_experiment_files` so the dev/ directory exists
    and the experiment reads its content from there.
    """
    return _pretty(_bb().set_file_mode(experiment_id, enabled))


@mcp.tool()
def sync_experiment_files(
    experiment_id: int,
    source_dir: str,
    public_root: str = "/generated/",
    dev_mode: bool = True,
) -> str:
    """Copy a directory tree of experiment files into the Breadboard server's
    dev/<directoryName>/ for this experiment — the MCP-side equivalent of
    `npm run serve` in the breadboard-v2.4-default template.

    `source_dir` should contain (any subset of) `Steps/`, `Content/`,
    `Images/`, `parameters.csv`, `client-html.html`, `client-graph.js`,
    `style.css`. The `Steps/` folder is normalized to lowercase `steps/`
    to match what Breadboard's file-mode reader expects.
    `client-graph.js` has `__PUBLIC_ROOT__`, `__DEV__`, `__PROD__` placeholders
    substituted the same way the webpack CopyPlugin does.

    Returns a manifest of files written.
    """
    return _pretty(
        _bb().sync_experiment_files(experiment_id, source_dir, public_root, dev_mode)
    )


@mcp.tool()
def set_selected_experiment(experiment_id: int) -> str:
    """Set the configured admin user's currently-selected experiment. This
    is the value the script engine reads to figure out which experiment's
    bindings to use. Note: setting this does NOT itself rebuild the script
    engine — for full live debug, the user still needs to load an instance
    via the UI for now.
    """
    return _pretty(_bb().set_selected_experiment(experiment_id))


@mcp.tool()
def get_current_selection() -> str:
    """Show which experiment + instance are currently loaded into the
    Breadboard admin's script engine.

    The /debug/script endpoint evaluates Groovy against whatever instance
    the admin user last selected in the UI. If selectedExperiment or
    experimentInstanceId is null, runtime bindings (g, a, c, ...) will not
    be available and script execution may fail or return stale state.
    """
    return _pretty(_bb().get_current_selection())


@mcp.tool()
def select_experiment_for_engine(experiment_id: int) -> str:
    """Bind the Breadboard script engine to an experiment. This rebuilds the
    engine, sets up its bindings (g, a, c, events, ...) and loads every
    Step's Groovy source into the engine. Required before launch_game or
    select_instance will produce a usable runtime.
    """
    return _pretty(_bb().select_experiment_for_engine(experiment_id))


@mcp.tool()
def launch_game(name: str, parameters: dict | None = None) -> str:
    """Create + start a new instance of the currently-selected experiment
    (call select_experiment_for_engine first). This runs every Step source
    into the engine so its run/done closures are registered and bound to a
    fresh ExperimentInstance.

    `name` is a label for the instance shown in the UI / data exports.
    `parameters` is an optional dict of run-time parameter overrides.
    Returns once the actor settles or 15s elapses.
    """
    return _pretty(_bb().launch_game(name, parameters))


@mcp.tool()
def select_instance_for_engine(instance_id: int) -> str:
    """Bind the Breadboard script engine to an existing ExperimentInstance
    by id. Useful after a server restart or when switching between live
    instances of the same experiment.
    """
    return _pretty(_bb().select_instance_for_engine(instance_id))


@mcp.tool()
def stop_game(instance_id: int) -> str:
    """Stop a running game instance."""
    return _pretty(_bb().stop_game(instance_id))


@mcp.tool()
def execute_script(script: str) -> str:
    """Evaluate a Groovy script against the running Breadboard script engine
    and return { output, error }.

    The engine has the same bindings as the in-app Scriptboard once an
    experiment instance has been selected:

      g          - the in-memory TinkerGraph (g.V, g.E, g.getVertex(id), ...)
      a          - PlayerActions (a.add, a.addEvent, ...)
      c          - Content fetcher (c.get(...))
      events     - the event bus (events.on / events.emit)
      r          - java.util.Random
      results    - a Map you can write into; its contents are serialized
                   back as JSON in addition to the script's return value

    Examples:
        execute_script("g.V.count()")
        execute_script("g.V.collect { [id: it.id, neighbors: it.neighbors.size()] }")

    First call get_current_selection() to confirm the engine is bound to
    the experiment instance you want to debug.
    """
    return _pretty(_bb().execute_script(script))


@mcp.tool()
def instance_data_csv(experiment_id: int) -> str:
    """Return the CSV summary of all instances of an experiment (one row
    per instance, columns are the parameters captured for that run). This
    mirrors the "Download CSV" button on the experiment page.
    """
    return _bb().instance_data_csv(experiment_id)


@mcp.tool()
def event_csv(instance_id: int) -> str:
    """Return the full per-event CSV log for a single instance. Useful for
    bulk export when paging through /debug/events would be tedious.
    """
    return _bb().event_csv(instance_id)


# ---------------------------------------------------------- per-session JVM

@mcp.tool()
def spawn_breadboard(
    repo_root: str | None = None,
    admin_email: str | None = None,
    admin_password: str | None = None,
) -> str:
    """Start a session-private Breadboard JVM subprocess (own port, own H2
    database, own dev/ directory). Useful when two Claude Code sessions
    need to run experiments in parallel without racing on a single shared
    Breadboard.

    `repo_root` defaults to the Breadboard repo containing this MCP
    server. The staged binary must already exist at
    `<repo_root>/target/universal/stage/bin/breadboard` (run `sbt stage`).
    Override the binary path with the BREADBOARD_STAGED_BIN env var.

    `admin_email` / `admin_password` default to admin@example.com /
    admin123 and are seeded via POST /createFirstUser after the JVM is
    ready. Subsequent MCP tool calls automatically log in with these
    credentials.

    Idempotent: if a spawn already exists and is alive, returns its
    metadata without starting another. Returns
    { url, port, pid, workdir, session_id, admin_email, admin_password,
    log_file }.
    """
    return _pretty(spawner.spawn_breadboard(repo_root, admin_email, admin_password))


@mcp.tool()
def terminate_breadboard() -> str:
    """Stop the session-private Breadboard subprocess (if one is running)
    via SIGTERM, falling back to SIGKILL after 10 seconds. Returns
    metadata of the stopped instance, or { "status": "no-spawn" } if
    nothing was running.

    The atexit handler in this MCP server also terminates the JVM
    automatically when the MCP server itself exits — calling this tool
    is optional unless you want to stop and free the port mid-session.
    """
    info = spawner.terminate_breadboard()
    # Invalidate any cached client pointing at the now-defunct URL.
    _reset_client()
    return _pretty(info)


def _alive_breadboards() -> list[dict[str, Any]]:
    """Attach candidates: only externally-running Breadboards reachable
    via the BREADBOARD_URL env var. Spawns are session-private — each
    MCP owns its own — so they're never offered as attach candidates
    here. To attach to a specific spawn, pass its url explicitly to
    attach_breadboard()."""
    out: list[dict[str, Any]] = []
    env_url = os.environ.get("BREADBOARD_URL")
    if env_url:
        try:
            import httpx
            r = httpx.get(env_url, timeout=2.0, follow_redirects=False)
            if r.status_code < 500:
                out.append({"url": env_url, "source": "env"})
        except Exception:
            pass
    return out


@mcp.tool()
def list_alive_breadboards() -> str:
    """List currently-alive Breadboard instances visible to this MCP:
    every spawn (from any sibling MCP) that has a live JVM, plus the
    URL configured via the BREADBOARD_URL env var if that one responds.

    Each entry has at least { url, source }. Spawn entries also have
    workdir/port/pid/admin_email/admin_password from their metadata.json.
    Useful before calling attach_breadboard() to decide which one to
    attach to."""
    return _pretty(_alive_breadboards())


@mcp.tool()
def attach_breadboard(
    url: str | None = None,
    email: str | None = None,
    password: str | None = None,
) -> str:
    """Attach this MCP to an externally-running Breadboard. The attach
    takes precedence over any spawn for subsequent tool calls (until
    detach_breadboard() is called).

    With a `url`: attach to that URL. Works for any reachable Breadboard,
    including a spawn URL if you happen to know it. `email`/`password`
    default to the BREADBOARD_EMAIL/BREADBOARD_PASSWORD env vars when
    not given.

    Without a `url`: look for a candidate via list_alive_breadboards
    (the BREADBOARD_URL env var if it responds; spawns are excluded
    because they're session-private to their MCP).
      - 0 found -> error.
      - 1 found -> attach.
      - 2+ found -> return a warning ("very unusual") with the list of
                    candidates and explicit attach / quit options. The
                    quit option is to simply not call attach_breadboard
                    again; the MCP will fall back to auto-spawn on the
                    next tool call.

    Returns the attach state, or the candidate list when ambiguous."""
    global _attached
    candidates = _alive_breadboards()
    if url is None:
        if not candidates:
            raise RuntimeError(
                "No alive Breadboards. Call spawn_breadboard() to start one "
                "or attach_breadboard(url=...) with an explicit URL."
            )
        if len(candidates) > 1:
            return _pretty({
                "status": "ambiguous",
                "warning": (
                    "Multiple Breadboard instances were discovered. This is "
                    "very unusual — only proceed if you know what you are "
                    "doing. Attaching to the wrong one could touch live "
                    "data in another running session."
                ),
                "options": {
                    "attach": (
                        "To attach to a specific one, call "
                        "attach_breadboard(url=...) with the explicit URL."
                    ),
                    "quit": (
                        "To NOT attach, do nothing — no further tool calls "
                        "are required. The MCP will fall back to auto-spawn "
                        "on the next tool call."
                    ),
                },
                "candidates": candidates,
            })
        chosen = candidates[0]
    else:
        # Look up credentials for `url` from any known source so the
        # caller doesn't have to repeat them.
        norm = _normalize_url(url)
        chosen = next(
            (c for c in candidates if _normalize_url(c["url"]) == norm),
            None,
        )
        if chosen is None:
            own = spawner.get_spawned()
            if own and _normalize_url(own["url"]) == norm:
                chosen = {**own, "source": "self_spawn"}
        if chosen is None:
            for s in spawner.list_alive_spawns():
                if _normalize_url(s["url"]) == norm:
                    chosen = {**s, "source": "spawn"}
                    break
        if chosen is None:
            chosen = {"url": url, "source": "explicit"}

    e = email or chosen.get("admin_email") or os.environ.get("BREADBOARD_EMAIL")
    p = password or chosen.get("admin_password") or os.environ.get("BREADBOARD_PASSWORD")
    if not e or not p:
        raise RuntimeError(
            f"Cannot attach to {chosen['url']}: missing admin email/password. "
            "Pass email= and password=, or set BREADBOARD_EMAIL / "
            "BREADBOARD_PASSWORD env vars."
        )
    # Release any previously-held attach lock (e.g. re-attach to a new URL).
    if _attached and _normalize_url(_attached["url"]) != _normalize_url(chosen["url"]):
        _release_attach_lock(_attached["url"])
    # Try to acquire exclusive lock for the target URL.
    lock = _try_acquire_attach_lock(chosen["url"])
    if not lock["acquired"]:
        kind = lock.get("kind")
        owner = lock.get("holder_pid")
        if kind == "spawn":
            msg = (
                f"Cannot attach to {chosen['url']}: it's the spawn target "
                f"of another MCP process (pid={owner}). Wait for that MCP "
                f"to exit, or pick a different URL."
            )
        else:
            msg = (
                f"Cannot attach to {chosen['url']}: already attached by "
                f"another MCP process (pid={owner}). Only one MCP can "
                f"attach to a given Breadboard at a time. Wait for that "
                f"MCP to detach/exit, or pick a different URL."
            )
        raise RuntimeError(msg)
    _attached = {"url": chosen["url"], "email": e, "password": p}
    _reset_client()
    return _pretty({
        "status": "attached",
        **_attached,
        "source": chosen.get("source"),
        "warning": (
            "Do NOT use this Breadboard manually (admin UI, separate scripts, "
            "another MCP, etc.) while it's attached here. The MCP is now "
            "driving the engine; concurrent manual changes will race with "
            "tool calls and may corrupt state or game flow. Call "
            "detach_breadboard() when done."
        ),
    })


@mcp.tool()
def detach_breadboard() -> str:
    """Clear an attach set by attach_breadboard(), releasing the
    exclusive attach lock so another MCP can attach to that Breadboard.
    Subsequent tool calls revert to the spawned Breadboard if one
    exists, or trigger an auto-spawn. Returns the previous attach state
    (or null)."""
    global _attached
    prev = _attached
    if prev:
        _release_attach_lock(prev["url"])
    _attached = None
    _reset_client()
    return _pretty(prev) if prev else "null"


@mcp.tool()
def cleanup_orphan_breadboards(dry_run: bool = True) -> str:
    """Find (and optionally terminate) orphaned Breadboard JVMs from
    previous MCP sessions that died abnormally (SIGKILL, segfault,
    force-quit) without running the atexit handler. A session is orphaned
    when its owner MCP process is dead but the JVM is still alive.

    Defaults to `dry_run=True` (list orphans without killing) so an
    accidental call is non-destructive. Pass `dry_run=False` to actually
    terminate them.

    `spawn_breadboard()` always runs a real (non-dry) cleanup
    automatically, so explicit invocation is mainly useful to inspect
    orphans or to reclaim resources without spawning anything new.

    Returns a list of orphans (workdir, jvm_pid, owner_pid). Safe to
    call any time — live spawns from sibling MCPs are never touched."""
    return _pretty(spawner.cleanup_orphans(dry_run=dry_run))


@mcp.tool()
def get_spawned_breadboard() -> str:
    """Show metadata about the session-private Breadboard if one was
    spawned via spawn_breadboard. Includes an `alive` flag indicating
    whether the JVM subprocess is still running. Returns null if no
    spawn happened in this MCP session."""
    info = spawner.get_spawned()
    return _pretty(info) if info is not None else "null"


# --------------------------------------------------------------------- entry

def _print_claude_config() -> None:
    """Print a JSON block ready to paste into ~/.claude.json. The `command`
    field is filled with the absolute path of this script's installed entry
    point (resolved from sys.argv[0]), so it works whether the user
    installed via venv/pip, pipx, uv, etc."""
    import sys
    from pathlib import Path

    script = str(Path(sys.argv[0]).resolve())
    config = {
        "mcpServers": {
            "breadboard": {
                "command": script,
                "env": {
                    "BREADBOARD_URL": "http://localhost:9000",
                    "BREADBOARD_EMAIL": "admin@example.com",
                    "BREADBOARD_PASSWORD": "...",
                },
            }
        }
    }
    sys.stderr.write(
        "# Paste this into ~/.claude.json (or merge with existing mcpServers).\n"
        "# Set BREADBOARD_EMAIL / BREADBOARD_PASSWORD to your admin credentials.\n"
    )
    print(json.dumps(config, indent=2))


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(prog="breadboard-mcp", add_help=True)
    parser.add_argument(
        "--print-claude-config",
        action="store_true",
        help="Print a JSON block ready to paste into ~/.claude.json and exit.",
    )
    parser.add_argument(
        "--cleanup-orphans",
        action="store_true",
        help="Reap orphaned Breadboard JVMs from previous crashed MCP "
             "sessions, then exit. Prints a JSON summary of what was killed.",
    )
    args = parser.parse_args()

    if args.print_claude_config:
        _print_claude_config()
        return
    if args.cleanup_orphans:
        cleaned = spawner.cleanup_orphans()
        print(json.dumps({"cleaned": cleaned, "count": len(cleaned)}, indent=2))
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()
