# Breadboard MCP server

> **Do not use in production.** Spawn mode seeds a hard-coded default admin
> (`admin@example.com` / `admin123`) into each session-private Breadboard, the
> `/debug/*` endpoints are not hardened against untrusted callers, and the
> spawned JVM binds locally without TLS. This MCP is intended for local
> development and debugging only.

An [MCP](https://modelcontextprotocol.io) server that lets an LLM agent
(e.g. Claude in Claude Code) **build, run, and debug Breadboard experiments
through HTTP+JSON** — no clicking around the admin UI, no `npm run serve`.

## What it does

The agent can:

- **Create** a new experiment, toggle file-mode, and **sync a local
  `backend/` directory** of Groovy steps + frontend assets into Breadboard's
  `dev/<exp>/` folder. This replaces the webpack `CopyPlugin` that
  `npm run serve` runs in template projects like
  [`breadboard-v2.4-default`](https://github.com/human-nature-lab/breadboard-v2.4-default).
- **Launch** an `ExperimentInstance` and **bind the script engine** to it,
  so every Step's `run` / `done` closure, every helper class loaded by
  your experiment, and runtime bindings (`g`, `a`, `c`, `events`, ...)
  are live.
- **Evaluate Groovy** against the live engine — the same engine the in-app
  Scriptboard uses.
- **Inspect** experiments, instances, the event log, and the CSV exports.

## Server requirements

This MCP server talks to a small set of `/debug/*` HTTP endpoints added to
Breadboard on this branch (`app/controllers/DebugController.java` + new
routes). You **must** be running a build that contains those changes; see
[`DEV_NOTES.md`](./DEV_NOTES.md) for the working build/run recipe (Java 8,
`sbt stage` + staged binary, etc.).

The `/debug/*` endpoints are gated by `mcp.enabled` (default **off**) so a
production Breadboard never exposes them by accident. Spawn mode passes
`-Dmcp.enabled=true` to the JVM automatically. **Attach mode requires you
to enable it yourself** — either set `mcp.enabled = true` in
`conf/application.conf`, or start Breadboard with `-Dmcp.enabled=true`, or
export `MCP_ENABLED=true` in the env. If you don't, every `/debug/*` call
returns 404.

## Two ways to use it

| Mode | When | How |
|---|---|---|
| **Spawn** a session-private Breadboard | **Default.** First tool call auto-spawns a fresh JVM on a free port with its own H2 database. Best for parallel Claude Code sessions / worktrees that don't want to race on a shared server. | Either call `spawn_breadboard` explicitly, or just call any tool — the MCP auto-spawns transparently on first use. |
| **Attach** to an existing Breadboard | You're already running a Breadboard (e.g. `./start`) and want the MCP to talk to it. | Call `attach_breadboard(url=...)`. With no `url` arg, the MCP looks for the Breadboard at the `BREADBOARD_URL` env var; if it responds, that becomes the attach target. Spawns are session-private — they're never auto-offered as attach candidates (each MCP owns its own). You can still attach to a spawn by passing its url explicitly. |

Precedence: explicit attach > current spawn > auto-spawn.

## Tools (25)

### Read-only inspection

| Tool | Purpose |
|---|---|
| `list_experiments` | All experiments owned by the configured admin user |
| `get_experiment(id)` | Full experiment: steps (with Groovy source), content, parameters, languages, instance stubs |
| `list_instances(experimentId)` | All runs of an experiment |
| `get_instance(id)` | One instance (status, data, AMT hits) |
| `get_instance_events(id, limit?, offset?, name_filter?)` | Paged event log |
| `get_current_selection` | Which experiment + instance are currently bound to the script engine |
| `get_experiment_paths(id)` | On-disk paths Breadboard uses for this experiment's `dev/` directory |
| `instance_data_csv(experimentId)` | CSV of all instances of an experiment |
| `event_csv(instanceId)` | CSV of all events for one instance |

### Mutating: experiment setup

| Tool | Purpose |
|---|---|
| `create_experiment(name, copy_experiment_id?)` | Create a new experiment |
| `set_file_mode(id, enabled?)` | Toggle / set file-mode. When turning on, Breadboard exports the experiment into `dev/<dir>/` |
| `sync_experiment_files(id, source_dir, public_root?, dev_mode?)` | Copy a local `backend/` directory into Breadboard's `dev/<dir>/`. Equivalent of `npm run serve` |
| `set_selected_experiment(id)` | Set the admin user's selected experiment (lighter-weight than `select_experiment_for_engine`) |

### Mutating: live engine lifecycle

| Tool | Purpose |
|---|---|
| `select_experiment_for_engine(id)` | **Rebuild the engine, load all Step sources.** Required before `launch_game` |
| `launch_game(name, parameters?)` | Create + start an `ExperimentInstance`; runs each Step source into the engine so closures are registered |
| `select_instance_for_engine(id)` | Bind the engine to an existing instance (e.g. after a restart) |
| `stop_game(instanceId)` | Stop a running instance |
| `execute_script(groovy)` | Eval Groovy against the live engine. Returns `{output, error}` |

`execute_script` is **not sandboxed** — it's the same engine the Scriptboard
hits, so a mutating script really mutates the running game. Treat it the
way you'd treat typing into the Scriptboard.

### Per-session Breadboard subprocess

| Tool | Purpose |
|---|---|
| `spawn_breadboard(repo_root?, admin_email?, admin_password?)` | Start a session-private Breadboard JVM (free port, own H2 db, own dev/ dir). Idempotent — returns existing metadata if one is already alive. Auto-runs orphan cleanup first. Also runs implicitly on the first tool call if no Breadboard is selected. |
| `terminate_breadboard()` | Stop the spawned subprocess (graceful stop, then force-kill fallback on Unix; Windows uses `TerminateProcess` outright). Optional: atexit also fires this on MCP exit. |
| `get_spawned_breadboard()` | Show url/port/pid/workdir/`alive` of the spawn (or null). |
| `list_alive_breadboards()` | List externally-running Breadboards the MCP could attach to (currently: the `BREADBOARD_URL` env var if it responds). Spawns are excluded — they're session-private. |
| `attach_breadboard(url?, email?, password?)` | Attach to a Breadboard. With `url`: explicit. Without: looks for one candidate via `list_alive_breadboards`; errors if 0, attaches if 1. Subsequent tool calls route to the attached URL. **Exclusive**: at most one MCP can be attached to a given Breadboard at a time (file lock under `~/.breadboard-mcp/attach-locks/`); attempts to attach to a URL that's already attached, or to another MCP's spawn URL, are refused with the holder's PID. The success response includes a warning to avoid manual use of the Breadboard while attached. |
| `detach_breadboard()` | Clear an attach. Subsequent calls revert to spawn (or auto-spawn). |
| `cleanup_orphan_breadboards(dry_run?)` | List orphan JVMs from sibling MCP processes that died abnormally (SIGKILL on Unix, crash, hard-terminate on Windows). **Defaults to `dry_run=True`** — just lists, doesn't kill. Pass `dry_run=False` to actually terminate them. Only kills JVMs whose owner MCP is dead; live spawns from other Claude/MCP sessions are preserved. |

Spawned instances live under `~/.breadboard-mcp/sessions/<id>/` (db/, dev/,
logs/, RUNNING_PID, owner.pid, stdout.log). Workdirs are intentionally not
auto-deleted after termination so you can inspect logs; clean them yourself
when you're done. Override the parent dir with `BREADBOARD_MCP_SESSION_DIR`;
override the staged binary path with `BREADBOARD_STAGED_BIN`.

### Multiple Claude instances / worktrees

The spawner is built for the case where you have one Claude Code session per
git worktree, each running its own MCP server, each spawning its own
Breadboard. Each spawn writes the MCP process's PID into the workdir as
`owner.pid`. Cleanup logic considers a JVM "orphan" **only** when its
recorded owner is dead — so the cleanup tool can run in any session and
will never touch live spawns belonging to other live MCPs.

Orphan reaping runs automatically:
- At the start of every `spawn_breadboard()` call.
- From the `atexit` handler when an MCP process exits cleanly.
- On demand via the `cleanup_orphan_breadboards()` MCP tool or
  `breadboard-mcp --cleanup-orphans` from the shell.

If an MCP process is killed abruptly without running `atexit` (SIGKILL
on Unix, hard-terminate on Windows), the JVM is orphaned until any of
the above triggers reap it. Worst case: extra RAM use until the next
session does anything.

## Platform support

macOS and Linux are first-class. Windows works with two behavior
differences in the spawner; functionally everything else is the same.

| Aspect | macOS / Linux | Windows |
|---|---|---|
| Staged binary | Invokes `target/universal/stage/bin/breadboard` (Unix shell script). | Invokes `target/universal/stage/bin/breadboard.bat`. Auto-detected. |
| `groovy/` & `data/` linkage into per-session workdirs | **Symlink** (`os.symlink`). Edits to the source dirs are picked up live on next experiment reload. | **Recursive copy** (`shutil.copytree`). Each spawn gets a frozen snapshot at spawn time. Edits made *after* spawn require terminate + respawn to take effect. Why: Windows symlinks need admin or Developer Mode; junctions would work without elevation but require shelling out to `mklink /J`. Copy is simpler and the dirs are tiny (~130 KB on the default install). |
| Process termination | `terminate_breadboard()` sends SIGTERM, waits 10s, escalates to SIGKILL. | `terminate_breadboard()` calls `TerminateProcess` (Python's cross-platform `Popen.terminate()`/`kill()`) which is already a hard kill — no escalation needed. |
| Orphan cleanup | Sends SIGTERM, waits, escalates to SIGKILL if needed. | Sends `TerminateProcess` directly (single step). |

If your `data/` directory grows large (multi-MB CSVs for big experiments)
and you're spawning a lot of sessions on Windows, you may want to set
`BREADBOARD_MCP_SESSION_DIR` to a fast SSD location to keep the
per-spawn copy cheap.

## Installation

Requires Python 3.10+. Install into a venv with pip:

```bash
cd mcp-server
python -m venv .venv
```

Activate the venv:

```bash
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows (cmd)
.venv\Scripts\Activate.ps1         # Windows (PowerShell)
```

Then install:

```bash
pip install -e .
```

With the venv activated, the `breadboard-mcp` command is now available.

## Configuration

For spawn mode (the default) no env vars are needed — the spawner picks a
port, seeds its own admin user (default `admin@example.com` / `admin123`),
and the MCP routes tool calls to it automatically.

For attach mode, `attach_breadboard(url=..., email=..., password=...)`
takes the credentials directly. As a convenience, if `email` /
`password` are omitted, these env vars are used as fallback:

| Variable | Meaning |
|---|---|
| `BREADBOARD_URL` | A pre-set attach candidate. If set and responding, it appears in `list_alive_breadboards`. |
| `BREADBOARD_EMAIL` | Admin email fallback for `attach_breadboard()`. |
| `BREADBOARD_PASSWORD` | Admin password fallback for `attach_breadboard()`. |

Optional spawn-mode overrides:

| Variable | Default | Meaning |
|---|---|---|
| `BREADBOARD_STAGED_BIN` | `<repo_root>/target/universal/stage/bin/breadboard` | Path to the staged Breadboard launcher script. |
| `BREADBOARD_MCP_SESSION_DIR` | `~/.breadboard-mcp/sessions` | Parent directory for per-session workdirs. |

The server authenticates via `POST /debug/login` (JSON), not the legacy
`POST /login` (form-based, broken on this build — see `DEV_NOTES.md`).

## Running

Standalone (stdio transport — the default MCP transport, mainly useful as
a sanity check that `breadboard-mcp` is on PATH):

```bash
breadboard-mcp
```

## Wiring it into Claude Code

Claude Code launches MCP servers from a context with no active venv, so the
config has to point at the venv's `breadboard-mcp` binary by absolute path.
There are two equivalent ways to set that up.

### Option 1 (recommended): use `--print-claude-config`

With the venv activated, run:

```bash
breadboard-mcp --print-claude-config
```

This prints a JSON block with the absolute path to the installed binary
already filled in. Paste it into `~/.claude.json` (or merge it with your
existing `mcpServers` section), then edit the `env` values to match your
admin credentials.

Example output:

```json
{
  "mcpServers": {
    "breadboard": {
      "command": "/absolute/path/to/breadboard/mcp-server/.venv/bin/breadboard-mcp",
      "env": {
        "BREADBOARD_URL": "http://localhost:9000",
        "BREADBOARD_EMAIL": "admin@example.com",
        "BREADBOARD_PASSWORD": "..."
      }
    }
  }
}
```

On Windows the `command` ends in `.venv\Scripts\breadboard-mcp.exe` —
`--print-claude-config` handles that automatically.

### Option 2: use the Claude Code CLI

If you have the `claude` CLI installed, no manual JSON editing is needed.
With the venv activated:

macOS / Linux:

```bash
claude mcp add breadboard "$(which breadboard-mcp)" \
  --env BREADBOARD_URL=http://localhost:9000 \
  --env BREADBOARD_EMAIL=admin@example.com \
  --env BREADBOARD_PASSWORD=changeme
```

Windows (PowerShell):

```powershell
claude mcp add breadboard (Get-Command breadboard-mcp).Source `
  --env BREADBOARD_URL=http://localhost:9000 `
  --env BREADBOARD_EMAIL=admin@example.com `
  --env BREADBOARD_PASSWORD=changeme
```

After either option, restart Claude Code. The 25 tools above appear under
the `breadboard` MCP server.

## Typical debugging flow

1. Start Breadboard from the staged binary (see `DEV_NOTES.md`).
2. In Claude Code, ask things like:
   - *"List my experiments and show me the source of the OnJoinStep in
     experiment 33."* → `list_experiments`, `get_experiment(33)`
   - *"Run experiment 33: bind the engine, launch a new instance, then
     show me `g.V.count()` and the names of the registered steps."* →
     `select_experiment_for_engine(33)`, `launch_game(...)`,
     `execute_script(...)`
   - *"In instance 47, what were the last 50 events with 'Choice' in the
     name?"* → `get_instance_events(47, 50, 0, "Choice")`

## Simulating players

See [`examples/simulate_players.py`](./examples/simulate_players.py) for
a minimal, experiment-agnostic player simulator template, plus
[`examples/README.md`](./examples/README.md) for the workflow to extend
it for your experiment.

## Limitations

- **No browser.** The MCP drives the server, not the Vue client. If you
  need to see what a real browser renders, run `npm run build` and use a
  real browser alongside.
- **One admin user / one classloader / one engine.** Concurrent debug
  sessions for the same admin will interleave. Not a problem for a single
  Claude session.
- **Re-running `select_experiment_for_engine` rebuilds the engine** — any
  previously-bound state (the `g` graph, in-flight timers) is wiped.
- **Play 2.2 doesn't answer WS ping frames.** If you write a Python WS
  client to simulate players, set `ping_interval=None` and rely on the
  experiment's application-level heartbeat instead.

See `DEV_NOTES.md` for build prereqs, the bugs this branch fixes, and the
sbt + Java 8 + jfrog-mirror story.
