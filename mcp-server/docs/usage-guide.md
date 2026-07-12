# Breadboard MCP — Usage Guide

This is the long-form reference. The tool descriptions and the server's
`instructions` block carry the fast-path summary; come here when you
need more depth.

## What Breadboard is

Breadboard is a Play 2.2 / Java / Groovy platform for running
multi-participant experiments (originally networked games, behavioral
studies, etc.). It serves a Vue admin UI for authoring experiments and
a WebSocket-driven participant client. Internally it runs a single
Groovy script engine that the admin (or this MCP) can poke at via the
"Scriptboard" — a runtime that evaluates Groovy against a live
ExperimentInstance.

This MCP exposes that Scriptboard plus a handful of CRUD + file-sync
endpoints. The MCP **tools** themselves do not produce participant
behavior. For the participant side you have two options:
- **Manual testing:** open the participant URL in a real browser and
  click through the experiment yourself.
- **Automated simulation:** run the bundled Python WebSocket
  simulator (resource `examples://simulate-players`) as a separate
  process.

## Glossary

- **Experiment** — the design / template. A set of Steps, Content
  blocks, parameters, and a starting graph. Authored once.
- **ExperimentInstance** — one execution of an experiment, with its
  own runtime graph, event log, and status. Often called "instance" /
  "run" / "game" interchangeably.
- **Step** — a stage of the experiment, authored as a Groovy file
  with `run` and `done` closures. The engine progresses through Steps
  in response to triggers (player joins, custom events from clients,
  step closures setting `player.step` directly).
- **The engine** / **Scriptboard** — the in-server Groovy interpreter.
  One per Breadboard JVM. Binds to one Experiment + one
  ExperimentInstance at a time.
- **Bindings** — variables auto-injected into every Groovy script the
  engine evaluates (`g`, `a`, `c`, `events`, `r`, `results`). See
  `docs://groovy-cheatsheet` for usage examples.
- **Player** — a participant. Represented as a Vertex in `g`.
- **`g`** — a TinkerGraph (Gremlin-style property graph). Vertices are
  typically players + experiment entities; edges are relationships.
- **file mode** — when ON, the experiment reads its
  steps/content/parameters from `dev/<dirName>/` on disk; when OFF,
  it reads from the DB.
- **Spawn mode** — the MCP starts its own private Breadboard JVM
  (default; auto-spawn on first tool call).
- **Attach mode** — the MCP drives a Breadboard the user is already
  running. Requires `mcp.enabled=true` on the target.

## Engine binding — the central concept

The engine must be bound to BOTH an experiment (so step source is
loaded) AND an ExperimentInstance (so the runtime graph, players, and
events have something to operate on). Without both bindings,
`execute_script` produces stale or empty bindings.

Check with `get_current_selection`. Rebuild with
`select_experiment_for_engine` (destructive — wipes prior engine
state). Bind an instance with `launch_game` (creates a new one) or
`select_instance_for_engine` (use an existing one).

Only one experiment / one instance is bound at a time. Breadboard itself
can RUN many instances concurrently, but MCP `launch_game` first stops
every active instance owned by the admin so MCP-managed runs do not pile
up with stale `RUNNING` statuses.

## Canonical workflows

### A. Read-only inspection (no engine needed)

```
list_experiments
get_experiment(id)              # read Step source
list_instances(id)              # see runs
get_instance_events(instance_id)
```

### B. Run a new game from scratch (experiment already exists)

```
select_experiment_for_engine(id)
launch_game('run-name')
execute_script('g.V.count()')   # 0 until a player joins
stop_game(instance_id)
```

### C. Debug an existing running instance

```
select_experiment_for_engine(id)
select_instance_for_engine(instance_id)
execute_script(...)
```

### D. Push local files into an existing experiment

```
set_file_mode(id, enabled=True)
sync_experiment_files(id, source_dir='/abs/path/to/backend')
select_experiment_for_engine(id)   # reload updated source
```

### E. Create a new experiment

