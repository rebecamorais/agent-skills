#!/usr/bin/env python3
"""Collect a read-only snapshot of a repository for project-pickup.

Reads the GitHub Project, open pull requests, warnings from watched workflows, local
git worktrees, plan checkboxes, handoff notes and agent session metadata. Writes the full
snapshot to the private state directory and prints a compact JSON digest to stdout.

It never writes to GitHub or git, never fetches, never calls `gh`, and accepts no
free-form query: a profile is a fixed list of named operations.

Exit codes: 0 every source ok · 1 fatal error (not a git repo, bad config) ·
2 usage error · 3 snapshot written but some sources are missing or partial (treat as
unknown, never as fine).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import engcore  # noqa: E402
from engcore import Budget, GitHub, SourceError, UnsafeOperation, clip, git, git_ok  # noqa: E402

COLLECTOR_VERSION = "1.0.0"
CACHE_TTL_S = 600
DIGEST_MAX_CHARS = 24000

PROFILES: dict[str, dict[str, Any]] = {
    "pickup": {
        "ops": ["project_items", "open_prs", "workflow_warnings", "local_git", "local_context"],
        "max_requests": 30,
        "deadline_s": 45,
    },
}

DEFAULT_PROJECT = {
    "owner_type": "user",
    "status_field": "Status",
    "priority_field": "Priority",
    "done_status": "Done",
    "in_progress_status": "In progress",
    "planned_status": "Planned",
    "priority_order": ["Now", "Next", "Later", "Someday"],
}
DEFAULT_PLANS = {"globs": ["docs/plans/*.md", "docs/superpowers/plans/*.md", "plans/*.md"], "max_files": 3}
MAX_WORKTREES = 20
MAX_DIRTY_FILES = 50
MAX_WATCHED_WORKFLOWS = 5
MAX_JOBS_PER_RUN = 10
HANDOFF_MAX_LINES = 200
HANDOFF_MAX_CHARS = 8000
PLAN_MAX_BYTES = 512 * 1024
SESSION_RETENTION_DAYS = 30

# Replaced by tests with a fake transport.
TRANSPORT = engcore.urllib_transport
SLEEP: Callable[[float], None] = time.sleep


class Fatal(Exception):
    pass


# =========================================================================== helpers


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(ts: float | datetime) -> str:
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, timezone.utc)
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def days_between(earlier: datetime | None, later: datetime) -> int | None:
    return None if earlier is None else max(0, (later - earlier).days)


def ramp_up_minutes(days: int | None) -> int | None:
    """Sizing rubric: +0 if touched within 2 days, +15 for 3-14 days, +30 after that."""
    if days is None:
        return None
    return 0 if days <= 2 else 15 if days <= 14 else 30


def data_of(ctx: "Context", name: str) -> dict[str, Any]:
    """The validated data of a source that already ran, or {} if it failed or didn't run."""
    return (ctx.results.get(name) or {}).get("data") or {}


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:10]


