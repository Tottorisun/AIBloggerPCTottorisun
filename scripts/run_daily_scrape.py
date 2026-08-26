"""Entry point for the daily scheduled scrape-all run.

Runs `python -m pc_price_tracker.cli scrape-all` as a subprocess (so its
behavior is identical to a manual run) and retries up to MAX_ATTEMPTS times
total, waiting RETRY_DELAY_SECONDS between attempts, if it exits non-zero —
that only happens on a genuine crash; per-category SourceBlocked events are
already handled and logged inside scrape-all itself without failing the run.

Everything is logged to a rotating file under logs/, since this runs
headless under Windows Task Scheduler with no console to watch. Registered
via scripts/register_scheduled_task.ps1, daily at 04:00.

On a successful scrape, also runs `backup` — price history is the one
asset here that can't be reconstructed, so it needs a standing backup
cadence, not just a command that exists for someone to remember to run.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 3600

# Windows subprocesses default their stdout/stderr encoding to the console
# codepage (cp1251 for a Russian locale) even when piped, not UTF-8 — the
# parent's subprocess.run(..., encoding="utf-8") only controls how *this*
# process decodes what it reads, not what the child actually wrote. Without
# this, Cyrillic log lines (product names, "Готово: ...") come through as
# mojibake in the rotating log file.
_CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _configure_logging() -> logging.Logger:
    logger = logging.getLogger("scheduled_scrape")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # One log file per day, 30 days kept — matches the DB backup retention
    # policy so both roughly cover "the last month".
    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_DIR / "scrape_all.log",
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)
    return logger


def _run_backup(logger: logging.Logger) -> None:
    cmd = [sys.executable, "-m", "pc_price_tracker.cli", "backup"]
    logger.info("running %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=_CHILD_ENV
        )
        for line in result.stdout.splitlines():
            logger.info("backup: %s", line)
        for line in result.stderr.splitlines():
            logger.warning("backup(stderr): %s", line)
        if result.returncode != 0:
            logger.error("backup failed (exit %s) — scrape data is saved, but not freshly backed up", result.returncode)
    except OSError:
        logger.exception("failed to launch backup subprocess")


def main() -> int:
    logger = _configure_logging()
    cmd = [sys.executable, "-m", "pc_price_tracker.cli", "scrape-all"]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info("attempt %d/%d: running %s", attempt, MAX_ATTEMPTS, " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_CHILD_ENV,
            )
            for line in result.stdout.splitlines():
                logger.info("scrape-all: %s", line)
            for line in result.stderr.splitlines():
                logger.warning("scrape-all(stderr): %s", line)
            returncode = result.returncode
        except OSError:
            logger.exception("attempt %d: failed to launch subprocess", attempt)
            returncode = -1

        if returncode == 0:
            logger.info("attempt %d succeeded (exit 0)", attempt)
            _run_backup(logger)
            return 0

        logger.error("attempt %d failed (exit %s)", attempt, returncode)
        if attempt < MAX_ATTEMPTS:
            logger.info("retrying in %d seconds", RETRY_DELAY_SECONDS)
            time.sleep(RETRY_DELAY_SECONDS)

    logger.error("all %d attempts failed; giving up for today", MAX_ATTEMPTS)
    return 1


if __name__ == "__main__":
    sys.exit(main())
