# Groovy / Gremlin Cheatsheet for `execute_script`

All snippets are runnable in `execute_script` against a Breadboard
engine bound to an experiment + instance. Output uses Gremlin pipe
format: `==>value`.

## Bindings refresher

```
g        TinkerGraph (the live graph for the bound instance)
a        PlayerActions
c        Content fetcher
events   event bus
r        java.util.Random
results  Map<String, Object> — written contents are serialized back
```

## Inspecting the graph

```groovy
// Count vertices and edges.
[v: g.V.count(), e: g.E.count()]

// List all vertices with their properties.
g.V.collect { [id: it.id, props: it.getPropertyKeys().collectEntries { k -> [k, it.getProperty(k)] }] }

// Fetch one vertex by id.
g.getVertex(0)

// All properties of all vertices.
g.V.collect { v -> v.getPropertyKeys().collectEntries { [it, v.getProperty(it)] } }

// Vertices that have a given property set.
g.V.has('name', 'Alice')
```

## Adding a vertex to the graph (NOT a full player simulation)

You can add a Vertex directly to `g` for testing graph queries:

```groovy
// Add one vertex.
def v = g.addVertex(null)
v.setProperty('name', 'Alice')
v.id   // ==>0 (or whatever id Tinker assigned)

// Add several.
['Alice', 'Bob', 'Carol'].each { name ->
    def x = g.addVertex(null)
    x.setProperty('name', name)
}
g.V.count()   // ==>3
```

`g.addVertex(...)` triggers Breadboard's graph listener, which
auto-sets `_system: [:]` and `private: [:]` properties on every new
vertex. So a freshly-added vertex has at least those two properties
plus whatever you set yourself.

### IMPORTANT: this does NOT simulate a player joining

A real participant joining via WebSocket triggers these things:
1. The graph gets a vertex (same as above), AND
2. Breadboard's `Admin.java` sends a `RunOnJoinStep` actor message,
   causing `OnJoinStep.run(playerId)` to fire, AND
3. Subsequent step-lifecycle events get logged to the instance
   event log.

`g.addVertex(...)` only does (1). `a.add(vertex, ...)` (covered
below) is a different thing entirely — it queues choices for an
existing player. **Neither fires OnJoinStep, and neither writes an
entry to the event log.**

If you need real step-lifecycle behavior for testing, either run the
WebSocket simulator at `examples://simulate-players` (a real-ish
client that handshakes, joins, and rides the lifecycle) as a separate
process, or open the participant URL in a browser yourself and click
through manually. "Simulating in a browser" without driving the
browser programmatically just means you're a participant.

If you want to invoke `OnJoinStep.run` manually from Groovy for a
synthetic vertex, you can — but you're stepping around the actor
plumbing, so be aware that side effects normally driven by that
plumbing (acknowledgments back to the client, idle timers) won't
happen:

```groovy
// Manually fire OnJoinStep against a synthetic vertex.
def v = g.addVertex(null)
v.setProperty('name', 'Alice')
onJoinStep.run(v.id)
```

## Queuing choices for a player with `a.add`

`a.add(Vertex player, [choices...])` queues a set of choice options
that the player must pick from. It sets the player's `choices`
property to the choice array (which is what the participant UI reads)
and registers each choice's `result` closure to fire when chosen. It
does NOT add the player to the experiment, and it does NOT fire
OnJoinStep.

```groovy
def alice = g.V.has('name', 'Alice').next()

a.add(alice,
      [name: 'Stay', result: { /* closure runs when picked */ }],
      [name: 'Leave', result: { /* ... */ }])

alice.getProperty('choices')   // the array of choice maps
```

## Connecting players in a graph

```groovy
def alice = g.V.has('name', 'Alice').next()
def bob   = g.V.has('name', 'Bob').next()
g.addEdge(null, alice, bob, 'friend')

// Read edges from alice.
alice.outE.collect { [label: it.label, to: it.inV.next().getProperty('name')] }
```

## Setting / reading player attributes

```groovy
def alice = g.V.has('name', 'Alice').next()
alice.setProperty('score', 0)
alice.setProperty('color', 'red')
alice.getProperty('color')   // ==>red
```

## Firing custom events

```groovy
// Writes to the instance event log (visible via get_instance_events).
a.addEvent('CustomDebug', [reason: 'inspection', who: 'Alice'])
```

## Reading content

```groovy
// c.get returns a Content object that the participant client would
// render. Useful to verify content blocks resolve correctly.
c.get('Welcome')
```

## Advancing the experiment

There is no single universal "advance" method. How an experiment
progresses is experiment-specific — typically driven by client-side
custom events (e.g. `accept-realtime`, `tutorial-next-page`,
`results-complete`) that fire listeners registered in the Steps,
which then set `player.step = <next>` and queue new actions.

To learn how a particular experiment advances, grep its Groovy for
listener-registration calls:

```bash
grep -RnE 'player\.on\(|vertex\.once\(' backend/Steps/
```

Each match shows which event name advances which step. To drive the
experiment from a WebSocket simulator, emit the matching custom
events. See `examples://simulate-players` for the framing.

## Using `results` to return structured data

```groovy
results.put('v_count', g.V.count())
results.put('e_count', g.E.count())
results.put('players', g.V.collect { it.getProperty('name') })
// The MCP serializes `results` to JSON in addition to the script's
// return value.
```

## Inspecting the API via reflection

When a binding's signature isn't documented, ask Groovy:

```groovy
a.class.methods.findAll { it.name == 'add' }.collect { it.toString() }
// ==>[public java.lang.Object PlayerActions.add(com.tinkerpop.blueprints.Vertex,...), ...]

a.class.methods.collect { it.name }.unique().sort()
```

## Pipe-step name collisions

Groovy + Gremlin lets you write `g.V.collect { it.foo }` to extract
the `foo` property from each vertex — usually. But Gremlin pipes have
their own methods (`step`, `out`, `in`, `path`, `loop`, etc.), and if
a property name collides with one of those, `it.<name>` resolves to
the pipe method, not your property. The classic landmine:

```groovy
// WRONG — `step` is a Gremlin pipe method, not your vertex's property.
g.V.collect { it.step }   // raises a confusing pipe-step error

// RIGHT — go through the explicit property API.
g.V.collect { it.getProperty('step') }
```

Use `getProperty(name)` whenever you suspect a name collision. It's
also safe for properties that don't exist (returns null) where the
shorthand might throw.

## Common errors and what they mean

- `groovy.lang.MissingMethodException: No signature of method: X.method() for argument types: (Y)`
  — You called `method` with the wrong argument type. Read the
  "Possible solutions:" line in the error — Groovy lists candidate
  signatures.

- `MissingPropertyException: No such property: foo`
  — Either a typo, or you're trying to access a non-existent binding.
  Confirm the engine is bound (`get_current_selection`).

- `==>null` (no error)
  — The script ran and the last expression evaluated to null. Often
  because a Step's run closure returned nothing implicitly. Wrap in
  brackets or explicitly return a value.
