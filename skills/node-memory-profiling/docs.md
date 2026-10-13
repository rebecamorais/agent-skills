# node-memory-profiling

A skill for investigating memory leaks and memory pressure in Node.js services running on Kubernetes (APIs, queue/stream consumers, workers), in staging or production. The agent leads the investigation one phase at a time (context → triage → metrics → hypothesis → capture → cause → validation), shows every command with its impact before running it, and keeps a findings file you can paste into a ticket or postmortem.

It never attaches a debugger to production, never runs commands that change production itself, and never reads heap snapshots, environment variables or log lines: those can hold personal data and secrets, and anything the agent reads goes to the LLM.

The skill follows the [Agent Skills](https://agentskills.io) format, so it works with any agent that supports it. Agents that don't can follow `SKILL.md` as plain instructions.

## Requirements

- `kubectl` with read access to the cluster (`get`, `top`; `exec` in staging for `--mode exec`)
- Python 3.10+ (standard library only) for the sampling script
- Optional: a metrics backend the agent can query (Datadog, Prometheus) and Chrome for DevTools

## Installation

```bash
npx skills add rebecamorais/agent-skills/skills/node-memory-profiling
```

## Using it with an agent

Requests like these trigger the skill:

- "our consumer in production keeps restarting because of memory, help me investigate"
- "pod OOMKilled with exit code 137 since yesterday's deploy"
- "the comparison shows `(compiled code)` +5000, what does it mean?"
- "how do I attach DevTools to the staging pod?"

## Rules per environment

| | Staging | Production |
| --- | --- | --- |
| Metrics, `kubectl get/top` | Yes | Yes |
| `kubectl exec` | Yes | No |
| Inspector + live DevTools | Yes | No, reproduce in staging |
| Heap snapshot | Yes | By signal only, with written approval, run by you |

## Using the script directly

```bash
# 1h of samples (30 × 2 min) of the working set, read-only
python3 scripts/sample_memory.py orders-prod -l app.kubernetes.io/name=orders-worker

# Staging: cgroup + /proc of the node process
python3 scripts/sample_memory.py orders-staging -l app.kubernetes.io/name=orders-worker --mode exec

# Summarize a CSV already collected
python3 scripts/sample_memory.py orders-prod -l x --summary
```

It prints, per pod, the slope of the memory floor between GCs (MiB/h) and when the container reaches its limit. Exit codes: `0` ok, `1` error, `2` invalid arguments, `3` `--mode exec` refused because the namespace or context looks like production (or the context is unknown).

## Side effects and limitations

- Writes `~/mem-investigations/<namespace>-<date>.md` (findings) and `~/mem-investigations/<context>_<namespace>.csv` (samples), outside any repo. They hold numbers and pod names only.
- The production check in the script is by name (`prod`, `production`, `prd`, `live`); it is a safety net, not a permission.
- Native memory leaks (only RSS grows) don't show up in heap snapshots; the skill points to the metrics that separate the two.

## Evals

`evals/evals.json` has test prompts and the expected agent behavior: refusing DevTools in production under pressure, not reading a heap snapshot, not running `describe`/log lines, and not treating "ok, and now?" as approval.
