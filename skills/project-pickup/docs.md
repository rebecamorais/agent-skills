# project-pickup

For a solo maintainer coming back to a repository after hours, days or months. It reads the GitHub Project, open PRs, workflow warnings, local worktrees, plan checkboxes and handoff notes **read-only**, says briefly where things stand, and suggests **one** next action that fits the time and energy you have. When you stop, it can save a private handoff note. It never writes to GitHub, git, the Project, secrets or settings, and it uses no subagents. It has no deadlines, scores or streaks.

## Requirements

- Python 3.10+ (standard library only) and git
- Optional: a read-only GitHub token file and a small per-repository config (below)

It works in three setups. Missing sources are always reported as "couldn't check", never as "nothing there":

| Setup | What it reads |
| --- | --- |
| GitHub repo with a Project, token and config | Project, PRs, workflow warnings, local work |
| GitHub repo without a Project (no `project` in the config) | PRs, workflow warnings if configured, local work |
| Local only (no token, or no github.com `origin`) | worktrees, plans, handoffs. The repository ID is `local/<folder>-<hash>`. |

## Installation

```bash
npx skills add rebecamorais/agent-skills/skills/project-pickup
```

Or, from the root of a local clone, link it so updates come with `git pull` (Claude Code path shown; other agents use their own skills folder):

```bash
ln -s "$PWD/skills/project-pickup" ~/.claude/skills/project-pickup
```

### Create the read-only token

1. GitHub → Settings → Developer settings → Fine-grained tokens → Generate. Choose **only** the repository, and grant **read-only** access to: Metadata, Contents, Issues, Pull requests and Actions. Nothing else, and no write access.
2. Save it with private permissions:

```bash
mkdir -p ~/.config/eng && chmod 700 ~/.config/eng
```

```bash
(umask 077; read -rs -p "token: " t && printf '%s\n' "$t" > ~/.config/eng/read.token)
```

3. If the Project source says "not found or not readable with this token" (fine-grained tokens may not reach user-owned Projects), create a **classic** token with only the `read:project` scope and save it the same way as `~/.config/eng/read-project.token`. It's used only for the Project.

The collector reads only these files. It never calls `gh`, so your normal GitHub login is never used.

### Config

Put the config in `~/.config/eng/repos/<owner>__<name>.json` (local only), or commit it as `<repo>/.eng.json`. The repository file wins.

```json
{
  "schema": "eng.config.v1",
  "repo": "owner/name",
  "default_branch": "main",
  "project": { "owner_type": "user", "owner": "owner", "number": 1 },
  "plans": { "globs": ["docs/plans/*.md", "docs/superpowers/plans/*.md"], "max_files": 3 },
  "watch_workflows": ["dependabot-auto-merge.yml"]
}
```

Optional `project` keys: `status_field` (`Status`), `priority_field` (`Priority`), `done_status` (`Done`), `in_progress_status` (`In progress`), `planned_status` (`Planned`), `priority_order` (`["Now","Next","Later","Someday"]`). `watch_workflows` lists up to 5 workflow files whose latest default-branch run is checked for warning annotations. Unknown keys, absolute paths and `..` are rejected.

## Using it with an agent

- "I'm back, where was I?"
- "I have 45 minutes and low energy, what's next?"
- "Park this, I'm stopping for today."

## Using the scripts directly

```bash
python3 scripts/collect.py --repo ~/code/my-repo        # digest on stdout
```

```bash
python3 scripts/collect.py --repo . --fresh             # skip the 10-minute cache
```

```bash
echo "Next: task 3. Stop point: tests green." | python3 scripts/handoff.py --repo .
```

Exit codes for `collect.py`: `0` all sources ok · `1` fatal · `2` usage · `3` some sources missing (treat them as unknown).

## Tests

```bash
python3 -m unittest discover -s skills/project-pickup/tests -t skills/project-pickup/tests
```

The tests use temporary git repositories and a fake GitHub. They never touch the network.

## Side effects and limitations

- Writes only under `~/.local/state/eng/<owner>__<name>/` (snapshots, a 10-minute cache, handoff notes), mode 600. `--out` refuses folders that git tracks but doesn't ignore.
- Never fetches. Ahead/behind counts use your last fetched `origin/<default>`, or the local default branch.
- Plan progress comes from `- [ ]` / `- [x]` lines in the plan files that changed on each branch. Unticked plans can understate progress.
- Session hints are Claude Code-only: the newest `~/.claude/projects/<encoded-path>/*.jsonl` file's ID and time. Contents are never read. Other agents simply don't get the "resume" hint.
- Workflow warnings come from check-run annotations. Fine-grained tokens have no "Checks" permission, so this works on **public** repositories only. On a private one the source reports an error, and the skill says it couldn't check.
- At most 20 worktrees, 10 pages × 100 Project items, 30 requests and 45 seconds per run.
