#!/usr/bin/env python3
"""Read messages from an SQS queue (DLQ or not) without consuming them and save each one to
<dir>/<MessageId>.json (the SQS Message object, always with Attributes and MessageAttributes).

It never deletes anything, but reading is not harmless: each read increments ApproximateReceiveCount
(in a queue with a RedrivePolicy a message can move to the DLQ), hides the message from the real
consumer for -v seconds and, in FIFO queues, locks its MessageGroupId. Before reading, the script
inspects the queue and, if any of these risks applies, stops and asks for --yes.

Works for standard and FIFO queues. In a FIFO queue, while a message group is locked SQS does not
return the group's later messages, so one read sees at most the first 10 messages of each group;
the script reports when that cut the read short.

Exit codes: 0 ok · 1 error · 2 incomplete read · 3 risks found, confirm with --yes.
Uses the AWS CLI (Python standard library only).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import NoReturn

EXIT_ERROR = 1
EXIT_INCOMPLETE = 2
EXIT_RISKS = 3

BATCH_SIZE = 10
# Stop reading when less than this is left before the first batch's visibility timeout expires.
SAFETY_SECONDS = 15

REGION_FROM_URL = re.compile(r"^https://(?:sqs\.([a-z0-9-]+)|([a-z0-9-]+)\.queue)\.amazonaws\.com/")
AUTH_ERRORS = re.compile(
    r"ExpiredToken|AccessDenied|InvalidClientTokenId|UnrecognizedClient|Unable to locate credentials|SSO"
)
AUTH_HINT = (
    "Credentials are missing, expired or lack permission: authenticate the AWS CLI profile "
    "(e.g. `aws sso login --profile <profile>`) and retry with --profile <profile>."
)
ATTRIBUTES = [
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "MessageRetentionPeriod",
    "RedrivePolicy",
    "FifoQueue",
]


def fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(EXIT_ERROR)


class Parser(argparse.ArgumentParser):
    # argparse exits with 2 on usage errors, which would read as "incomplete read".
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        fail(message)


class AwsError(Exception):
    """An `aws sqs` call failed; the error is already printed."""


def region_from_url(url: str) -> str | None:
    match = REGION_FROM_URL.match(url)
    return (match.group(1) or match.group(2)) if match else None


class Aws:
    def __init__(self, region: str | None, profile: str | None) -> None:
        self.base = ["--output", "json"]
        if region:
            self.base += ["--region", region]
        if profile:
            self.base += ["--profile", profile]

    def __call__(self, *command: str, quiet: bool = False) -> dict:
        result = subprocess.run(["aws", "sqs", *command, *self.base], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            if quiet:
                raise AwsError(command[0])
            stderr = result.stderr.strip()
            print(f"ERROR: aws sqs {command[0]} failed:\n{stderr}", file=sys.stderr)
            if AUTH_ERRORS.search(stderr):
                print(AUTH_HINT, file=sys.stderr)
            raise AwsError(command[0])
        return json.loads(result.stdout) if result.stdout.strip() else {}


def check_gitignored(directory: Path) -> None:
    """Bodies may contain personal data: inside a git work tree, the folder must be ignored."""
    if not shutil.which("git"):
        return
    # The repo that matters is the destination folder's, not the current directory's.
    target = directory.resolve()
    anchor = next(path for path in (target, *target.parents) if path.exists())
    inside = subprocess.run(
        ["git", "-C", str(anchor), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, check=False,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return
    ignored = subprocess.run(["git", "-C", str(anchor), "check-ignore", "-q", str(target / "x.json")], check=False)
    # 0 = ignored, 1 = not ignored, 128 = git error (cannot tell, do not block).
    if ignored.returncode == 1:
        fail(
            f"{directory} is not in .gitignore and message bodies may contain personal data.\n"
            "Add the folder to .gitignore and run again (or pass --skip-gitignore-check)."
        )


def is_dead_letter_queue(aws: Aws, url: str) -> bool | None:
    """True if some queue sends its failures here; None if that cannot be checked (no permission)."""
    try:
        sources = aws("list-dead-letter-source-queues", "--queue-url", url, "--max-items", "1", quiet=True)
    except AwsError:
        return None
    return bool(sources.get("queueUrls"))


def risks_of(attrs: dict, visibility_timeout: int, fifo: bool, dead_letter_queue: bool | None) -> list[str]:
    risks = []
    if attrs.get("RedrivePolicy"):
        policy = json.loads(attrs["RedrivePolicy"])
        target = str(policy.get("deadLetterTargetArn", "?")).split(":")[-1]
        risks.append(
            f"RedrivePolicy (maxReceiveCount={policy.get('maxReceiveCount')} → {target}): every read increments "
            "ApproximateReceiveCount; messages already at the limit move to that queue on this read, "
            "without being returned."
        )
    in_flight = int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0))
    if in_flight:
        risks.append(
            f"{in_flight} message(s) in flight: there is a consumer, a redrive or a recent read by this script; "
            f"messages read here stay invisible to it for {visibility_timeout}s."
        )
    # A FIFO DLQ has no consumer to hold up, so locking its groups only matters for other queues.
    if fifo and not dead_letter_queue:
        risks.append(
            f"FIFO: reading a message locks the rest of its MessageGroupId for {visibility_timeout}s, "
            "holding up the consumer for that whole group"
            + (" (could not check whether this is a DLQ)." if dead_letter_queue is None else ".")
        )
    return risks


def save(directory: Path, message: dict) -> None:
    outfile = directory / f"{message['MessageId']}.json"
    tmpfile = outfile.with_name(f"{outfile.name}.tmp.{os.getpid()}")
    tmpfile.write_text(json.dumps(message, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmpfile, outfile)


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    parser = Parser(
        description="Read messages from an SQS queue into <dir>/<MessageId>.json without consuming them.",
        epilog="Exit codes: 0 ok · 1 error · 2 incomplete read · 3 risks found, confirm with --yes.",
    )
    parser.add_argument("queue", help="queue URL or name")
    parser.add_argument("-o", "--output-dir", type=Path, help="folder for the JSON files (default: sqs-messages/<queue>)")
    amount = parser.add_mutually_exclusive_group()
    amount.add_argument("-n", "--count", type=int, default=10, help="how many messages to read (default: 10)")
    amount.add_argument("--all", action="store_true", help="read every visible message")
    parser.add_argument(
        "-v", "--visibility-timeout", type=int,
        help="seconds each message read stays invisible (default: ~2 s per batch of 10 + 30, "
        "based on how many will be read)",
    )
    parser.add_argument("-r", "--region", help="AWS region (default: taken from the URL, else the profile's)")
    parser.add_argument("-p", "--profile", help="AWS CLI profile")
    parser.add_argument("--check", action="store_true", help="only show the queue and the risks, without reading")
    parser.add_argument("-y", "--yes", action="store_true", help="read despite the risks (after the user confirms)")
    parser.add_argument(
        "--skip-gitignore-check", action="store_true",
        help="do not require the folder to be git-ignored (only outside version-controlled repos)",
    )
    args = parser.parse_args()

    if args.count < 1:
        fail("-n must be >= 1.")
    if args.visibility_timeout is not None and not 1 <= args.visibility_timeout <= 43200:
        fail("--visibility-timeout must be an integer between 1 and 43200 seconds.")
    if not shutil.which("aws"):
        fail("AWS CLI not found in PATH (install AWS CLI v2).")

    region = args.region or region_from_url(args.queue)
    aws = Aws(region, args.profile)
    try:
        url = args.queue if args.queue.startswith("https://") else aws("get-queue-url", "--queue-name", args.queue)["QueueUrl"]
    except AwsError:
        if not args.queue.endswith(".fifo"):
            print("HINT: FIFO queue names end in .fifo; pass the full name.", file=sys.stderr)
        raise
    name = url.rstrip("/").split("/")[-1]
    output_dir = args.output_dir or Path("sqs-messages") / name

    attrs = aws("get-queue-attributes", "--queue-url", url, "--attribute-names", *ATTRIBUTES).get("Attributes", {})
    visible = int(attrs.get("ApproximateNumberOfMessages", 0))
    fifo = attrs.get("FifoQueue") == "true" or name.endswith(".fifo")
    target = visible if args.all else min(args.count, visible) or args.count
    visibility_timeout = args.visibility_timeout or min(43200, math.ceil(target / BATCH_SIZE) * 2 + 30)
    risks = risks_of(attrs, visibility_timeout, fifo, is_dead_letter_queue(aws, url) if fifo else False)

    print(f"Queue:      {url} ({'FIFO' if fifo else 'standard'})")
    print(
        f"Messages:   {visible} visible | {attrs.get('ApproximateNumberOfMessagesNotVisible', 0)} in flight | "
        f"retention {int(attrs.get('MessageRetentionPeriod', 0)) / 86400:.1f} d"
    )
    print(
        f"Read:       {'all' if args.all else f'up to {args.count}'} → {output_dir} "
        f"(visibility timeout {visibility_timeout}s)"
    )
    for risk in risks:
        print(f"RISK:       {risk}")
    if args.check:
        return
    # Before the risks: it is local, and avoids asking for confirmation only to refuse afterwards.
    if not args.skip_gitignore_check:
        check_gitignored(output_dir)
    if risks and not args.yes:
        print("\nNothing was read. Show the risks to the user and, if they confirm, run again with --yes.")
        sys.exit(EXIT_RISKS)
    previous = len(list(output_dir.glob("*.json"))) if output_dir.is_dir() else 0
    if previous:
        print(f"WARNING:    the folder already has {previous} file(s) from earlier reads; new ones mix with them.")
    output_dir.mkdir(parents=True, exist_ok=True)

    max_receive = json.loads(attrs["RedrivePolicy"]).get("maxReceiveCount") if attrs.get("RedrivePolicy") else None
    # A short -v must still leave room for at least one batch.
    safety = min(SAFETY_SECONDS, visibility_timeout // 2)
    seen: set[str] = set()
    saved = failed = repeated = at_limit = 0
    deadline = None
    incomplete = ""
    aws_failed = False
    fifo_cut = False
    # Count received messages, not saved ones: every received message already had its receive count bumped.
    while args.all or len(seen) < args.count:
        if deadline and time.monotonic() >= deadline:
            # With --all and producers still writing, the queue never drains: having read the
            # visible count from the start is a complete read.
            if not (args.all and len(seen) >= visible):
                incomplete = f"the first batch's visibility timeout ({visibility_timeout}s) was about to expire"
            break
        wanted = BATCH_SIZE if args.all else min(BATCH_SIZE, args.count - len(seen))
        started = time.monotonic()
        # FIFO only: if the AWS CLI retries a call, the same attempt id returns the same messages
        # instead of hiding a second set.
        attempt = ["--receive-request-attempt-id", uuid.uuid4().hex] if fifo else []
        try:
            messages = aws(
                "receive-message", "--queue-url", url,
                "--max-number-of-messages", str(wanted),
                "--visibility-timeout", str(visibility_timeout),
                "--wait-time-seconds", "5",
                "--attribute-names", "All",
                "--message-attribute-names", "All",
                *attempt,
            ).get("Messages", [])
        except AwsError:
            # Still print the summary: what was already read is saved and invisible for -v seconds.
            aws_failed = True
            break
        if not messages:
            # FIFO: an empty answer can mean "every group left is locked by this read", not "drained".
            fifo_cut = fifo and len(seen) < target
            break
        # The visibility timeout clock starts when the first batch is received.
        deadline = deadline or started + visibility_timeout - safety
        new = [m for m in messages if m.get("MessageId") not in seen]
        repeated += len(messages) - len(new)
        if not new:
            incomplete = f"only already-read messages came back (the {visibility_timeout}s visibility timeout expired)"
            break
        for message in new:
            seen.add(message["MessageId"])
            receives = int(message.get("Attributes", {}).get("ApproximateReceiveCount", 0))
            at_limit += bool(max_receive and receives >= int(max_receive))
            try:
                save(output_dir, message)
                saved += 1
            except (OSError, TypeError, ValueError) as error:
                print(f"ERROR: could not save {message['MessageId']}: {error}", file=sys.stderr)
                failed += 1
        print(f"Read: {len(seen)}")
        if failed:
            # A local failure (permissions, disk) will not go away on the next batch: stop reading.
            break

    print(f"\nSaved: {saved} | failed: {failed} | repeated: {repeated} | folder: {output_dir}")
    print(f"Nothing was removed; messages read become visible again within ~{visibility_timeout}s.")
    if at_limit:
        print(
            f"WARNING: {at_limit} message(s) reached ApproximateReceiveCount >= maxReceiveCount ({max_receive}): "
            "the next read (including the consumer's) moves them to the DLQ."
        )
    if fifo_cut:
        print(
            f"FIFO: read {len(seen)} of ~{target}. The rest are later messages of groups this read locked; "
            "SQS returns them only after the earlier ones are deleted, so they cannot be peeked. "
            "Reading again after the visibility timeout returns the same messages."
        )
    if failed or aws_failed:
        sys.exit(EXIT_ERROR)
    if incomplete:
        print(f"INCOMPLETE: stopped because {incomplete}. Run again with a larger -v once they are visible again.")
        sys.exit(EXIT_INCOMPLETE)


if __name__ == "__main__":
    try:
        main()
    except AwsError:
        sys.exit(EXIT_ERROR)
