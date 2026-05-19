"""Breadboard MCP server.

Exposes a small set of tools an LLM agent can use to inspect and debug
Breadboard experiments running on a Breadboard server (default
http://localhost:9000). The server authenticates as an admin user using
credentials from environment variables and holds the Play session cookie
for the lifetime of the process.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import BreadboardClient, from_env

log = logging.getLogger("breadboard_mcp")

mcp = FastMCP("breadboard")

# A single, lazily-initialized client lives for the life of the server
# process so we don't re-login on every tool call.
_client: BreadboardClient | None = None


def _bb() -> BreadboardClient:
    global _client
    if _client is None:
        _client = from_env()
        _client.login()
    return _client


def _pretty(value: Any) -> str:
    """Tools must return text. Render structured values as compact JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, default=str, sort_keys=False)


# -------------------------------------------------------------------- tools

@mcp.tool()
def list_experiments() -> str:
    """List all experiments owned by the configured admin user.

    Returns an array of { id, name, uid, fileMode, instanceCount }.
    """
    return _pretty(_bb().list_experiments())


@mcp.tool()
def get_experiment(experiment_id: int) -> str:
    """Get full metadata for one experiment: steps (with Groovy source),
    content, parameters, languages, instance stubs, and styling.

    Use this to read step source code or content before deciding what to
    inspect at runtime.
    """
    return _pretty(_bb().get_experiment(experiment_id))


@mcp.tool()
def list_instances(experiment_id: int) -> str:
    """List all instances (runs) of an experiment.

    Each stub includes id, name, status (RUNNING / TESTING / STOPPED /
    FINISHED / ARCHIVED), creationTime, AMT hit info, and the per-instance
    Data values (parameters at run time).
    """
    return _pretty(_bb().list_instances(experiment_id))


@mcp.tool()
def get_instance(instance_id: int) -> str:
    """Get one experiment instance by id (stub form: no event list)."""
    return _pretty(_bb().get_instance(instance_id))


@mcp.tool()
def get_instance_events(
    instance_id: int,
    limit: int = 200,
    offset: int = 0,
    name_filter: str | None = None,
) -> str:
    """Get the event log for an instance, ordered by datetime.

    Use limit/offset to page through long logs (default limit 200). Pass
    name_filter to substring-match on event name (e.g. "Choice", "Step",
    "Joined").
    """
    return _pretty(_bb().get_instance_events(instance_id, limit, offset, name_filter))


@mcp.tool()
def create_experiment(name: str, copy_experiment_id: int | None = None) -> str:
    """Create a new experiment owned by the configured admin user. Returns
    the created experiment as JSON (use the `id` field for follow-up calls).

    If `copy_experiment_id` is given, the new experiment is initialized as a
    copy of an existing one; otherwise it starts blank.
    """
    return _pretty(_bb().create_experiment(name, copy_experiment_id))


@mcp.tool()
def get_experiment_paths(experiment_id: int) -> str:
    """Return the on-disk paths Breadboard uses for this experiment's
    file-mode directory (devDirectory, stepsDir, contentDir, parametersFile,
    clientHtmlFile, clientGraphFile, styleFile). Useful for verifying where
    `sync_experiment_files` will write to.
    """
    return _pretty(_bb().get_experiment_paths(experiment_id))


@mcp.tool()
def set_file_mode(experiment_id: int, enabled: bool | None = None) -> str:
    """Turn file mode on or off for an experiment. When turning on,
    Breadboard exports the current experiment into `dev/<directoryName>/`.
    When turning off, it re-imports from there. Pass `enabled` to set a
    specific value, or omit to toggle.

    Call this before `sync_experiment_files` so the dev/ directory exists
    and the experiment reads its content from there.
    """
    return _pretty(_bb().set_file_mode(experiment_id, enabled))


