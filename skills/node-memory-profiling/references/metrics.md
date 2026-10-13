# Metrics — backend and Kubernetes

## Triage without `kubectl`

- **Expired credentials or no `Last State`** (a deploy recreated the pods): OOM events in the
  backend confirm the kill, with pod name and version. In Datadog, search `event_type:oom` with
  `kube_namespace:<ns>`; in Prometheus,
  `kube_pod_container_status_last_terminated_reason{namespace="<ns>",reason="OOMKilled"}`.
- **Pod replaced without OOM** (node drained, spot instance reclaimed): not a memory symptom, but it
  shortens pod life and makes the slope harder to measure.

## Metrics backend

Use the backend's MCP server or CLI. If neither works (MCP not authenticated, token expired), say
so and measure through Kubernetes (below).

| What | Datadog | Prometheus (kube-state-metrics, cAdvisor, `prom-client`) | Why |
| --- | --- | --- | --- |
| Container memory vs limit | `kubernetes.memory.working_set`, `kubernetes.memory.limits` `by {kube_deployment}` | `container_memory_working_set_bytes{container="<c>"}`, `kube_pod_container_resource_limits{resource="memory"}` | How far from the OOMKill |
| Kills | `kubernetes.containers.restarts`, `container.memory.oom_events` | `kube_pod_container_status_restarts_total`, `kube_pod_container_status_last_terminated_reason` | How often it dies |
| Node runtime memory | `runtime.node.mem.rss`, `.heap_used`, `.heap_total`, `.external` `by {version}` / `by {host}` | `process_resident_memory_bytes`, `nodejs_heap_size_used_bytes`, `nodejs_heap_size_total_bytes`, `nodejs_external_memory_bytes` | Where memory grows |
| GC | `runtime.node.gc.pause.*` | `nodejs_gc_duration_seconds` | GC working harder and harder |

- **Runtime metrics need instrumentation:** Datadog's `runtime.node.*` needs
  `DD_RUNTIME_METRICS_ENABLED=true`; Prometheus needs `prom-client`'s `collectDefaultMetrics()`.
  Before saying there is no data, query it: sometimes runtime metrics exist and container metrics
  have gaps, or the other way round.
- **Which deployment grows:** runtime metrics are tagged by service, not by deployment, and several
  deployments often share the same service name (e.g. an API and a consumer built from the same
  repo). Break them down by host/pod and use container working set by deployment to find out.
- **New or old leak:** compare by version over a **10–14 day** window. If the same ramp shows up in
  earlier versions, the leak is old and only became visible now (pods living longer, more load per
  pod). Then a rollback won't fix it.

## JS heap vs native: traps

- **Container vs process:** working set is the whole container; runtime `rss` is only the Node
  process. If working set rises and Node's `rss` doesn't, it's another process in the container.
- **`worker_threads`:** `heap_used` is only the main thread's, so a leak inside a worker shows up as
  "RSS only".
- **Measuring through `exec`:** don't use `kubectl exec ... node -e "process.memoryUsage()"`. That
  measures a new process, not the app.
- **Estimating the native part:** `rss − heap_total` is an estimate, but V8 reserves heap in steps
  (`heap_total` grows more than `heap_used`). If `heap_used` also rises, fix the heap leak first and
  measure again before investigating native memory.
- **`MALLOC_ARENA_MAX=2`:** only works on glibc images (Debian/Ubuntu), not on Alpine/musl. Test in staging.

## Measuring through Kubernetes (no backend, or to confirm)

Age counts from the last start of the main **container** (`state.running.startedAt` of the
`containerStatuses` item with the container's name, not `[0]`, which is sorted alphabetically), not
the pod: an OOMKill restarts the container and keeps the pod. With a single `kubectl top`, pods of
different ages in the same ReplicaSet already give the slope (e.g. 90Mi at 10 min and 112Mi at
75 min → ~20Mi/h).

For a series, use [scripts/sample_memory.py](../scripts/sample_memory.py) (`python3` >= 3.10,
standard library + `kubectl`), for a bounded time (default: 30 samples × 2 min = 1h):

```bash
python3 <skill>/scripts/sample_memory.py <namespace> -l <selector> [--context <ctx>] [-c <container>] [--mode exec] [-n 30] [-i 120]
```

- **Selector:** the deployment's pod labels, not the repo name. In a namespace with an API and a
  consumer, check with `kubectl get pods --show-labels`.
- **Container:** measures only the main container (the first in the spec; `-c <container>` picks
  another), with its own limit and age. The pod total would add the sidecars.
- **`--mode top`** (default): reads the container's working set (`kubectl top --containers`), the
  number the OOMKill uses. Read-only; fine in production.
- **`--mode exec`**: reads the container cgroup (v2: `memory.current`/`anon`; v1:
  `usage_in_bytes`/`total_rss`) and `/proc/<pid>/status` (`RssAnon`, `RssFile`) of the `node`
  process, which is not always PID 1 (`npm start`, `dumb-init`). If the read fails (image without
  `sh`/`awk`), the script warns once with the reason. Staging only: the script refuses when the
  namespace or context contains `prod`, `production`, `prd` or `live` as a word (exit 3). This is a
  safety net, not a permission: never run `exec` in production even when the name doesn't match.
  If `RssFile` stays flat and `anon` rises, the memory belongs to the process, not to file cache.
- **Output:** appends to `~/mem-investigations/<namespace>.csv`. At the end (or on Ctrl+C) it
  prints, per pod, the slope of the **floor between GCs** and in how many hours the container
  reaches its limit. The floor is the minimum per `--window` minutes, ignoring the current window
  and the first `--warmup` minutes of life. `--summary` only summarizes an existing CSV.
- **kubectl errors:** a transient error loses only that sample. Expired credentials or 3 failures in
  a row stop the collection, but the summary of what was collected is still printed (exit 1).
- **Duration:** needs ~3 windows after the warm-up (≈ 1h15 of container life with the defaults). If
  a pod is replaced midway, the series starts again on the new pod.

## Continuous profiler

Heap profiles per function, with low overhead and without touching the pod (Datadog Continuous
Profiler, Pyroscope, Grafana Cloud Profiles, etc.). Check whether the service already has one. To
enable it, change the env/config through a normal deploy (PR + review), then compare heap profiles
before and after.
