#!/usr/bin/env python3
"""Retry queued notification emails from Berry's persistent queue.

Each subdirectory of pending/ is one queue. A message file starts with a
"Subject: ..." line followed by the body. The producer only writes files;
this worker never interprets queued text as shell commands.
"""

import argparse
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
PENDING = ROOT / "pending"
LOCK_PATH = PENDING / ".retry.lock"

QUEUE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
GENERIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

MAX_PER_QUEUE = 10
SEND_TIMEOUT = 60
MIN_AGE_SECONDS = 30


def make_logger():
    logger = logging.getLogger("email-retry-queue")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        ROOT / "retry_queue.log", maxBytes=512_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def discover_queues() -> list[Path]:
    if not PENDING.is_dir():
        return []
    return [p for p in sorted(PENDING.iterdir())
            if p.is_dir() and QUEUE_NAME.fullmatch(p.name)]


def parse_message(path: Path) -> tuple[str | None, str]:
    """Return (subject, body); subject is None when the file is malformed."""
    text = path.read_text(encoding="utf-8")
    first_line, _, body = text.partition("\n")
    if not first_line.startswith("Subject:"):
        return None, text
    return (first_line[len("Subject:"):].strip() or None), body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Inspect only; do not send")
    args = parser.parse_args()
    logger = make_logger()
    PENDING.mkdir(parents=True, exist_ok=True)

    with LOCK_PATH.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Skipped: another retry worker is running")
            return 0

        for queue_dir in discover_queues():
            sent_file = queue_dir / "sent_ids.txt"
            sent_ids = (set(sent_file.read_text(encoding="utf-8").splitlines())
                        if sent_file.exists() else set())
            queued = [
                p for p in sorted(queue_dir.glob("*.txt"))
                if p.name != sent_file.name and GENERIC_ID.fullmatch(p.stem)
            ]
            if args.dry_run:
                print(f"queue={queue_dir.name} queued={len(queued)} sent_ids={len(sent_ids)}")
                for p in queued:
                    print(f"pending: {queue_dir.name}/{p.name}")
                continue

            for body_file in queued[:MAX_PER_QUEUE]:
                status_id = body_file.stem
                if status_id in sent_ids:
                    body_file.unlink(missing_ok=True)
                    logger.info("Already sent, cleaned pending queue=%s ID=%s",
                                queue_dir.name, status_id)
                    continue

                # Producer may still be writing the file; let the next run handle it.
                if time.time() - body_file.stat().st_mtime < MIN_AGE_SECONDS:
                    logger.info("Deferred recently written queue=%s ID=%s",
                                queue_dir.name, status_id)
                    continue

                subject, body = parse_message(body_file)
                if subject is None:
                    logger.warning("Malformed message (missing 'Subject:' line) retained "
                                   "queue=%s ID=%s", queue_dir.name, status_id)
                    continue
                if not body.strip():
                    logger.warning("Empty body: retained queue=%s ID=%s",
                                   queue_dir.name, status_id)
                    continue

                try:
                    outcome = subprocess.run(
                        [sys.executable, str(ROOT / "notify.py"), "send", "-s", subject],
                        input=body, cwd=ROOT, text=True, capture_output=True,
                        timeout=SEND_TIMEOUT, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    logger.warning("Delivery failed queue=%s ID=%s: %s",
                                   queue_dir.name, status_id, type(exc).__name__)
                    continue

                # A zero exit code alone is insufficient when confirming delivery.
                if outcome.returncode != 0 or "已发送至" not in outcome.stdout:
                    logger.warning("Delivery not confirmed queue=%s ID=%s exit=%s",
                                   queue_dir.name, status_id, outcome.returncode)
                    continue

                with sent_file.open("a", encoding="utf-8") as ledger:
                    ledger.write(status_id + "\n")
                    ledger.flush()
                    os.fsync(ledger.fileno())
                sent_ids.add(status_id)
                body_file.unlink(missing_ok=True)
                logger.info("Delivery confirmed and dequeued queue=%s ID=%s",
                            queue_dir.name, status_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
