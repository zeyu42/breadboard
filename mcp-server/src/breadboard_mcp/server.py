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

mcp = FastMCP(
    "breadboard",
    instructions=(
        "Control a Breadboard experiment platform via HTTP+JSON.\n\n"
        "==========================\n"
        "GLOSSARY (Breadboard terms)\n"
        "==========================\n"
        "- Experiment: the design / template — a set of Steps, Content,\n"
        "  parameters, and a starting graph. Authored once, run many times.\n"
        "- ExperimentInstance (a.k.a. 'instance' / 'run' / 'game'): one\n"
        "  execution of an experiment with participants. Has its own graph,\n"
        "  event log, and status (RUNNING / TESTING / STOPPED / FINISHED /\n"
        "  ARCHIVED). A server can have many instances at once; the engine\n"
        "  binds to one at a time.\n"
        "- Step: a stage of the experiment, written as Groovy with `run`\n"
        "  and `done` closures. The engine advances through steps as the\n"
        "  experiment progresses.\n"
        "- The engine / Scriptboard: the in-server Groovy interpreter that\n"
        "  evaluates Step closures and arbitrary Groovy via execute_script.\n"
        "  ONE per Breadboard JVM.\n"
        "- Bindings — variables auto-injected into every Groovy script:\n"
        "    g       a TinkerGraph (Gremlin-style property graph). Vertices\n"
        "            are typically players + experiment entities; edges are\n"
        "            relationships. Query with `g.V`, `g.E`, `g.getVertex(id)`.\n"
        "    a       PlayerActions: a.add(Vertex, choices...) queues\n"
        "            choices for an existing player; a.addEvent(name,\n"
        "            propsMap) writes an event to the instance event log.\n"
        "            Step transitions are experiment-specific (driven by\n"
        "            custom events from clients).\n"
        "    c       Content fetcher: c.get('Welcome') returns a Content\n"
        "            object that renders to HTML.\n"
        "    events  Event bus: events.on('Joined') { ... },\n"
        "            events.emit('Custom', [k: v]).\n"
        "    r       java.util.Random.\n"
        "    results a Map; what you write into it is returned as JSON.\n"
        "- Player: a participant connected over WebSocket. Vertex in `g`.\n"
        "- file mode: when ON, the experiment reads steps/content/parameters\n"
        "  from `dev/<dirName>/` on disk; when OFF, it reads from the DB.\n"
        "- Spawn vs Attach: spawn = the MCP starts its own private JVM\n"
        "  (default, auto on first tool call). Attach = the MCP drives a\n"
        "  Breadboard the user is already running.\n\n"
        "============================\n"
        "KEY CONCEPT — engine binding\n"
        "============================\n"
        "Breadboard runs a single Groovy script engine. To evaluate Groovy\n"
        "or drive a running game, the engine must be BOUND to:\n"
        "  1. an experiment (its Step source loaded), AND\n"
        "  2. an ExperimentInstance (its runtime graph, players, events).\n"
        "Only one experiment / one instance is bound at a time. Rebinding\n"
        "wipes prior engine state. If `get_current_selection` shows null\n"
        "for either, `execute_script` will return stale or empty bindings.\n\n"
        "==================\n"
        "CANONICAL WORKFLOWS\n"
        "==================\n"
        "A. Read-only inspection (no engine needed):\n"
        "   list_experiments -> get_experiment(id) -> list_instances(id) ->\n"
        "   get_instance_events(instance_id)\n\n"
        "B. Run a new game from scratch:\n"
        "   select_experiment_for_engine(id)   # build engine, load step source\n"
        "   launch_game(name)                  # create instance, bind to engine\n"
        "   execute_script('g.V.count()')      # 0 until players join (see below)\n"
        "   stop_game(instance_id)\n\n"
        "C. Debug an existing running instance:\n"
        "   select_experiment_for_engine(id)\n"
        "   select_instance_for_engine(instance_id)\n"
        "   execute_script(...)\n\n"
        "D. Push local files into Breadboard (replaces `npm run serve`):\n"
        "   set_file_mode(id, enabled=True)\n"
        "   sync_experiment_files(id, source_dir='/path/to/backend')\n"
        "   select_experiment_for_engine(id)   # reload updated source\n\n"
        "E. Create a new experiment (optionally from a template):\n"
        "   create_experiment('name', copy_experiment_id=42)\n\n"
        "F. TYPICAL PROJECT cold start — user has a local `backend/`\n"
        "   folder (v2.4-default layout) and no experiment yet:\n"
        "   create_experiment('MyExpt')                # gets id\n"
        "   set_file_mode(id, enabled=True)\n"
        "   sync_experiment_files(id, source_dir='/abs/path/to/backend')\n"
        "   select_experiment_for_engine(id)           # load source\n"
        "   launch_game('run-1')\n"
        "   # After every local edit, re-sync + re-select. NOT WATCHED.\n"
        "   See resource docs://typical-project-workflow for details.\n\n"
        "=======\n"
        "GOTCHAS\n"
        "=======\n"
        "- `launch_game` produces an empty graph. Players do not appear\n"
        "  by magic. The MCP does NOT drive the browser. Right after a\n"
        "  launch, `g.V.count()` is typically 0 — that's normal. To get\n"
        "  participants in: either open the participant URL in a browser,\n"
        "  or run the WebSocket simulator at\n"
        "  `mcp-server/examples/simulate_players.py`.\n"
        "- Steps fire on signals, not on binding. Selecting an experiment\n"
        "  and launching a game does not 'execute' any step on its own;\n"
        "  step `run` closures fire when triggered (player joins,\n"
        "  custom event from a client, parameter change, etc.).\n"
        "- `g` is per-instance. Two RUNNING instances have two separate\n"
        "  graphs; switch with `select_instance_for_engine` to see each.\n"
        "- `set_selected_experiment` sets the admin's UI preference; it\n"
        "  does NOT rebuild the engine. Use `select_experiment_for_engine`\n"
        "  for engine binding.\n"
        "- `select_experiment_for_engine` is DESTRUCTIVE: it rebuilds the\n"
        "  engine and wipes previously-bound state (`g` graph, in-flight\n"
        "  timers, registered handlers).\n"
        "- The first tool call in a session may take ~30s because auto-\n"
        "  spawn is booting a JVM. Looks like a hang; isn't.\n"
        "- Spawn-mode default credentials are `admin@example.com` /\n"
        "  `admin123`. Hardcoded; don't assume the user set them.\n"
        "- All tool returns are JSON strings (or CSV strings for the\n"
        "  *_csv tools). Parse before treating fields as objects.\n"
        "- In ATTACH mode the target Breadboard must have `mcp.enabled=true`\n"
        "  (in conf, JVM flag, or env var). Otherwise every /debug/* call\n"
        "  returns 404. Spawn mode passes the flag automatically.\n\n"
        "=====================\n"
        "SPAWN PREREQUISITES\n"
        "=====================\n"
        "Spawn mode requires a staged binary at\n"
        "  <repo_root>/target/universal/stage/bin/breadboard\n"
        "produced by `sbt stage`. The spawner resolves `repo_root`\n"
        "automatically from the MCP server's installation path, which\n"
        "works only if the MCP was installed from inside the Breadboard\n"
        "repo (e.g. `pip install -e .` in `mcp-server/`).\n\n"
        "If `spawn_breadboard` errors with 'Staged Breadboard binary not\n"
        "found':\n"
        "  - The user must run `sbt stage` in the repo root, OR\n"
        "  - The MCP was installed outside the repo — the user must\n"
        "    point it at the right one via either:\n"
        "      * the BREADBOARD_STAGED_BIN env var (path to the\n"
        "        `breadboard` binary), or\n"
        "      * passing `repo_root=...` to `spawn_breadboard`.\n"
        "Do NOT silently fall back to attaching. If spawn fails, tell\n"
        "the user, explain the options, and let them decide. The MCP\n"
        "never auto-discovers externally-running Breadboards — `attach_\n"
        "breadboard` is always an explicit, user-driven action.\n\n"
        "If the user is already running Breadboard themselves (e.g. via\n"
        "`./start` or `play run`), spawn is the wrong tool. Tell them to\n"
        "either (a) call `attach_breadboard(url=...)` against their\n"
        "running instance with `mcp.enabled=true` set, or (b) stop their\n"
        "instance and let spawn take over. Do not pick for them.\n\n"
        "===========================\n"
        "WHAT THIS MCP CANNOT DO\n"
        "===========================\n"
        "- Drive the Vue admin UI or the participant browser. Server-side\n"
        "  only.\n"
        "- Connect WebSocket players. Use the simulator code at resource\n"
        "  examples://simulate-players or a real browser.\n"
        "- Run two engine bindings in parallel within one session.\n"
        "  Concurrent runs exist on the server, but `execute_script` sees\n"
        "  only the currently-bound instance.\n\n"
        "==================\n"
        "DEEPER REFERENCES\n"
        "==================\n"
        "When you need more depth than fits here, read these resources:\n"
        "  docs://usage-guide              - full glossary + workflows\n"
        "  docs://typical-project-workflow - the local-files cold-start\n"
        "  docs://groovy-cheatsheet        - runnable Groovy/Gremlin examples\n"
        "  docs://error-recipes            - symptoms -> diagnoses -> fixes\n"
        "  docs://dev-notes                - server internals & threat model\n"
        "  examples://simulate-players     - WebSocket player simulator code"
    ),
)

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

    USE WHEN: You need an experiment id to pass to any other tool, or to
    show the user what experiments exist. This is the natural entry point
    when you don't know any experiment ids yet.

    Returns an array of { id, name, uid, fileMode, instanceCount }. Pick
    an id and follow up with get_experiment(id) to read step source, or
    list_instances(id) to see runs.

    IF THE LIST IS EMPTY and the user's working directory looks like a
    Breadboard experiment project (contains a `backend/` folder with
    Steps/ and Content/), they almost certainly want to push those files
    in rather than run a default blank experiment. See resource
    `docs://typical-project-workflow`.
    """
    return _pretty(_bb().list_experiments())


@mcp.tool()
def get_experiment(experiment_id: int) -> str:
    """Get full metadata for one experiment: steps (with Groovy source),
    content, parameters, languages, instance stubs, and styling.

    USE WHEN: You need to read the actual Step source code (the closures
    that run when the experiment progresses), inspect parameters, or see
    what content blocks the experiment defines. Read this BEFORE editing
    step logic with sync_experiment_files, and before debugging a run
    with execute_script — knowing the step source tells you which
    variables are in scope.
    """
    return _pretty(_bb().get_experiment(experiment_id))


@mcp.tool()
def list_instances(experiment_id: int) -> str:
    """List all instances (runs) of an experiment.

    USE WHEN: The user asks "what runs do I have," or you need an
    instance_id for get_instance_events, event_csv,
    select_instance_for_engine, or stop_game. RUNNING/TESTING instances
    are still alive; FINISHED/STOPPED/ARCHIVED are historical data.

    Each stub: id, name, status, creationTime, AMT hit info, and the
    per-instance Data values (parameters captured at run time).
    """
    return _pretty(_bb().list_instances(experiment_id))


@mcp.tool()
def get_instance(instance_id: int) -> str:
    """Get one experiment instance by id (stub form: no event list).

    USE WHEN: You already have an instance_id and need its current
    status, name, AMT hit info, or per-instance Data values without
    fetching the full event log. For the event log, use
    get_instance_events. For everything-at-once CSV, use event_csv.

    STATUS DOES NOT AUTO-COMPLETE. The `status` field stays RUNNING
    even after every player has finished — Breadboard never flips
    RUNNING -> FINISHED on its own. To detect end-of-run, look at the
    event log instead. Event names are NOT framework-defined; they
    come from whatever the experiment's Steps emit via `a.addEvent`.
    To find this experiment's vocabulary:
      grep -REn 'addEvent\\(' backend/Steps/
    Then verify the run by counting expected occurrences (per-round
    events × rounds, terminal-step events × players, etc.) — see
    `docs://typical-project-workflow` "Verifying an end-to-end run"
    for the recipe. Call stop_game(id) when you want the status to
    actually transition.
    """
    return _pretty(_bb().get_instance(instance_id))


@mcp.tool()
def get_instance_events(
    instance_id: int,
    limit: int = 200,
    offset: int = 0,
    name_filter: str | None = None,
) -> str:
    """Get the event log for an instance, ordered by datetime.

    USE WHEN: Diagnosing what happened during a run — which steps fired,
    what choices players made, which timers triggered. This is the
    primary tool for after-the-fact debugging. For wholesale export, use
    event_csv instead.

    Use limit/offset to page through long logs (default limit 200). Pass
    name_filter to substring-match on event name (e.g. "Choice", "Step",
    "Joined", "Connected").

    RETURN FIELDS:
      - totalEvents: total events for the instance, BEFORE name_filter
        is applied (this number stays constant regardless of filter).
      - returned: number of events actually in this response (subject
        to name_filter and limit/offset).
      - events: the matching event objects for this page.
    To get a filtered total, call once with a very large limit (e.g.
    100000) and read `returned`.
    """
    return _pretty(_bb().get_instance_events(instance_id, limit, offset, name_filter))


@mcp.tool()
def create_experiment(name: str, copy_experiment_id: int | None = None) -> str:
    """Create a new experiment owned by the configured admin user.

    USE WHEN: The user wants a fresh experiment, or wants to fork an
    existing one as a starting point. Pass copy_experiment_id to clone
    an existing experiment's steps/content/parameters as a template.

    NOT TRULY BLANK: Even without copy_experiment_id, Breadboard seeds
    three default lifecycle Steps (OnJoinStep, OnLeaveStep, InitStep).
    The experiment is launchable as-is, though without participants the
    runtime graph stays empty.

    Returns the created experiment as JSON — note the `id` field for
    follow-ups (set_file_mode, sync_experiment_files,
    select_experiment_for_engine, etc.).
    """
    return _pretty(_bb().create_experiment(name, copy_experiment_id))


@mcp.tool()
def get_experiment_paths(experiment_id: int) -> str:
    """Return the on-disk paths Breadboard uses for this experiment's
    file-mode directory.

    USE WHEN: You want to verify WHERE sync_experiment_files will write,
    or to find the dev/ directory so the user can edit files outside
    the MCP. Returns devDirectory, stepsDir, contentDir, parametersFile,
    clientHtmlFile, clientGraphFile, styleFile.
    """
    return _pretty(_bb().get_experiment_paths(experiment_id))


@mcp.tool()
def set_file_mode(experiment_id: int, enabled: bool | None = None) -> str:
    """Turn file mode on or off for an experiment.

    USE WHEN: You're about to push local files via sync_experiment_files
    (call this with enabled=True first), or the user wants Breadboard to
    re-import its content from disk (call with enabled=False then back
    to True). Pass `enabled` to set a specific value, or omit to toggle.

    Side effects: Turning ON exports the current experiment into
    dev/<directoryName>/ on the Breadboard server, then makes the
    experiment read its steps / content / parameters from THAT directory.
    Turning OFF re-imports the dev/ contents back into the database.

    WARNING: If the experiment already has Steps/Content in the DB,
    turning file mode ON writes those into dev/ — potentially BEFORE
    your subsequent sync_experiment_files call overlays them. For a
    freshly created experiment this is harmless (it writes the three
    default lifecycle steps). For an experiment with prior content,
    treat dev/ as overwritten by the DB-side state at the moment file
    mode flips on.

    Required precondition for sync_experiment_files — without file mode
    on, synced files are ignored.
    """
    return _pretty(_bb().set_file_mode(experiment_id, enabled))


@mcp.tool()
def sync_experiment_files(
    experiment_id: int,
    source_dir: str,
    public_root: str = "/generated/",
    dev_mode: bool = True,
) -> str:
    """Copy a directory tree of experiment files into the Breadboard
    server's dev/<directoryName>/ for this experiment — the MCP-side
    equivalent of `npm run serve` in the breadboard-v2.4-default
    template.

    USE WHEN: The user has a local `backend/` directory (Steps, Content,
    parameters, client-html, client-graph, style) and wants Breadboard
    to pick up those files instead of using its DB copy. This is the
    standard inner loop for editing Groovy steps locally and seeing
    them run.

    PRECONDITION: File mode must be ON. Call set_file_mode(id, True)
    first. After syncing, call select_experiment_for_engine(id) so the
    engine picks up the new step source (without this, the running
    engine still has the OLD source cached).

    ONE-SHOT, NOT WATCHED. Unlike `npm run serve` (which watches and
    re-syncs on save), this is a single copy. After the user edits a
    file locally, call sync_experiment_files again followed by
    select_experiment_for_engine to reload the engine.

    SCOPE = `backend/` ONLY. Files the experiment reads from sibling
    directories (e.g. a `data/` folder containing any data the
    experimenter wants the engine to see — CSVs, JSON lookups,
    whatever) are NOT synced here. Those resolve relative to the JVM's
    working directory (the spawn workdir). You must copy them there
    manually before `select_experiment_for_engine`, or that call will
    fail when a Step's constructor tries to read the missing file.
    See `docs://typical-project-workflow` section "Files outside
    backend/" for the recipe.

    `source_dir` should be an ABSOLUTE path on whichever filesystem the
    Breadboard JVM can read (typically the same machine as the MCP).
    Relative paths resolve against the JVM's working directory, NOT the
    user's working directory — agents should expand to absolute before
    calling.

    Layout convention (from breadboard-v2.4-default template):
    `source_dir` MAY contain any subset of `Steps/`, `Content/`,
    `Images/`, `parameters.csv`, `client-html.html`, `client-graph.js`,
    `style.css`, `.breadboard`. All are optional — missing files are
    silently skipped, not errors. In practice `Steps/` is the only one
    that's typically present. `parameters.csv` may be header-only,
    `client-html.html` may be empty — both are fine.

    The `Steps/` folder is normalized to lowercase `steps/`.
    `client-graph.js` has `__PUBLIC_ROOT__`, `__DEV__`, `__PROD__`
    placeholders substituted the same way the webpack CopyPlugin does.
    The `.breadboard` metadata file (if present) is copied as-is;
    Breadboard ignores it in file-mode sync.

    For the full "I have a local project and want to run it" workflow,
    see resource `docs://typical-project-workflow`.

    Returns a manifest of files written.
    """
    return _pretty(
        _bb().sync_experiment_files(experiment_id, source_dir, public_root, dev_mode)
    )


@mcp.tool()
def set_selected_experiment(experiment_id: int) -> str:
    """Set the configured admin user's currently-selected experiment
    (the value shown in the admin UI's dropdown).

    USE WHEN: You explicitly want to change which experiment the admin
    UI defaults to — usually for human users who will open the browser
    next. RARELY useful from an agent context.

    NOT WHAT YOU WANT FOR ENGINE BINDING. Setting the selected
    experiment does NOT rebuild the script engine and does NOT load
    step source into it. For Groovy debugging, use
    select_experiment_for_engine instead.
    """
    return _pretty(_bb().set_selected_experiment(experiment_id))


@mcp.tool()
def get_current_selection() -> str:
    """Show which experiment + instance are currently bound to the
    Breadboard script engine.

    USE WHEN: Sanity-checking engine state before execute_script. If
    `selectedExperiment` is null, no experiment is loaded — call
    select_experiment_for_engine(id). If `experimentInstanceId` is null,
    no instance is bound — call launch_game(...) or
    select_instance_for_engine(id). Without both, execute_script
    bindings (g, a, c, events, ...) will be missing or stale.

    Cheap and non-destructive. Safe to call any time as a status check.
    """
    return _pretty(_bb().get_current_selection())


@mcp.tool()
def select_experiment_for_engine(experiment_id: int) -> str:
    """Bind the Breadboard script engine to an experiment: rebuild the
    engine, install its bindings (g, a, c, events, r, results), and
    load every Step's Groovy source into the engine.

    USE WHEN: Starting a fresh debugging/run session, or after editing
    step source via sync_experiment_files (to pick up the new code).
    REQUIRED before launch_game or select_instance_for_engine — without
    this, the engine has no step closures registered and Groovy
    execution against an instance will fail.

    DESTRUCTIVE: This rebuilds the engine from scratch. Any previously-
    bound state — the in-memory graph `g`, in-flight timers, registered
    event handlers — is WIPED. Don't call this in the middle of a
    running game unless you want to start over.
    """
    return _pretty(_bb().select_experiment_for_engine(experiment_id))


@mcp.tool()
def launch_game(name: str, parameters: dict | None = None) -> str:
    """Create + start a new ExperimentInstance and bind the script engine
    to it. Equivalent to clicking "Launch" in the Breadboard admin UI.

    USE WHEN: You want to start a fresh run of an experiment and then
    poke at it via execute_script. The instance starts in RUNNING /
    TESTING state immediately. After this returns, execute_script's
    bindings (g, a, c, events) operate on this new instance.

    PRECONDITION: Call select_experiment_for_engine(id) first. Without
    a bound experiment, the engine has no step closures to register.

    SIDE EFFECTS: Stops every RUNNING/TESTING instance owned by this
    admin, re-runs every Step source into the engine, then creates and
    binds a fresh ExperimentInstance. This MCP intentionally permits
    only one active run at a time.

    `name` is a label for the instance (shown in UI + data exports).
    `parameters` is an optional dict of run-time parameter overrides.
    Returns once the actor settles or 15s elapses.
    """
    return _pretty(_bb().launch_game(name, parameters))


@mcp.tool()
def select_instance_for_engine(instance_id: int) -> str:
    """Bind the Breadboard script engine to an existing ExperimentInstance
    by id, so execute_script operates on this instance's runtime state.

    USE WHEN: (a) Switching between two RUNNING instances of the same
    experiment to inspect each in turn; (b) reattaching to an instance
    after a server restart; (c) inspecting an older TESTING/FINISHED
    instance for diagnosis.

    PRECONDITION: Call select_experiment_for_engine(experiment_id)
    first so the engine has the step closures loaded. Without that,
    binding to an instance leaves the engine without code to run.
    """
    return _pretty(_bb().select_instance_for_engine(instance_id))


@mcp.tool()
def stop_game(instance_id: int) -> str:
    """Stop a running ExperimentInstance.

    USE WHEN: A run is finished, stuck, or no longer needed. Transitions
    the instance from RUNNING/TESTING to STOPPED. Players currently
    connected receive a stop signal. The instance's data and event log
    remain in the DB (still retrievable via get_instance_events /
    event_csv).

    Does NOT terminate the Breadboard JVM — use terminate_breadboard
    for that. Does NOT delete the instance — data is preserved.
    """
    return _pretty(_bb().stop_game(instance_id))


@mcp.tool()
def execute_script(script: str) -> str:
    """Evaluate a Groovy script against the Breadboard script engine and
    return { output, error }. Same engine the in-app Scriptboard uses.

    USE WHEN: Inspecting or mutating live experiment state — counting
    vertices, reading player attributes, firing events, advancing
    steps. This is your main read/write interface to a running game.

    PRECONDITIONS:
      1. An experiment must be bound (select_experiment_for_engine).
      2. An instance must be bound (launch_game or
         select_instance_for_engine).
    If unsure, call get_current_selection() first — null fields there
    mean execute_script will fail or return stale bindings.

    Bindings (all live, same as Scriptboard):
      g        - in-memory TinkerGraph (Gremlin-style property graph).
                 g.V, g.E, g.getVertex(id), g.addVertex(null).
      a        - PlayerActions. a.add(Vertex, choices...) queues
                 choices for a player (does NOT simulate joining);
                 a.addEvent(name, propsMap) writes an event to the
                 instance event log. Advancing the experiment is
                 experiment-specific (no universal a.next).
      c        - Content fetcher: c.get('ContentName').
      events   - event bus: events.on / events.emit.
      r        - java.util.Random.
      results  - Map; its contents are serialized back as JSON
                 alongside the script's return value.

    OUTPUT FORMAT: Returns { output, error }. `output` echoes the
    script then shows the result in Gremlin pipe format (e.g. `==>0`
    for count==0, or `==>[a, b, c]` for a list). Errors land in
    `error` as Groovy exception text.

    Examples (all runnable):
        execute_script("g.V.count()")
        execute_script("def v = g.addVertex(null); v.id")
        execute_script("a.addEvent('Manual', [reason: 'debug'])")
        execute_script("g.V.collect { [id: it.id, props: it.getPropertyKeys()] }")

    For longer Groovy/Gremlin examples (synthetic players, multi-vertex
    setup, common queries), see resource `docs://groovy-cheatsheet`.

    NOT SANDBOXED. Same engine as the Scriptboard — you can read or
    corrupt anything in the JVM. Treat each call like typing into the
    Scriptboard.
    """
    return _pretty(_bb().execute_script(script))


@mcp.tool()
def instance_data_csv(experiment_id: int) -> str:
    """Return the CSV summary of all instances of an experiment — one row
    per instance, columns are the per-instance parameters/Data values
    captured for that run. Mirrors the "Download CSV" button on the
    experiment page.

    USE WHEN: The user wants a tabular overview across many runs
    (analysis, spreadsheet export). For one instance's event timeline,
    use event_csv instead.
    """
    return _bb().instance_data_csv(experiment_id)


@mcp.tool()
def event_csv(instance_id: int) -> str:
    """Return the full per-event CSV log for a single instance.

    USE WHEN: You want the entire event timeline of one run as CSV
    (post-hoc analysis, full export). For interactive browsing with
    filters and paging, use get_instance_events instead.
    """
    return _bb().event_csv(instance_id)


# ------------------------------------------------------------ resources
# Longer reference material is exposed as MCP resources rather than
# stuffed into tool descriptions. Clients that support resources (Claude
# Code, Codex) can list/read them; clients that don't still get the
# fast-path guidance from tool docstrings and the `instructions` block.

# Resource files live at two possible locations depending on install
# style:
#   - Editable install (`pip install -e .` from the repo): docs/ and
#     examples/ sit at the project root (parents[2] of this file).
#   - Wheel install: hatch's force-include in pyproject.toml maps them
#     into <package>/_resources/{docs,examples}/. From this file's
#     perspective that's parents[0]/_resources/.
# We try the editable layout first (matches the source-of-truth during
# development) and fall back to the wheel layout.
_EDITABLE_ROOT = Path(__file__).resolve().parents[2]
_WHEEL_ROOT = Path(__file__).resolve().parent / "_resources"


def _read_doc(relpath: str) -> str:
    """Read a doc file shipped alongside the MCP server. Returns a
    helpful message instead of raising if the file is missing — keeps
    the MCP usable even if the install dropped the docs/."""
    for root in (_EDITABLE_ROOT, _WHEEL_ROOT):
        path = root / relpath
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return (
        f"# Missing resource\n\n"
        f"Expected {relpath} under {_EDITABLE_ROOT} or {_WHEEL_ROOT}, "
        f"but it was not found. The MCP server install may be "
        f"incomplete. Reinstall from the breadboard repo's mcp-server/ "
        f"directory."
    )


@mcp.resource("docs://usage-guide", name="Usage guide",
              mime_type="text/markdown",
              description="Full glossary, canonical workflows, and gotchas")
def _usage_guide() -> str:
    return _read_doc("docs/usage-guide.md")


@mcp.resource("docs://typical-project-workflow",
              name="Typical project workflow",
              mime_type="text/markdown",
              description="How to push a local backend/ folder into Breadboard "
                          "(the user-with-local-files cold-start)")
def _typical_workflow() -> str:
    return _read_doc("docs/typical-project-workflow.md")


@mcp.resource("docs://groovy-cheatsheet", name="Groovy cheatsheet",
              mime_type="text/markdown",
              description="Runnable Groovy/Gremlin examples for execute_script "
                          "— synthetic players, graph queries, events")
def _groovy_cheatsheet() -> str:
    return _read_doc("docs/groovy-cheatsheet.md")


@mcp.resource("docs://error-recipes", name="Error recipes",
              mime_type="text/markdown",
              description="Symptom -> diagnosis -> fix catalog for common "
                          "failures (spawn, engine binding, sync, Groovy)")
def _error_recipes() -> str:
    return _read_doc("docs/error-recipes.md")


@mcp.resource("docs://dev-notes", name="Dev notes",
              mime_type="text/markdown",
              description="Server internals, threat model, cross-platform "
                          "behavior, alt wiring")
def _dev_notes() -> str:
    return _read_doc("DEV_NOTES.md")


@mcp.resource("examples://simulate-players", name="Player simulator",
              mime_type="text/x-python",
              description="WebSocket player simulator — minimal "
                          "experiment-agnostic Python script for driving "
                          "synthetic participants into a launched game")
def _simulate_players() -> str:
    return _read_doc("examples/simulate_players.py")


# ---------------------------------------------------------- per-session JVM

@mcp.tool()
def spawn_breadboard(
    repo_root: str | None = None,
    admin_email: str | None = None,
    admin_password: str | None = None,
) -> str:
    """Start a session-private Breadboard JVM subprocess (own port, own
    H2 database, own dev/ directory).

    USUALLY NOT NEEDED EXPLICITLY: The first tool call auto-spawns one
    if no attach/spawn is active. Call this explicitly only if you
    want to control admin credentials, the repo root, or start the
    JVM eagerly before your first real call.

    Idempotent: if a spawn already exists and is alive in this MCP
    session, returns its metadata without starting another. If the
    previous spawn died, this respawns.

    `repo_root` defaults to the Breadboard repo containing this MCP
    server. The staged binary must already exist at
    `<repo_root>/target/universal/stage/bin/breadboard` (run
    `sbt stage`). Override the binary path with the BREADBOARD_STAGED_BIN
    env var.

    `admin_email` / `admin_password` default to admin@example.com /
    admin123 and are seeded via POST /createFirstUser after the JVM is
    ready. Subsequent MCP tool calls automatically log in.

    Returns { url, port, pid, workdir, session_id, admin_email,
    admin_password, log_file }.
    """
    return _pretty(spawner.spawn_breadboard(repo_root, admin_email, admin_password))


@mcp.tool()
def terminate_breadboard() -> str:
    """Stop the session-private Breadboard subprocess (the JVM).

    USE WHEN: The user wants to free the port mid-session, or restart
    Breadboard cleanly (terminate then spawn again). For stopping a
    single experiment run while keeping the JVM alive, use stop_game.

    Sends SIGTERM, escalating to SIGKILL after 10s. The MCP's atexit
    handler also runs this automatically when the MCP server exits —
    calling this tool is optional in normal cases.

    Returns metadata of the stopped JVM, or { "status": "no-spawn" }
    if nothing was running.
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
    """List currently-alive Breadboard instances reachable for attach:
    the BREADBOARD_URL env var (if it responds). Sibling MCPs' spawns
    are session-private and NOT listed here.

    USE WHEN: The user wants to know what external Breadboards are
    available before calling attach_breadboard(). Typically a precursor
    to attach_breadboard(); skip if you're using the default auto-spawn.

    Each entry: { url, source }. Returns [] if nothing is reachable —
    in that case, fall back to spawn_breadboard or auto-spawn."""
    return _pretty(_alive_breadboards())


@mcp.tool()
def attach_breadboard(
    url: str | None = None,
    email: str | None = None,
    password: str | None = None,
) -> str:
    """Attach this MCP to an externally-running Breadboard instead of
    spawning a private one.

    USE WHEN: The user is running Breadboard themselves (e.g. via the
    `./start` script or a deployed instance) and wants the MCP to drive
    THAT server rather than its own auto-spawned JVM. Skip this if you
    just want the default behavior — auto-spawn handles most cases.

    PRECONDITION: The target Breadboard must be built from this branch
    (or any build with the /debug/* endpoints) AND have mcp.enabled=true
    set (in conf/application.conf, via -Dmcp.enabled=true, or
    MCP_ENABLED=true). Without that flag, all /debug/* calls 404.

    The attach takes precedence over any spawn for subsequent tool
    calls until detach_breadboard() is called. Attaches are exclusive
    (file-locked) — only one MCP can attach to a given URL at a time.

    With a `url`: attach to that URL. `email`/`password` default to the
    BREADBOARD_EMAIL/BREADBOARD_PASSWORD env vars when not given.

    Without a `url`: look for a candidate via list_alive_breadboards
    (BREADBOARD_URL if responding; spawns are excluded as session-
    private).
      - 0 found -> error.
      - 1 found -> attach.
      - 2+ found -> return a warning ("very unusual") with candidates
                    and explicit attach / quit options.

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
    """Release an attach set by attach_breadboard().

    USE WHEN: You're done driving an external Breadboard and want this
    MCP to revert to its own spawn (or auto-spawn) for subsequent
    calls, or you want to free the attach lock so another MCP can
    take over.

    Releases the exclusive attach lock and clears the cached client.
    Returns the previous attach state (or null if nothing was
    attached)."""
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
    previous MCP sessions that died abnormally without running their
    atexit handler.

    USE WHEN: Ports seem stuck in use, the user mentions stray JVMs,
    or you want to reclaim resources without starting a new spawn.
    Otherwise unnecessary — spawn_breadboard runs a real (non-dry)
    cleanup automatically on every call.

    An "orphan" is a JVM whose owning MCP process is dead but the JVM
    is still alive (typical after SIGKILL / force-quit / crash).

    SAFETY: Defaults to dry_run=True (lists orphans, kills nothing).
    Pass dry_run=False to actually terminate. Live spawns from sibling
    MCPs are NEVER touched, even with dry_run=False.

    Returns a list of orphans (workdir, jvm_pid, owner_pid)."""
    return _pretty(spawner.cleanup_orphans(dry_run=dry_run))


@mcp.tool()
def get_spawned_breadboard() -> str:
    """Show metadata about this MCP session's spawned Breadboard.

    USE WHEN: You want the URL/port/pid of the auto- or explicit-spawn,
    or to check whether the JVM is still alive. Returns null if no
    spawn happened in this MCP session (e.g. when running in attach
    mode).

    Returned fields include `alive` (whether the JVM subprocess is
    still running), `url`, `port`, `pid`, `workdir`, `admin_email`,
    `admin_password`, `log_file`."""
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
