# sqs-peek-messages

A skill for reading messages from an AWS SQS queue (standard or FIFO, a DLQ or any other queue) **without consuming them**. It saves one `<MessageId>.json` per message, and the agent, or your project's own analysis, reads those files to explain what is in the queue.

It never deletes, purges or redrives. Cleaning up is your call, in the AWS console.

The skill follows the [Agent Skills](https://agentskills.io) format, so it works with any agent that supports it. Agents that don't can follow `SKILL.md` as plain instructions.

## Requirements

- Python 3.10+ (standard library only) and [AWS CLI v2](https://docs.aws.amazon.com/cli/)
- An authenticated AWS CLI profile with `sqs:GetQueueUrl`, `sqs:GetQueueAttributes` and `sqs:ReceiveMessage` (plus `sqs:ListDeadLetterSourceQueues` for FIFO queues)

## Installation

```bash
npx skills add rebecamorais/agent-skills/skills/sqs-peek-messages
```

Or copy the folder into your agent's skills directory:

```bash
cp -r skills/sqs-peek-messages ~/.claude/skills/   # Claude Code
cp -r skills/sqs-peek-messages .agents/skills/     # agents that read .agents/skills
```

## Using it with an agent

Just ask. Requests like these trigger the skill:

- "what's in the `orders-dlq` queue?"
- "download 50 messages from https://sqs.us-east-1.amazonaws.com/111111111111/orders-dlq"
- "why are messages landing in the DLQ?"

If reading could affect the queue, the agent shows you the risks and waits for your confirmation.

## Using the script directly

```bash
# Only show volume, retention and risks
python3 scripts/peek_messages.py orders-dlq -r us-east-1 -p my-profile --check

# Read 10 messages (default) into sqs-messages/orders-dlq/
python3 scripts/peek_messages.py https://sqs.us-east-1.amazonaws.com/111111111111/orders-dlq -p my-profile

# Read every visible message
python3 scripts/peek_messages.py orders-dlq -r us-east-1 --all
```

Exit codes: `0` ok, `1` error, `2` incomplete read (run again with a larger `-v`), `3` risks found and nothing read (run again with `--yes` once you accept them).

## Why reading is not harmless

| Risk | What happens |
| --- | --- |
| Queue with a `RedrivePolicy` | Each read increments `ApproximateReceiveCount`; messages at `maxReceiveCount` move to the DLQ |
| Messages in flight | Messages you read stay invisible to the real consumer for `-v` seconds |
| FIFO queue (not a DLQ) | Reading a message locks the rest of its `MessageGroupId` for `-v` seconds |

When any of these applies, the script stops and asks for `--yes`. Reading the DLQ instead of the source queue avoids the first and the last risk.

## FIFO queues

FIFO queues work like standard ones, with one limit from SQS: a read sees at most the first 10 messages of each message group. Later messages of a group are only returned after the earlier ones are deleted, so they can't be peeked. The script tells you when this cut the read short.

## Sensitive data

Bodies may contain personal data (GDPR, LGPD). Inside a git repo, the script refuses to write to a folder that isn't in `.gitignore`:

```gitignore
sqs-messages/
```

Don't paste bodies into external channels.

## Evals

`evals/evals.json` has test prompts and the expected agent behavior: refusing to purge, and asking before reading a queue with risks.
