"""SQLite compartilhado entre os processos (captura, painel, fluxo).

WAL deixa um processo escrever enquanto outro lê, que é o caso normal aqui: a
captura grava o status a cada segundo e o painel lê ao mesmo tempo.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Cada migração roda uma vez, na ordem; PRAGMA user_version guarda a última.
# Nunca edite uma migração já distribuída — acrescente outra no fim.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE capture_runs (
        id            INTEGER PRIMARY KEY,
        source_kind   TEXT NOT NULL CHECK (source_kind IN ('pulse', 'file')),
        source_name   TEXT NOT NULL,
        started_at    TEXT NOT NULL,
        ended_at      TEXT,
        end_reason    TEXT,
        audio_dir     TEXT NOT NULL
    );
    CREATE TABLE chunks (
        id              INTEGER PRIMARY KEY,
        run_id          INTEGER NOT NULL REFERENCES capture_runs(id),
        idx             INTEGER NOT NULL,
        segment         INTEGER NOT NULL,
        offset_start_s  REAL NOT NULL,
        offset_end_s    REAL NOT NULL,
        wall_start      TEXT,
        wall_end        TEXT,
        path            TEXT NOT NULL,
        mean_dbfs       REAL NOT NULL,
        status          TEXT NOT NULL DEFAULT 'captured',
        created_at      TEXT NOT NULL,
        UNIQUE (run_id, idx)
    );
    CREATE TABLE capture_status (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        run_id          INTEGER,
        state           TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        level_dbfs      REAL,
        silence_seconds REAL NOT NULL DEFAULT 0,
        silence_alert   INTEGER NOT NULL DEFAULT 0,
        last_chunk_idx  INTEGER,
        last_chunk_at   TEXT,
        message         TEXT
    );
    """,
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        with transaction(conn):
            _run_script(conn, sql)
            conn.execute(f"PRAGMA user_version = {version}")


def _run_script(conn: sqlite3.Connection, sql: str) -> None:
    # executescript() faz COMMIT implícito e quebraria a transação da
    # migração; executar comando a comando mantém tudo-ou-nada. O corte por
    # ";" exige que nenhuma migração tenha ";" dentro de texto ou trigger.
    for statement in sql.split(";"):
        if statement.strip():
            conn.execute(statement)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
