"""End-to-end tests of collect.py against a temporary repo and a fake GitHub."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import RepoTestCase, sh

import collect
import engcore

HANDOFF = Path(helpers.SCRIPTS, "handoff.py")


def finding_ids(digest):
    return {f["id"] for f in digest["findings"]}


class FullRunTest(RepoTestCase):
    def test_happy_path(self):
        code, digest, err = self.run_collect("--fresh")
        self.assertEqual(code, 0, err)
        self.assertEqual(digest["unknown"], [])
        snapshot = self.snapshot()
        self.assertEqual(engcore.validate(snapshot, engcore.load_schema()), [])

        # Only GETs and POSTs to /graphql went out, all authenticated with the read token.
        self.assertLessEqual(self.fake.methods(), {"GET", "POST"})
        for method, url, headers in self.fake.calls:
            if method == "POST":
                self.assertEqual(url, "https://api.github.com/graphql")
            self.assertEqual(headers.get("Authorization"), "Bearer test-token-not-real")

        ids = finding_ids(digest)
        for expected in (
            "project:no-priority:acme/widgets#11",
            "project:closed-not-done:acme/widgets#12",
            "pr:checks-failing:acme/widgets#20",
            "pr:conflicts:acme/widgets#20",
            "maintenance:dependabot-automerge-off:acme/widgets#21",
        ):
            self.assertIn(expected, ids)
        warnings = [f for f in digest["findings"] if f["id"].startswith("maintenance:workflow-warning:")]
        self.assertEqual(len(warnings), 1, "notice-level annotations must be ignored")
        self.assertIn("AUTOMERGE_TOKEN not set", warnings[0]["title"])

        project = digest["project"]
        self.assertEqual([i["ref"] for i in project["in_progress"]], ["#10"])
        # Planned items ordered by Priority (Now, Next, ...), items without Priority last.
        self.assertEqual([i["ref"] for i in project["planned_next_up"]], ["#14", "#13", "#11"])
        self.assertEqual(project["planned_next_up"][1]["sub_issues"], "1/3")

        trees = {t["branch"]: t for t in digest["worktrees"]}
        self.assertEqual(set(trees), {"main", "feat/export", "feat/locked"})
        self.assertTrue(trees["feat/locked"]["locked"])
        feature = trees["feat/export"]
        self.assertEqual(feature["dirty_files"], ["wip.txt"])
        self.assertEqual(feature["ahead_base"], 1)
        self.assertEqual(feature["plan"], {"file": "docs/plans/export-plan.md", "done": 1, "total": 3,
                                           "next_tasks": ["Task 2: endpoint", "Task 3: UI"]})
        self.assertEqual(feature["ramp_up_min"], 30, "last touched long ago -> +30 min ramp-up")

    def test_every_emitted_text_is_pressure_free(self):
        _, digest, _ = self.run_collect("--fresh")
        texts = [f["title"] for f in digest["findings"]]
        texts += [json.dumps(s) for s in digest["sources"].values()]
        for text in texts:
            self.assertEqual(engcore.banned_words_in(text), [], text)

    def test_local_git_changes_nothing(self):
        def fingerprint():
            files = {}
            for root in (self.main / ".git", self.locked, self.feature):
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        files[str(path)] = (path.stat().st_mtime_ns, path.stat().st_size)
            return files

        sh(["git", "status"], self.main)  # let git settle its own index once
        before = fingerprint()
        time.sleep(0.01)
        self.run_collect("--fresh")
        self.assertEqual(before, fingerprint(), "the collector must not touch the repo, index or worktrees")
        self.assertIn("locked", sh(["git", "worktree", "list", "--porcelain"], self.main))


class DegradedTest(RepoTestCase):
    def test_no_token_is_unknown_not_fine(self):
        self.token.unlink()
        code, digest, err = self.run_collect("--fresh")
        self.assertEqual(code, 3)
        self.assertEqual(set(digest["unknown"]), {"project_items", "open_prs", "workflow_warnings"})
        self.assertEqual(digest["sources"]["local_git"]["status"], "ok")
        self.assertIn("not found", digest["sources"]["open_prs"]["error"])
        self.assertIsNone(digest["project"])
        self.assertEqual(self.fake.calls, [])
        self.assertIn("unknown", err)

    def test_open_token_file_is_refused(self):
        os.chmod(self.token, 0o644)
        code, digest, _ = self.run_collect("--fresh")
        self.assertEqual(code, 3)
        self.assertIn("chmod 600", digest["sources"]["open_prs"]["error"])

    def test_schema_mismatch_drops_only_that_source(self):
        bad = helpers.pr(30)
        bad["number"] = "thirty"
        self.fake.overrides["query OpenPrs"] = helpers.FakeGitHub._json(
            {"data": {"repository": {"defaultBranchRef": {"name": "main"}, "pullRequests": {
                "totalCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": [bad]}}}})
        code, digest, _ = self.run_collect("--fresh")
        self.assertEqual(code, 3)
        self.assertEqual(digest["sources"]["open_prs"]["status"], "error")
        self.assertIn("schema mismatch", digest["sources"]["open_prs"]["error"])
        self.assertEqual(digest["prs"], [])
        self.assertEqual(digest["sources"]["project_items"]["status"], "ok")
        self.assertIsNone(self.snapshot()["data"]["open_prs"])

    def test_server_errors_mark_source_error(self):
        self.fake.overrides["query OpenPrs"] = (502, {}, b"bad gateway")
        code, digest, _ = self.run_collect("--fresh")
        self.assertEqual(code, 3)
        self.assertIn("HTTP 502", digest["sources"]["open_prs"]["error"])

    def test_pagination_is_capped_and_marked_truncated(self):
        self.fake.always_next_page = True
        code, digest, _ = self.run_collect("--fresh")
        self.assertEqual(code, 0)
        self.assertTrue(digest["sources"]["project_items"]["truncated"])
        pages = [c for c in self.fake.calls if c[0] == "POST"]
        self.assertEqual(len(pages), 10 + 1, "10 project pages + 1 PR page")

    def test_budget_exhaustion_skips_remaining_sources(self):
        with mock.patch.dict(collect.PROFILES["pickup"], {"max_requests": 1}):
            code, digest, _ = self.run_collect("--fresh")
        self.assertEqual(code, 3)
        self.assertEqual(digest["sources"]["project_items"]["status"], "ok")
        self.assertEqual(digest["sources"]["open_prs"]["status"], "skipped")
        self.assertEqual(digest["sources"]["local_git"]["status"], "ok")

    def test_unconfigured_project(self):
        self.write_config({"schema": "eng.config.v1", "repo": "acme/widgets"})
        code, digest, _ = self.run_collect("--fresh")
        self.assertEqual(code, 3)
        self.assertEqual(digest["sources"]["project_items"]["status"], "unconfigured")
        self.assertEqual(digest["sources"]["workflow_warnings"]["status"], "unconfigured")
        self.assertEqual(digest["sources"]["open_prs"]["status"], "ok")


class ConfigAndOutputTest(RepoTestCase):
    def test_config_for_another_repo_is_fatal(self):
        self.write_config({"schema": "eng.config.v1", "repo": "acme/other"})
        code, digest, err = self.run_collect("--fresh")
        self.assertEqual(code, 1)
        self.assertIn("is for acme/other", err)

    def test_config_cannot_escape_the_repo(self):
        for bad in ({"globs": ["../secrets/*.md"]}, {"globs": ["/etc/*"]}):
            self.write_config({"schema": "eng.config.v1", "repo": "acme/widgets", "plans": bad})
            code, _, err = self.run_collect("--fresh")
            self.assertEqual(code, 1, err)

    def test_unknown_config_keys_and_workflow_paths_are_refused(self):
        for bad in ({"command": "rm -rf /"}, {"watch_workflows": ["../../evil"]}):
            self.write_config({"schema": "eng.config.v1", "repo": "acme/widgets", **bad})
            code, _, err = self.run_collect("--fresh")
            self.assertEqual(code, 1, err)
            self.assertIn("invalid", err)

    def test_not_a_repo_is_fatal(self):
        out = io.StringIO()
        with mock.patch("sys.stderr", out):
            code = collect.main(["--repo", str(self.tmp)])
        self.assertEqual(code, 1)
        self.assertIn("ERROR:", out.getvalue())

    def test_refuses_to_write_snapshot_into_tracked_folder(self):
        code, _, err = self.run_collect("--fresh", "--out", str(self.main / "snapshot.json"))
        self.assertEqual(code, 1)
        self.assertIn("not ignored", err)
        self.assertFalse((self.main / "snapshot.json").exists())

    def test_writes_snapshot_into_ignored_folder(self):
        (self.main / ".gitignore").write_text("private/\n")
        code, _, err = self.run_collect("--fresh", "--out", str(self.main / "private" / "s.json"))
        self.assertEqual(code, 0, err)
        self.assertTrue((self.main / "private" / "s.json").is_file())

    def test_state_files_are_private(self):
        self.run_collect("--fresh")
        path = self.state / "acme__widgets" / "snapshots" / "pickup-latest.json"
        for item in (path, path.parent, path.parent.parent, self.state):
            self.assertEqual(item.stat().st_mode & 0o077, 0, item)
        self.assertFalse((self.state / "acme__widgets" / "cache").exists(), "the snapshot is the cache")


class CacheTest(RepoTestCase):
    def test_second_run_uses_cache_and_is_stable(self):
        _, first, _ = self.run_collect()
        calls = len(self.fake.calls)
        code, second, _ = self.run_collect()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.calls), calls, "cached run must not call GitHub")
        self.assertTrue(second["from_cache"])
        self.assertEqual(finding_ids(first), finding_ids(second))

        _, third, _ = self.run_collect("--fresh")
        self.assertGreater(len(self.fake.calls), calls)
        self.assertEqual(finding_ids(first), finding_ids(third), "finding IDs are stable across runs")

    def test_config_change_invalidates_cache(self):
        self.run_collect()
        calls = len(self.fake.calls)
        self.write_config({"schema": "eng.config.v1", "repo": "acme/widgets", "project": {"owner": "acme", "number": 2}})
        self.run_collect()
        self.assertGreater(len(self.fake.calls), calls)


class RecoveryTest(RepoTestCase):
    SESSION = "0b6c2f1e-1111-4222-8333-944455556666"

    def make_session(self, age_s: int = 60) -> Path:
        folder = self.projects / collect.claude_project_dir_name(str(self.feature))
        folder.mkdir(parents=True)
        path = folder / f"{self.SESSION}.jsonl"
        path.write_text('{"type":"internal format, never parsed"}\n')
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
        return path

    def feature_tree(self, digest):
        return next(t for t in digest["worktrees"] if t["branch"] == "feat/export")

    def test_session_after_last_commit_with_dirty_files_is_flagged(self):
        self.make_session()
        _, digest, _ = self.run_collect("--fresh")
        tree = self.feature_tree(digest)
        self.assertTrue(tree["possible_unexpected_exit"])
        self.assertEqual(tree["session"]["resume_command"], f"claude --resume {self.SESSION}")
        self.assertEqual(tree["ramp_up_min"], 0)

    def test_handoff_after_session_clears_the_flag(self):
        self.make_session()
        result = subprocess.run([sys.executable, str(HANDOFF), "--repo", str(self.feature)],
                                input="Next: Task 2 endpoint. Stop point: after tests pass.", capture_output=True, text=True,
                                env={**os.environ})
        self.assertEqual(result.returncode, 0, result.stderr)
        note = Path(result.stdout.strip())
        self.assertTrue(note.is_file())
        self.assertIn("parked: true", note.read_text())
        self.assertIn("handoff/feat-export/", str(note))

        _, digest, _ = self.run_collect("--fresh")
        tree = self.feature_tree(digest)
        self.assertFalse(tree["possible_unexpected_exit"])
        self.assertIn("Task 2 endpoint", tree["handoff"]["text"])

    def test_old_session_has_no_resume_hint(self):
        self.make_session(age_s=40 * 86400)
        _, digest, _ = self.run_collect("--fresh")
        self.assertIsNone(self.feature_tree(digest)["session"]["resume_command"])

    def test_clean_worktree_is_not_flagged(self):
        (self.feature / "wip.txt").unlink()
        self.make_session()
        _, digest, _ = self.run_collect("--fresh")
        self.assertFalse(self.feature_tree(digest)["possible_unexpected_exit"])


class HandoffTest(RepoTestCase):
    def run_handoff(self, text: str):
        return subprocess.run([sys.executable, str(HANDOFF), "--repo", str(self.feature)], input=text,
                              capture_output=True, text=True, env={**os.environ})

    def test_empty_and_oversized_notes_are_refused(self):
        self.assertEqual(self.run_handoff("   ").returncode, 1)
        self.assertEqual(self.run_handoff("x" * (21 * 1024)).returncode, 1)
        self.assertFalse((self.state / "acme__widgets" / "handoff").exists())

    def test_notes_are_kept_not_overwritten(self):
        self.assertEqual(self.run_handoff("first").returncode, 0)
        time.sleep(1.1)
        self.assertEqual(self.run_handoff("second").returncode, 0)
        notes = sorted((self.state / "acme__widgets" / "handoff" / "feat-export").glob("*.md"))
        self.assertEqual(len(notes), 2)


if __name__ == "__main__":
    unittest.main()
