# Dev notes for the `mcp-server` branch

What this branch adds to Breadboard, how to build/run it from a fresh clone
on modern macOS, and the bugs encountered (and fixed) along the way.

## What this branch adds (server-side)

| File | Purpose |
|---|---|
| `app/controllers/DebugController.java` | All `/debug/*` HTTP endpoints |
| `app/models/NoopThrottledWebSocketOut.java` | `ThrottledWebSocketOut` subclass that captures actor messages instead of sending them to a real client; lets the debug controller drive actor messages from an HTTP handler |
| `app/models/ScriptBoard.java` | Added `processScriptSync(String)` — same Groovy eval as `processScript` but returns the JSON output synchronously |
| `app/models/Breadboard.java` | Made `instances` and `breadboardController` `public` (they were package-private) so the controller can address actors directly |
| `app/controllers/ExperimentController.java` | **Bug fix:** `getStepsFromDirectory` now sorts step files alphabetically |
| `app/models/Experiment.java` | Unchanged — `fileMode` was already a field |
| `conf/routes` | New routes for all `/debug/*` endpoints |
| `app/models/Experiment.java` | Added `@Transient` on `TEST_INSTANCE` — the actual fix for the VerifyError-on-fresh-build issue, without which `-noverify` was previously required |
| `repositories` | Sbt resolver config pointing at the jfrog mirror for old Play 2.2 deps |
| `project/build.properties` | Bumped `sbt.version` from `0.13.0` → `0.13.18` |
| `mcp-server/` | The Python MCP server, demo script, README |

## What this branch adds (HTTP API)

`/debug/*` endpoints — JSON in/out, secured by a Play session cookie set by
`POST /debug/login`. Full list:

```
POST /debug/login                         { email, password }            -> { uid, email }
GET  /debug/experiments                                                  -> [{ id, name, ... }]
GET  /debug/experiments/:id                                              -> full experiment JSON
GET  /debug/experiments/:id/instances                                    -> [{ id, status, ... }]
GET  /debug/experiments/:id/paths                                        -> { devDirectory, stepsDir, ... }
POST /debug/experiments/:id/file-mode     { enabled?: bool }             -> { fileMode, changed }
GET  /debug/instances/:id                                                -> instance stub
GET  /debug/instances/:id/events ?limit&offset&nameFilter                -> { totalEvents, events: [...] }
GET  /debug/selection                                                    -> { selectedExperiment, experimentInstanceId, ... }
POST /debug/selection/experiment          { experimentId }               -> ok
POST /debug/select-experiment             { experimentId }               -> ok   (rebuilds engine + loads steps)
POST /debug/launch-game                   { name, parameters? }          -> { experimentInstanceId, ... }
POST /debug/select-instance               { instanceId }                 -> ok
POST /debug/stop-game                     { instanceId }                 -> ok
POST /debug/script                        { script }                     -> { output, error }
POST /debug/bootstrap-schema                                             -> { status: "ok" }
```

`/debug/bootstrap-schema` exists only because of an upstream Breadboard quirk
(see "Per-session Breadboard spawner" below). It's idempotent and safe to
call any time; the only situation it actually mutates anything is on a
freshly-evolved DB that's never run `Global.onStart`'s pre-v2.3 upgrade path.

## Per-session Breadboard spawner

`mcp-server/src/breadboard_mcp/spawner.py` can launch its own Breadboard JVM
with a free port, a session-private H2 database, and a session-private
`dev/` directory. Useful for running two Claude Code sessions in parallel
without racing on a shared Breadboard.

### Workdir layout

```
~/.breadboard-mcp/sessions/<id>/
├── db/                  -- H2 file (created by Play on first connect)
├── dev/                 -- session-private file-mode experiment dir
├── logs/                -- Play logs
├── RUNNING_PID          -- written by Play, removed on graceful shutdown
└── stdout.log           -- combined JVM stdout/stderr
```

The session dir is named with an 8-char uuid prefix. Override the parent
with `BREADBOARD_MCP_SESSION_DIR`. Workdirs are NOT auto-deleted after
termination so you can inspect logs; clean them up yourself.

