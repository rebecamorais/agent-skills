"""Test helpers: a temporary repository with worktrees and a fake GitHub transport.

Nothing here touches the network or a real repository.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collect  # noqa: E402
import engcore  # noqa: E402

OLD_DATE = "2026-01-01T12:00:00+00:00"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_AUTHOR_DATE": OLD_DATE,
    "GIT_COMMITTER_DATE": OLD_DATE,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def sh(args: list[str], cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, **GIT_ENV}).stdout


# ----------------------------------------------------------------------------- fixtures


def item(number, title, state="OPEN", status=None, priority=None, sub=(0, 0), kind="Issue"):
    values = [{"__typename": "ProjectV2ItemFieldTextValue"}]
    if status:
        values.append({"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": status, "field": {"name": "Status"}})
    if priority:
        values.append({"__typename": "ProjectV2ItemFieldSingleSelectValue", "name": priority, "field": {"name": "Priority"}})
    content = {"__typename": kind, "title": title, "updatedAt": "2026-11-01T00:00:00Z"}
    if kind == "Issue":
        content.update(
            number=number, url=f"https://github.com/acme/widgets/issues/{number}", state=state,
            stateReason="COMPLETED" if state == "CLOSED" else None, closedAt=None,
            repository={"nameWithOwner": "acme/widgets"}, labels={"nodes": [{"name": "enhancement"}]},
            parent=None, subIssuesSummary={"total": sub[1], "completed": sub[0]},
            closedByPullRequestsReferences={"nodes": []},
        )
    return {"id": f"PVTI_{number}", "isArchived": False, "type": kind.upper(), "updatedAt": "2026-11-01T00:00:00Z",
            "fieldValues": {"nodes": values}, "content": content}


PROJECT_ITEMS = [
    item(10, "Character sheet export", status="In progress", priority="Now"),
    item(11, "Spell filters", status="Planned"),
    item(12, "Old bug", state="CLOSED", status="Planned", priority="Later"),
    item(13, "Party notes", status="Planned", priority="Next", sub=(1, 3)),
    item(14, "Dice roller", status="Planned", priority="Now"),
    item(None, "A loose idea", kind="DraftIssue"),
]


def pr(number, author="acme", checks="SUCCESS", mergeable="MERGEABLE", auto=None, draft=False):
    return {
        "number": number, "title": f"PR {number}", "url": f"https://github.com/acme/widgets/pull/{number}",
        "isDraft": draft, "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-02T00:00:00Z",
        "headRefName": f"branch-{number}", "baseRefName": "main", "mergeable": mergeable,
        "reviewDecision": "REVIEW_REQUIRED", "author": {"login": author}, "autoMergeRequest": auto,
        "labels": {"nodes": []}, "closingIssuesReferences": {"nodes": [{"number": 10}]},
        "commits": {"nodes": [{"commit": {"oid": "a" * 40, "statusCheckRollup": {"state": checks}}}]},
    }


OPEN_PRS = [pr(20, checks="FAILURE", mergeable="CONFLICTING"), pr(21, author="dependabot")]


class FakeGitHub:
    """Routes requests by operation name or REST path. Records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.kinds: list[str] = []  # "project", "prs" or "rest", one per call
        self.overrides: dict[str, object] = {}
        self.always_next_page = False

    def __call__(self, request, timeout):
        method = request.get_method()
        url = request.full_url
        body = json.loads(request.data) if request.data else {}
        self.calls.append((method, url, dict(request.header_items())))
        query = body.get("query", "")
        self.kinds.append("project" if "query ProjectItems" in query else "prs" if "query OpenPrs" in query else "rest")
        for key, value in self.overrides.items():
            if key in url or key in query:
                if callable(value):
                    return value(request)
                return value
        if "query ProjectItems" in query:
            page = {"hasNextPage": self.always_next_page, "endCursor": "c1"}
            return self._json({"data": {"owner": {"projectV2": {"title": "Roadmap", "url": "https://github.com/users/acme/projects/1",
                    "items": {"totalCount": len(PROJECT_ITEMS), "pageInfo": page, "nodes": PROJECT_ITEMS}}},
                    "rateLimit": {"cost": 1, "remaining": 4999, "resetAt": "x"}}})
        if "query OpenPrs" in query:
            return self._json({"data": {"repository": {"defaultBranchRef": {"name": "main"},
                    "pullRequests": {"totalCount": len(OPEN_PRS), "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": OPEN_PRS}},
                    "rateLimit": {"cost": 1, "remaining": 4998, "resetAt": "x"}}})
        if "/actions/workflows/" in url:
            return self._json({"workflow_runs": [{"id": 5, "name": "Dependabot Auto-merge", "html_url": "https://github.com/acme/widgets/actions/runs/5",
                                                  "conclusion": "success", "created_at": "2026-11-10T00:00:00Z"}]})
        if url.endswith("/jobs?per_page=30"):
            return self._json({"jobs": [{"id": 7, "name": "next", "conclusion": "success"}, {"id": 8, "name": "approve", "conclusion": "skipped"}]})
        if "/check-runs/7/annotations" in url:
            return self._json([{"annotation_level": "warning", "message": "AUTOMERGE_TOKEN not set; skipping."},
                               {"annotation_level": "notice", "message": "runner image notice"}])
        return 404, {}, b'{"message":"Not Found"}'

    @staticmethod
    def _json(payload, status=200):
        return status, {"content-type": "application/json"}, json.dumps(payload).encode()

    def methods(self) -> set[str]:
        return {method for method, _, _ in self.calls}


