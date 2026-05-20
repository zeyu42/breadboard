# Breadboard MCP server

> **Do not use in production.** This MCP exposes a Groovy-eval surface
> (`/debug/script`), seeds hard-coded default admin credentials in spawn
> mode (`admin@example.com` / `admin123`), and binds locally without TLS.
> The `mcp.enabled` config flag keeps `/debug/*` 404'd by default, but
> turning it on against a public Breadboard is unsafe regardless of the
> flag — treat `mcp.enabled=true` as a local-machine-only setting. See
> [`DEV_NOTES.md`](./DEV_NOTES.md) for the full threat model.

An [MCP](https://modelcontextprotocol.io) server that lets an LLM agent
(e.g. Claude in Claude Code) **build, run, and debug Breadboard
experiments through HTTP+JSON** — no clicking around the admin UI, no
`npm run serve`.

## What it does

- **Create** experiments, toggle file-mode, and **sync a local
  `backend/`** into Breadboard's `dev/<exp>/` (the MCP equivalent of
  `npm run serve` in the
  [v2.4 template](https://github.com/human-nature-lab/breadboard-v2.4-default)).
- **Launch** an `ExperimentInstance` and **bind the script engine** to
  it; runtime bindings (`g`, `a`, `c`, `events`, ...) are live.
- **Evaluate Groovy** against the live engine (same engine the
  Scriptboard uses).
- **Inspect** experiments, instances, the event log, and CSV exports.

## Server requirements

Talks to `/debug/*` endpoints added on this branch
(`app/controllers/DebugController.java` + new routes). You must run a
build that contains those changes — see [`DEV_NOTES.md`](./DEV_NOTES.md)
for the working build/run recipe (Java 8, `sbt stage` + staged binary).

The endpoints are gated by `mcp.enabled` (default off). Spawn mode
passes `-Dmcp.enabled=true` automatically. **Attach mode requires you
to enable it** — set `mcp.enabled = true` in `conf/application.conf`,
pass `-Dmcp.enabled=true` on the JVM, or export `MCP_ENABLED=true`.

## Two ways to use it

| Mode | When | How |
|---|---|---|
| **Spawn** (default) | You want a fresh, private Breadboard per Claude session. | Auto-spawns on first tool call; or call `spawn_breadboard` explicitly. |
| **Attach** | You already have a Breadboard running and want the MCP to talk to it. | `attach_breadboard(url=...)`. With no `url`, uses the `BREADBOARD_URL` env var if it responds. Attaches are exclusive (file-locked). |

Precedence: explicit attach > current spawn > auto-spawn.

## Tools (25)

### Read-only inspection
| Tool | Purpose |
|---|---|
| `list_experiments` | All experiments owned by the configured admin |
| `get_experiment(id)` | Full experiment incl. step Groovy source |
| `list_instances(experimentId)` | All runs of an experiment |
| `get_instance(id)` | One instance (status, data, AMT hits) |
| `get_instance_events(id, limit?, offset?, name_filter?)` | Paged event log |
| `get_current_selection` | What the engine is bound to |
| `get_experiment_paths(id)` | On-disk paths Breadboard uses for `dev/` |
| `instance_data_csv(experimentId)` | CSV of all instances |
| `event_csv(instanceId)` | CSV of all events for one instance |

### Experiment setup
| Tool | Purpose |
|---|---|
| `create_experiment(name, copy_experiment_id?)` | New experiment |
| `set_file_mode(id, enabled?)` | Toggle / set file-mode |
| `sync_experiment_files(id, source_dir, ...)` | Copy a local `backend/` into Breadboard (MCP equivalent of `npm run serve`) |
| `set_selected_experiment(id)` | Set the admin's selected experiment |

### Live engine
| Tool | Purpose |
|---|---|
| `select_experiment_for_engine(id)` | Rebuild the engine, load Step sources (required before `launch_game`) |
| `launch_game(name, parameters?)` | Create + start an `ExperimentInstance` |
| `select_instance_for_engine(id)` | Bind engine to an existing instance |
| `stop_game(instanceId)` | Stop a running instance |
| `execute_script(groovy)` | Eval Groovy against the live engine |

`execute_script` is **not sandboxed** — same engine the Scriptboard
hits. Treat it like typing into the Scriptboard.

### Spawner lifecycle
| Tool | Purpose |
|---|---|
| `spawn_breadboard(...)` | Start a session-private JVM. Idempotent. Auto-runs orphan cleanup first. |
| `terminate_breadboard()` | Stop the spawn (graceful → force-kill). Atexit also fires this. |
| `get_spawned_breadboard()` | Current spawn's url/port/pid/workdir/alive (or null). |
| `list_alive_breadboards()` | Externally-running Breadboards the MCP could attach to. |
| `attach_breadboard(url?, email?, password?)` | Attach to a Breadboard. Exclusive (file lock). |
| `detach_breadboard()` | Clear an attach. |
| `cleanup_orphan_breadboards(dry_run?)` | List/kill JVMs whose owner MCP is dead. Defaults to `dry_run=True`. |

Spawn internals (workdir layout, orphan recovery semantics across
multiple Claude sessions, cross-platform behavior) are documented in
[`DEV_NOTES.md`](./DEV_NOTES.md).

## Installation

Requires Python 3.10+. Install into a venv with pip:

```bash
cd mcp-server
python -m venv .venv
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows (cmd)
.venv\Scripts\Activate.ps1         # Windows (PowerShell)
pip install -e .
```

With the venv activated, the `breadboard-mcp` command is now on PATH.

## Configuration

Spawn mode (default) needs no env vars.

Attach mode reads these as fallbacks when `attach_breadboard()` is
called without explicit args:

| Variable | Meaning |
|---|---|
| `BREADBOARD_URL` | Attach candidate; appears in `list_alive_breadboards` if responding. |
| `BREADBOARD_EMAIL` | Admin email fallback for `attach_breadboard()`. |
| `BREADBOARD_PASSWORD` | Admin password fallback. |

Spawn-mode overrides (`BREADBOARD_STAGED_BIN`,
`BREADBOARD_MCP_SESSION_DIR`) and platform-specific behavior are in
[`DEV_NOTES.md`](./DEV_NOTES.md).

## Wiring into Claude Code

Claude Code launches MCP servers without an active venv, so the config
must point at the venv binary by absolute path. With the venv
activated, run:

```bash
breadboard-mcp --print-claude-config
```

That prints a JSON block with the path already filled in. Merge it into
your `~/.claude.json` `mcpServers` section, edit the `env` values, and
restart Claude Code. On Windows the command auto-resolves to
`breadboard-mcp.exe`.

Alternative wiring via `claude mcp add` is in
[`DEV_NOTES.md`](./DEV_NOTES.md).

## Typical debugging flow

Ask Claude things like:
- *"List my experiments and show me the source of the OnJoinStep in
  experiment 33."* → `list_experiments`, `get_experiment(33)`
- *"Run experiment 33: bind the engine, launch a new instance, then
  show me `g.V.count()` and the registered steps."* →
  `select_experiment_for_engine(33)`, `launch_game(...)`,
  `execute_script(...)`
- *"In instance 47, last 50 events with 'Choice' in the name?"* →
  `get_instance_events(47, 50, 0, "Choice")`

## Simulating players

See [`examples/simulate_players.py`](./examples/simulate_players.py) for
a minimal, experiment-agnostic WebSocket player simulator, and
[`examples/README.md`](./examples/README.md) for how to extend it.

## Limitations

- **No browser.** The MCP drives the server, not the Vue client.
- **One admin user / one classloader / one engine** — concurrent debug
  sessions for the same admin will interleave.
- **`select_experiment_for_engine` rebuilds the engine** — previously-
  bound state (the `g` graph, in-flight timers) is wiped.
- **Play 2.2 doesn't answer WS ping frames.** Python WS simulators
  must set `ping_interval=None` and rely on an application-level
  heartbeat.

See [`DEV_NOTES.md`](./DEV_NOTES.md) for build prereqs, spawner
internals, multi-session/orphan behavior, platform notes, and the bugs
this branch fixes.
