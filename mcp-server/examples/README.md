# Player simulator example

`simulate_players.py` connects N fake players to a launched Breadboard
instance over WebSocket — no experiment-specific logic. Use it as a
starting template; extend `Player.handle_step()` for the events your
experiment expects.

## Breadboard model

A **player** connects to a launched **instance** over WebSocket. The
server keeps an authoritative game graph; the player has a vertex in
that graph carrying their current `step` and any private state (quiz
scores, chosen amounts, etc.). Steps advance when the player emits a
`CustomEvent` whose `eventName` matches a listener the experiment's
groovy code registered (e.g. via `player.on("foo", ...)` or
`vertex.once("foo", ...)`).

## What the example already handles

**Universal** (intrinsic to Breadboard — these work for any experiment):

- Reading the player's `step` and `_system.actionQueue` from incoming
  graph frames.
- `aq:<queueId>:<questionId>` answers — the ActionQueue protocol the
  framework uses whenever your groovy calls `a.add(...)`.

**Common conventions** (not framework-level — included as defaults you
may keep or strip depending on your experiment):

- Sends `heartbeat` every 5s. Many multiplayer experiments add a
  listener in OnJoinStep that drops players who go silent. Harmless
  no-op if your experiment doesn't.
- Sends `waiting-room:ready` every 2s. Experiments that pair players
  via a waiting-room step typically expect this to clear a one-shot
  ready-up listener. The default v2.4 template has no waiting room,
  so this is a no-op there.

## Extending it for your experiment

Step transitions that aren't ActionQueue-based are experiment-specific.
Find them by grepping your experiment's `backend/` groovy plus the
breadboard repo's bundled `groovy/`:

```bash
grep -rn "player\.on\|vertex\.once" backend/ /path/to/breadboard/groovy/
```

Each match shows an `eventName` the server is listening for and the
step that registered it. Add a branch in `handle_step()`:

```python
TERMINAL_STEPS = {"finish", "finish-prolific", "results"}

async def handle_step(self, ws) -> None:
    if self.step == "irb-consent":
        await self._emit(ws, "consent-irb", {})
    elif self.step == "tutorial":
        await self._emit(ws, "tutorial-next-page", {"current": 1})
    elif self.step in TERMINAL_STEPS:
        # If this terminal step expects a closing event, send it first.
        if self.step == "results":
            await self._emit(ws, "results-complete", {})
        # Then close the socket; the read loop will exit on the next
        # iteration. The Play 2.2 server may not echo a close frame,
        # so `websockets` will log "no close frame received" — that's
        # benign noise, not failure.
        await ws.close()
        return
```

For events that carry a payload (decisions, quiz answers), read the
handler body to see what fields it pulls from `data` — e.g.
`data.choiceId`, `data.amount`, `data.answers`.

## Debugging

- After a run, pull `event_csv(instanceId)` via the MCP to confirm
  your events actually fired.
- To see raw frames as they arrive, log `msg` inside `_read_loop`.
  Most experiment-specific state appears on the player's own vertex
  under custom keys your groovy code sets on it — extend `_absorb` to
  pull those into Player attributes when you need to react to them.
- Live introspection works mid-run via the MCP's `execute_script`
  tool, e.g. `g.V.count()` or
  `g.V.findAll { it.step == "tutorial" }.collect { it.id }`.