```
create_experiment('name')                    # blank (3 default steps)
create_experiment('name', copy_experiment_id=42)   # from template
```

### F. Typical project cold-start (user has local backend/, no experiment)

See `docs://typical-project-workflow`.

## Gotchas

- **`launch_game` produces an empty graph.** Players don't appear by
  magic. The MCP doesn't drive the browser. Right after launch,
  `g.V.count()` is typically 0 — that's normal. To add participants,
  open the participant URL in a browser, run the Python WebSocket
  simulator (`examples://simulate-players`), or spawn synthetic
  vertices in Groovy (`docs://groovy-cheatsheet`).

- **Steps fire on signals, not on binding.** Selecting an experiment
  and launching a game does not execute any step's `run` closure on
  its own; closures fire when triggered (player joins, custom events
  from clients, parameter changes, internal scheduling).

- **`g` is per-instance.** Two RUNNING instances have two separate
  graphs; switch with `select_instance_for_engine` to see each.

- **`select_experiment_for_engine` is DESTRUCTIVE.** Rebuilds the
  engine. Wipes the `g` graph, in-flight timers, registered handlers.

- **`set_selected_experiment` is NOT the same as
  `select_experiment_for_engine`.** The former sets the admin's UI
  preference; only the latter actually rebuilds the engine.

- **First tool call can take ~30s** because auto-spawn boots a JVM.

- **MCP doc / tool-description changes require restarting the MCP
  server to take effect.** Editing `server.py`, the `.md` files under
  `mcp-server/docs/`, or the example scripts does not hot-reload —
  the running process holds the previous descriptions/resources. In
  Claude Code / Codex this usually means restarting the host so the
  MCP subprocess is relaunched. Tool *behavior* is unaffected (it
  comes from the live code, not the cached schema), but agents
  reading the descriptions will see the stale version until restart.

- **Spawn-mode default credentials** are `admin@example.com` /
  `admin123`. Hardcoded.

- **Tool returns are JSON strings** (or CSV strings for `*_csv`
  tools). Some MCP clients also wrap them in `{"result": "..."}`.
  Parse before treating fields as objects.

- **Attach mode requires `mcp.enabled=true`** on the target Breadboard
  (config, `-Dmcp.enabled=true`, or `MCP_ENABLED=true` env). Without
  it, every `/debug/*` call 404s. Spawn passes the flag automatically.

- **`sync_experiment_files` is ONE-SHOT, not watched.** Unlike
  `npm run serve`, it does not re-sync after the user edits a file.
  Re-run sync + select_experiment_for_engine after every edit.

- **Gremlin pipe output.** `execute_script("g.V.count()")` returns
  output `==>0`, not `0`. The `==>` prefix is Gremlin's pipe-result
  format. Expression results, lists, and maps all use it.

## What the MCP cannot do

- Drive the Vue admin UI or the participant browser.
- Produce participant behavior on its own. The MCP tools drive the
  server side only. To get participant-side activity you either run
  the bundled Python simulator (`examples://simulate-players`) as a
  separate process, or open the participant URL yourself in a
  browser and click through it manually.
- Run two engine bindings in parallel within one session.
- Watch local files (sync is one-shot).
- Detect your local project layout — you must pass an absolute path
  to `sync_experiment_files`.

## Spawn prerequisites

Spawn mode requires a staged binary at
`<repo_root>/target/universal/stage/bin/breadboard`, produced by
`sbt stage` from the Breadboard repo root. The MCP resolves `repo_root`
automatically from its own installation path — works when the MCP was
installed via `pip install -e .` from inside `mcp-server/`.

If `spawn_breadboard` errors with "Staged Breadboard binary not
found":
1. Have the user run `sbt stage` in the Breadboard repo, OR
2. Set `BREADBOARD_STAGED_BIN=/abs/path/to/breadboard`, OR
3. Pass `repo_root=...` to `spawn_breadboard`.

If the user is already running Breadboard themselves (e.g. via
`./start` or `play run`), use `attach_breadboard(url=...)` instead.
Never auto-fall-back to attach — make the user choose explicitly.