### JVM args we pass to the staged binary

```
-Dhttp.port=<port>
-Dpidfile.path=<workdir>/RUNNING_PID
-Ddb.default.url=jdbc:h2:file:<workdir>/db/breadboard;MODE=MYSQL
-Duser.dir=<workdir>
-DapplyEvolutions.default=true
-Dmcp.enabled=true
```

The `-Duser.dir` override is the load-bearing one: the staged binary's
launcher script (`target/universal/stage/bin/breadboard`) hardcodes its own
`addJava "-Duser.dir=$(cd "${app_home}/.."; pwd -P)"` near the end of the
script. Because that's prepended to `java_args` BEFORE our command-line
`-D*` args, our override comes later on the JVM command line, and Java's
last-D-wins semantics make ours stick. Verifiable with `ps -eo command`:

```
java ... -Duser.dir=/abs/stage -Duser.dir=/abs/workdir ...
```

`applyEvolutions.default=true` is required because the staged binary runs
in PROD mode (`application.mode=PROD` in `application.conf`), where Play
2.2 won't auto-apply evolutions without the explicit flag.

`mcp.enabled=true` unlocks the `/debug/*` routes. Gating happens in two
places (one alone isn't enough — see below):

1. **`Global.onRequest`** intercepts any request whose path starts with
   `/debug/` and returns 404 if the flag isn't set. This catches the
   unauthenticated routes (`/debug/login`, `/debug/bootstrap-schema`).
2. **`Secured.onUnauthorized`** returns 404 instead of the usual 401
   when an unauthenticated client hits a `/debug/*` route while the
   flag is unset. This catches the authenticated routes.

Why both: Play 2.2's action composition fires method-level annotations
(including `@Security.Authenticated`) *before* the `onRequest`-supplied
action, so without (2) any authenticated `/debug/*` route would leak a
401 to scanners (telling them the endpoint exists) even though the gate
is on. With both, every disabled `/debug/*` request looks like a 404,
indistinguishable from a non-existent route.

Spawn mode always needs the flag on, so the MCP spawner forces it via
`-Dmcp.enabled=true` in the JVM args. Attach mode users who run
Breadboard themselves must enable it in their own `application.conf`,
pass the same `-D` flag, or export `MCP_ENABLED=true`.

### Why we need `/debug/bootstrap-schema`

Evolution 28.sql creates an empty `breadboard_version` table. `Global.onStart`
treats the presence of that table as "already migrated past v2.2," skipping
the pre-v2.3 upgrade path — which is where `experiments.file_mode` would
normally get added. Result: on a freshly-evolved DB, the `file_mode` column
never exists, and any query that touches it (e.g. `/debug/experiments`,
which Ebean joins through `users.owned_experiments`) blows up with
`Column "T1.FILE_MODE" not found`.

The user's existing dev DB doesn't hit this because it predates evolution
28 — `Global.onStart` saw "no breadboard_version table" and ran the full
upgrade, including adding `file_mode`. So this only bites on greenfield
spawns.

`DebugController.bootstrapSchema()` runs the missing
`ALTER TABLE experiments ADD COLUMN IF NOT EXISTS file_mode BIT DEFAULT 0`.
The spawner POSTs to it once after the server is ready, before doing
anything else against the DB.

### Admin user seeding

After the server is ready and the schema is fixed, the spawner:

1. `GET /languages` → find the English language id (Locale enumeration
   order is JVM/OS-dependent, so we can't assume id=1).
2. `POST /createFirstUser` with `{ email, password, defaultLanguageId }`.
   Defaults are `admin@example.com` / `admin123`; override via
   `spawn_breadboard(admin_email=..., admin_password=...)`. 200 means
   created, 400 ("User table is not empty") means already seeded — both OK.

The MCP client (`BreadboardClient` in `server._bb()`) then picks up the
spawned URL + admin credentials automatically; subsequent tool calls
route to the spawned instance.

### Workdir linkage (groovy/, data/)

Since `-Duser.dir` is overridden to the empty session workdir, several
file reads relative to `user.dir` would fail unless we make those dirs
visible inside the workdir:

- `groovy/` — `ScriptBoard.resetEngine` reads bundled scripts
  (`util.groovy`, `timer.groovy`, `graph.groovy`, ...) from
  `<user.dir>/groovy/`.
- `data/` — experiment-specific data files referenced from groovy code
  with `data/...` paths.

On **Unix** the spawner creates these as symlinks pointing back at the
repo's `groovy/` and `data/` directories — live, no copy cost. On
**Windows** it uses `shutil.copytree` instead, because Windows symlinks
require admin/Developer-Mode privilege. The directories are small
(~130 KB on the default install) so the per-spawn copy is negligible,
but the snapshot is frozen: mid-session edits to the source aren't
reflected until the spawn is terminated and respawned. Add more
directories here only when an experiment is observed to need them.

### Lifecycle

- `spawn_breadboard()` is idempotent. If a previous spawn is still alive,
  the call returns its metadata. If the previous JVM died, the dead state
  is cleared and a fresh one is started.
- `terminate_breadboard()` sends SIGTERM, waits 10s, falls back to
  SIGKILL on Unix. Exit code 143 = clean SIGTERM shutdown. On Windows
  `signal.SIGKILL` doesn't exist and `os.kill(pid, SIGTERM)` already maps
  to `TerminateProcess` (a forceful kill), so the escalation step is a
  no-op.
- The atexit handler in `spawner._atexit_cleanup` calls
  `terminate_breadboard(timeout=5.0)` when the Python interpreter exits,
  and then also runs `cleanup_orphans()` to sweep up dead siblings.

### Orphan detection (SIGKILL / crash recovery)

If the MCP Python process dies without running atexit (SIGKILL, segfault,
host crash), the JVM subprocess becomes an orphan re-parented to PID 1.
To recover:

1. At spawn time, we write `<workdir>/owner.pid` containing the MCP's
   `os.getpid()`. `terminate_breadboard()` deletes this file on clean
   shutdown.
2. `cleanup_orphans()` walks `~/.breadboard-mcp/sessions/*/` and, for
   each session where `owner.pid` exists but the owner process is dead
   AND `RUNNING_PID` is alive, sends SIGTERM (then SIGKILL after 5s on
   Unix; on Windows SIGTERM is already a hard kill so no escalation) to
   the JVM and removes the stale pid files. Sessions whose owner is
   still alive are skipped — so this is safe to run in any MCP process
   without disturbing sibling MCPs.
3. Cleanup runs automatically at the start of every `spawn_breadboard()`
   call and from the atexit handler. Manual triggers:
   `cleanup_orphan_breadboards()` MCP tool, or
   `breadboard-mcp --cleanup-orphans` CLI flag.

PID reuse is a theoretical concern (if owner PID is reused by an
unrelated process before cleanup runs, the JVM looks "owned" again and
won't be reaped). In practice the window is small; if it bites, kill the
JVM by hand from the RUNNING_PID file.

## Build / run recipe (modern macOS, fresh clone)

This is **NOT** the README's `./start` path (that wraps the `play` activator
launcher which isn't installed on most modern Macs). The flow below is the
one that's been verified to work.

### 1. Toolchain

```bash
brew install sbt   # the launcher; project pins its own sbt 0.13.18
```

You need a Java 8 JDK installed (Zulu 8 / Temurin 8 — anything providing
`/usr/libexec/java_home -v 1.8`). sbt 0.13 hard-crashes on Java 11+.

### 2. Set Java 8 + the project-local sbt repositories file

```bash
export JAVA_HOME=$(/usr/libexec/java_home -v 1.8)
export SBT_OPTS="-Dsbt.override.build.repos=true \
                 -Dsbt.repository.config=$(pwd)/repositories"
```

The `repositories` file in the repo root points sbt at
`https://scala.jfrog.io/artifactory/...` — Play 2.2 / sbt 0.13 deps are
no longer at their old typesafe.com URLs (they 302 to jfrog now, but sbt
0.13's bootstrap doesn't follow redirects reliably).

### 3. Build a staged distribution (do NOT use `sbt run`)

```bash
sbt stage
```

`sbt run` in Play 2.2 dev mode triggers a classloader reload on **every
HTTP request**. That wipes the `Breadboard.instances` actor map between
calls, so any flow that spans more than one HTTP request fails. `sbt stage`
produces a self-contained launcher under `target/universal/stage/bin/` that
runs in **prod mode** with no reloading.

### 4. Database

This branch's `conf/evolutions/default/` runs 1..29 exactly like the
release. The `file_mode` column on `experiments` (referenced by
`Experiment.fileMode`) is in the release's pre-built db; if you ever
bootstrap a fresh dev db from scratch and Ebean DDL doesn't generate it for
you, add it manually:

```bash
java -cp ~/.ivy2/cache/com.h2database/h2/jars/h2-1.3.172.jar \
     org.h2.tools.Shell -url "jdbc:h2:db/breadboard;MODE=MYSQL" -user "" -password "" \
     -sql "ALTER TABLE experiments ADD COLUMN IF NOT EXISTS file_mode BIT DEFAULT 0;"
```

### 5. Run the staged binary

```bash
target/universal/stage/bin/breadboard \
  -J-Duser.dir=$(pwd) \
  -Dhttp.port=9000 \
  -Dapplyevolutions.default=true \
  -DapplyEvolutions.default=true
```

Flags, line by line:

| Flag | Why |
|---|---|
| `-J-Duser.dir=$(pwd)` | The launcher's default `user.dir` is `target/universal/stage/`, which means the H2 db is loaded from a different path than the dev environment expects. Pointing it back at the repo root unifies them |
| `-Dhttp.port=9000` | The default port. Pick another if 9000 is busy |
| `-D{apply,Apply}Evolutions.default=true` | Auto-apply pending evolutions without an interactive "Apply this script now!" page. Both spellings exist for historical reasons in Play 2.2 |

> **Historical note:** earlier iterations of this branch required `-J-noverify`
> because a fresh Ebean enhancement of `Experiment.TEST_INSTANCE` (a non-
> persisted field that was missing `@Transient`) produced bytecode that failed
> Zulu 8.92's stricter JVM verifier. Marking the field `@Transient` made the
> verifier happy and `-noverify` is no longer needed.

Server is up when the log shows `Listening for HTTP on /0:0:0:0:0:0:0:0:9000`.

### 6. Bootstrap an admin user (one-time)

On a fresh db, no admin user exists. Create one:

```bash
curl -X POST -H "Content-Type: application/json" \
     -d '{"email":"admin@example.com","password":"admin123","defaultLanguageId":14}' \
     http://localhost:9000/createFirstUser
```

`defaultLanguageId: 14` is English (the languages table is pre-seeded by
`Global.onStart`). `/createFirstUser` reads JSON directly (uses
`request().body().asJson()` — works fine; it's `POST /login` that has the
Form-binding bug).

## Bugs in `main` I had to fix on this branch

### 0. `Experiment.TEST_INSTANCE` missing `@Transient`

**File:** `app/models/Experiment.java`

```java
public ExperimentInstance TEST_INSTANCE = null;   // before
@javax.persistence.Transient                       // after
public ExperimentInstance TEST_INSTANCE = null;
```

`TEST_INSTANCE` is a per-process in-memory test fixture, never meant to be
persisted. Without `@Transient`, Ebean treats it as a managed field and
enhances `Experiment.getTestInstance()` accordingly. On Zulu 8.92's
stricter JVM verifier, the resulting bytecode fails with
`VerifyError: Bad type on operand stack` at class-load time. The release
jar happens to have been compiled with a slightly different toolchain
whose verifier accepts the enhanced bytecode; a fresh build doesn't.

**Fix:** mark it `@Transient`. After this, the JVM starts cleanly without
`-noverify`. This is the actually-correct annotation regardless of the
verifier issue (Ebean should never have been trying to persist it).

### 1. `getStepsFromDirectory` didn't sort

**File:** `app/controllers/ExperimentController.java`

`getStepsFromDirectory` iterated `File.listFiles()` and added steps in
filesystem order. On Linux ext4 that's usually inode/creation order, which
happens to match alphabetical order for files dropped by webpack's
`CopyPlugin` (it processes input files alphabetically). On macOS APFS it's
arbitrary. On any filesystem after `rsync` or `cp`, it's arbitrary.

The result: cross-step class references break. If `OnJoinStep.groovy`
references a class `Foo` defined in `0Foo.groovy`, loading the files out
of order causes Groovy to error with `unable to resolve class Foo`.

**Fix:** sort `stepFiles` by name before iterating. Trivial 4-line patch.

The framework documentation specifies that step files load in alphabetical
order, so this is a correctness fix, not a behavior change.

### 2. Stale lazy proxy on `user.selectedExperiment`

**File:** `app/controllers/DebugController.java` (workaround, not a fix in
Breadboard proper)

In Ebean 3.2, after `user.setSelectedExperiment(experiment); user.update();`,
re-loading the user via `User.findByUID(uid)` returns the relationship as a
lazy-loading proxy. The proxy works for some accesses, but
`user.toJson()` reads `selectedExperiment.name` directly and gets `null` —
which then NPEs `ObjectNode.put` when the actor's Update handler tries to
serialize the user.

**Fix in my handler:** keep the in-memory `Experiment` reference from the
explicit `Experiment.findById(experimentId)` call. Don't re-fetch the user
after setting the relationship. In `launch_game`, eager-reload
`selectedExperiment` via `Experiment.findById(user.selectedExperiment.id)`
before sending the actor message.

A proper fix would be to add `.fetch("selectedExperiment")` to
`User.findByUID`, or to mark the relationship `FetchType.EAGER`. Out of
scope for this branch.

### 3. `POST /login` form-binding is broken on this build

**File:** `app/controllers/Application.java` (left untouched; workaround
lives in `DebugController.login`)

`Application.authenticate` uses
`Form<Login>.bindFromRequest()` to read the email/password fields. On this
build it consistently parses `email = null` and `password = null`
regardless of whether the body is form-urlencoded or JSON. Cause unknown —
probably an interaction between this old Play 2.2 + sbt 0.13.18 + the
current SDK.

**Workaround:** added `POST /debug/login`, which reads JSON directly via
`request().body().asJson()` and sets the same Play session keys. The MCP
client uses this. The legacy `/login` is unchanged (the admin UI logs in
over WebSocket, not via the HTTP form route, so this didn't affect them).

### 4. `user.update()` doesn't persist `uid` changes

**File:** `app/controllers/DebugController.login` (workaround)

After `user.uid = newUid; user.update();`, `User.findByUID(newUid)` returns
`null`. Ebean's dirty-tracking on this build appears to miss the `uid`
field for some reason (possibly because the loaded entity has lazy
associations that interfere).

**Workaround:** the debug login uses a direct SQL update:

```java
Ebean.createSqlUpdate("update users set uid = :uid where email = :email")
     .setParameter("uid", uid)
     .setParameter("email", email)
     .execute();
```

### 5. sbt 0.13 + Akka in dev mode → wrong classloader for actors

**Not really a bug, more a property of `sbt run` + Play 2.2.** Every HTTP
request triggers a classloader reload. The pre-existing `breadboardController`
actor (created at static-init of `Breadboard.java`) keeps using its
original classloader's static maps, while my hot-reloaded HTTP handler
writes to the new classloader's static maps. The two never agree.

**Workaround:** (a) build via `sbt stage` and run the staged binary, which
runs in prod mode with no reloads; and (b) in handlers like
`selectExperiment` and `selectInstance`, bypass `breadboardController` and
send messages directly to the per-user actor via `Breadboard.instances.get(email)`.
The actor created during `/debug/login` is in the same classloader as the
handler that uses it.

### 6. `messagesCaptured` settling

**File:** `app/controllers/DebugController.awaitOrTimeout`

The actor model is async: `LaunchGame` queues N `RunStep` messages, each
producing one `out.write`. My HTTP handler needs to wait until all N are
processed before returning, otherwise downstream `execute_script` calls
hit an empty engine.

The current implementation polls `out.captured().size()` every 100ms and
returns once the count hasn't changed for 1500ms (or 10s total
elapses). Works in practice; could be made tighter by counting expected
messages.

## Walkthrough: drive an experiment end-to-end

This is what the MCP can do that the existing tooling can't do in one
shot. Setup:

```bash
# 1. (one-time) copy the production experiment somewhere safe
rsync -a --exclude=node_modules --exclude=.git --exclude=.idea \
     path/to/your/experiment/ \
     path/to/safe/copy/

# 2. start the staged Breadboard (see "Build / run recipe" above)

# 3. drive the whole loop through the MCP client. The agent calls
#    create_experiment, set_file_mode, sync_experiment_files,
#    select_experiment_for_engine, launch_game in sequence — equivalent
#    to a small Python driver script.
```

Expected output (trimmed) for an experiment id 41:

```
# 4. sync_experiment_files(41, src='path/to/safe/copy/backend')
  wrote 14 files to dev/MyExperiment_41

# 5. get_experiment(41) — steps loaded from disk
  fileMode: True
  steps: ['00Log', '00Utils', '0Globals', '0Helpers',
          '0WaitGroup', '0WaitingRoom', 'Game', 'InitStep',
          'OnJoinStep', 'OnLeaveStep', 'SurveyStep']

# 6. select_experiment_for_engine(41)
  messagesCaptured: 11    ← all 11 steps loaded into engine

# 8. launch_game('mcp-e2e')
  experimentInstanceId: 98, messagesCaptured: 11

# 9. execute_script — verify live bindings
  [V: g.V.count(), E: g.E.count()]
    output: '...==>{V=0, E=0}'    ← live graph, empty (no players)
  onJoinStep?.toString()
    output: '...==>Step@91a12d0'  ← Step object registered
  initStep?.name
    output: '...==>InitStep'      ← named via stepFactory.createStep("InitStep")
```

At this point the engine has every closure registered, every helper class
loaded by the experiment (e.g. `WaitingRoom`), and an empty in-memory
graph. From Claude Code, the agent can now run arbitrary Groovy against
this state.

## File-mode dev directory caveat

`sync_experiment_files` only **adds or overwrites** files in
`dev/<exp>/`. It does not delete files that aren't in the source.

When you turn file-mode on for the first time, Breadboard auto-exports the
db-mode default steps (`OnJoinStep`, `OnLeaveStep`, `InitStep` stubs) into
the dev dir. If your source has different step files (or doesn't ship
`OnLeaveStep`), the stale exported file lingers. Harmless for inspection,
but worth knowing.

A future `sync_experiment_files(..., clean=True)` flag would fix this. Not
yet implemented.

## Outstanding rough edges

- `POST /debug/login` returns a `lookupByUid` field used during debugging
  the uid-persistence issue. It can be removed.
- `awaitOrTimeout` could replace polling with the actor-`ask` pattern for
  tighter latency.
- The `set_selected_experiment` MCP tool predates `select_experiment_for_engine`
  and is now mostly redundant. Kept for the case where you want to set
  the relationship without rebuilding the engine.
- The original `POST /login` form-binding bug isn't fixed — just worked
  around. A real fix would track down why `Form.bindFromRequest()` returns
  null fields in this build.

## Resolver / version pins

- `repositories` — points sbt at https://scala.jfrog.io/artifactory/...
  for boot deps and at https://repo1.maven.org/maven2/ (HTTPS!) for
  everything else. Old typesafe.com URLs all 302 to jfrog now.
- `project/build.properties` — pinned to `sbt.version=0.13.18` (was `0.13.0`,
  which doesn't ship the `compiler-interface-src` jar).
- `project/plugins.sbt` — unchanged; still uses Play `sbt-plugin` 2.2.0.
- Java: Zulu 8 / OpenJDK 8u required. Tested with `1.8.0_482`.