def claude_project_dir_name(path: str) -> str:
    """Claude Code's documented encoding: non-alphanumeric characters become '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


# =========================================================================== repo + config


def resolve_repo(path: Path) -> tuple[Path, str, bool]:
    """Return (main worktree root, repo id, is_github). See engcore.repo_identity."""
    if not path.is_dir():
        raise Fatal(f"{path} is not a directory")
    try:
        return engcore.repo_identity(path)
    except SourceError as error:
        raise Fatal(f"{path} is not inside a git repository ({error})") from None


def find_config(explicit: Path | None, main_root: Path, repo: str) -> Path | None:
    if explicit:
        if not explicit.is_file():
            raise Fatal(f"config file {explicit} not found")
        return explicit
    for candidate in (main_root / ".eng.json", engcore.config_dir() / "repos" / f"{engcore.repo_slug(repo)}.json"):
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None, repo: str) -> dict[str, Any]:
    config: dict[str, Any] = {"repo": repo, "plans": dict(DEFAULT_PLANS), "watch_workflows": []}
    if path is None:
        return config
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Fatal(f"config {path} is not valid JSON: {error}") from None
    schema = engcore.load_schema("config.v1.json")
    problems = engcore.validate(raw, schema)
    if problems:
        raise Fatal(f"config {path} is invalid: " + "; ".join(problems[:5]))
    if raw["repo"].lower() != repo.lower():
        raise Fatal(f"config {path} is for {raw['repo']}, but this checkout is {repo}")
    for glob in raw.get("plans", {}).get("globs", []):
        if glob.startswith(("/", "~")) or ".." in Path(glob).parts:
            raise Fatal(f"config {path}: plan glob {glob!r} must be relative and stay inside the repository")
    config["source"] = str(path)
    config["default_branch"] = raw.get("default_branch")
    if "project" in raw:
        config["project"] = {**DEFAULT_PROJECT, **raw["project"]}
    config["plans"] = {**DEFAULT_PLANS, **raw.get("plans", {})}
    config["watch_workflows"] = raw.get("watch_workflows", [])[:MAX_WATCHED_WORKFLOWS]
    return config


# =========================================================================== context


class Context:
    def __init__(self, args: argparse.Namespace, main_root: Path, repo: str, config: dict[str, Any], github: bool = True) -> None:
        self.args = args
        self.main_root = main_root
        self.repo = repo
        self.github = github
        self.owner, self.name = repo.split("/")
        self.config = config
        profile = PROFILES[args.profile]
        self.budget = Budget(max_requests=profile["max_requests"], deadline_s=profile["deadline_s"])
        self.state = engcore.state_dir() / engcore.repo_slug(repo)
        self.results: dict[str, dict[str, Any]] = {}
        self._clients: dict[str, GitHub | SourceError] = {}
        self._tokens: dict[Path, str] = {}

    def client(self, kind: str = "read") -> GitHub:
        """A GitHub client for the read token, or the project token when one exists."""
        if kind not in self._clients:
            path = self.args.token_file
            if kind == "project" and self.args.project_token_file and self.args.project_token_file.exists():
                path = self.args.project_token_file
            try:
                if path not in self._tokens:  # read each file once (it may be a one-shot pipe)
                    self._tokens[path] = engcore.read_token(path)
                self._clients[kind] = GitHub(self._tokens[path], self.budget, transport=TRANSPORT, sleep=SLEEP)
            except SourceError as error:
                self._clients[kind] = SourceError(
                    f"{error}. Create a fine-grained read-only token and save it there (see docs.md)"
                )
        client = self._clients[kind]
        if isinstance(client, SourceError):
            raise client
        return client


# =========================================================================== GitHub ops


def op_project_items(ctx: Context) -> tuple[str, Any, dict[str, Any]]:
    project = ctx.config.get("project")
    if not project:
        return "unconfigured", None, {"error": "no project in the config (owner, number); see docs.md"}
    doc = engcore.load_graphql("project_items_org" if project["owner_type"] == "org" else "project_items_user")
    client = ctx.client("project")
    meta: dict[str, Any] = {}

    def fetch(cursor: str | None):
        data = client.graphql(doc, {"owner": project["owner"], "number": project["number"], "after": cursor})
        node = (data.get("owner") or {}).get("projectV2")
        if node is None:
            raise SourceError(f"project {project['owner']}#{project['number']} not found or not readable with this token")
        meta.update(title=node.get("title"), url=node.get("url"))
        items = node["items"]
        return items["nodes"], items["pageInfo"], items.get("totalCount")

    nodes, truncated, total = engcore.paginate(fetch)
    items = [normalise_item(node, project) for node in nodes]
    data = {"title": meta.get("title") or "", "url": meta.get("url") or "", "total": total or len(items), "items": items}
    return "ok", data, {"count": len(items), "truncated": truncated}


def normalise_item(node: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    fields = {}
    for value in (node.get("fieldValues") or {}).get("nodes", []):
        if value and value.get("__typename") == "ProjectV2ItemFieldSingleSelectValue":
            fields[(value.get("field") or {}).get("name")] = value.get("name")
    content = node.get("content") or {}
    kind = content.get("__typename") or "Unknown"
    sub = content.get("subIssuesSummary") or {}
    return {
        "type": kind,
        "archived": bool(node.get("isArchived")),
        "status": fields.get(project["status_field"]),
        "priority": fields.get(project["priority_field"]),
        "number": content.get("number"),
        "title": clip(content.get("title"), 200),
        "url": content.get("url") or "",
        "state": content.get("state"),
        "state_reason": content.get("stateReason"),
        "repo": (content.get("repository") or {}).get("nameWithOwner"),
        "labels": [label["name"] for label in ((content.get("labels") or {}).get("nodes") or [])][:20],
        "parent": (content.get("parent") or {}).get("number"),
        "sub_total": sub.get("total", 0) or 0,
        "sub_completed": sub.get("completed", 0) or 0,
        "closed_by_prs": [
            {"number": pr["number"], "state": pr["state"]}
            for pr in ((content.get("closedByPullRequestsReferences") or {}).get("nodes") or [])
        ],
        "updated_at": content.get("updatedAt") or node.get("updatedAt"),
    }


def op_open_prs(ctx: Context) -> tuple[str, Any, dict[str, Any]]:
    doc = engcore.load_graphql("open_prs")
    client = ctx.client("read")
    meta: dict[str, Any] = {}

    def fetch(cursor: str | None):
        data = client.graphql(doc, {"owner": ctx.owner, "name": ctx.name, "after": cursor})
        repo = data.get("repository")
        if repo is None:
            raise SourceError(f"repository {ctx.repo} not found or not readable with this token")
        meta["default_branch"] = (repo.get("defaultBranchRef") or {}).get("name")
        prs = repo["pullRequests"]
        return prs["nodes"], prs["pageInfo"], prs.get("totalCount")

    nodes, truncated, total = engcore.paginate(fetch)
    prs = [normalise_pr(node) for node in nodes]
    data = {"default_branch": meta.get("default_branch") or "main", "total": total or len(prs), "prs": prs}
    return "ok", data, {"count": len(prs), "truncated": truncated}


def normalise_pr(node: dict[str, Any]) -> dict[str, Any]:
    commits = (node.get("commits") or {}).get("nodes") or []
    commit = (commits[0] or {}).get("commit") if commits else {}
    rollup = (commit or {}).get("statusCheckRollup") or {}
    author = (node.get("author") or {}).get("login") or "ghost"
    return {
        "number": node["number"],
        "title": clip(node.get("title"), 200),
        "url": node.get("url") or "",
        "draft": bool(node.get("isDraft")),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "head": node.get("headRefName") or "",
        "base": node.get("baseRefName") or "",
        "mergeable": node.get("mergeable"),
        "review": node.get("reviewDecision"),
        "author": author,
        "is_dependabot": author in ("dependabot", "dependabot[bot]", "app/dependabot"),
        "auto_merge": node.get("autoMergeRequest") is not None,
        "labels": [label["name"] for label in ((node.get("labels") or {}).get("nodes") or [])][:10],
        "closes": [issue["number"] for issue in ((node.get("closingIssuesReferences") or {}).get("nodes") or [])],
        "checks": rollup.get("state"),
        "head_oid": (commit or {}).get("oid"),
    }


def op_workflow_warnings(ctx: Context) -> tuple[str, Any, dict[str, Any]]:
    files = ctx.config.get("watch_workflows") or []
    if not files:
        return "unconfigured", None, {"error": "no watch_workflows in the config"}
    client = ctx.client("read")
    branch = default_branch(ctx)
    base = f"/repos/{ctx.owner}/{ctx.name}"
    workflows = []
    for file in files:
        runs = client.rest_get(
            f"{base}/actions/workflows/{file}/runs",
            {"branch": branch, "status": "completed", "per_page": 1, "exclude_pull_requests": "true"},
        )
        run = ((runs or {}).get("workflow_runs") or [None])[0]
        entry: dict[str, Any] = {"file": file, "name": file, "run_url": None, "conclusion": None, "created_at": None, "annotations": []}
        if run:
            entry.update(name=run.get("name") or file, run_url=run.get("html_url"), conclusion=run.get("conclusion"), created_at=run.get("created_at"))
            jobs = client.rest_get(f"{base}/actions/runs/{int(run['id'])}/jobs", {"per_page": 30})
            for job in ((jobs or {}).get("jobs") or [])[:MAX_JOBS_PER_RUN]:
                if job.get("conclusion") == "skipped":
                    continue
                notes = client.rest_get(f"{base}/check-runs/{int(job['id'])}/annotations", {"per_page": 50})
                for note in notes or []:
                    if note.get("annotation_level") in ("warning", "failure"):
                        entry["annotations"].append(
                            {"level": note["annotation_level"], "job": clip(job.get("name"), 80), "message": clip(note.get("message"), 200)}
                        )
        workflows.append(entry)
    return "ok", {"workflows": workflows}, {"count": len(workflows)}


# =========================================================================== local ops


def parse_worktrees(porcelain: str) -> list[dict[str, Any]]:
    trees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in porcelain.splitlines() + [""]:
        if not line:
            if current:
                trees.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": value, "locked": False, "prunable": False, "detached": False, "bare": False}
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key in ("detached", "bare"):
            current[key] = True
        elif key == "locked":
            current["locked"] = True
            current["lock_reason"] = value or None
        elif key == "prunable":
            current["prunable"] = True
    return trees


def parse_status(porcelain: str) -> dict[str, Any]:
    info: dict[str, Any] = {"upstream": None, "ahead_upstream": None, "behind_upstream": None, "files": [], "untracked": 0}
    for line in porcelain.splitlines():
        if line.startswith("# branch.upstream "):
            info["upstream"] = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            ahead, behind = line.split(" ")[2:4]
            info["ahead_upstream"], info["behind_upstream"] = int(ahead), abs(int(behind))
        elif line.startswith("1 "):
            parts = line.split(" ", 8)
            info["files"].append({"xy": parts[1], "path": parts[8]})
        elif line.startswith("2 "):
            parts = line.split(" ", 9)
            info["files"].append({"xy": parts[1], "path": parts[9].split("\t")[0]})
        elif line.startswith("u "):
            parts = line.split(" ", 10)
            info["files"].append({"xy": parts[1], "path": parts[10]})
        elif line.startswith("? "):
            info["files"].append({"xy": "??", "path": line[2:]})
            info["untracked"] += 1
    return info


def default_branch(ctx: Context) -> str:
    """From GitHub if open_prs ran, else the config, else 'main'."""
    return data_of(ctx, "open_prs").get("default_branch") or ctx.config.get("default_branch") or "main"


def base_ref_for(ctx: Context) -> str:
    branch = default_branch(ctx)
    remote = f"refs/remotes/origin/{branch}"
    if git_ok(["rev-parse", "--verify", "--quiet", remote], ctx.main_root) == 0:
        return f"origin/{branch}"
    return branch


def op_local_git(ctx: Context) -> tuple[str, Any, dict[str, Any]]:
    trees = parse_worktrees(git(["worktree", "list", "--porcelain"], ctx.main_root))
    base = base_ref_for(ctx)
    try:
        base_time = int(git(["log", "-1", "--format=%ct", base], ctx.main_root).strip())
        base_info = {"ref": base, "commit_at": iso(base_time)}
    except (SourceError, ValueError):
        base_info = {"ref": base, "commit_at": None}
    worktrees = []
    partial = False
    for index, tree in enumerate(trees[:MAX_WORKTREES]):
        path = Path(tree["path"])
        entry: dict[str, Any] = {
            "path": tree["path"],
            "branch": tree.get("branch"),
            "head": tree.get("head"),
            "main": index == 0,
            "detached": tree["detached"],
            "locked": tree["locked"],
            "lock_reason": tree.get("lock_reason"),
            "missing": False,
        }
        if tree["bare"]:
            continue
        if tree["prunable"] or not path.is_dir() or not path.is_absolute():
            entry["missing"] = True
            worktrees.append(entry)
            continue
        try:
            status = parse_status(git(["status", "--porcelain=v2", "--branch", "--untracked-files=normal"], path))
            entry.update(
                upstream=status["upstream"],
                ahead_upstream=status["ahead_upstream"],
                behind_upstream=status["behind_upstream"],
                dirty_count=len(status["files"]),
                untracked_count=status["untracked"],
                dirty_files=status["files"][:MAX_DIRTY_FILES],
            )
            sha, ctime, subject = git(["log", "-1", "--format=%H%x1f%ct%x1f%s"], path).rstrip("\n").split("\x1f", 2)
            entry["last_commit"] = {"sha": sha, "at": iso(int(ctime)), "subject": clip(subject, 120)}
            behind, ahead = git(["rev-list", "--left-right", "--count", f"{base}...HEAD"], path).split()
            entry["ahead_base"], entry["behind_base"] = int(ahead), int(behind)
        except (SourceError, ValueError) as error:
            entry["error"] = str(error)[:200]
            partial = True
        worktrees.append(entry)
    truncated = len(trees) > MAX_WORKTREES
    return ("partial" if partial else "ok"), {"base": base_info, "worktrees": worktrees}, {"count": len(worktrees), "truncated": truncated}


def newest_handoff(ctx: Context, branch: str | None) -> dict[str, Any] | None:
    if not branch:
        return None
    folder = ctx.state / "handoff" / engcore.safe_name(branch)
    notes = sorted(folder.glob("*.md")) if folder.is_dir() else []
    if not notes:
        return None
    note = notes[-1]
    text = note.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()[:HANDOFF_MAX_LINES]
    return {"path": str(note), "written_at": iso(note.stat().st_mtime), "text": "\n".join(lines)[:HANDOFF_MAX_CHARS]}


def plan_files(ctx: Context, tree: dict[str, Any], base: str) -> list[Path]:
    root = Path(tree["path"])
    globs = ctx.config["plans"]["globs"]
    candidates: list[str] = []
    if not tree["main"]:
        try:
            candidates += git(["diff", "--name-only", f"{base}...HEAD"], root).splitlines()
        except SourceError:
            pass
    candidates += [f["path"] for f in tree.get("dirty_files", [])]
    chosen: list[Path] = []
    resolved_root = root.resolve()
    for rel in dict.fromkeys(candidates):
        if not any(fnmatch.fnmatch(rel, glob) for glob in globs):
            continue
        full = (root / rel).resolve()
        if resolved_root not in full.parents or not full.is_file():
            continue
        chosen.append(full)
    chosen.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return chosen[: ctx.config["plans"]["max_files"]]


_CHECKBOX = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*)$")


def read_plan(path: Path, root: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        text = handle.read(PLAN_MAX_BYTES).decode("utf-8", errors="replace")
    done = total = 0
    next_tasks: list[str] = []
    for line in text.splitlines():
        match = _CHECKBOX.match(line)
        if not match:
            continue
        total += 1
        if match.group(1).lower() == "x":
            done += 1
        elif len(next_tasks) < 3:
            next_tasks.append(clip(match.group(2), 100))
    return {"file": str(path.relative_to(root.resolve())), "done": done, "total": total, "next_tasks": next_tasks}


def newest_session(tree_path: str, now: datetime) -> dict[str, Any] | None:
    """Claude Code session metadata only: file name (session ID), mtime and size. Never content."""
    projects = Path(os.environ.get("ENG_CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects"))).expanduser()
    folder = projects / claude_project_dir_name(tree_path)
    if not folder.is_dir():
        return None
    sessions = [p for p in folder.glob("*.jsonl") if re.fullmatch(r"[A-Za-z0-9-]{8,64}", p.stem)]
    if not sessions:
        return None
    newest = max(sessions, key=lambda p: p.stat().st_mtime)
    info = newest.stat()
    modified = datetime.fromtimestamp(info.st_mtime, timezone.utc)
    age = (now - modified).days
    return {
        "agent": "claude-code",
        "session_id": newest.stem,
        "modified_at": iso(modified),
        "age_days": age,
        "size_bytes": info.st_size,
        "resume_command": f"claude --resume {newest.stem}" if age < SESSION_RETENTION_DAYS else None,
    }


def op_local_context(ctx: Context) -> tuple[str, Any, dict[str, Any]]:
    local = ctx.results.get("local_git") or {}
    if not local.get("data"):
        return "skipped", None, {"error": "local_git did not produce worktrees"}
    now = now_utc()
    base = local["data"]["base"]["ref"]
    entries = []
    partial = False
    for tree in local["data"]["worktrees"]:
        entry: dict[str, Any] = {"path": tree["path"], "branch": tree.get("branch"), "handoff": None, "plans": [], "session": None}
        if tree.get("missing"):
            entries.append(entry)
            continue
        try:
            entry["handoff"] = newest_handoff(ctx, tree.get("branch"))
            entry["plans"] = [read_plan(p, Path(tree["path"])) for p in plan_files(ctx, tree, base)]
            entry["session"] = newest_session(tree["path"], now)
        except (OSError, SourceError, ValueError) as error:
            entry["error"] = str(error)[:200]
            partial = True
        entries.append(entry)
    return ("partial" if partial else "ok"), {"worktrees": entries}, {"count": len(entries)}


GITHUB_OPS = {"project_items", "open_prs", "workflow_warnings"}

OPS: dict[str, Callable[[Context], tuple[str, Any, dict[str, Any]]]] = {
    "project_items": op_project_items,
    "open_prs": op_open_prs,
    "workflow_warnings": op_workflow_warnings,
    "local_git": op_local_git,
    "local_context": op_local_context,
}


# =========================================================================== digest + findings


def item_ref(item: dict[str, Any], repo: str) -> str:
    if item.get("number") is None:
        return "draft"
    return f"#{item['number']}" if (item.get("repo") or repo).lower() == repo.lower() else f"{item['repo']}#{item['number']}"


def build_findings(ctx: Context) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    repo = ctx.repo

    def add(fid: str, severity: str, kind: str, title: str, url: str | None = None, evidence: list[str] | None = None) -> None:
        findings.append({"id": fid, "severity": severity, "kind": kind, "title": title, "url": url, "evidence": evidence or []})

    project = ctx.config.get("project") or {}
    items = data_of(ctx, "project_items").get("items", [])
    for item in items:
        if item["archived"] or item["type"] != "Issue":
            continue
        ref = item_ref(item, repo)
        key = f"{item.get('repo') or repo}#{item['number']}"
        if item["state"] == "OPEN":
            if not item["status"]:
                add(f"project:no-status:{key}", "S4", "hygiene", f"{ref} has no Status in the Project", item["url"])
            if not item["priority"]:
                add(f"project:no-priority:{key}", "S4", "hygiene", f"{ref} has no Priority in the Project", item["url"])
        elif item["state"] == "CLOSED" and item["status"] != project.get("done_status"):
            add(f"project:closed-not-done:{key}", "S4", "hygiene", f"{ref} is closed but its Status is {item['status'] or 'empty'}", item["url"])

    prs = data_of(ctx, "open_prs").get("prs", [])
    for pr in prs:
        if pr["checks"] in ("FAILURE", "ERROR"):
            add(f"pr:checks-failing:{repo}#{pr['number']}", "S4", "pr", f"PR #{pr['number']} has failing checks", pr["url"])
        if pr["mergeable"] == "CONFLICTING":
            add(f"pr:conflicts:{repo}#{pr['number']}", "S4", "pr", f"PR #{pr['number']} has merge conflicts", pr["url"])
        if pr["is_dependabot"] and not pr["auto_merge"]:
            add(
                f"maintenance:dependabot-automerge-off:{repo}#{pr['number']}",
                "S3",
                "maintenance",
                f"Dependabot PR #{pr['number']} has no auto-merge enabled",
                pr["url"],
            )

    workflows = data_of(ctx, "workflow_warnings").get("workflows", [])
    for wf in workflows:
        if wf["conclusion"] in ("failure", "timed_out", "startup_failure"):
            add(f"maintenance:workflow-failed:{repo}:{wf['file']}", "S3", "maintenance", f"Latest {wf['name']} run on the default branch ended with {wf['conclusion']}", wf["run_url"])
        for note in wf["annotations"]:
            add(
                f"maintenance:workflow-warning:{repo}:{wf['file']}:{fingerprint(note['job'], note['message'])}",
                "S3",
                "maintenance",
                f"{wf['name']} ({note['job']}) reported: {clip(note['message'], 120)}",
                wf["run_url"],
                [f"level: {note['level']}"],
            )
    return findings


def summarise_worktrees(ctx: Context, now: datetime) -> list[dict[str, Any]]:
    trees = data_of(ctx, "local_git").get("worktrees", [])
    context = {c["path"]: c for c in (data_of(ctx, "local_context").get("worktrees", []))}
    out = []
    for tree in trees:
        extra = context.get(tree["path"], {})
        commit_at = parse_iso((tree.get("last_commit") or {}).get("at"))
        session = extra.get("session")
        handoff = extra.get("handoff")
        session_at = parse_iso(session["modified_at"]) if session else None
        handoff_at = parse_iso(handoff["written_at"]) if handoff else None
        known = [t for t in (commit_at, session_at, handoff_at) if t]
        touched = max(known) if known else None
        days = days_between(touched, now)
        # A session newer than both the last handoff and the last commit, with uncommitted
        # files, probably ended without a handoff. Only a hint: never stash, reset or commit.
        unexpected = bool(
            session_at
            and tree.get("dirty_count")
            and (handoff_at is None or session_at > handoff_at)
            and (commit_at is None or session_at > commit_at)
        )
        plan = (extra.get("plans") or [None])[0]
        out.append(
            {
                "branch": tree.get("branch"),
                "path": tree["path"],
                "main": tree["main"],
                "locked": tree["locked"],
                "missing": tree.get("missing", False),
                "error": tree.get("error") or extra.get("error"),
                "dirty_count": tree.get("dirty_count", 0),
                "dirty_files": [f["path"] for f in tree.get("dirty_files", [])][:10],
                "ahead_base": tree.get("ahead_base"),
                "behind_base": tree.get("behind_base"),
                "last_commit_at": (tree.get("last_commit") or {}).get("at"),
                "last_commit_subject": (tree.get("last_commit") or {}).get("subject"),
                "last_touched_at": iso(touched) if touched else None,
                "days_since_touched": days,
                "ramp_up_min": ramp_up_minutes(days),
                "plan": plan,
                "handoff": {"written_at": handoff["written_at"], "text": handoff["text"][:2000]} if handoff else None,
                "session": session,
                "possible_unexpected_exit": unexpected,
            }
        )
    return out


def summarise_project(ctx: Context) -> dict[str, Any] | None:
    result = ctx.results.get("project_items") or {}
    if not result.get("data"):
        return None
    project = ctx.config["project"]
    order = {name: i for i, name in enumerate(project["priority_order"])}
    items = [i for i in result["data"]["items"] if not i["archived"] and i["state"] != "CLOSED"]

    def brief(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "ref": item_ref(item, ctx.repo),
            "title": clip(item["title"], 100),
            "status": item["status"],
            "priority": item["priority"],
            "sub_issues": f"{item['sub_completed']}/{item['sub_total']}" if item["sub_total"] else None,
            "parent": item["parent"],
            "url": item["url"],
        }

    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"] or "(none)"] = counts.get(item["status"] or "(none)", 0) + 1
    # Within one Priority group, card order is the work order; keep API order (stable sort).
    planned = sorted(
        (i for i in items if i["status"] == project["planned_status"]),
        key=lambda i: order.get(i["priority"] or "", len(order)),
    )
    return {
        "title": result["data"]["title"],
        "url": result["data"]["url"],
        "open_counts": counts,
        "in_progress": [brief(i) for i in items if i["status"] == project["in_progress_status"]][:10],
        "planned_next_up": [brief(i) for i in planned][:5],
    }


# Each kind of next-action candidate and the sources it needs. A candidate whose sources
# aren't all ok must not be suggested: missing data is unknown, never "nothing there".
CANDIDATE_SOURCES = {
    "urgent_finding": ["open_prs", "workflow_warnings"],
    "next_plan_task": ["local_git", "local_context"],
    "ready_pr": ["open_prs"],
    "planned_item": ["project_items"],
    "hygiene_item": ["project_items"],
}


def suggestion_inputs(ctx: Context) -> dict[str, Any]:
    available, unavailable = [], {}
    for kind, sources in CANDIDATE_SOURCES.items():
        missing = [f"{s}: {ctx.results.get(s, {}).get('status', 'not run')}" for s in sources
                   if ctx.results.get(s, {}).get("status") != "ok"]
        if missing:
            unavailable[kind] = missing
        else:
            available.append(kind)
    return {"available": available, "unavailable": unavailable}


def build_digest(ctx: Context, snapshot_path: Path) -> dict[str, Any]:
    now = now_utc()
    prs = data_of(ctx, "open_prs").get("prs", [])
    digest: dict[str, Any] = {
        "schema": "eng.digest.v1",
        "repo": ctx.repo,
        "collected_at": iso(now),
        "snapshot_path": str(snapshot_path),
        "sources": {name: {k: v for k, v in r.items() if k != "data"} for name, r in ctx.results.items()},
        "unknown": sorted(name for name, r in ctx.results.items() if r["status"] != "ok"),
        "suggestion_inputs": suggestion_inputs(ctx),
        "base": data_of(ctx, "local_git").get("base"),
        "project": summarise_project(ctx),
        "prs": [
            {
                "ref": f"#{p['number']}",
                "title": clip(p["title"], 100),
                "draft": p["draft"],
                "checks": p["checks"],
                "mergeable": p["mergeable"],
                "review": p["review"],
                "closes": p["closes"],
                "author": p["author"],
                "dependabot": p["is_dependabot"],
                "auto_merge": p["auto_merge"],
                "head": p["head"],
                "url": p["url"],
            }
            for p in prs[:10]
        ],
        "worktrees": summarise_worktrees(ctx, now),
        "findings": sorted(build_findings(ctx), key=lambda f: (f["severity"], f["id"]))[:30],
    }
    return shrink(digest)


def shrink(digest: dict[str, Any]) -> dict[str, Any]:
    """Keep the digest small enough to read in one go (about 6k tokens)."""
    if len(json.dumps(digest)) <= DIGEST_MAX_CHARS:
        return digest
    digest["trimmed"] = True
    digest["findings"] = digest["findings"][:15]
    digest["prs"] = digest["prs"][:5]
    for tree in digest["worktrees"]:
        tree["dirty_files"] = tree["dirty_files"][:5]
        if tree.get("handoff"):
            tree["handoff"]["text"] = tree["handoff"]["text"][:800]
    return digest


# =========================================================================== run


def run_ops(ctx: Context) -> None:
    schema = engcore.load_schema()
    for name in PROFILES[ctx.args.profile]["ops"]:
        started = time.monotonic()
        try:
            if name in GITHUB_OPS and not ctx.github:
                status, data, extra = "unconfigured", None, {"error": "no github.com 'origin' remote; local sources only"}
            else:
                status, data, extra = OPS[name](ctx)
        except engcore.BudgetExceeded as error:
            status, data, extra = "skipped", None, {"error": str(error)}
        except SourceError as error:
            status, data, extra = "error", None, {"error": str(error)}
        except UnsafeOperation:
            raise
        except Exception as error:  # a bug in one source must not hide the others
            status, data, extra = "error", None, {"error": f"internal error: {type(error).__name__}: {str(error)[:150]}"}
        if data is not None:
            problems = engcore.validate(data, {"$ref": f"#/$defs/{name}"}, schema)
            if problems:
                # Never trust half-valid data: drop it and say why.
                status, data, extra = "error", None, {"error": "schema mismatch: " + "; ".join(problems[:3])}
        result = {"status": status, **extra, "elapsed_ms": int((time.monotonic() - started) * 1000)}
        if data is not None:
            result["data"] = data
        ctx.results[name] = result


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


def check_out_path(out: Path) -> None:
    """The snapshot holds private context: refuse a folder git tracks and doesn't ignore."""
    probe = out.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    code = git_ok(["check-ignore", "-q", "--no-index", str(out.resolve() if out.exists() else out.absolute())], probe)
    if code == 1:
        raise Fatal(f"{out} is inside a git repository and not ignored; write it under the state directory or a git-ignored folder")


