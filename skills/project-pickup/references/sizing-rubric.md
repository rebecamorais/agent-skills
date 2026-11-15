# Sizing rubric and choosing the one next action

Sizes belong to **actions** ("plan task 3 of #177"), not to whole issues. They're computed when needed and never written anywhere.

## Dimensions

| Dimension | Values |
| --- | --- |
| **Effort** | XS ≤ 30 min · S ≤ 2 h · M ≈ half a day · L = several sessions. **Never suggest an L**: split it and suggest the first part. |
| **Uncertainty** | Low: a plan exists and the path is known. Medium: some unknowns. High: needs investigation or design. |
| **Energy** | Low: mechanical (review, merge-ready check, docs, small config). Normal: implement from a plan. High: brainstorm, design, debugging. |
| **Ramp-up** | Use `ramp_up_min` from the digest: +0 if touched in the last 2 days, +15 min for 3–14 days, +30 min after that. Continuing in the active worktree with a handoff note: subtract 10 min (minimum 0). |

## Candidates, in this order

1. An **S1/S2** finding. If it can't be fixed in the available time, the action is "acknowledge it and write down what you know".
2. The **next unchecked plan task** in the worktree being worked on (uncommitted files or commits ahead of base, not locked).
3. A **PR ready for the user to merge**: checks `SUCCESS`, `MERGEABLE`, not draft. The action is to review it and decide, never "merge it".
4. The **first plan task of the top Planned item** in `planned_next_up`. If it has no plan, the action is "write a short plan" (S, high energy) or a spike.
5. **One small hygiene item** (an S4 finding), only when the user has little time or low energy.

S3 maintenance findings (for example a workflow warning about a missing token) are reported, **never** chosen as the default action. Credentials and automation are the user's decision.

## Fit filter

- effort + ramp-up ≤ **70 %** of the available time
- energy needed ≤ the user's energy
- high uncertainty with **less than 1 h** → turn it into a **spike**: "30 min: investigate X, then write down a conclusion (or 'still unclear because …')"

## Score (only to break ties)

impact (unblocks something, user-facing) + continuity (same worktree or branch as last time) − an uncertainty penalty. On a tie, continuity wins.

## Output

```
Next: <action>
Why: <one line>
First step: <first command or file>
Done when: <observable result>
Safe stop point: <where pausing loses nothing>
Size: <XS|S|M> · uncertainty <…> · energy <…> · ramp-up +<n> min
```

A good **safe stop point** is a commit, a passing test, a ticked plan box or a saved handoff. "Halfway through refactoring X" is not one.
