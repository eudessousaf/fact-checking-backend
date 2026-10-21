"""Descoberta das fontes de áudio do sistema e montagem do comando ffmpeg.

Tanto no PipeWire (com pipewire-pulse) quanto no PulseAudio, cada saída de som
tem um "monitor": uma fonte de gravação que devolve exatamente o que está
tocando. É por ele que o som do Globoplay chega aqui, sem cabo virtual.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16_000
CHANNELS = 1
BYTES_PER_SAMPLE = 2  # s16le


class AudioSetupError(RuntimeError):
    pass


@dataclass(frozen=True)
class MonitorSource:
    name: str
    description: str
    state: str
    is_default: bool


def run_pactl(*args: str) -> str:
    if shutil.which("pactl") is None:
        raise AudioSetupError(
            "comando 'pactl' não encontrado. Instale com: sudo apt install pulseaudio-utils "
            "(no PipeWire, confira também se o pacote pipewire-pulse está instalado)."
        )
    # LC_ALL=C: em sistema em português o pactl escreve "Nome:" e "Descrição:",
    # e a leitura abaixo procura os rótulos em inglês.
    env = {**os.environ, "LC_ALL": "C"}
    try:
        out = subprocess.run(["pactl", *args], capture_output=True, text=True, env=env,
                             timeout=10, check=True)
    except subprocess.CalledProcessError as exc:
        raise AudioSetupError(f"pactl {' '.join(args)} falhou: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioSetupError(f"pactl {' '.join(args)} não respondeu em 10 s") from exc
    return out.stdout


def parse_server_name(info_output: str) -> str:
    for line in info_output.splitlines():
        if line.strip().startswith("Server Name:"):
            return line.split(":", 1)[1].strip()
    return "desconhecido"


def describe_server(server_name: str) -> str:
    """'PulseAudio (on PipeWire 1.0.5)' é o PipeWire se passando por PulseAudio."""
    if "PipeWire" in server_name:
        return "PipeWire"
    if "pulseaudio" in server_name.lower():
        return "PulseAudio"
    return server_name


def parse_default_sink(info_output: str) -> str | None:
    for line in info_output.splitlines():
        if line.strip().startswith("Default Sink:"):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


def parse_sources(list_output: str, default_sink: str | None) -> list[MonitorSource]:
    """Lê `pactl list sources` (formato longo) e devolve só os monitores."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in list_output.splitlines():
        if raw.startswith("Source #"):
            current = {}
            blocks.append(current)
            continue
        if current is None:
            continue
        line = raw.strip()
        for key in ("Name", "Description", "State", "Monitor of Sink"):
            prefix = f"{key}:"
            if line.startswith(prefix) and key not in current:
                current[key] = line[len(prefix):].strip()
    default_monitor = f"{default_sink}.monitor" if default_sink else None
    result = []
    for b in blocks:
        name = b.get("Name", "")
        monitor_of = b.get("Monitor of Sink", "n/a")
        if not name.endswith(".monitor") and monitor_of in ("", "n/a"):
            continue
        result.append(MonitorSource(
            name=name,
            description=b.get("Description", name),
            state=b.get("State", "?"),
            is_default=(name == default_monitor),
        ))
    result.sort(key=lambda s: (not s.is_default, s.name))
    return result


def detect_server() -> str:
    return describe_server(parse_server_name(run_pactl("info")))


def list_monitor_sources() -> list[MonitorSource]:
    info = run_pactl("info")
    return parse_sources(run_pactl("list", "sources"), parse_default_sink(info))


def resolve_monitor(requested: str | None, prefer: str | None = None) -> MonitorSource:
    """Escolhe o monitor: o pedido explicitamente > o preferido, se existir > o padrão."""
    sources = list_monitor_sources()
    if not sources:
        raise AudioSetupError("nenhum monitor de saída de som encontrado (pactl list sources).")
    if requested:
        for s in sources:
            if s.name == requested:
                return s
        names = "\n  ".join(s.name for s in sources)
        raise AudioSetupError(f"fonte {requested!r} não existe. Disponíveis:\n  {names}")
    if prefer:
        for s in sources:
            if s.name == prefer:
                return s
    default = next((s for s in sources if s.is_default), None)
    if default is None:
        raise AudioSetupError(
            "não consegui descobrir o monitor da saída padrão. Escolha um com "
            "CAPTURE_SOURCE no .env (lista: factcheck capture --list-sources)."
        )
    return default


def check_ffmpeg(require_pulse: bool) -> None:
    if shutil.which("ffmpeg") is None:
        raise AudioSetupError("ffmpeg não encontrado. Instale com: sudo apt install ffmpeg")
    if not require_pulse:
        return
    out = subprocess.run(["ffmpeg", "-hide_banner", "-devices"], capture_output=True,
                         text=True, timeout=10)
    # Linhas como " DE pulse  Pulse audio": o "D" diz que ele sabe LER do pulse.
    has_pulse_input = any(
        len(parts) >= 2 and parts[1] == "pulse" and "D" in parts[0]
        for parts in (line.split() for line in out.stdout.splitlines())
    )
    if not has_pulse_input:
        raise AudioSetupError("este ffmpeg foi compilado sem suporte a 'pulse'. "
                              "Use o ffmpeg do apt (sudo apt install ffmpeg).")


_PCM_OUT = ["-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]


def ffmpeg_pulse_cmd(source_name: str) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "pulse", "-i", source_name, *_PCM_OUT]


def ffmpeg_file_cmd(path: Path, realtime: bool, start_seconds: float = 0.0) -> list[str]:
    # -re lê o arquivo no ritmo real: simula o ao vivo com um arquivo, e é o
    # jeito de testar o fluxo inteiro sem navegador.
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if realtime:
        cmd.append("-re")
    if start_seconds > 0:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    return cmd + ["-i", str(path), "-vn", *_PCM_OUT]
