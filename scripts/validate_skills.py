#!/usr/bin/env python3
"""Check every skill in skills/ against docs/skill-authoring-standard.md.

Checks: SKILL.md frontmatter (name, description), body length, required files (docs.md,
evals/evals.json), evals format and that scripts/*.py compile. Standard library only.

Exit codes: 0 all skills pass · 1 at least one problem found.
"""

from __future__ import annotations

import argparse
import json
import py_compile
import re
import sys
import tempfile
from pathlib import Path

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_BODY_LINES = 500
MIN_EVALS = 2


def parse_frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    """Minimal YAML frontmatter reader: `key: value`, quoted values and `>`/`|` block scalars."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None
    fields: dict[str, str] = {}
    key = None
    folded = False
    for line in lines[1:end]:
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match:
            key, value = match.groups()
            folded = value.startswith(">")
            if value[:1] in (">", "|"):
                fields[key] = ""
            else:
                fields[key] = value.strip().strip("'\"")
        elif key and line.startswith((" ", "\t")):
            separator = " " if folded else "\n"
            fields[key] = f"{fields[key]}{separator if fields[key] else ''}{line.strip()}"
    return fields, "\n".join(lines[end + 1 :])


def check_skill(folder: Path) -> list[str]:
    problems = []
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        return ["missing SKILL.md"]

    parsed = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    if parsed is None:
        problems.append("SKILL.md must start with YAML frontmatter between '---' lines")
    else:
        fields, body = parsed
        name = fields.get("name", "")
        description = fields.get("description", "")
        if not name:
            problems.append("frontmatter: missing name")
        elif not NAME.match(name) or len(name) > MAX_NAME:
            problems.append(f"frontmatter: name {name!r} must be lowercase-hyphenated, at most {MAX_NAME} chars")
        elif name != folder.name:
            problems.append(f"frontmatter: name {name!r} must match the folder name {folder.name!r}")
        if not description:
            problems.append("frontmatter: missing description")
        else:
            if len(description) > MAX_DESCRIPTION:
                problems.append(f"frontmatter: description has {len(description)} chars (max {MAX_DESCRIPTION})")
            if not description.startswith("Use when"):
                problems.append('frontmatter: description must start with "Use when"')
        body_lines = len(body.strip().splitlines())
        if body_lines > MAX_BODY_LINES:
            problems.append(f"SKILL.md body has {body_lines} lines (max {MAX_BODY_LINES}); move detail to references/")

    if not (folder / "docs.md").is_file():
        problems.append("missing docs.md")

    evals_file = folder / "evals" / "evals.json"
    if not evals_file.is_file():
        problems.append("missing evals/evals.json")
    else:
        try:
            evals = json.loads(evals_file.read_text(encoding="utf-8"))
            if evals.get("skill_name") != folder.name:
                problems.append(f"evals.json: skill_name must be {folder.name!r}")
            cases = evals.get("evals", [])
            if len(cases) < MIN_EVALS:
                problems.append(f"evals.json: needs at least {MIN_EVALS} cases")
            for case in cases:
                if not case.get("prompt") or not case.get("expected_output"):
                    problems.append(f"evals.json: case {case.get('id')} needs prompt and expected_output")
        except (ValueError, AttributeError) as error:
            problems.append(f"evals.json is not valid: {error}")

    with tempfile.TemporaryDirectory() as tmp:
        for script in sorted((folder / "scripts").glob("*.py")):
            try:
                py_compile.compile(str(script), cfile=str(Path(tmp) / "out.pyc"), doraise=True)
            except py_compile.PyCompileError as error:
                problems.append(f"{script.relative_to(folder)} does not compile: {error.msg}")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate the skills in skills/ against the authoring standard.",
        epilog="Exit codes: 0 all skills pass · 1 at least one problem found.",
    )
    parser.add_argument("skills", nargs="*", type=Path, help="skill folders to check (default: every folder in skills/)")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    skills_dir = root / "skills"
    folders = args.skills or (
        sorted(path for path in skills_dir.iterdir() if path.is_dir()) if skills_dir.is_dir() else []
    )
    if not folders:
        print("No skills found.")
        return

    failed = 0
    for folder in folders:
        problems = check_skill(folder)
        if problems:
            failed += 1
            print(f"FAIL {folder.name}")
            for problem in problems:
                print(f"  - {problem}")
        else:
            print(f"ok   {folder.name}")
    if failed:
        print(f"\n{failed} of {len(folders)} skill(s) have problems.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
