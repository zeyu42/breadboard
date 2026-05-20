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

These four are universal across Breadboard experiments and already
wired in:

- `heartbeat` every 5s — OnJoinStep's checker drops players that go silent.
- `waiting-room:ready` every 2s — clears the one-shot ready-up listener.
- `aq:<queueId>:<questionId>` answers — the ActionQueue protocol used by
  every experiment that calls `a.add(...)`.
- Reading the player's `step` and `_system.actionQueue` from incoming
  graph frames.

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
async def handle_step(self, ws) -> None:
    if self.step == "irb-consent":
        await self._emit(ws, "consent-irb", {})
    elif self.step == "tutorial":
        await self._emit(ws, "tutorial-next-page", {"current": 1})
    elif self.step == "results":
        await self._emit(ws, "results-complete", {})
    elif self.step == "finish":
        raise asyncio.CancelledError    # exits the run cleanly
```

For events that carry a payload (decisions, quiz answers), read the
handler body to see what fields it pulls from `data` — e.g.
`data.choiceId`, `data.amount`, `data.answers`.

## Debugging

- After a run, pull `event_csv(instanceId)` via the MCP to confirm
  your events actually fired.
- To see raw frames as they arrive, log `msg` inside `_read_loop`.
  Most experiment-specific state appears on the player's own vertex
  (`v["tutorialStep"]`, `v["bdmReceived"]`, etc.) — extend `_absorb`
  to pull it into Player attributes when you need to react to it.
- Live introspection works mid-run via the MCP's `execute_script`
  tool, e.g. `g.V.count()` or
  `g.V.findAll { it.step == "tutorial" }.collect { it.id }`.
