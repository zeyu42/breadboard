# Breadboard MCP server

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
  so every Step's `run` / `done` closure, every helper class
  (e.g. `TreatmentManager`), and runtime bindings (`g`, `a`, `c`,
  `events`, ...) are live.
- **Evaluate Groovy** against the live engine — the same engine the in-app
  Scriptboard uses.
- **Inspect** experiments, instances, the event log, and the CSV exports.

## Server requirements

This MCP server talks to a small set of `/debug/*` HTTP endpoints added to
Breadboard on this branch (`app/controllers/DebugController.java` + new
routes). You **must** be running a build that contains those changes; see
[`DEV_NOTES.md`](./DEV_NOTES.md) for the working build/run recipe (Java 8,
`sbt stage` + staged binary, etc.).

## Tools (18)

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

Three environment variables. Copy `.env.example` or export them in the
launching shell.

| Variable | Default | Meaning |
|---|---|---|
| `BREADBOARD_URL` | `http://localhost:9000` | Base URL of the Breadboard server |
| `BREADBOARD_EMAIL` | _required_ | Admin email |
| `BREADBOARD_PASSWORD` | _required_ | Admin password |

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

### Option B (recommended): use `--print-claude-config`

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

### Option C: use the Claude Code CLI

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

After either option, restart Claude Code. The 18 tools above appear under
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
