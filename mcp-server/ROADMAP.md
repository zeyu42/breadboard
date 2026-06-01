# Roadmap

This document tracks the plan for evolving the Breadboard MCP server
beyond its current state (a subdirectory of the Breadboard repo) into
a separately-released package on PyPI. Order matters: each phase
depends on the previous one being solid.

## Phase 1 — Land current changes into Breadboard upstream

Get `feature/mcp-server` reviewed and merged into whichever Breadboard
repo will be the long-term host. Once merged, the surface the MCP
depends on becomes Breadboard's published API and can be targeted
from an independent MCP release.

The Breadboard-side changes that need to stay (and be reviewed as
intentional API):

| Change | Reason it stays |
|---|---|
| `/debug/*` routes + `app/controllers/DebugController.java` | The MCP's entire surface |
| `mcp.enabled` config gate (`Global.java`, `Secured.java`, `application.conf`) | Production safety |
| `@Transient` on `Experiment.TEST_INSTANCE` | Boot fix on stricter JVM verifiers |
| `/debug/bootstrap-schema` workaround | Schema upgrade path on fresh DBs |
| `experiments.file_mode` column + reader | The sync-files flow |

Before merge, write an `API.md` (or extend `DEV_NOTES.md`) that
explicitly documents the `/debug/*` contract — what each endpoint
accepts, returns, and guarantees. That doc serves two purposes:
- Reviewer aid for the upstream merge.
- Compatibility documentation for downstream MCP users once the
  MCP repo is separate.

## Phase 2 — Add one forward-compat hook (small PR, optional)

While you're already touching Breadboard, add a `/debug/server-info`
endpoint that returns `{breadboardVersion, mcpApiVersion}`. Cheap to
add now; lets the MCP do compatibility checks later without forcing
another Breadboard release just for a handshake.

This is **not blocking** for the separation — the MCP can ship without
it and add a soft check later. But it's easier to land while the merge
context is fresh.

## Phase 3 — Extract the MCP to its own repo

Once Breadboard upstream has the `/debug/*` API merged, the MCP can
live independently. Concrete tasks at extraction time:

- **Drop the repo-path-walking spawn logic.** `spawner.py` currently
  does `Path(__file__).resolve().parents[3]` to find the Breadboard
  repo. That breaks the moment someone `pip install`s from PyPI.
  Replace with: require `BREADBOARD_BIN` env var (or
  `--breadboard-bin` arg) for spawn mode. If unset, fail with a clear
  message that points the user at attach mode.
- **Default to attach mode.** Most PyPI users won't have a Breadboard
  checkout — they'll have a running server they want the MCP to talk
  to. Attach should be the path of least resistance. Spawn becomes a
  power-user flag for developers working on Breadboard itself.
- **Move `docs/` and `examples/` INTO the package** at
  `src/breadboard_mcp/_resources/{docs,examples}/`. The current
  `force-include` in `pyproject.toml` works but is awkward. Once
  separated, put the files at their final location directly; simpler
  hatch config, simpler resource resolution.
- **Drop the bundled `groovy/` and `data/` symlink/copy logic in the
  spawner.** Those live in the Breadboard repo, not the MCP package.
  When `BREADBOARD_BIN` is set, derive their location from it
  (e.g. `<bin>/../../../groovy/`); otherwise refuse to spawn.

## Phase 4 — CI for PyPI releases

GitHub Actions, triggered on tag push:
- Build wheel via `uv build --wheel` (or `python -m build`).
- Publish to PyPI using **trusted publishing (OIDC)** rather than
  long-lived tokens.

Tag `v0.1.0` and ship.

## Things to defer

These are tempting but not worth doing now:

- **Symmetric versioning between Breadboard and MCP.** Don't lock the
  version numbers. Let each release independently. The MCP just
  declares a minimum Breadboard version it speaks to (via
  `mcpApiVersion` once the handshake exists).
- **Renaming.** `breadboard-mcp` is a fine PyPI name if it's
  available. Specificity helps discoverability — don't rename for
  branding.
- **A starter kit / installer.** A `breadboard-mcp init` that builds
  Breadboard for the user is tempting but a maintenance trap. Users
  who want spawn mode can follow the build docs; users who want
  attach mode don't need this at all.
- **Plugin/extension API for custom experiment-specific tools.** If
  someone needs a `/debug/two-door/round-summary`-style tool for
  their experiment, they can write a small experiment-local script
  rather than have the MCP server grow knobs. Keep the MCP generic.

## What to do this week vs. later

**This week:**
1. Push for upstream merge of `feature/mcp-server`.
2. Write `API.md` (or a section in `DEV_NOTES.md`) documenting the
   `/debug/*` contract.

**Next:**
3. Add `/debug/server-info` to Breadboard while the merge is fresh.

**Whenever (after upstream merge lands):**
4. Create the new MCP repo, copy `mcp-server/` over, strip
   repo-relative paths, set up release CI, publish v0.1.0.
