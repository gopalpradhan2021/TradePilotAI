"""
Minimal migration runner: applies numbered *.sql files from migrations/ that
aren't yet recorded in schema_migrations, in order, once each.
"""
import logging
import os
import re

from core.db.connection import get_connection

logger = logging.getLogger("groww_agent.db.migrate")

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def run_migrations() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}

        files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))
        for fname in files:
            match = re.match(r"^(\d+)_", fname)
            if not match:
                continue
            version = int(match.group(1))
            if version in applied:
                continue

            path = os.path.join(MIGRATIONS_DIR, fname)
            with open(path) as f:
                sql = f.read()

            logger.info("Applying migration %s", fname)
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, datetime('now'))",
                (version, fname),
            )
            conn.commit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migrations()
