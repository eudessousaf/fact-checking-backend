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
    # 2 — fluxo de checagem. Uma linha de `claims` vive do "detectada" ao
    # "publicada"/"removida": o estado mora na linha, e é isso que deixa o
    # processo cair e voltar sem duplicar nada.
    """
    ALTER TABLE chunks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE chunks ADD COLUMN error TEXT;
    CREATE INDEX chunks_status ON chunks(status, run_id, idx);
    CREATE TABLE transcripts (
        id          INTEGER PRIMARY KEY,
        chunk_id    INTEGER NOT NULL UNIQUE REFERENCES chunks(id),
        provider    TEXT NOT NULL,
        raw_json    TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE segments (
        id              INTEGER PRIMARY KEY,
        chunk_id        INTEGER NOT NULL REFERENCES chunks(id),
        run_id          INTEGER NOT NULL REFERENCES capture_runs(id),
        start_s         REAL NOT NULL,
        end_s           REAL NOT NULL,
        wall_at         TEXT,
        speaker_label   TEXT NOT NULL,
        role            TEXT NOT NULL CHECK (role IN ('A', 'B', 'M', '?')),
        uncertain       INTEGER NOT NULL DEFAULT 0,
        uncertain_reason TEXT,
        text            TEXT NOT NULL
    );
    CREATE INDEX segments_run_time ON segments(run_id, start_s);
    CREATE TABLE chunk_class (
        chunk_id    INTEGER PRIMARY KEY REFERENCES chunks(id),
        label       TEXT NOT NULL CHECK (label IN ('debate', 'comercial', 'vinheta', 'indefinido')),
        source      TEXT NOT NULL,
        reason      TEXT,
        effective   TEXT NOT NULL CHECK (effective IN ('debate', 'intervalo')),
        created_at  TEXT NOT NULL
    );
    CREATE TABLE debate_state (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        status          TEXT NOT NULL DEFAULT 'aguardando',
        mode            TEXT NOT NULL DEFAULT 'ao_vivo',
        manual_interval TEXT,
        started_at      TEXT,
        file_anchor_at  TEXT,
        publish_paused  INTEGER NOT NULL DEFAULT 0,
        auto_publish    INTEGER NOT NULL DEFAULT 1,
        speakers_ready  INTEGER NOT NULL DEFAULT 0,
        timeline_seq    INTEGER NOT NULL DEFAULT 0,
        updated_at      TEXT NOT NULL
    );
    INSERT INTO debate_state (id, updated_at) VALUES (1, '1970-01-01T00:00:00+00:00');
    CREATE TABLE claims (
        id              INTEGER PRIMARY KEY,
        chunk_id        INTEGER NOT NULL REFERENCES chunks(id),
        run_id          INTEGER NOT NULL,
        candidate_id    TEXT NOT NULL CHECK (candidate_id IN ('A', 'B')),
        start_s         REAL NOT NULL,
        wall_at         TEXT,
        quote           TEXT NOT NULL,
        claim           TEXT NOT NULL,
        dedup_key       TEXT NOT NULL,
        status          TEXT NOT NULL,
        verdict         TEXT,
        explanation     TEXT,
        what_is_off     TEXT,
        sources_json    TEXT,
        verify_json     TEXT,
        publish_at      TEXT,
        published_at    TEXT,
        updated_at      TEXT,
        removed_at      TEXT,
        note            TEXT,
        attempts        INTEGER NOT NULL DEFAULT 0,
        error           TEXT,
        created_at      TEXT NOT NULL
    );
    CREATE INDEX claims_status ON claims(status);
    CREATE UNIQUE INDEX claims_dedup ON claims(candidate_id, dedup_key);
    CREATE TABLE api_costs (
        id              INTEGER PRIMARY KEY,
        at              TEXT NOT NULL,
        provider        TEXT NOT NULL,
        model           TEXT NOT NULL,
        purpose         TEXT NOT NULL,
        input_tokens    INTEGER NOT NULL DEFAULT 0,
        output_tokens   INTEGER NOT NULL DEFAULT 0,
        extra           TEXT,
        usd             REAL NOT NULL
    );
    CREATE TABLE operator_log (
        id      INTEGER PRIMARY KEY,
        at      TEXT NOT NULL,
        action  TEXT NOT NULL,
        detail  TEXT
    );
    """,
    # 3 — "voltou ao debate" do operador vale a partir do instante do clique.
    """
    ALTER TABLE debate_state ADD COLUMN resume_after TEXT;
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
