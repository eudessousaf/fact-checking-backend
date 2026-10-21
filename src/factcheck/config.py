"""Configuração: segredos e máquina no .env, dados do debate num TOML.

Separados de propósito: o TOML pode ser copiado entre máquinas e revisado sem
risco; o .env tem chave de API e nunca sai da máquina do operador.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from factcheck.paths import REPO_ROOT


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    short_name: str


@dataclass(frozen=True)
class DebateConfig:
    title: str
    broadcaster: str
    timezone: str
    scheduled_start: str | None
    candidates: tuple[Candidate, ...]
    mediator_name: str
    veto_minutes: float
    min_checks_to_display: int


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    debate_config_path: Path
    environment: str
    panel_password: str
    panel_host: str
    panel_port: int
    capture_source: str | None
    chunk_seconds: float
    chunk_overlap_seconds: float
    silence_alert_seconds: float
    silence_threshold_dbfs: float
    budget_warn_usd: float
    budget_hard_stop_usd: float

    @property
    def db_path(self) -> Path:
        return self.data_dir / "factcheck.sqlite"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} não é número") from exc


def _resolve(path_str: str) -> Path:
    # Caminho relativo no .env é relativo à raiz do repositório, não ao
    # diretório de onde o comando foi chamado — senão "./data" muda de lugar
    # conforme o terminal.
    p = Path(path_str).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p)


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or (REPO_ROOT / ".env"), override=False)
    environment = _env("ENVIRONMENT", "teste")
    if environment not in ("teste", "producao"):
        raise ConfigError(f"ENVIRONMENT deve ser 'teste' ou 'producao', veio {environment!r}")
    chunk = _env_float("CHUNK_SECONDS", 30.0)
    overlap = _env_float("CHUNK_OVERLAP_SECONDS", 2.0)
    if not (5 <= chunk <= 300):
        raise ConfigError("CHUNK_SECONDS deve ficar entre 5 e 300")
    if not (0 <= overlap < chunk / 2):
        raise ConfigError("CHUNK_OVERLAP_SECONDS deve ser >= 0 e menor que metade do bloco")
    return Settings(
        data_dir=_resolve(_env("DATA_DIR", "./data")),
        debate_config_path=_resolve(_env("DEBATE_CONFIG", "./config/debate.toml")),
        environment=environment,
        panel_password=_env("PANEL_PASSWORD", "") or "",
        panel_host=_env("PANEL_HOST", "127.0.0.1") or "127.0.0.1",
        panel_port=int(_env_float("PANEL_PORT", 8765)),
        capture_source=_env("CAPTURE_SOURCE"),
        chunk_seconds=chunk,
        chunk_overlap_seconds=overlap,
        silence_alert_seconds=_env_float("SILENCE_ALERT_SECONDS", 20.0),
        silence_threshold_dbfs=_env_float("SILENCE_THRESHOLD_DBFS", -50.0),
        budget_warn_usd=_env_float("BUDGET_WARN_USD", 80.0),
        budget_hard_stop_usd=_env_float("BUDGET_HARD_STOP_USD", 100.0),
    )


def load_debate_config(path: Path) -> DebateConfig:
    if not path.exists():
        raise ConfigError(
            f"{path} não existe. Copie config/debate.example.toml para config/debate.toml e ajuste."
        )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    try:
        ev = data["event"]
        cands = tuple(
            Candidate(id=c["id"], name=c["name"], short_name=c.get("short_name", c["name"]))
            for c in data["candidates"]
        )
        pub = data.get("publication", {})
        cfg = DebateConfig(
            title=ev["title"],
            broadcaster=ev["broadcaster"],
            timezone=ev.get("timezone", "America/Sao_Paulo"),
            scheduled_start=ev.get("scheduled_start"),
            candidates=cands,
            mediator_name=data["mediator"]["name"],
            veto_minutes=float(pub.get("veto_minutes", 3)),
            min_checks_to_display=int(pub.get("min_checks_to_display", 8)),
        )
    except KeyError as exc:
        raise ConfigError(f"{path}: campo obrigatório ausente: {exc}") from exc
    if sorted(c.id for c in cfg.candidates) != ["A", "B"]:
        raise ConfigError(f"{path}: precisa de exatamente dois candidatos, com id A e B")
    return cfg