def exit_code(results: dict[str, dict[str, Any]]) -> int:
    return 0 if all(r["status"] == "ok" for r in results.values()) else 3


def collect(args: argparse.Namespace) -> int:
    main_root, repo, github = resolve_repo(args.repo.resolve())
    config = load_config(find_config(args.config, main_root, repo), repo)
    ctx = Context(args, main_root, repo, config, github)
    engcore.vet_all()

    if args.out:
        check_out_path(args.out)
    latest = ctx.state / "snapshots" / f"{args.profile}-latest.json"
    key = config_hash({"config": config, "version": COLLECTOR_VERSION, "repo_path": str(main_root)})

    if not args.fresh and latest.is_file() and time.time() - latest.stat().st_mtime < CACHE_TTL_S:
        try:
            cached = json.loads(latest.read_text(encoding="utf-8"))
            if cached.get("cache_key") == key:
                cached["digest"]["from_cache"] = True
                print(json.dumps(cached["digest"], ensure_ascii=False, indent=1))
                return exit_code(cached["sources"])
        except (OSError, ValueError, KeyError):
            pass

    run_ops(ctx)
    snapshot = {
        "schema": "eng.snapshot.v1",
        "collected_at": iso(now_utc()),
        "profile": args.profile,
        "repo": repo,
        "collector_version": COLLECTOR_VERSION,
        "config_source": config.get("source"),
        "cache_key": key,
        "budget": {"requests": ctx.budget.requests, "elapsed_ms": ctx.budget.elapsed_ms(), "rate_remaining": ctx.budget.rate_remaining},
        "sources": {name: {k: v for k, v in r.items() if k != "data"} for name, r in ctx.results.items()},
        "data": {name: r.get("data") for name, r in ctx.results.items()},
        "digest": build_digest(ctx, args.out or latest),
    }
    problems = engcore.validate(snapshot, engcore.load_schema())
    if problems:
        raise Fatal("internal error: snapshot failed its own schema: " + "; ".join(problems[:3]))
    text = json.dumps(snapshot, ensure_ascii=False, indent=1)
    engcore.atomic_write_text(latest, text)
    if args.out:
        engcore.atomic_write_text(args.out, text)
    print(json.dumps(snapshot["digest"], ensure_ascii=False, indent=1))
    code = exit_code(ctx.results)
    if code:
        missing = ", ".join(f"{n} ({r['status']})" for n, r in ctx.results.items() if r["status"] != "ok")
        print(f"note: some sources are missing or partial: {missing}. Treat them as unknown.", file=sys.stderr)
    return code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect a read-only snapshot of a repository for project-pickup. Prints a JSON digest.",
        epilog=(
            "Exit codes: 0 every source ok · 1 fatal error · 2 usage error · "
            "3 snapshot written, some sources missing or partial (treat as unknown)."
        ),
    )
    parser.add_argument("--profile", choices=sorted(PROFILES), default="pickup", help="fixed list of read operations (default: pickup)")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="any path inside the repository (default: current directory)")
    parser.add_argument("--config", type=Path, help="config file (default: <repo>/.eng.json, then ~/.config/eng/repos/<owner>__<name>.json)")
    parser.add_argument("--fresh", action="store_true", help=f"ignore the {CACHE_TTL_S // 60}-minute cache")
    parser.add_argument("--out", type=Path, help="snapshot path (default: ~/.local/state/eng/<owner>__<name>/snapshots/)")
    parser.add_argument("--token-file", type=Path, default=engcore.config_dir() / "read.token", help="read-only GitHub token (default: ~/.config/eng/read.token)")
    parser.add_argument(
        "--project-token-file",
        type=Path,
        default=engcore.config_dir() / "read-project.token",
        help="optional token used only for the Project, if the main token can't read it (default: ~/.config/eng/read-project.token)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return collect(args)
    except Fatal as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except UnsafeOperation as error:
        print(f"ERROR: refused an unsafe operation: {error}. This is a bug in the collector.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
