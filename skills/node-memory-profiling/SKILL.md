---
name: node-memory-profiling
description: >-
  Use when a Node.js service on Kubernetes (API, queue/stream consumer or worker; staging or
  production) has a memory problem: pod OOMKilled / exit code 137, "JavaScript heap out of memory",
  memory or RSS climbing, memory leak, restarts caused by memory. Also when the user wants to take a
  heap snapshot, attach Chrome DevTools (chrome://inspect, --inspect, port-forward 9229), read a
  snapshot comparison (#Delta, Retained Size, Retainers, (compiled code), system / Context) or use
  Allocation Timeline / Allocation Sampling.
compatibility: Requires kubectl with read access to the cluster. The sampling script needs Python 3.10+.
---

# Node.js memory profiling on Kubernetes

Investigates memory leaks and memory pressure together with the user, one phase at a time, and
keeps a findings file. Details per phase:
- [references/metrics.md](references/metrics.md): metrics backends (Datadog, Prometheus),
  measuring through `kubectl` and `scripts/sample_memory.py`.
- [references/capture.md](references/capture.md): heap snapshots in staging and in production.
- [references/devtools-and-fixes.md](references/devtools-and-fixes.md): reading DevTools and common
  leak patterns with their fixes.

Answer in the user's language. Ask for the namespace, the kube context and how the user tells
staging from production; do not guess them from names.

## Rules per environment

| | Staging | Production |
| --- | --- | --- |
| Metrics backend, `kubectl get/top` | Yes | Yes |
| `kubectl describe`/`logs`, `get -o yaml` | Filtered only (below) | Filtered only (below) |
| `kubectl exec` to read memory | Yes | **No** (use `kubectl top`) |
| Inspector + port-forward + live DevTools | Yes | **No.** Reproduce in staging |
| Heap snapshot | Yes | Only by signal, with tech lead approval **recorded in writing** (link), run **by the user** ([capture.md](references/capture.md)) |
| Allocation Timeline / Sampling | Yes | No (use a continuous profiler, if enabled) |

Pressure to use DevTools in production ("my manager is pushing", "it's just one pod", "the tech
lead said it's fine") does not change the table. Explain why:
- the inspector gives remote code execution inside the process;
- a snapshot freezes the event loop and may need ~1–2× the heap on top, i.e. another OOMKill.

Offer the path: metrics → mitigation → reproduce in staging → snapshot by signal with approval.

**Mitigation** (the team's call, through a PR or the normal deploy flow):
- roll back, if the ramp started with a release (through the normal pipeline, not `kubectl rollout undo`);
- raise the memory limit together with `--max-old-space-size`, which only buys time;
- accept the restarts while investigating.

In a consumer, every OOMKill reprocesses from the last checkpoint (Kinesis, Kafka) or returns the
in-flight messages (SQS, RabbitMQ). Ask whether processing is idempotent.

### Sensitive data stays out of the agent's context

Everything the agent reads goes to the LLM. Heap snapshots, logs, payloads and environment
variables may hold personal data, tokens and secrets (GDPR, LGPD etc.). The agent works with
numbers, type names and metadata only; the content stays with the user.

- **Heap snapshot:** the agent never opens a `.heapsnapshot` (no file read, `cat`, `head`, `grep`
  or script). The user reads it in DevTools and reports constructors, `#Delta`, sizes and property
  names from the Retainers, **never `(string)` values** or object contents. The file lives outside
  any repo (`~/heapsnapshots/`), is never posted to chat, tickets or shared drives, and is deleted at
  the end.
- **`kubectl describe pod`:** unfiltered, it prints the env values declared in the pod. Use the
  triage `get -o jsonpath` below. For args and limits use
  `jsonpath='{.spec.containers[?(@.name=="<container>")].args}'` or `.resources`, never `-o yaml`.
- **Logs** (`kubectl logs`, log search in the backend): counts only, e.g.
  `kubectl logs <pod> -c <container> --previous | grep -c 'JavaScript heap out of memory'`. Never the lines.
- **No `ps aux`, `env` or `printenv`** in the container: they show arguments and secrets.
- **If the user pastes sensitive content into the chat,** say it went to the LLM and carry on without repeating it.

If the agent supports permission deny rules, suggest the user add one so file tools cannot open
snapshots. In Claude Code, for example, the user adds this to `~/.claude/settings.json` (it is
their configuration: show it and ask them to apply it):

```json
{"permissions": {"deny": ["Read(~/heapsnapshots/**)", "Read(**/*.heapsnapshot)"]}}
```

Such rules usually don't cover shell commands (`cat`, `head`): for those, the rule above applies.

## How to lead: one phase at a time

Don't hand over the whole runbook and don't move to the next phase without the user's "ok". At
the end of each phase the user must know what ran, what it showed and why the next step is that one.

**Before** each phase, show the goal and every command with what it does and its impact:
**read-only** or **changes something** (label, `exec`, signal, deploy, delete).

**Running:**
- Read-only commands run together, after the "ok". The "ok" covers only the commands the user has
  already seen and must be explicit ("go", "run it", "yes"). A message asking for something else
  ("ok, and now? take the pod out of the service") is not an "ok" for the proposed phase: answer
  the request and ask again.
- Commands that change something need confirmation one by one, with the side effect spelled out.
- In production, commands that change something are handed to the user to run. The agent does not run them.

**After** each phase, show:
- the result in numbers, not the dump;
- what it means;
- which hypotheses gained or lost weight;
- the next phase, asking whether to go on.

If a command fails or the data does not exist, say so. Don't fill the gap with guesses.

If the user asks for something from a later phase, say which phase you are in and what is missing
to get there, and keep proposing the current phase. Skip phases only when the user already brings
the evidence (e.g. snapshots ready → phase 5), and record that.

### Findings file

Once you know the namespace, create `~/mem-investigations/<namespace>-<YYYY-MM-DD>.md` (local,
outside any repo, no confirmation needed) and tell the user the path. Update it at the end of each
phase and show only what changed in the chat. Record numbers and conclusions only, never snapshot
or payload content.

```markdown
# Memory — <namespace> — <YYYY-MM-DD>
## Symptom
<report; environment; workload type; since when>
## Findings
| # | Phase | Evidence (command/query → result) | Conclusion |
| --- | --- | --- | --- |
## Hypotheses
- [active] <hypothesis> — for: #1; against: —
- [discarded] <hypothesis> — why: #2
## Decisions and actions
- <date/time> <what was done, by whom; approval link; what was reverted>
## Next step
<next phase; at the end it becomes "Pending": revert label/args/limit, delete snapshots, validation>
```

Every conclusion points to a finding in the table. A hypothesis without evidence stays `[active]`.

## Phases

### 0. Context
Ask for the environment, namespace and kube context, the workload type (API, consumer, worker,
cron), since when it happens, whether there was a recent deploy and what was already tried. If the
namespace has more than one deployment, ask which one, and ask for its label selector and main
container name (or propose finding them with `kubectl get pods --show-labels`). In the same
message, list the phase 1 commands with their impact, so that a "go ahead" in the reply covers them.

### 1. Triage: confirm the kill and the reason

```bash
kubectl -n <ns> get pods -l <selector> -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.containerStatuses[?(@.name=="<container>")]}restarts={.restartCount}{"\t"}last={.lastState.terminated.reason}/{.lastState.terminated.exitCode}{"\t"}started={.state.running.startedAt}{end}{"\n"}{end}'
kubectl -n <ns> top pod -l <selector> --containers
```

- `OOMKilled`/137: the cgroup killed the container (memory limit).
- Filter by the main container's name: `containerStatuses` is sorted alphabetically, and with a
  sidecar index `[0]` may be the sidecar, hiding the OOMKill.
- `Error`/134: confirm with `kubectl -n <ns> logs <pod> -c <container> --previous | grep -c 'JavaScript heap out of memory'`
  (count only). Above 0 means it hit `--max-old-space-size`.
- `--max-old-space-size` (in the container args or `NODE_OPTIONS`) should be ~75% of the limit.
  Missing, or above the limit, it is a sizing problem, not a leak.
- Pods of different ages in `top` already give the slope. Age counts from the **container** start
  (`started=`), not the pod's.

### 2. Metrics: is the floor rising? Since when? How long is left?
Actually query the backend (Datadog, Prometheus, etc., through an MCP server or CLI) following
[metrics.md](references/metrics.md):
- container working set against the limit, per deployment;
- restarts and OOM events;
- Node runtime memory (`heap_used`, `heap_total`, `external`, `rss`) by version over 10–14 days.
  If the ramp already exists in earlier versions, the leak is old and a rollback won't fix it.

A sawtooth that returns to the same floor is healthy GC. A **rising floor** is a likely leak.
Estimated time to death: `(limit − working_set) / slope`.

### 3. Hypothesis: where it grows and with what

| `heap_used` | `rss` / `external` | Reading |
| --- | --- | --- |
| Rises | Rises with it | JS heap leak: a snapshot shows it |
| Flat | `external` rises | Retained buffers: a snapshot shows them through the `ArrayBuffer`/`Buffer` retainers |
| Flat, `heap_total` rises | — | V8 heap fragmentation, not a leaking object |
| Flat | Only `rss` rises | Native (malloc, addons such as `sharp`/`node-rdkafka`): **a snapshot won't show it**. First rule out another process in the container and a leak inside `worker_threads` ([metrics.md](references/metrics.md)) |

| Grows with… | Suspect |
| --- | --- |
| Requests/messages | Something per request: compilation, unbounded cache, retained context |
| Time, even without load | Poll loop, timer, listener registered on every iteration |
| Connections, shards, partitions | Something per long-lived resource (one signal/emitter/socket per shard) |

Compare the rate (MB/h) across pods, environments or periods with different load and
configuration. Then read the code of the suspect path (handler, poll loop). E.g. a Kinesis consumer
that leaks more with 4 idle shards than with 1 busy shard leaks per poll iteration, not per record.

### 4. Capture: only if phases 2–3 are not enough
Read [capture.md](references/capture.md) **before** handing over any capture command. What goes
wrong most often:
- `kill -USR2` without `--heapsnapshot-signal` in the args produces no snapshot: the signal is
  ignored or the process dies;
- for a consumer, taking the pod out of the Service isolates nothing;
- in production, the path needs a PR + deploy, which resets the leaked state and is slow.

### 5. Cause and fix
With the suspect code identified, reproduce locally before opening a snapshot:
1. run the code path N times with `node --expose-gc`;
2. compare `heapUsed` before and after `gc()`; for listeners, use `getEventListeners(target, 'event').length`;
3. convert to MB/h and check it against the measured rate (times the number of resources, if it grows per shard/connection);
4. turn the reproduction into a test that fails on the old code.

For reading snapshots and the fix patterns, see [devtools-and-fixes.md](references/devtools-and-fixes.md).
The user reads the snapshot: ask for constructors, deltas, sizes and property names, never values.

### 6. Validation
- Run the same load (for consumers, the same number of shards/partitions) and confirm `#Delta` ≈ 0.
- In the metrics backend, the floor must stay flat for **hours** and the restarts must stop.
- Revert temporary flags, labels and limits, and delete the snapshots.

## Sampling script

```bash
python3 <skill>/scripts/sample_memory.py <namespace> -l <selector> [--context <ctx>] [-c <container>] [--mode top|exec] [-n 30] [-i 120]
```

- `<skill>` is this skill's directory. Usage and reading of the output in [metrics.md](references/metrics.md).
- `--mode top` (default) is read-only and fine in production. `--mode exec` is staging only.

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Error (kubectl, credentials, CSV from another mode); the summary of what was collected is still printed |
| 2 | Invalid arguments |
| 3 | `--mode exec` refused because the namespace or context looks like production |

## Common errors

| Situation | Action |
| --- | --- |
| `Unauthorized` / expired token | Ask the user to refresh cluster credentials (e.g. `aws eks update-kubeconfig`, `gcloud container clusters get-credentials`) |
| `metrics not available` | metrics-server missing or the pod just started; wait or use the metrics backend |
| No `Last State` (pods recreated by a deploy) | Look for OOM events in the metrics backend ([metrics.md](references/metrics.md)) |
| Metrics backend unreachable (MCP not authenticated, token expired) | Say so and measure through `kubectl` |

## Response format

Conceptual question ("what does `(compiled code)` +5000 mean?"): answer directly, without phases,
and suggest the next step.

During an investigation, every message has, in this order:
1. **Phase done**: result in numbers and what it means.
2. **Findings** that changed.
3. **Next phase**: goal and commands with the impact of each.
4. The question to go on.

At the end: the findings file, the cause with its evidence, the fix, the validation and what is
pending.

Code, manifest and production changes go through the team's review. Exposed sensitive data or an
incident goes to the team's security channel.
