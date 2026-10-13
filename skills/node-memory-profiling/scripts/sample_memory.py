#!/usr/bin/env python3
"""Sample the memory of a deployment's pods through kubectl and estimate the slope (MiB/h) of each
pod's floor, with a projection of when it reaches its limit. Use it to measure a leak without a
metrics backend, or to confirm what the backend shows.

Modes:
  top  (default) `kubectl top` = working set, the number the OOMKill uses. Read-only; fine in prod.
  exec           reads the container cgroup (v2: memory.current/anon; v1: usage_in_bytes/total_rss)
                 and /proc/<pid>/status of the node process (RssAnon, RssFile). Separates process
                 memory from file cache. **Staging only**: refused when the namespace or context
                 contains prod, production, prd or live as a word.

Measures only the main container (--container; default: the first in the spec), with that
container's limit and age, not the pod total with sidecars.

The slope uses the floor of each --window minutes (the minimum between GCs), ignoring the warm-up
(--warmup minutes of container life). Ctrl+C stops and still prints the summary.

Exit codes: 0 ok · 1 error · 2 invalid arguments · 3 exec refused in production.
Uses kubectl (Python standard library only).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXIT_ERROR = 1
EXIT_PRODUCTION = 3

UNITS_MIB = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "K": 1000 / 1024**2, "M": 1000**2 / 1024**2, "G": 1000**3 / 1024**2}
QUANTITY = re.compile(r"^(\d+(?:\.\d+)?)([KMG]i?)?$")
# A safety net only: the agent must never run exec in production, whatever the name.
PRODUCTION = re.compile(r"(?:^|[^a-z0-9])(?:prod|production|prd|live)(?:$|[^a-z0-9])", re.I)
AUTH_ERRORS = re.compile(r"Unauthorized|provide credentials|token has expired|ExpiredToken", re.I)
AUTH_HINT = (
    "Cluster credentials are missing or expired: refresh them "
    "(e.g. `aws eks update-kubeconfig`, `gcloud container clusters get-credentials`) and retry."
)
# node is not always PID 1 (npm start, dumb-init): look for the first process whose comm is "node".
# cgroup v2 (memory.current) with a fallback to v1 (memory/memory.usage_in_bytes, total_rss).
EXEC_SCRIPT = r"""
pid=1
for p in /proc/[0-9]*; do [ "$(cat $p/comm 2>/dev/null)" = node ] && pid=${p#/proc/} && break; done
if [ -f /sys/fs/cgroup/memory.current ]; then
  cur=$(cat /sys/fs/cgroup/memory.current); anon=$(awk '/^anon /{print $2}' /sys/fs/cgroup/memory.stat)
else
  cur=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes); anon=$(awk '/^total_rss /{print $2}' /sys/fs/cgroup/memory/memory.stat)
