# Typical Project Workflow

For the case where the user is working in a Breadboard experiment
project (e.g. one based on the
[`breadboard-v2.4-default`](https://github.com/human-nature-lab/breadboard-v2.4-default)
template) and wants to test their local files against a Breadboard
server.

## What the user's project looks like

A typical project directory may contain any subset of the following.
**All of these except `Steps/` are optional** — real templates often
omit several:

```
<project>/
  backend/
    Steps/             # *.groovy files — one Step per file (REQUIRED in practice)
    Content/           # *.html content blocks                   (optional)
    Images/            # static images                            (optional)
    parameters.csv     # run-time parameters (may be header-only) (optional)
    client-html.html   # participant HTML (may be empty)          (optional)
    client-graph.js    # participant graph script                 (optional)
    style.css                                                     (optional)
    .breadboard        # template metadata (see below)            (optional)
  package.json         # frontend tooling — npm run serve/build, NOT used by this MCP
  ...
```

The `backend/` directory is what Breadboard reads when file mode is
on. `sync_experiment_files` copies whatever subset is present and
silently skips the rest — missing files are not errors.

### The `.breadboard` file

Templates often ship a `.breadboard` JSON file with fields like
`version`, `experimentUid`, `experimentName`. Breadboard reads this
**only during ZIP import** (the admin UI's "Import" action), where
`version` decides whether to use the v2.2 or v2.3+ import path. In
file-mode sync (this MCP's flow), the file is just copied along as a
benign artifact — Breadboard does not act on it. The experiment's
real uid/name stay whatever `create_experiment` set them to.

The participant frontend (Vue / JS / CSS) is a separate concern handled
by the project's `npm run build` (or `npm run serve` during
development). This MCP does NOT build or serve the frontend — the user
must do that themselves if they care about what participants see.

## Discovering the right path

The MCP does not know your working directory. Before calling
`sync_experiment_files`, ensure you have the absolute path to the
user's `backend/`. If your agent has filesystem tools, look for
`backend/Steps/*.groovy` to confirm the layout.

## The workflow

Assume the user has no existing Breadboard experiment for this project
(fresh database).

> **About the code snippets below.** They use Python-like function-call
> syntax for readability (`create_experiment("MyExperiment")`,
> `sync_experiment_files(id, source_dir=...)`), but the actual MCP
> tool-call shape is structured JSON arguments dispatched through your
> agent's MCP client (e.g. tool name `create_experiment`, args
> `{"name": "MyExperiment"}`). The snippets are illustrative; don't
> copy them verbatim into a Python file.

```python
# 1. Create the experiment. It gets three default lifecycle steps
#    (OnJoinStep, OnLeaveStep, InitStep) which will be overwritten in
#    step 3.
result = create_experiment("MyExperiment")
expt_id = json.loads(result)["id"]

# 2. Turn file mode on. Breadboard exports the current DB content into
#    dev/<dirName>/ (the three default steps land there). The
#    experiment now reads from dev/, not the DB.
set_file_mode(expt_id, enabled=True)

# 3. Sync the user's backend/ into Breadboard's dev/<dirName>/.
#    OVERWRITES any files that came from the DB export.
sync_experiment_files(expt_id, source_dir="/abs/path/to/backend")

# 4. Rebuild the engine so it picks up the new Step source.
select_experiment_for_engine(expt_id)

# 5. Launch a run.
result = launch_game("dev-run-1")
instance_id = json.loads(result)["experimentInstanceId"]

# 6. Inspect / poke at the running engine.
execute_script("g.V.count()")
# 0 unless someone (or the simulator) connects as a participant.
```

## If the experiment already exists

Skip step 1 and use `list_experiments` to find the id. Be aware of the
file-mode-on overwrite (step 2) — Breadboard exports whatever the DB
holds into dev/ at that moment. Step 3 then overlays the user's
backend/ on top, but anything in DB and NOT in backend/ remains.

## Files outside `backend/` (data, lookup tables, anything else)

`sync_experiment_files` only copies `backend/` into Breadboard's
`dev/<dirName>/`. Anything the experiment reads from a **different**
path — most commonly a sibling `data/` directory, but it could be any
folder the user keeps alongside `backend/` — has to be staged
separately. Examples seen in real projects:

- A Step reads `data/some-file.csv` to load conditions / parameters
  at engine startup.
- A Step reads `data/lookup.json` to map IDs to display strings.
- A Step opens any other file the experimenter wants the engine to
  see.

Those paths are resolved **relative to the Breadboard JVM's working
directory**, not relative to `dev/<dirName>/`. In spawn mode the JVM
CWD is the spawn workdir (returned by `get_spawned_breadboard()` in
the `workdir` field), so to satisfy `data/foo` you must put the file
at `<workdir>/data/foo` before `select_experiment_for_engine`.

How to handle this in a workflow:

1. Inspect the experiment's Steps for any path-like strings (`data/`,
   `lookup/`, `csv/`, hard-coded `new File("...")`, etc.). Grep is
   fine.
