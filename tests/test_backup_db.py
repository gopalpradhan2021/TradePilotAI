import gzip
import os
import sqlite3

import pytest

from scripts.backup_db import backup_database, _rotate_old_backups


def make_source_db(path: str):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, symbol TEXT)")
    conn.execute("INSERT INTO orders (symbol) VALUES ('RELIANCE')")
    conn.commit()
    conn.close()


def test_backup_database_creates_gzipped_copy_with_matching_data(tmp_path):
    db_path = str(tmp_path / "trading.db")
    make_source_db(db_path)
    backup_dir = str(tmp_path / "backups")

    backup_path = backup_database(db_path, backup_dir, keep=14)

    assert backup_path.endswith(".db.gz")
    assert os.path.exists(backup_path)

    restored_path = str(tmp_path / "restored.db")
    with gzip.open(backup_path, "rb") as f_in, open(restored_path, "wb") as f_out:
        f_out.write(f_in.read())
    conn = sqlite3.connect(restored_path)
    rows = conn.execute("SELECT symbol FROM orders").fetchall()
    conn.close()
    assert rows == [("RELIANCE",)]


def test_backup_database_raises_when_source_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_database(str(tmp_path / "missing.db"), str(tmp_path / "backups"), keep=14)


def test_rotate_old_backups_keeps_only_most_recent_n(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    names = [f"trading_2026010{i}_000000.db.gz" for i in range(1, 6)]  # 5 files, oldest-first names
    for name in names:
        (backup_dir / name).write_bytes(b"fake")

    _rotate_old_backups(str(backup_dir), keep=3)

    remaining = sorted(os.listdir(backup_dir))
    assert remaining == names[-3:]  # the 3 lexicographically-latest (== most recent, timestamped names)


def test_rotate_old_backups_no_op_when_under_limit(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "trading_20260101_000000.db.gz").write_bytes(b"fake")

    _rotate_old_backups(str(backup_dir), keep=14)

    assert len(os.listdir(backup_dir)) == 1


def test_backup_database_rotates_after_exceeding_keep(tmp_path):
    db_path = str(tmp_path / "trading.db")
    make_source_db(db_path)
    backup_dir = str(tmp_path / "backups")

    for _ in range(3):
        backup_database(db_path, backup_dir, keep=2)

    assert len(os.listdir(backup_dir)) == 2


def test_backup_database_ignores_unrelated_files_in_backup_dir(tmp_path):
    backup_dir = str(tmp_path / "backups")
    os.makedirs(backup_dir)
    (tmp_path / "backups" / "readme.txt").write_text("not a backup")

    db_path = str(tmp_path / "trading.db")
    make_source_db(db_path)

    backup_database(db_path, backup_dir, keep=14)

    assert "readme.txt" in os.listdir(backup_dir)  # untouched, not rotated away