fi
echo "$cur,$anon,$(awk '/RssAnon/{print $2}' /proc/$pid/status),$(awk '/RssFile/{print $2}' /proc/$pid/status)"
"""
COLUMNS = {
    "top": ["ts", "pod", "age_min", "limit_mib", "working_set_mib"],
    "exec": ["ts", "pod", "age_min", "limit_mib", "cgroup_mib", "anon_mib", "rss_anon_mib", "rss_file_mib"],
}


def to_mib(quantity: str) -> float | None:
    match = QUANTITY.match(quantity or "")
    if not match:
        return None
    value, unit = match.groups()
    return float(value) * UNITS_MIB[unit] if unit else float(value) / 1024**2


class Kubectl:
    def __init__(self, namespace: str, context: str | None) -> None:
        self.base = ["kubectl", "-n", namespace] + (["--context", context] if context else [])

    def run(self, *args: str) -> str:
        try:
            result = subprocess.run(self.base + list(args), capture_output=True, text=True)
        except FileNotFoundError:
            raise RuntimeError("kubectl not found in PATH")
        if result.returncode != 0:
            message = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "kubectl failed"
            raise RuntimeError(f"{message}\n{AUTH_HINT}" if AUTH_ERRORS.search(message) else message)
        return result.stdout

    def pods(self, selector: str, container: str | None) -> dict[str, tuple[str, datetime, float | None]]:
        """Running pods → (measured container, that container's start, its limit in MiB)."""
        items = json.loads(self.run("get", "pods", "-l", selector, "-o", "json"))["items"]
        pods = {}
        for item in items:
            if item["status"].get("phase") != "Running":
                continue
            specs = item["spec"]["containers"]
            spec = next((c for c in specs if c["name"] == container), None) if container else specs[0]
            if not spec:
                continue
            # containerStatuses is sorted alphabetically, not in spec order: match by name.
            status = next((cs for cs in item["status"].get("containerStatuses", []) if cs["name"] == spec["name"]), {})
            # Memory age = container start, not pod start: an OOMKill restarts the container and
            # keeps the pod (and its startTime).
            started = status.get("state", {}).get("running", {}).get("startedAt") or item["status"]["startTime"]
            limit = to_mib(spec.get("resources", {}).get("limits", {}).get("memory", ""))
            pods[item["metadata"]["name"]] = (spec["name"], datetime.fromisoformat(started.replace("Z", "+00:00")), limit)
        return pods

    def top(self, selector: str) -> dict[tuple[str, str], float | None]:
        """(pod, container) → MiB. Per container: the pod total adds the sidecars."""
        lines = self.run("top", "pod", "-l", selector, "--containers", "--no-headers").splitlines()
        return {(pod, name): to_mib(memory) for pod, name, _cpu, memory, *_ in (line.split() for line in lines)}

    def read_cgroup(self, pod: str, container: str) -> list[float]:
        output = self.run("exec", pod, "-c", container, "--", "sh", "-c", EXEC_SCRIPT).strip()
        try:
            current, anon, rss_anon_kb, rss_file_kb = output.split(",")
            return [int(current) / 1024**2, int(anon) / 1024**2, int(rss_anon_kb) / 1024, int(rss_file_kb) / 1024]
        except ValueError:
            raise ValueError(f"incomplete cgroup/proc read ({output!r}): image without sh/awk or cgroup elsewhere")


def sample(kubectl: Kubectl, selector: str, mode: str, container: str | None, warned: set[str]) -> list[list]:
    now = datetime.now(timezone.utc)
    pods = kubectl.pods(selector, container)
    usage = kubectl.top(selector) if mode == "top" else {}
    rows = []
    for pod, (name, started, limit) in sorted(pods.items()):
        try:
            values = [usage[(pod, name)]] if mode == "top" else kubectl.read_cgroup(pod, name)
        except KeyError:
            continue  # pod going away or not in metrics-server yet
        except (RuntimeError, ValueError) as error:
            # Warn once per reason: a silent failure turns into "0 pods" for an hour with no explanation.
            if str(error) not in warned:
                warned.add(str(error))
                print(f"WARNING: skipping {pod}/{name}: {error}", file=sys.stderr)
            continue
        if values[0] is None:
            continue
        age = (now - started).total_seconds() / 60
        rows.append([now.strftime("%Y-%m-%dT%H:%M:%SZ"), pod, round(age, 1), limit and round(limit), *(round(v, 1) for v in values)])
    return rows


def slope_per_hour(points: list[tuple[float, float]]) -> float:
    """Simple linear regression of (age in min, MiB) → MiB/h."""
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    variance = sum((x - mean_x) ** 2 for x, _ in points)
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / variance * 60


def summarize(csv_path: Path, warmup: float, window: float) -> None:
    with csv_path.open() as file:
        rows = list(csv.DictReader(file))
    if not rows:
        print("No samples.")
        return
    metric = "working_set_mib" if "working_set_mib" in rows[0] else "anon_mib"
    by_pod: dict[str, list[dict]] = {}
    for row in rows:
        by_pod.setdefault(row["pod"], []).append(row)

    print(f"\nFloor of `{metric}` per pod (warm-up {warmup:g} min, {window:g} min windows):")
    for pod, samples in by_pod.items():
        floors: dict[int, tuple[float, float]] = {}
        for row in samples:
            age, value = float(row["age_min"]), float(row[metric])
            if age < warmup:
                continue
            bucket = int(age // window)
            if bucket not in floors or value < floors[bucket][1]:
                floors[bucket] = (age, value)
        last = samples[-1]
        current, age_now = float(last[metric]), float(last["age_min"])
        floors.pop(int(age_now // window), None)  # current window: may hold only a peak, not a floor
        if len(floors) < 3:
            print(f"- {pod}: {current:.0f} MiB at {age_now:.0f} min; not enough samples after the warm-up")
            continue
        slope = slope_per_hour(sorted(floors.values()))
        line = f"- {pod}: {current:.0f} MiB at {age_now:.0f} min, floor {slope:+.1f} MiB/h"
        limit = float(last["limit_mib"]) if last["limit_mib"] else None
        if limit and slope > 0.5:
            line += f", limit {limit:.0f} MiB in ~{(limit - current) / slope:.0f} h"
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 ok · 1 error · 2 invalid arguments · 3 exec refused in production.",
    )
    parser.add_argument("namespace", help="e.g. orders-staging")
    parser.add_argument("-l", "--selector", required=True, help="pod label selector, e.g. app.kubernetes.io/name=orders-consumer")
    parser.add_argument("--context", help="kubeconfig context (does not switch the current one)")
    parser.add_argument("-c", "--container", help="container to measure (default: the first in the spec)")
    parser.add_argument("--mode", choices=["top", "exec"], default="top")
    parser.add_argument("-n", "--samples", type=int, default=30, help="number of samples (default 30)")
    parser.add_argument("-i", "--interval", type=int, default=120, help="seconds between samples (default 120)")
    parser.add_argument("-o", "--output", type=Path, help="CSV (default ~/mem-investigations/<namespace>.csv)")
    parser.add_argument("--warmup", type=float, default=30, help="minutes of container life ignored (default 30)")
    parser.add_argument("--window", type=float, default=15, help="minutes per floor window (default 15)")
    parser.add_argument("--summary", action="store_true", help="only summarize an existing CSV")
    args = parser.parse_args()

    output = args.output or Path.home() / "mem-investigations" / f"{args.namespace}.csv"
    if args.summary:
        if not output.is_file():
            print(f"ERROR: {output} not found; pass the CSV with -o.", file=sys.stderr)
            return EXIT_ERROR
        summarize(output, args.warmup, args.window)
        return 0
    if args.mode == "exec" and (PRODUCTION.search(args.namespace) or PRODUCTION.search(args.context or "")):
        print("ERROR: kubectl exec is not allowed in production. Use --mode top.", file=sys.stderr)
        return EXIT_PRODUCTION

    output.parent.mkdir(parents=True, exist_ok=True)
    new_file = not output.exists()
    if not new_file:
        with output.open() as file:
            header = file.readline().strip().split(",")
        if header != COLUMNS[args.mode]:
            print(f"ERROR: {output} was collected in another mode; pass another -o.", file=sys.stderr)
            return EXIT_ERROR
    kubectl = Kubectl(args.namespace, args.context)
    warned: set[str] = set()
    failures = status = 0
    try:
        # Appends and flushes every sample, so an interrupted run keeps what it collected.
        with output.open("a", newline="") as file:
            writer = csv.writer(file)
            if new_file:
                writer.writerow(COLUMNS[args.mode])
            for i in range(args.samples):
                try:
                    rows = sample(kubectl, args.selector, args.mode, args.container, warned)
                    failures = 0
                except RuntimeError as error:
                    # A transient error (API, "metrics not available" after a rollout) loses only this
                    # sample; expired credentials or 3 failures in a row stop, still with the summary.
                    failures += 1
                    print(f"ERROR: [{i + 1}/{args.samples}] kubectl: {error}", file=sys.stderr)
                    if AUTH_ERRORS.search(str(error)) or "not found in PATH" in str(error) or failures >= 3:
                        status = EXIT_ERROR
                        break
                    rows = []
                writer.writerows(rows)
                file.flush()
                print(f"[{i + 1}/{args.samples}] {len(rows)} pods → {output}", file=sys.stderr)
                if i < args.samples - 1:
                    time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    summarize(output, args.warmup, args.window)
    return status


if __name__ == "__main__":
    sys.exit(main())
