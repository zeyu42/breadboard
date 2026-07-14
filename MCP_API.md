# Breadboard MCP HTTP contract

This document defines the Breadboard server surface used by the separately
released `breadboard-mcp` Python package. It includes both the dedicated
`/debug/*` routes and older Breadboard routes that the package reuses.

## Supported topology and security

This API is for local debugging only. Do not enable it on a production or
publicly reachable Breadboard server. The MCP process and Breadboard process
must run on the same machine and share a filesystem because file-mode sync
writes directly to the path returned by
`GET /debug/experiments/:experimentId/paths`.

All `/debug/*` routes return 404 unless `mcp.enabled=true`, supplied through
`conf/application.conf`, `MCP_ENABLED=true`, or `-Dmcp.enabled=true`.
Authenticated routes use the Play session cookie created by
`POST /debug/login`. The CSV and experiment-creation routes use the same
session.

The contract is currently unversioned. Additive response fields are allowed.
Removing a route, renaming a field, changing a request body, or changing a
documented side effect requires a coordinated `breadboard-mcp` compatibility
update.

## Bootstrap routes

These routes are used only when the MCP spawns a fresh private Breadboard.

| Method and path | Request | Successful response | Guarantee |
|---|---|---|---|
| `POST /debug/bootstrap-schema` | Empty | `{ "status": "ok" }` | Idempotently ensures `experiments.file_mode` exists. |
| `GET /languages` | Empty | `{ "languages": [...] }` | Returns seeded languages; seeds them if the table is empty. |
| `POST /createFirstUser` | `{ "email", "password", "defaultLanguageId" }` | Empty 200 response | Creates the first admin. Returns 400 if a user already exists. |

## Authentication

### `POST /debug/login`

Request:

```json
{ "email": "admin@example.com", "password": "secret" }
```

Returns `{ "uid", "email", "actorCreated" }` and sets the Play session
cookie used by subsequent calls. It also ensures that the authenticated admin
has a ScriptBoard actor. Invalid credentials return 400.

## Inspection routes

| Method and path | Successful response |
|---|---|
| `GET /debug/experiments` | Array of `{ id, name, uid, fileMode, instanceCount }`. |
| `GET /debug/experiments/:experimentId` | `Experiment.toJson()` for the requested experiment. |
| `GET /debug/experiments/:experimentId/instances` | Array of `ExperimentInstance.toJsonStub()` values. |
| `GET /debug/instances/:instanceId` | One `ExperimentInstance.toJsonStub()` value. |
| `GET /debug/selection` | `{ userEmail, selectedExperiment, experimentInstanceId, experimentInstanceStatus?, experimentInstanceName? }`; nullable selections are represented as JSON null. |

Unknown experiment or instance IDs return 404.

### `GET /debug/instances/:instanceId/events`

Optional query parameters:

- `limit`: integer, default 200.
- `offset`: integer, default 0, applied after filtering.
- `nameFilter`: case-sensitive substring of the event name.

Returns:

```json
{
  "instanceId": 7,
  "totalEvents": 42,
  "returned": 10,
  "offset": 0,
  "limit": 10,
  "events": []
}
```

`totalEvents` is the unfiltered instance total. Events are ordered by their
stored datetime. Invalid integer parameters return 400.

## Experiment and file-mode routes

### `PUT /experiment`

Existing Breadboard route reused by the MCP.

Request:

```json
{ "newExperimentName": "My experiment", "copyExperimentId": 0 }
```

`copyExperimentId=0` creates a new experiment; an existing ID copies that
experiment. Returns `Experiment.toJson()` for the created experiment.

### `POST /debug/selection/experiment`

Request `{ "experimentId": 7 }`. Updates the admin's selected-experiment
relationship without rebuilding the engine. Returns `{ experimentId, name }`.

### `GET /debug/experiments/:experimentId/paths`

Returns `{ experimentId, name, directoryName, fileMode, applicationPath,
devDirectory, stepsDir, contentDir, imagesDir, parametersFile,
clientHtmlFile, clientGraphFile, styleFile }`.

The paths are local to the Breadboard process. The Python MCP may write to
them only when it shares that filesystem.

### `POST /debug/experiments/:experimentId/file-mode`

Accepts `{ "enabled": true }` or an empty body. An explicit value sets file
mode; an absent value toggles it. Returns `{ experimentId, fileMode, changed,
devDirectory? }`. Enabling exports database-backed experiment files to the
experiment's `dev/` directory; disabling imports from that directory.

## Engine lifecycle routes

These calls operate on Breadboard's single shared ScriptBoard engine. Calls
from multiple administrators or MCP processes must not be interleaved.

| Method and path | Request | Successful response and side effect |
|---|---|---|
| `POST /debug/select-experiment` | `{ "experimentId": 7 }` | Rebuilds the engine and loads that experiment's steps. Returns `{ experimentId, name, messagesCaptured, errors }`. |
| `POST /debug/launch-game` | `{ "name": "run-1", "parameters": {...} }` | Creates and starts an instance for the selected experiment, binds the engine, and returns `{ experimentId, name, experimentInstanceId?, messagesCaptured, errors }`. |
| `POST /debug/select-instance` | `{ "instanceId": 9 }` | Binds the selected experiment's engine to an existing instance. Returns `{ instanceId, messagesCaptured, errors }`. |
| `POST /debug/stop-game` | `{ "instanceId": 9 }` | Stops the instance and returns `{ instanceId }`. Breadboard clears the current instance selection when any instance is stopped. |
| `POST /debug/script` | `{ "script": "g.V.count()" }` | Evaluates Groovy in the shared engine and returns `{ output, error }`. This is arbitrary code execution, not a sandbox. |

Actor-backed lifecycle calls wait until captured output has been quiet for
1.5 seconds or their handler timeout is reached. A 200 response can therefore
contain non-empty `errors`; clients must inspect that field. The Python MCP
additionally verifies the live EventTracker binding after launch or instance
selection and refuses to stop an instance that is not the verified binding.

## CSV routes

Existing authenticated Breadboard routes reused by the MCP:

| Method and path | Response |
|---|---|
| `GET /csv/instances/:experimentId` | CSV containing instance identity, status, creation time, and parameter columns. |
| `GET /csv/data/:experimentInstanceId` | CSV containing event ID, event name, datetime, data name, and data value. |

Both return text rather than JSON.

## Breadboard implementation that must remain

The API depends on more than `DebugController` and `conf/routes`. Keep these
server-side pieces with Breadboard:

- The `mcp.enabled` gate in `Global`, `Secured`, and `application.conf`.
- `DebugController` and `NoopThrottledWebSocketOut`.
- ScriptBoard's synchronous script evaluation and actor-output handling.
- The Breadboard actor visibility used by `DebugController`.
- File-mode persistence and the fresh-database schema bootstrap.
- The `@Transient` marker on `Experiment.TEST_INSTANCE` required for staged
  startup on strict JVM verifiers.
