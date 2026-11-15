# Engineering automation: decisions in force

Short record of the decisions that govern the engineering-automation work in this repository. The full design and the Phase 0 validation live next to this file once they're committed (`design-v2.md`, `phase-0-validation.md`).

## Where things live (D0, D7)

- **This repository** (`agent-skills`) holds everything reusable: skills, the read-only collector, monitors, tests and documentation.
- Claude Code–specific pieces (subagent definitions, hook examples) go in a separate integration directory, `integrations/claude-code/`, created when the first one is needed. Every skill must work without them.
- A **consuming project** (for example RPGWorld) holds only its own config (`.eng.json`), its decision log and the workflow that calls the shared code, pinned to a commit of this repository.
- Local runtime state: `~/.local/state/eng/<owner>__<name>/`. Credentials: `~/.config/eng/`. Neither is ever in a repository.

## Decisions

| ID | Decision |
| --- | --- |
| D1 | Private Discord `#ops` channel for maintenance alerts, enabled after dry runs pass (Phase 3). |
| D2 | The Project's built-in "Item closed → Done" workflow stays disabled. |
| D3 | No extra Project fields. |
| D4 | Command names approved. The first skill is `project-pickup`. |
| D5 | Uptime probe deferred. |
| D6 | No per-turn checkpoint hook. Recovery uses git state plus session-file metadata. |
| D8 | No secret-scanning API in CI for now. Check that GitHub's security notifications reach the maintainer before relying on email. |
| D9 | Production/main signals: Vercel Production deployments, Deploy Migrations, CodeQL and Crowdin. No new push-to-main test workflow for now. |
| D10 | `eng-apply` runs only interactively in the maintainer's terminal, never through an agent. |

## Requirements carried into later phases

1. **`plan.json` is untrusted input** (Phase 4, `eng-apply`):
   - a strict, versioned schema with unknown keys rejected
   - an allowlist of operations; anything else is refused
   - repository and field validation (only the configured repository, Project and field names; option values must exist)
   - a current-state check right before each write (`expected_current` must match)
   - a visible diff per item and an individual `y/N` confirmation on a TTY
   - **never executes commands, URLs or code from the plan**
2. **Dependabot auto-merge credentials and fixes are deferred.** Skills report the broken automation (workflow warning annotations, Dependabot PRs without auto-merge) and never fix or enable it.
3. **Skills are portable Agent Skills** that work without Claude-specific subagents.
4. **Agent tool-isolation test is a mandatory gate** before any specialised subagent is enabled: an agent limited to read-only tools must be shown, in the installed agent version, to be unable to run shell commands.
5. **Solo-maintainer principles:** no artificial deadlines, no productivity scoring, no pressure from inactivity. Enforced by a word list checked in tests (`skills/project-pickup/references/principles.md`).
