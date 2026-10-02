# agent-skills

Agent-agnostic skills for AI coding agents, written in the [Agent Skills](https://agentskills.io) format. Each skill is a folder with a `SKILL.md` that tells the agent when and how to do a task, plus optional Python scripts it can run.

## Table of contents

- [What is a skill?](#what-is-a-skill)
- [Available skills](#available-skills)
- [Installation](#installation)
- [Usage](#usage)
- [Skill structure](#skill-structure)
- [How to create a new skill](#how-to-create-a-new-skill)
- [Contributing](#contributing)
- [License](#license)

## What is a skill?

A skill is a folder of instructions the agent loads only when they are relevant. `SKILL.md` starts with YAML frontmatter that has two required fields:

- **`name`**: a unique identifier (lowercase, hyphens), the same as the folder name.
- **`description`**: what the skill does and when to use it. The agent reads only this to decide whether to load the skill, so it starts with "Use when…" and lists the phrases that should trigger it.

The rest of the file has the instructions the agent follows once the skill is active.

## Available skills

| Skill | Description |
| --- | --- |
| [sqs-peek-messages](skills/sqs-peek-messages/) | Read messages from an AWS SQS queue (DLQ or not) without consuming them |

### sqs-peek-messages

Reads messages from any AWS SQS queue and saves one `<MessageId>.json` per message, without deleting anything. Before reading, it checks the queue and stops to ask for confirmation if reading could move messages to the DLQ, delay a live consumer or lock a FIFO group.

**Use when:**

- You want to know what is in a DLQ, or why messages landed there
- You need to download or inspect messages from a queue without consuming them
- Another skill or project tool needs to read a queue safely

**Output:** a `sqs-messages/<queue>/` folder (which must be git-ignored) with one JSON file per message; the agent reads them and summarizes the errors with their `MessageId`s.

**Requirements:** Python 3.10+, AWS CLI v2 and a profile with `sqs:GetQueueUrl`, `sqs:GetQueueAttributes` and `sqs:ReceiveMessage`.

More in [its docs](skills/sqs-peek-messages/docs.md).

<!--
Per skill, add a table row above and a section like this one:

### skill-name

One or two sentences on what it does and what it never does.

**Use when:**

- ...

**Output:** what the user gets.

**Requirements:** runtimes, tools and permissions.

More in [its docs](skills/skill-name/docs.md).
-->

## Installation

With the [`skills` CLI](https://github.com/vercel-labs/skills), which installs into the skills folder of the agent you choose:

```bash
npx skills add rebecamorais/agent-skills
```

```bash
npx skills add rebecamorais/agent-skills/skills/<skill-name>
```

Or copy a skill folder by hand:

```bash
git clone https://github.com/rebecamorais/agent-skills.git
cp -r agent-skills/skills/<skill-name> ~/.claude/skills/
```

`~/.claude/skills/` is the Claude Code location. Other agents use their own folder, such as `.agents/skills/`.

## Usage

Skills trigger on their own: describe the task in plain language and the agent loads the matching skill. You don't need to name it. Agents without skill support can follow a `SKILL.md` as plain instructions.

## Skill structure

```
skills/<skill-name>/
├── SKILL.md          # Instructions for the agent (required)
├── docs.md           # Short guide for humans (required)
├── scripts/          # Python helper scripts, standard library only (optional)
├── references/       # Supporting docs the agent reads on demand (optional)
└── evals/evals.json  # Test prompts and expected behavior (required)
```

## How to create a new skill

1. Copy the template: `cp -r template skills/<skill-name>`.
2. Fill in `SKILL.md`, `docs.md` and `evals/evals.json` following the [authoring standard](docs/skill-authoring-standard.md).
3. Validate: `python3 scripts/validate_skills.py`.
4. Add a row to [Available skills](#available-skills) and a short section for the skill.

## Contributing

1. Fork the repository.
2. Create a branch for your work.
3. Create or change a skill following the [authoring standard](docs/skill-authoring-standard.md).
4. Run `python3 scripts/validate_skills.py` and the skill's evals.
5. Open a pull request describing what the skill does and how you tested it.

## License

MIT License. See [LICENSE](LICENSE) for details.
