# Reading DevTools and fixing leaks

## Mode A — Heap snapshot comparison (retention)

Best for long-lived objects: unbounded caches, closures, listeners.

> The user does this reading in DevTools. To the agent, they report constructors, `#Delta`, sizes
> and property names along the Retainers chain, never `(string)` values or object contents.

1. **Warm-up**: hit the endpoint ~200× (JIT and legitimate caches settle).
2. Click *Collect garbage* (trash can) and take **Snapshot 1**.
3. Hit the suspect endpoint **N** times. Use a round number (e.g. 1000) so proportional growth is easy to spot.
4. *Collect garbage* and take **Snapshot 2**.
5. Repeat steps 3–4 for **Snapshot 3**. What grows from 1→2 and from 2→3 is the strongest leak signal.
   In Snapshot 3, the filter *Objects allocated between Snapshot 1 and Snapshot 2* shows what is
   still alive.
6. Select Snapshot 2 and switch *Summary* to **Comparison** against Snapshot 1; then 3 against 2.
   The same constructor growing in both comparisons confirms the leak.

| Column | Meaning |
| --- | --- |
| `# Delta` | Net change in object count. Positive and persistent after GC means a leak. Delta ≈ N or a multiple of N is the proof |
| `Size Delta` / `Alloc. Size` | Net bytes added per constructor |
| `Shallow Size` | Memory of the object itself |
| `Retained Size` | Memory freed together with the object. **Sort by this** |

7. Expand the suspect constructor and read the **Retainers** panel bottom-up until you reach
   something of yours: a module-level `Map`, an EventEmitter's `_events`, a cache, a handler closure.

### Common constructors in a comparison

| Grows | Likely reading |
| --- | --- |
| `(compiled code)` + `Function` (+ `system / Context`) proportional to N | Code generated at runtime per request (`new Function`/`eval`/`vm`): `ajv.compile`, `new Ajv()`, templates, `fast-json-stringify` created in the handler. Unnamed `anonymous` functions reinforce it |
| `Function` + `system / Context` without `(compiled code)` | Retained closure: listener registered per request or per loop iteration, callback kept in an array/Map, `setInterval` never cleared. The `Context` holds the scope variables (`req`, payload) |
| `Timeout` + `Function` retained by `AbortSignal`/`EventTarget` | Abort listener registered on every sleep/retry and never removed (pattern 3) |
| `Array`/`Object`/`Map` retained by a module | Unbounded cache or collection |
| `ArrayBuffer`/`Buffer`/`Uint8Array` | Retained buffers (`external` grows); see who holds them |
| `(string)` | Strings concatenated/kept (in-memory logs, cache keys) |

TypeScript code: DevTools shows the `dist/` line. Turn on `--enable-source-maps` or map the line by
hand through `dist/`.

## Mode B — Allocation instrumentation on timeline (when it allocates and whether it frees)

Use it to see when objects are allocated and whether they are freed. Staging only: high overhead.

1. Select **Allocation instrumentation on timeline**, check **Record stack traces** and click *Start*.
2. Run the test operations against the service.
3. Click *Stop*. **Blue** bar = still alive at the end of the recording. **Grey** bar = freed. Blue
   repeating on every burst of identical requests means retention.
4. Drag a window around a burst to filter the constructors allocated there and see the allocation stack.

## Mode C — Allocation sampling (where the pressure comes from)

Low overhead; good under a load test (`autocannon`, `k6`) in staging.

1. Select **Allocation sampling**, click *Start*, run the load, then *Stop*.
2. Open the **Heavy (Bottom Up)** view and focus on the heaviest items: `localCompile`,
   `compileSchema`, huge string/array builders, templates compiled on every render.
3. Hover an item to see file and line. That tells you whether the cost is in your code or in a
   dependency (e.g. `node_modules/ajv/dist/compile/index.js`).

Sampling shows **allocation pressure**, not retention. Confirm retention with mode A.

## Leak patterns and fixes

### 1. Compiling per request (Ajv, templates, regex, clients)

Ajv caches compiled schemas per instance. So `new Ajv()` per request, or `compile()` of a schema
built on every call, generates new code every time.

```js
// Before: instance and compilation per request
app.post('/items', (req, res) => {
  const ajv = new Ajv();
  const validate = ajv.compile(schema);
  if (!validate(req.body)) return res.status(400).json(validate.errors);
});

// After: module singleton, compiles once
const ajv = new Ajv({ allErrors: true });
const validateItem = ajv.compile(schema);
app.post('/items', (req, res) => {
  if (!validateItem(req.body)) return res.status(400).json(validateItem.errors);
});
```

The same goes for regexes built from strings, template engines, GraphQL schemas, query builders,
HTTP agents and DB clients created per request.

### 2. Unbounded global Map/Array/cache

Bound it with TTL + LRU (`new LRUCache({ max: 5000, ttl: 5 * 60_000 })` from `lru-cache`), or use
`WeakMap`/`WeakSet` when the key is an object. Never key an unbounded cache by request id,
timestamp or user input.

### 3. Event listeners

Symptoms: `EventEmitter`/`EventTarget`/`AbortSignal`/socket retainers holding closures;
`MaxListenersExceededWarning` or "Possible EventTarget memory leak" in the logs. The warning is
printed only once, to stderr, and may never reach the logs: check with
`getEventListeners(target, 'event').length` (from `node:events`). Register the listener once at
boot or remove it on teardown (`.off()` / `removeEventListener`). Raising `setMaxListeners` doesn't
fix the leak.

**Listener per iteration on a long-lived object.** A loop (poll, retry, heartbeat) registers one
listener per turn on an `AbortSignal`/emitter that lives for hours (one per shard, connection or
lease). `{ once: true }` and `.once()` **don't help**: they only remove the listener if the event
fires. On the normal path (the timeout wins), it stays forever. It grows with time and with the
number of resources, not with load.

```ts
// Before: 1 listener per sleep, stuck on the shard's signal
return new Promise((resolve) => {
  const id = setTimeout(resolve, ms);
  signal.addEventListener('abort', () => { clearTimeout(id); resolve(); }, { once: true });
});

// After: the timeout removes the listener
return new Promise((resolve) => {
  const onAbort = () => { clearTimeout(id); resolve(); };
  const id = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve(); }, ms);
  signal.addEventListener('abort', onAbort, { once: true });
});
```

`setTimeout` from `node:timers/promises` with `{ signal }` also cleans up after itself (it rejects
with `AbortError` on abort). Test: N iterations on the same signal → `getEventListeners(signal, 'abort')` empty.

### Other suspects

- `setInterval` never cleared, holding request data.
- Promises that never resolve, holding their context.
- Metrics/logs with high-cardinality labels (one series per user/URL).
- Request context (`AsyncLocalStorage`, custom contexts) stored and never cleared.
- Only RSS grows: `MALLOC_ARENA_MAX=2` (or jemalloc via `LD_PRELOAD`), `ws` with
  `perMessageDeflate`, heavy zlib, native addons.
