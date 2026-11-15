#!/usr/bin/env python3
"""Save a handoff note for the current branch, so the next pickup knows where you stopped.

Reads the note (Markdown) from stdin and writes it under the private state directory:
~/.local/state/eng/<owner>__<name>/handoff/<branch>/<UTC timestamp>.md
Older notes are kept. Nothing is written to the repository, git or GitHub.

Exit codes: 0 saved · 1 error (not a repository, empty or oversized note) · 2 usage error.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import engcore  # noqa: E402

MAX_NOTE_BYTES = 20 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Save a handoff note (Markdown on stdin) for the current branch in the private state directory.",
        epilog="Exit codes: 0 saved · 1 error · 2 usage error.",
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="any path inside the worktree (default: current directory)")
    parser.add_argument("--branch", help="branch name (default: the worktree's current branch)")
    args = parser.parse_args(argv)

    try:
        root = Path(engcore.git(["rev-parse", "--show-toplevel"], args.repo.resolve()).strip())
        repo = engcore.parse_github_remote(engcore.git(["remote", "get-url", "origin"], root))
        branch = args.branch or engcore.git(["rev-parse", "--abbrev-ref", "HEAD"], root).strip()
        head = engcore.git(["rev-parse", "HEAD"], root).strip()
    except engcore.SourceError as error:
        print(f"ERROR: {error}. Run this from inside the repository's worktree.", file=sys.stderr)
        return 1
    if not repo:
        print("ERROR: the 'origin' remote is not a github.com repository.", file=sys.stderr)
        return 1
    if branch == "HEAD":
        print("ERROR: detached HEAD; pass --branch <name>.", file=sys.stderr)
        return 1

    raw = sys.stdin.buffer.read(MAX_NOTE_BYTES + 1)
    if len(raw) > MAX_NOTE_BYTES:
        print(f"ERROR: the note is larger than {MAX_NOTE_BYTES // 1024} KB; keep it short.", file=sys.stderr)
        return 1
    note = raw.decode("utf-8", errors="replace").strip()
    if not note:
        print("ERROR: the note is empty; pass it on stdin.", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    header = (
        "---\n"
        "parked: true\n"
        f"written_at: {now.isoformat().replace('+00:00', 'Z')}\n"
        f"branch: {branch}\n"
        f"head: {head}\n"
        f"worktree: {root}\n"
        "---\n\n"
    )
    target = engcore.state_dir() / engcore.repo_slug(repo) / "handoff" / engcore.safe_name(branch) / f"{stamp}.md"
    engcore.atomic_write_text(target, header + note + "\n")
    print(str(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
