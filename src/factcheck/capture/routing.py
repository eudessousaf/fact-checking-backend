"""Saída virtual "Debate": separa o que o operador OUVE do que o sistema GRAVA.

    navegador ──► "Debate" (volume fixo) ──► monitor ──► captura
                       └──► loopback ──► alto-falantes  ◄── o operador silencia AQUI

Provado na máquina do operador em 21/10/2026, e cada peça existe por um
motivo medido, não suposto:

- Zerar o volume do SISTEMA zerava a captura mesmo com o navegador no
  "Debate": o Chrome corta o próprio som quando a saída padrão fica muda
  (bipe direto no "Debate" com o sistema mudo chegava a -21 dB; o Globoplay,
  a -91 dB). Por isso o operador nunca mexe no volume do sistema: o painel
  silencia só o loopback, que o navegador não enxerga — medido a -26 dB.
- Zerar o volume do PLAYER não tem conserto: o som já sai vazio dele.

Tudo aqui é temporário (módulos do servidor de som): some ao reiniciar o
computador e não altera configuração nenhuma.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from factcheck.capture.sources import AudioSetupError, run_pactl

log = logging.getLogger("factcheck.audio")

VIRTUAL_SINK = "debate"
VIRTUAL_MONITOR = f"{VIRTUAL_SINK}.monitor"
LOOPBACK_LATENCY_MS = 50

# Comparados por "contém", em minúsculas, com o nome do cliente no servidor de
# som. O Chrome aparece como "chrome"; os outros estão aqui por segurança.
BROWSER_NAMES = ("chrome", "chromium", "firefox", "brave", "msedge", "opera", "vivaldi")

Pactl = Callable[..., str]


@dataclass(frozen=True)
class Module:
    id: str
    name: str
    args: str


@dataclass(frozen=True)
class SinkInput:
    index: str
    sink: str          # índice da saída
    client: str        # nome do programa ("-" para módulos, como o loopback)
    owner_module: str | None
    muted: bool


@dataclass(frozen=True)
class RouteStatus:
    virtual_sink: bool
    loopback: bool
    loopback_muted: bool | None
    browsers_total: int
    browsers_on_debate: int
    default_sink: str | None

    @property
    def ok(self) -> bool:
        # Sem navegador tocando não há o que conferir — o Chrome só abre a
        # conexão de som quando há áudio, então "0 navegadores" não é erro.
        return (self.virtual_sink and self.loopback
                and self.browsers_on_debate == self.browsers_total)

    def as_dict(self) -> dict[str, object]:
        return {**self.__dict__, "ok": self.ok}


# --- leitura da saída do pactl ---------------------------------------------------

def parse_modules(short: str) -> list[Module]:
    mods = []
    for line in short.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            mods.append(Module(parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
    return mods


def parse_short_table(short: str) -> dict[str, list[str]]:
    """`pactl list short X`: primeira coluna é o índice."""
    table = {}
    for line in short.splitlines():
        parts = line.split("\t")
        if parts and parts[0].strip():
            table[parts[0]] = parts
    return table


def parse_sink_inputs(long: str, clients: dict[str, list[str]]) -> list[SinkInput]:
    result: list[SinkInput] = []
    cur: dict[str, str] | None = None

    def close() -> None:
        if cur is None:
            return
        client_id = cur.get("Client", "n/a")
        row = clients.get(client_id)
        name = row[2] if row and len(row) > 2 else "-"
        owner = cur.get("Owner Module")
        result.append(SinkInput(
            index=cur["index"], sink=cur.get("Sink", "?"), client=name,
            owner_module=None if owner in (None, "n/a") else owner,
            muted=cur.get("Mute") == "yes",
        ))

    for raw in long.splitlines():
        if raw.startswith("Sink Input #"):
            close()
            cur = {"index": raw.split("#", 1)[1].strip()}
            continue
        if cur is None:
            continue
        line = raw.strip()
        for key in ("Sink", "Client", "Owner Module", "Mute"):
            if line.startswith(f"{key}:") and key not in cur:
                cur[key] = line.split(":", 1)[1].strip()
    close()
    return result


def is_browser(client: str) -> bool:
    c = client.lower()
    return any(b in c for b in BROWSER_NAMES)


# --- operações ------------------------------------------------------------------

class AudioRouting:
    def __init__(self, pactl: Pactl = run_pactl):
        self.pactl = pactl

    def _modules(self) -> tuple[Module | None, Module | None]:
        mods = parse_modules(self.pactl("list", "short", "modules"))
        null = next((m for m in mods if m.name == "module-null-sink"
                     and f"sink_name={VIRTUAL_SINK}" in m.args.split()), None)
        loop = next((m for m in mods if m.name == "module-loopback"
                     and f"source={VIRTUAL_MONITOR}" in m.args.split()), None)
        return null, loop

    def _sinks(self) -> dict[str, str]:
        """índice → nome."""
        return {k: v[1] for k, v in parse_short_table(self.pactl("list", "short", "sinks")).items()
                if len(v) > 1}

    def _sink_inputs(self) -> list[SinkInput]:
        clients = parse_short_table(self.pactl("list", "short", "clients"))
        return parse_sink_inputs(self.pactl("list", "sink-inputs"), clients)

    def default_sink(self) -> str | None:
        out = self.pactl("get-default-sink").strip()
        return out or None

    def status(self) -> RouteStatus:
        null, loop = self._modules()
        sinks = self._sinks()
        inputs = self._sink_inputs()
        browsers = [i for i in inputs if is_browser(i.client)]
        loop_input = next((i for i in inputs if loop and i.owner_module == loop.id), None)
        return RouteStatus(
            virtual_sink=null is not None,
            loopback=loop is not None,
            loopback_muted=loop_input.muted if loop_input else None,
            browsers_total=len(browsers),
            browsers_on_debate=sum(1 for b in browsers if sinks.get(b.sink) == VIRTUAL_SINK),
            default_sink=self.default_sink(),
        )

    def setup(self) -> list[str]:
        """Monta (ou completa) a rota. Pode rodar quantas vezes quiser."""
        actions: list[str] = []
        real_sink = self.default_sink()
        if real_sink == VIRTUAL_SINK:
            raise AudioSetupError(
                "a saída padrão do sistema é o próprio 'Debate'. Escolha os alto-falantes "
                "ou o fone nas configurações de som e rode de novo — senão a cópia para "
                "ouvir apontaria para ela mesma."
            )
        if real_sink is None:
            raise AudioSetupError("não consegui descobrir a saída de som padrão (pactl get-default-sink).")

        null, loop = self._modules()
        if null is None:
            self.pactl("load-module", "module-null-sink", f"sink_name={VIRTUAL_SINK}",
                       "sink_properties=device.description=Debate")
            actions.append("saída virtual 'Debate' criada")
            # Criar uma saída nova pode torná-la a padrão; aí o volume do
            # sistema passaria a controlar o que é gravado. Devolve a padrão.
            if self.default_sink() == VIRTUAL_SINK:
                self.pactl("set-default-sink", real_sink)
                actions.append(f"saída padrão devolvida para {real_sink}")
        else:
            actions.append("saída virtual 'Debate' já existia")

        if loop is None:
            # A cópia aponta para uma saída FIXA, nunca para "a padrão": se a
            # padrão um dia virar o "Debate", o som entraria em laço.
            self.pactl("load-module", "module-loopback", f"source={VIRTUAL_MONITOR}",
                       f"sink={real_sink}", f"latency_msec={LOOPBACK_LATENCY_MS}")
            actions.append(f"cópia para ouvir ligada em {real_sink}")
        else:
            actions.append("cópia para ouvir já existia")

        moved = 0
        sinks = self._sinks()
        for inp in self._sink_inputs():
            if is_browser(inp.client) and sinks.get(inp.sink) != VIRTUAL_SINK:
                self.pactl("move-sink-input", inp.index, VIRTUAL_SINK)
                moved += 1
        browsers = [i for i in self._sink_inputs() if is_browser(i.client)]
        if moved:
            actions.append(f"{moved} conexão(ões) de som do navegador movida(s) para o 'Debate'")
        if not browsers:
            actions.append("AVISO: nenhum navegador tocando agora. Dê play no Globoplay e rode "
                           "de novo — o navegador só aparece enquanto toca.")
        else:
            actions.append(f"{len(browsers)} conexão(ões) do navegador no 'Debate'")
        return actions

    def set_listening(self, on: bool) -> None:
        """Liga/desliga o som do OPERADOR, sem tocar no que é gravado."""
        _, loop = self._modules()
        if loop is None:
            raise AudioSetupError("a cópia para ouvir não existe; rode factcheck audio-setup")
        target = next((i for i in self._sink_inputs() if i.owner_module == loop.id), None)
        if target is None:
            raise AudioSetupError("não achei a conexão da cópia para ouvir")
        self.pactl("set-sink-input-mute", target.index, "0" if on else "1")

    def teardown(self) -> list[str]:
        null, loop = self._modules()
        actions = []
        # A cópia sai antes: ela lê do "Debate".
        if loop is not None:
            self.pactl("unload-module", loop.id)
            actions.append("cópia para ouvir removida")
        if null is not None:
            self.pactl("unload-module", null.id)
            actions.append("saída virtual 'Debate' removida (o navegador volta à saída padrão)")
        if not actions:
            actions.append("nada a desfazer")
        return actions
