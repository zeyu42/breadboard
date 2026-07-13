# Error Recipes

Common symptoms, what they mean, and how to fix.

## Spawn / startup

### `FileNotFoundError: Staged Breadboard binary not found at ...`

The auto-spawn or explicit `spawn_breadboard()` couldn't find the
Breadboard staged binary.

**Diagnosis**: either (a) the user hasn't run `sbt stage` in the
Breadboard repo, or (b) the MCP is installed outside the repo and the
auto-resolved `repo_root` is wrong.

**Fix**: tell the user to run `sbt stage` in the Breadboard repo root.
If they installed the MCP elsewhere, also tell them to set
`BREADBOARD_STAGED_BIN=/abs/path/to/breadboard` or pass `repo_root=`
to `spawn_breadboard`.

**Do not** silently fall back to attach. Let the user choose.

### `TimeoutError: Breadboard at http://... did not become ready within 90s`

The JVM started but never bound to a port within 90s. Usually means a
boot-time exception in Breadboard.

**Diagnosis**: read `log_file` from `get_spawned_breadboard()` — the
JVM's stdout/stderr. Common boot failures include schema-migration
errors, evolution conflicts, port collisions.

**Fix**: depends on the log content. A clean fix often involves
deleting the session workdir
(`~/.breadboard-mcp/sessions/<session_id>/`) and retrying.

### First tool call takes 30+ seconds, then succeeds

Normal. Auto-spawn is booting the JVM and seeding the admin user. No
action needed.

## Engine binding

### `Caught error: groovy.lang.MissingPropertyException: No such property: g`

The engine has no bindings — usually because no experiment is selected.

**Fix**: call `get_current_selection`. If `selectedExperiment` is
null, call `select_experiment_for_engine(id)`. If
`experimentInstanceId` is null, call `launch_game(...)` or
`select_instance_for_engine(...)`.

Also require `runtimeBindingVerified: true`; the saved selection alone
does not prove EventTracker survived a restart. After a JVM restart,
explicitly select the experiment and intended instance again before
participants connect.

This check protects against a deceptive partial failure: participant
handlers can keep changing the in-memory graph (decisions, surveys,
bonus values, and step state) while EventTracker silently writes
nothing to H2. The UI can therefore look functional until the JVM
stops and the graph disappears. Always verify the instance event log;
live graph state is not evidence of persistence.

### `stop_game` refuses an unrelated instance

This is intentional. Breadboard clears its current instance selection
when any instance is stopped, including an unrelated one. MCP will not
trigger that bug. Never sweep all RUNNING/TESTING instances. If an old
run must be stopped, bind and verify it first, then stop it.

### `g.V.count()` returns `==>0` right after `launch_game`

**Not an error**. The graph is empty because no players have joined.
A fresh `launch_game` creates an empty instance.

**To add participants**:
- Open the participant URL in a browser, OR
- Run the Python WebSocket simulator (see
  `examples://simulate-players`), OR
- Spawn synthetic vertices in Groovy: `a.add(g.addVertex(null))` (see
  `docs://groovy-cheatsheet`).

### Step changes don't appear after `sync_experiment_files`

The engine still has the old Step source loaded.

**Fix**: call `select_experiment_for_engine(id)` after every sync to
re-load the source. Note that engine rebuilds are DESTRUCTIVE — wipes
the runtime graph. If you need the new code in an existing running
instance, you must stop and re-launch.

### Switched instances but `execute_script` still shows old data

The engine binding cache may be stale.

**Fix**: call `get_current_selection` to confirm what's actually
bound. If wrong, call `select_instance_for_engine` again. If still
wrong, the engine may have lost the prior binding — re-bind the
experiment with `select_experiment_for_engine`, then the instance.

## /debug/* endpoints

### All tool calls 404 against an attached Breadboard

The target Breadboard has `mcp.enabled` off.

**Fix**: the user must enable it on the target server:
- Edit `conf/application.conf` to add `mcp.enabled=true`, OR
- Restart the JVM with `-Dmcp.enabled=true`, OR
- Set `MCP_ENABLED=true` and restart.

Spawn mode passes the flag automatically — this only affects attach.

### `attach_breadboard` errors "Cannot attach to ...: already attached by another MCP"

Another MCP process holds the exclusive attach lock for this URL.

**Fix**: wait for the other MCP to detach/exit, or pick a different
URL. Locks at `~/.breadboard-mcp/attach-locks/`.

## Sync / file mode

### `sync_experiment_files` runs but Breadboard ignores the files

File mode isn't on.

**Fix**: call `set_file_mode(id, enabled=True)` first.

### Sync writes succeed but some files are missing

The source layout doesn't match what `sync_experiment_files` expects
(see its docstring or `docs://typical-project-workflow`). Missing
files don't error; they just don't get copied.

**Fix**: confirm the source_dir contains the expected subdirectories
(`Steps/`, `Content/`, `parameters.csv`, etc.).

### Sync passes a relative path and writes to the wrong place

`source_dir` is resolved by the Breadboard JVM, whose working
directory is its session workdir — NOT the user's project directory.

**Fix**: always pass an absolute path.

## Groovy execution

### `MissingMethodException: No signature of method: X.foo() for argument types: (Y)`

Wrong argument type. Groovy lists candidates after "Possible
solutions:" in the same error — pick the closest signature.

For `a.add(...)`, the correct form is `a.add(Vertex)`. A common
mistake is passing a string id; the actual signature wants a Vertex
object obtained via `g.getVertex(id)` or `g.addVertex(null)`.

### Script returns `==>null` with no error

The last expression evaluated to null. Usually because a Step's `run`
closure returned nothing implicitly. Wrap your script in `[ ... ]` or
explicitly return a value.

### Want to discover an API

Use Groovy reflection in `execute_script`:

```groovy
a.class.methods.collect { it.name }.unique().sort()
a.class.methods.findAll { it.name == 'add' }.collect { it.toString() }
```

## CSV / data export

### `instance_data_csv` or `event_csv` returns empty / minimal content

The instance has no recorded data or events yet. Verify the instance
has progressed past at least one Step and that participants have
generated events.

## Process / state

### MCP started a JVM I can't kill

Use `cleanup_orphan_breadboards(dry_run=False)` to terminate JVMs
whose owning MCP is dead. Live spawns from sibling MCPs are never
touched.

### Multiple MCP sessions race on a port

Each spawn picks a free port automatically, so port collisions are
rare. Attach-mode sessions are serialized via the exclusive attach
lock — only one MCP can drive a given URL at a time.
