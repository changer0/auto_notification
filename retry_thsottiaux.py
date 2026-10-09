#!/usr/bin/env python3
"""Retry X reset notification emails from Berry's persistent queue.

The producer (ChatGPT) writes <status_id>.txt before initiating delivery.
This independent worker never interprets queued text as shell commands.
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
QUEUE = ROOT / "pending" / "thsottiaux_reset"
SENT = QUEUE / "sent_ids.txt"
SUBJECT = "ChatGPT 额度重置通知 - @thsottiaux"


def make_logger():
    logger = logging.getLogger("thsottiaux-mail-retry")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        ROOT / "retry_thsottiaux.log", maxBytes=512_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Inspect only; do not send")
    args = parser.parse_args()
    logger = make_logger()
    QUEUE.mkdir(parents=True, exist_ok=True)

    with (QUEUE / ".retry.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Skipped: another retry worker is running")
            return 0

        sent_ids = set(SENT.read_text(encoding="utf-8").splitlines()) if SENT.exists() else set()
        queued = [
            p for p in sorted(QUEUE.glob("*.txt"))
            if p != SENT and re.fullmatch(r"[0-9]{16,22}", p.stem)
        ]
        if args.dry_run:
            print(f"queued={len(queued)} sent_ids={len(sent_ids)}")
            for p in queued:
                print(f"pending: {p.name}")
            return 0

        for body_file in queued[:10]:
            status_id = body_file.stem
            if status_id in sent_ids:
                body_file.unlink(missing_ok=True)
                logger.info("Already sent, cleaned pending ID=%s", status_id)
                continue

            # Producer may still be writing the file; let the next run handle it.
            if time.time() - body_file.stat().st_mtime < 30:
                logger.info("Deferred recently written ID=%s", status_id)
                continue

            if not body_file.read_text(encoding="utf-8").strip():
                logger.warning("Empty body: retained ID=%s", status_id)
                continue

            try:
                outcome = subprocess.run(
                    [sys.executable, str(ROOT / "notify.py"), "send", "-s", SUBJECT,
                     "--body-file", str(body_file)],
                    cwd=ROOT, text=True, capture_output=True, timeout=60, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("Delivery failed ID=%s: %s", status_id, type(exc).__name__)
                continue

            # A zero exit code alone is insufficient when confirming delivery.
            if outcome.returncode != 0 or "已发送至" not in outcome.stdout:
                logger.warning("Delivery not confirmed ID=%s exit=%s", status_id, outcome.returncode)
                continue

            with SENT.open("a", encoding="utf-8") as ledger:
                ledger.write(status_id + "\n")
                ledger.flush()
                os.fsync(ledger.fileno())
            sent_ids.add(status_id)
            body_file.unlink(missing_ok=True)
            logger.info("Delivery confirmed and dequeued ID=%s", status_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