# ----------------------------------------------------------------------------- base test case


class RepoTestCase(unittest.TestCase):
    """A repo `acme/widgets` with main, a feature worktree (plan + dirty file) and a locked one."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pickup-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.main = self.tmp / "widgets"
        self.main.mkdir()
        sh(["git", "init", "-q", "-b", "main"], self.main)
        sh(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], self.main)
        (self.main / "README.md").write_text("hi\n")
        sh(["git", "add", "."], self.main)
        sh(["git", "commit", "-q", "-m", "init"], self.main)

        self.feature = self.tmp / "wt-feature"
        sh(["git", "worktree", "add", "-q", "-b", "feat/export", str(self.feature)], self.main)
        plan = self.feature / "docs" / "plans" / "export-plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n- [x] Task 1: schema\n- [ ] Task 2: endpoint\n- [ ] Task 3: UI\n")
        sh(["git", "add", "."], self.feature)
        sh(["git", "commit", "-q", "-m", "plan"], self.feature)
        (self.feature / "wip.txt").write_text("unsaved\n")

        self.locked = self.tmp / "wt-locked"
        sh(["git", "worktree", "add", "-q", "-b", "feat/locked", str(self.locked)], self.main)
        sh(["git", "worktree", "lock", "--reason", "owned by another session", str(self.locked)], self.main)

        self.state = self.tmp / "state"
        self.config_dir = self.tmp / "config"
        self.projects = self.tmp / "claude-projects"
        (self.config_dir / "repos").mkdir(parents=True)
        self.token = self.config_dir / "read.token"
        self.token.write_text("test-token-not-real\n")
        os.chmod(self.token, 0o600)
        self.write_config({
            "schema": "eng.config.v1", "repo": "acme/widgets", "default_branch": "main",
            "project": {"owner": "acme", "number": 1},
            "watch_workflows": ["dependabot-auto-merge.yml"],
        })
        env = mock.patch.dict(os.environ, {"ENG_STATE_DIR": str(self.state), "ENG_CONFIG_DIR": str(self.config_dir),
                                           "ENG_CLAUDE_PROJECTS_DIR": str(self.projects)})
        env.start()
        self.addCleanup(env.stop)
        self.fake = FakeGitHub()
        for name, value in (("TRANSPORT", self.fake), ("SLEEP", lambda s: None)):
            patcher = mock.patch.object(collect, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_config(self, config: dict) -> None:
        (self.config_dir / "repos" / "acme__widgets.json").write_text(json.dumps(config))

    def run_collect(self, *extra: str) -> tuple[int, dict | None, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = collect.main(["--repo", str(self.main), "--token-file", str(self.token),
                                 "--project-token-file", str(self.config_dir / "none.token"), *extra])
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None), err.getvalue()

    def snapshot(self) -> dict:
        return json.loads((self.state / "acme__widgets" / "snapshots" / "pickup-latest.json").read_text())