2. For each such path, copy the corresponding file from the user's
   project into the spawn workdir at the **path the Step expects** —
   which may not match the repo's actual layout. Translate as you
   copy. Example seen in real projects: a Step expects
   `data/two-door/pilot-3.csv` but the repo only has
   `data/pilot-3.csv`. You must put the file at
   `<workdir>/data/two-door/pilot-3.csv`, not just mirror the repo
   verbatim. The Step source is authoritative; the repo's `data/`
   layout is just one possible source.
3. Then call `select_experiment_for_engine`. If a Step's constructor
   reads the file at engine bind, missing files will throw at this
   step (often with errors like "No available conditions to assign"
   or generic IOExceptions) — readable in the spawn's `log_file`.

The MCP intentionally does not auto-sync sibling directories — they
can be anything the experimenter cares to ship, in any layout, and
the right policy varies per project.

## After edits

`sync_experiment_files` is **one-shot**, not a watcher. After the user
edits a file in `backend/`:

```python
sync_experiment_files(expt_id, source_dir="/abs/path/to/backend")
select_experiment_for_engine(expt_id)   # reload source into engine
```

If a game is currently running and you want the new step source to
apply to it: there's no clean way. The engine binding to the old
instance carries the old code. Stop the run, re-select, and launch a
fresh instance.

## Verifying an end-to-end run

A multiplayer run that "looks alive" can still be subtly broken
(players never paired, only one player finished, decision step silently
skipped, etc.). The instance's `status` field stays `RUNNING` even
after every player has completed — don't rely on it. Verify via the
event log instead, using `get_instance_events(instance_id, ...)` or
`event_csv`.

**Event names are NOT framework-defined.** Breadboard's framework
itself emits almost nothing with a fixed name; what shows up in the
event log is whatever the experiment's Steps choose to write via
`a.addEvent(name, data)` (or the bundled `chat.groovy` / `form.groovy`
modules, if used). So the right checklist for "this run worked" is
ALWAYS experiment-specific.

How to derive the checklist for an unfamiliar experiment:

1. **Find the event vocabulary.** From the cloned project:
   ```bash
   grep -REn 'addEvent\(' backend/Steps/ | sort -u
   ```
   Every name that appears in an `addEvent(...)` call is one this
   experiment will write to the log. Note the ones that fire on
   round boundaries, decisions, terminal steps, errors.

2. **Find the terminal step(s).** Look for closures that set
   `player.step = '...'` to a value that has no further transitions —
   the names vary per experiment. Often called `finish`, `results`,
   `done`, `survey-complete`, or similar.

3. **Build expectations.** For round-based experiments, the
   round-end event should appear N times (rounds × pairs). The
   terminal event(s) should appear once per player. If decisions or
   choices are recorded, the choice-event count should match
   `players × rounds` (or whatever the experiment's pairing
   structure dictates).

4. **Check player exit.** Player removal events (look for these in
   the same grep) typically carry an `exitType` field — values like
   `COMPLETE`, `DROP`, `EXIT`, `TIMEOUT` distinguish clean exits from
   early dropouts. The exact event name and exit-type vocabulary are
   experiment-specific.

If any expected count is off, use `name_filter` against
`get_instance_events` to inspect the matching events. A `RUNNING`
status with the expected event pattern means the run succeeded — the
status field just never auto-transitions.

## Frontend changes

The MCP doesn't touch `npm run build` or the bundled JS. If the user
edits `backend/client-html.html` or `backend/client-graph.js`, those
will sync via `sync_experiment_files` and reach the next participant
that loads the page — but Vue bundle changes still require the user to
rebuild the frontend separately.

## Common confusions

- **Relative vs absolute paths.** `source_dir` is resolved by the
  Breadboard JVM, whose CWD is its session workdir (e.g.
  `~/.breadboard-mcp/sessions/<id>/`), NOT the user's project CWD.
  Always pass an absolute path.

- **"npm run serve" comparison.** Functionally, `sync_experiment_files`
  is the manual one-shot version. The user no longer needs to run
  `npm run serve` against the backend (though they may still need
  `npm run build` for frontend changes).

- **Folder naming.** Capitalized `Steps/` in source is fine — the
  syncer normalizes to lowercase `steps/` to match Breadboard's
  file-mode reader. Same for `Content/` (kept capitalized; Breadboard
  expects that one).

- **`client-graph.js` placeholders.** The syncer substitutes
  `__PUBLIC_ROOT__`, `__DEV__`, and `__PROD__` the same way the
  webpack CopyPlugin does. The user does not need to template these
  themselves.
