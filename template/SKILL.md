---
name: my-skill-name
description: Use when the user asks to [task] or says things like "[phrase 1]", "[phrase 2]". Also when [other trigger].
compatibility: Requires Python 3.10+ and [tool].
---

# My skill name

One or two sentences on what this skill does and what it does not do.

## Prerequisites

- `python3` >= 3.10 (standard library only) and [tool] in `PATH`.
- [Credentials or access needed. Tell the agent to ask the user instead of guessing.]

## Rules

- **Never** [destructive action]. If asked to, say this skill does not do that and explain the manual alternative.
- [Anything the agent must confirm with the user first.]

## Usage

```bash
python3 <skill>/scripts/my_script.py <arg> [--option value]
```

- `<skill>` is this skill's directory.
- [What each option does and its default.]

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Error |

## Common errors

| Situation | Action |
| --- | --- |
| [Error message] | [What to do] |