@mcp.tool()
def sync_experiment_files(
    experiment_id: int,
    source_dir: str,
    public_root: str = "/generated/",
    dev_mode: bool = True,
) -> str:
    """Copy a directory tree of experiment files into the Breadboard server's
    dev/<directoryName>/ for this experiment — the MCP-side equivalent of
    `npm run serve` in the breadboard-v2.4-default template.

    `source_dir` should contain (any subset of) `Steps/`, `Content/`,
    `Images/`, `parameters.csv`, `client-html.html`, `client-graph.js`,
    `style.css`. The `Steps/` folder is normalized to lowercase `steps/`
    to match what Breadboard's file-mode reader expects.
    `client-graph.js` has `__PUBLIC_ROOT__`, `__DEV__`, `__PROD__` placeholders
    substituted the same way the webpack CopyPlugin does.

    Returns a manifest of files written.
    """
    return _pretty(
        _bb().sync_experiment_files(experiment_id, source_dir, public_root, dev_mode)
    )


@mcp.tool()
def set_selected_experiment(experiment_id: int) -> str:
    """Set the configured admin user's currently-selected experiment. This
    is the value the script engine reads to figure out which experiment's
    bindings to use. Note: setting this does NOT itself rebuild the script
    engine — for full live debug, the user still needs to load an instance
    via the UI for now.
    """
    return _pretty(_bb().set_selected_experiment(experiment_id))


@mcp.tool()
def get_current_selection() -> str:
    """Show which experiment + instance are currently loaded into the
    Breadboard admin's script engine.

    The /debug/script endpoint evaluates Groovy against whatever instance
    the admin user last selected in the UI. If selectedExperiment or
    experimentInstanceId is null, runtime bindings (g, a, c, ...) will not
    be available and script execution may fail or return stale state.
    """
    return _pretty(_bb().get_current_selection())


@mcp.tool()
def select_experiment_for_engine(experiment_id: int) -> str:
    """Bind the Breadboard script engine to an experiment. This rebuilds the
    engine, sets up its bindings (g, a, c, events, ...) and loads every
    Step's Groovy source into the engine. Required before launch_game or
    select_instance will produce a usable runtime.
    """
    return _pretty(_bb().select_experiment_for_engine(experiment_id))


@mcp.tool()
def launch_game(name: str, parameters: dict | None = None) -> str:
    """Create + start a new instance of the currently-selected experiment
    (call select_experiment_for_engine first). This runs every Step source
    into the engine so its run/done closures are registered and bound to a
    fresh ExperimentInstance.

    `name` is a label for the instance shown in the UI / data exports.
    `parameters` is an optional dict of run-time parameter overrides.
    Returns once the actor settles or 15s elapses.
    """
    return _pretty(_bb().launch_game(name, parameters))


@mcp.tool()
def select_instance_for_engine(instance_id: int) -> str:
    """Bind the Breadboard script engine to an existing ExperimentInstance
    by id. Useful after a server restart or when switching between live
    instances of the same experiment.
    """
    return _pretty(_bb().select_instance_for_engine(instance_id))


@mcp.tool()
def stop_game(instance_id: int) -> str:
    """Stop a running game instance."""
    return _pretty(_bb().stop_game(instance_id))


@mcp.tool()
def execute_script(script: str) -> str:
    """Evaluate a Groovy script against the running Breadboard script engine
    and return { output, error }.

    The engine has the same bindings as the in-app Scriptboard once an
    experiment instance has been selected:

      g          - the in-memory TinkerGraph (g.V, g.E, g.getVertex(id), ...)
      a          - PlayerActions (a.add, a.addEvent, ...)
      c          - Content fetcher (c.get(...))
      events     - the event bus (events.on / events.emit)
      r          - java.util.Random
      results    - a Map you can write into; its contents are serialized
                   back as JSON in addition to the script's return value

    Examples:
        execute_script("g.V.count()")
        execute_script("g.V.collect { [id: it.id, neighbors: it.neighbors.size()] }")

    First call get_current_selection() to confirm the engine is bound to
    the experiment instance you want to debug.
    """
    return _pretty(_bb().execute_script(script))


@mcp.tool()
def instance_data_csv(experiment_id: int) -> str:
    """Return the CSV summary of all instances of an experiment (one row
    per instance, columns are the parameters captured for that run). This
    mirrors the "Download CSV" button on the experiment page.
    """
    return _bb().instance_data_csv(experiment_id)


@mcp.tool()
def event_csv(instance_id: int) -> str:
    """Return the full per-event CSV log for a single instance. Useful for
    bulk export when paging through /debug/events would be tedious.
    """
    return _bb().event_csv(instance_id)


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
    args = parser.parse_args()

    if args.print_claude_config:
        _print_claude_config()
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()
