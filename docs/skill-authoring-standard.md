# Skill authoring standard

Every skill in this repository follows these rules. `python3 scripts/validate_skills.py` checks the ones marked **(checked)**.

## Principles

- **Agent-agnostic.** Write for any agent that supports the [Agent Skills](https://agentskills.io) format. Don't depend on one agent's tools ("use the Read tool"), slash commands or file paths, except as install examples.
- **Public-safe.** No company names, internal CLIs, account IDs, hostnames or real queue/bucket names. Use placeholders like `111111111111` and `orders-dlq`.
- **English** for everything: `SKILL.md`, docs, script output, help text and code comments.
- **Safe by default.** If a skill touches external systems, read-only is the default. Destructive actions need an explicit user confirmation step, and `SKILL.md` must say what the agent must never run.

## Layout

```
skills/<skill-name>/
├── SKILL.md          # required (checked)
├── docs.md           # required (checked)
├── evals/evals.json  # required (checked)
├── scripts/          # optional: Python helpers
└── references/       # optional: long material the agent reads on demand
```

Start from [`template/`](../template/).

## `SKILL.md`

Frontmatter:

| Field | Rule |
| --- | --- |
| `name` | Required. Lowercase letters, digits and hyphens, at most 64 characters, equal to the folder name **(checked)** |
| `description` | Required. At most 1024 characters **(checked)**. Starts with "Use when" **(checked)**, then the situations and the phrases users actually say that should trigger the skill |
| `compatibility` | Recommended when the skill needs runtimes or tools, e.g. `Requires Python 3.10+ and AWS CLI v2.` |

Body:

- Under 500 lines **(checked)**. Move long reference material to `references/` and say when to read it.
- Use `<skill>` for the skill's own directory in commands: `python3 <skill>/scripts/foo.py`.
- Recommended sections: what the skill does, prerequisites, rules (what never to do), usage, exit codes, and common errors with the action for each.
- Tell the agent what to ask the user instead of letting it assume defaults (profiles, environments, destinations).

## Scripts

- Python 3.10+, **standard library only**, so nothing needs installing. Call external CLIs (e.g. `aws`) through `subprocess` when needed.
- Every `scripts/*.py` must compile **(checked)**.
- `argparse` with `-h` help and an epilog listing the exit codes.
- Documented exit codes, the same in `--help` and `SKILL.md`. `0` is success and `1` is a generic error. argparse exits with `2` on usage errors, so either keep `2` for usage or override `ArgumentParser.error`.
- Errors go to stderr, prefixed with `ERROR:`, with a hint about how to fix them.
- Write output files atomically (temp file + `os.replace`). If the output may contain personal data, refuse to write into a folder that git does not ignore.

## `docs.md`

A short guide for people browsing the repo: what the skill does, requirements, installation, examples of prompts that trigger it, direct script usage, side effects and limitations. Keep it under ~100 lines.

## `evals/evals.json`

At least two cases **(checked)**: one typical request and one where the skill must refuse or ask first (destructive action, risky input).

```json
{
  "skill_name": "<skill-name>",
  "evals": [
    { "id": 0, "prompt": "What the user says", "expected_output": "What the agent must and must not do", "files": [] }
  ]
}
```

`skill_name` must match the folder **(checked)**.

## Before opening a PR

- [ ] `python3 scripts/validate_skills.py` passes
- [ ] Scripts tested without touching real infrastructure (fake CLI on `PATH`, synthetic data)
- [ ] Evals run against at least one agent
- [ ] Skill listed in the root `README.md`
- [ ] No internal names, credentials or real identifiers
