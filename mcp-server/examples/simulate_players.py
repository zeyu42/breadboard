#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=12"]
# ///
"""Minimal Breadboard player simulator — experiment-agnostic starter.

Connects N fake players to an already-launched experiment instance over
WebSocket. The script handles:

  Universal (intrinsic to Breadboard):
    - reading the player's `step` and pending choices from incoming
      graph frames
    - answering ActionQueue prompts (aq:<queueId>:<questionId>) — these
      are exposed whenever your groovy code calls `a.add(...)`

  Common conventions (NOT in the default v2.4 template, but common in
  multiplayer experiments — included as defaults you may keep or strip):
    - sends `heartbeat` every 5s (some experiments register a listener
      in OnJoinStep that drops players that go silent — harmless no-op
      otherwise)
    - sends `waiting-room:ready` every 2s (clears the ready-up
      one-shot listener, if your experiment has a waiting-room step)

It does NOT handle experiment-specific events such as consent dialogs,
tutorial page-flips, decision payloads, or post-game "continue" buttons.
Add those yourself in `Player.handle_step()` (see the TODO marker).

Prerequisites:
  - Breadboard running and reachable at BREADBOARD_URL.
  - An experiment instance already launched (via the MCP server's
    launch_game tool or the admin UI).
  - Note its experiment id and instance id.

IMPORTANT — BREADBOARD_URL must match the Breadboard you launched the
instance in:
  - If the MCP server auto-spawned a private Breadboard, the URL is
    NOT localhost:9000 — it's a random port. Call the MCP tool
    `get_spawned_breadboard` and use the `url` field. The default
    here (localhost:9000) is convenient for attach-mode users with
    their own Breadboard at the standard port and otherwise wrong.
  - If a player connects to the wrong Breadboard, the WS handshake
    succeeds against whatever's at that URL but the LogIn for an
    unknown instance id silently goes nowhere — you'll see no graph
    frames and no errors. Misleading. Double-check the URL first.

Run:
  BREADBOARD_URL=http://localhost:9000 \
  EXPERIMENT_ID=1 INSTANCE_ID=1 NUM_PLAYERS=2 \
  ./simulate_players.py
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import websockets
from websockets.exceptions import ConnectionClosed


BB_URL = os.environ.get("BREADBOARD_URL", "http://localhost:9000")
EXPERIMENT_ID = int(os.environ["EXPERIMENT_ID"])
INSTANCE_ID = int(os.environ["INSTANCE_ID"])
NUM_PLAYERS = int(os.environ.get("NUM_PLAYERS", "2"))
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "300"))

WS_URL = BB_URL.replace("http://", "ws://").replace("https://", "wss://")


class Player:
    def __init__(self, idx: int) -> None:
        self.idx = idx
        self.client_id = f"sim-{idx}-{uuid.uuid4().hex[:8]}"
        self.step: str | None = None
        self._answered_aq: set[str] = set()

    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')} {self.client_id}] {msg}")

    async def run(self) -> None:
        url = (f"{WS_URL}/game/{EXPERIMENT_ID}/{INSTANCE_ID}"
               f"/{self.client_id}/connect")
        self.log(f"connect → {url}")
        # ping_interval=None: Play 2.2 doesn't reply to WS pings; we keep
        # the connection alive with our own app-level heartbeat below.
        # Note: Play 2.2 does not always send a WS close frame even
        # when the server-side player has cleanly exited. The
        # `websockets` library will log a warning ("no close frame
        # received or sent") on shutdown — this is harmless and does
        # NOT mean the run failed. Exit code 0 + a populated event log
        # are the success signals.
        async with websockets.connect(url, ping_interval=None) as ws:
            # The first frame must be a LogIn. Fields beyond clientId are
            # recorded as session metadata server-side.
            await ws.send(json.dumps({
                "action": "LogIn",
                "clientId": self.client_id,
                "referer": "sim",
                "userAgent": "breadboard-sim/0.1",
                "ipAddress": "127.0.0.1",
                "host": "localhost",
                "requestURI": "/sim",
                "connection": "keep-alive",
                "accept": "*/*",
                "acceptLanguage": "en",
                "acceptEncoding": "identity",
            }))

            heartbeat = asyncio.create_task(self._heartbeat(ws))
            ready = asyncio.create_task(self._ready(ws))
            try:
                await self._read_loop(ws)
            finally:
                heartbeat.cancel()
                ready.cancel()

    async def _heartbeat(self, ws) -> None:
        # Convention, not framework: many multiplayer experiments add a
        # `heartbeat` listener in OnJoinStep that drops players who go
        # silent. Sending one every 5s keeps such experiments happy and
        # is a harmless no-op when no listener is registered. Drop this
        # task if your experiment uses a different keepalive scheme.
        try:
            while True:
                await asyncio.sleep(5)
                await self._emit(ws, "heartbeat", {})
        except (asyncio.CancelledError, ConnectionClosed):
            # ConnectionClosed fires if the socket closed between
            # sleeps (e.g. after handle_step did `await ws.close()`).
            # Treat as a normal shutdown signal — same as Cancelled.
            pass

    async def _ready(self, ws) -> None:
        # Convention, not framework: experiments that pair players via a
        # waiting room typically register a one-shot `vertex.once`
        # listener for `waiting-room:ready` during the ready-up window.
        # Spamming every 2s is safe — noop outside the window, single
        # fire inside it. The default v2.4 template has no waiting room,
        # so this is a no-op there.
        try:
            while True:
                await asyncio.sleep(2)
                await self._emit(ws, "waiting-room:ready", {})
        except (asyncio.CancelledError, ConnectionClosed):
            pass

    async def _emit(self, ws, event_name: str, data: dict) -> None:
        # All client-driven actions ride the CustomEvent envelope. The
        # server dispatches by `eventName` to handlers registered via
        # `player.on(...)`, `vertex.once(...)`, or ActionQueue listeners.
        await ws.send(json.dumps({
            "action": "CustomEvent",
            "playerId": self.client_id,
            "eventName": event_name,
            "data": data,
        }))

    async def _read_loop(self, ws) -> None:
        last_step = None
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            await self._absorb(ws, msg)
            if self.step != last_step:
                self.log(f"step → {self.step!r}")
                last_step = self.step
                await self.handle_step(ws)

    async def _absorb(self, ws, msg: dict) -> None:
        # Server frames carry graph state in one of three shapes:
        #   {"graph": {"nodes": [...]}}                  full snapshot
        #   {"action": "addNode"|"updateNode", "node":}  incremental
        #   {"id": "<vertex_id>", ...}                   bare vertex
        # Find the vertex whose id matches our client_id — that's our
        # private player state.
        nodes: list[dict] = []
        if isinstance(msg.get("graph"), dict):
            nodes.extend(msg["graph"].get("nodes") or [])
        if msg.get("action") in ("addNode", "updateNode") and "node" in msg:
            nodes.append(msg["node"])
        if msg.get("id") == self.client_id:
            nodes.append(msg)
        for v in nodes:
            if v.get("id") != self.client_id:
                continue
            if "step" in v:
                self.step = v["step"]
            # ActionQueue: groovy's `a.add(...)` exposes pending choices
            # under _system.actionQueue, keyed like
            # "aq:<queueId>:<questionId>". The server expects
            # {"choiceId": <id>} back via that key.
            aq = (v.get("_system") or {}).get("actionQueue")
            if isinstance(aq, dict):
                for key, choices in aq.items():
                    if key in self._answered_aq:
                        continue
                    cid = 1
                    if isinstance(choices, list) and choices:
                        ids = [c["id"] for c in choices
                               if isinstance(c, dict) and "id" in c]
                        if ids:
                            cid = min(ids)
                    self.log(f"answer aq {key} choiceId={cid}")
                    await self._emit(ws, key, {"choiceId": cid})
                    self._answered_aq.add(key)

    async def handle_step(self, ws) -> None:
        # TODO: customize for your experiment.
        #
        # Most experiments have steps where the server is waiting for a
        # specific custom event from the client to advance — e.g.
        # `accept-realtime` on a consent step, `tutorial-next-page` on
        # a tutorial, `results-complete` after a results screen. Look
        # at the registered listeners in your experiment's groovy
        # files (search for `player.on(` / `vertex.once(`) and
        # dispatch here.
        #
        # CLEAN TERMINATION. When the player reaches a terminal step
        # (post-game survey, final results, "finish" / "finish-prolific"
        # / similar), emit any required closing event then return to
        # let the read loop fall out. Don't sit on a terminal step
        # waiting for more frames — the server won't send any, and the
        # outer asyncio.wait() will hang until RUN_SECONDS elapses.
        # Pattern:
        #
        #   TERMINAL_STEPS = {"finish", "finish-prolific", "results"}
        #
        #   if self.step in TERMINAL_STEPS:
        #       # If the terminal step expects a closing event, send it:
        #       if self.step == "results":
        #           await self._emit(ws, "results-complete", {})
        #       # Close the socket so the read loop exits cleanly.
        #       await ws.close()
        #       return
        pass


async def main() -> None:
    players = [Player(i) for i in range(NUM_PLAYERS)]
    print(f"# {NUM_PLAYERS} players, running up to {RUN_SECONDS}s")
    # Stagger connects by 0.5s. When two players join in the same
    # millisecond, OnJoinStep's add-player path sometimes races and the
    # later player never receives the initial graph snapshot — its
    # client just sits silent. Half a second between connects sidesteps
    # the race without being noticeable in practice.
    tasks = []
    for p in players:
        tasks.append(asyncio.create_task(p.run()))
        await asyncio.sleep(0.5)
    # gather(return_exceptions=True) keeps the asyncio bookkeeping
    # tidy: every task's exception (or result) is retrieved, so the
    # process won't print "Task exception was never retrieved" on GC.
    # Wrap in wait_for to enforce the overall timeout.
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=RUN_SECONDS,
        )
    except asyncio.TimeoutError:
        for t in tasks:
            t.cancel()
        # Drain after cancel so exceptions don't go unretrieved.
        results = await asyncio.gather(*tasks, return_exceptions=True)

    print(f"\n# final: {[(p.client_id, p.step) for p in players]}")
    # Surface anything unexpected (ConnectionClosed is normal at
    # shutdown — anything else is worth knowing about).
    for p, r in zip(players, results):
        if isinstance(r, Exception) and not isinstance(r, ConnectionClosed):
            print(f"# {p.client_id} exited with: {type(r).__name__}: {r}")


if __name__ == "__main__":
    asyncio.run(main())
