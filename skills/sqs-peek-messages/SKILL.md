---
name: sqs-peek-messages
description: >-
  Use when the user wants to read, download, peek at or inspect messages in an AWS SQS queue
  without consuming them, DLQ or not, by URL or name: "what's in the DLQ", "download the queue's
  messages", "stuck messages", "why did this land in the DLQ", ApproximateReceiveCount,
  maxReceiveCount. Also when another skill or a project CLI needs to read a queue without removing anything.
compatibility: Requires Python 3.10+ and AWS CLI v2 with read access to SQS.
---

# Peek at SQS messages without consuming them

Reads messages from any SQS queue and saves one `<MessageId>.json` per message (the SQS `Message`
object, always with `Attributes` and `MessageAttributes`). It does not analyze or group: that is up
to the caller, whether the agent reading the JSON files or the project's own analysis.

## Prerequisites

- `python3` >= 3.10 (standard library only) and AWS CLI v2 in `PATH`.
- An authenticated AWS CLI profile with `sqs:GetQueueUrl`, `sqs:GetQueueAttributes` and
  `sqs:ReceiveMessage`. If the user does not say which profile, ask; do not guess.

## Rule: never delete, and warn about the impact before reading

**Never** run `delete-message`, `purge-queue` or `start-message-move-task`, and never read the
queue with any other client. If asked to clean, purge or redrive, say this skill does not do that
and hand over the `MessageId`s so the user can act in the AWS console.

Reading is not harmless. The script inspects the queue first and, if it finds one of these risks,
stops without reading anything (exit code 3). Show the risks to the user and run again with
`--yes` only if they confirm:

| Risk | Why | How to reduce it |
| --- | --- | --- |
| The message moves to the DLQ | Every read increments `ApproximateReceiveCount`; in a queue with a `RedrivePolicy` (`maxReceiveCount` = 3), reading a message that already failed 3 times moves it to the DLQ instead of returning it | Read fewer messages (`-n`); prefer reading the DLQ over the source queue |
| The consumer is delayed | A message read stays invisible to the real consumer for `-v` seconds | Small `-v` and few messages when there are messages in flight |
| FIFO locks the group | Reading a message blocks the rest of its `MessageGroupId` for `-v` seconds | Read few messages with a small `-v` |

After reading, if the script warns that messages reached `ApproximateReceiveCount >=
maxReceiveCount`, pass it on: the next read (including the consumer's) moves them to the DLQ.

Bodies may contain personal data (GDPR, LGPD etc.): the folder must be in `.gitignore`, and the
script refuses to read otherwise. Never paste bodies into external channels.

## Usage

```bash
python3 <skill>/scripts/peek_messages.py <url|name> [-n 10 | --all] [-o folder] [-p profile] [-r region] [-v seconds]
```

- `<skill>` is this skill's directory.
- `-n <count>` (default 10), or `--all` to read every visible message.
- `-o` defaults to `sqs-messages/<queue-name>/` relative to the current directory. If the folder
  already has `*.json` files and the user did not ask to read again, use what is there; to read
  again, confirm and remove only the `*.json` files.
- `-v` defaults to ~2 s per batch of 10 + 30 s, based on how many will be read. The script stops on
  its own before the first batch's timeout expires.
- `--check` only shows volume, retention and risks, without reading.

| Code | Meaning |
| --- | --- |
| 0 | Read and saved |
| 1 | Error (arguments, AWS, credentials, `.gitignore`, writing) |
| 2 | Incomplete read (timeout or repeated messages); run again with a larger `-v` once they are visible again |
| 3 | Risks found and nothing was read; confirm with the user and run with `--yes` |

## After reading

With few messages, read the JSON files and group them by the error found in `Body`. With many,
write a short Python script that loads each file, parses `Body` with `json.loads` (keeping it as
text when that fails) and counts by the error field. Bodies that came through SNS hold the payload
in `Body` → `Message`, another JSON string. If the project has its own analysis (a CLI or a skill), run
it over the same folder. `Attributes.SentTimestamp`, `ApproximateReceiveCount` and
`DeadLetterQueueSourceArn` give the period, the attempts and the source queue.

## Common errors

| Situation | Action |
| --- | --- |
| `AccessDenied` / `ExpiredToken` / no credentials | Ask the user to authenticate the profile (e.g. `aws sso login --profile <profile>`) and retry with `-p` |
| `aws` not found | Install AWS CLI v2 |
| Refused because of `.gitignore` | Add the folder to `.gitignore`; `--skip-gitignore-check` only outside version-controlled repos |
| Exit 1 after some `Read: N` lines | An AWS call failed mid-read; the files already saved are valid. Fix the cause before reading again |
| Read < visible | Messages in flight or still invisible from an earlier read; wait for the visibility timeout |
