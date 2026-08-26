"""
Backs up the trading database. Run via deploy/groww-backup.timer (daily).

Uses sqlite3's own backup API (Connection.backup()), not a raw file copy — a plain `cp` of a
live WAL-mode database while the bot is writing to it risks copying a torn, inconsistent
snapshot. backup() is the safe way to back up a SQLite DB while other connections may be
writing to it concurrently.

This is a LOCAL backup only (same disk as the database it protects) — it guards against
corruption, a bad migration, or accidental deletion, but NOT physical disk failure. Shipping
backups off the droplet (DigitalOcean's own backup add-on, or your own remote storage) is a
deliberate follow-up, not done here.

Usage:
    python -m scripts.backup_db [--keep N]
"""
import argparse
import gzip
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

logger = logging.getLogger("groww_agent.backup")

DEFAULT_KEEP = 14
BACKUP_DIR = "backups"


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                         help=f"How many most-recent backups to retain (default {DEFAULT_KEEP}).")
    return parser.parse_args()


def backup_database(db_path: str, backup_dir: str, keep: int) -> str:
    """Returns the path to the newly created backup file. Raises on failure — callers
    (main() here, and anything calling this from tests) must not swallow a backup failure
    silently, that defeats the point of having backups."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found at {db_path} — nothing to back up.")

    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    raw_path = os.path.join(backup_dir, f"trading_{timestamp}.db")
    gz_path = raw_path + ".gz"

    source = sqlite3.connect(db_path)
    dest = sqlite3.connect(raw_path)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    with open(raw_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(raw_path)

    _rotate_old_backups(backup_dir, keep)
    return gz_path


def _rotate_old_backups(backup_dir: str, keep: int) -> None:
    files = sorted(
        (f for f in os.listdir(backup_dir) if f.startswith("trading_") and f.endswith(".db.gz")),
    )
    excess = len(files) - keep
    for fname in files[:max(excess, 0)]:
        os.remove(os.path.join(backup_dir, fname))
        logger.info("Rotated out old backup: %s", fname)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = _parse_args()

    from dotenv import load_dotenv
    load_dotenv()
    db_path = os.getenv("DATABASE_PATH", "data/trading.db")

    try:
        backup_path = backup_database(db_path, BACKUP_DIR, args.keep)
    except Exception as e:
        logger.error("Backup FAILED: %s", e)
        try:
            from core.notifier import send_notification_raw
            send_notification_raw(os.getenv("NTFY_TOPIC", ""), f"🔴 Database backup FAILED: {e}")
        except Exception:
            pass
        sys.exit(1)

    size_kb = os.path.getsize(backup_path) / 1024
    logger.info("Backup complete: %s (%.1f KB)", backup_path, size_kb)


if __name__ == "__main__":
    main()
