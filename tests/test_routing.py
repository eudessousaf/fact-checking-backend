"""Rota "Debate" contra um pactl falso, montado com as saídas reais da máquina
do operador (21/10/2026): PipeWire 1.6.2, Chrome como cliente "chrome"."""

import pytest

from factcheck.capture.routing import AudioRouting, parse_sink_inputs, parse_short_table
from factcheck.capture.sources import AudioSetupError

ALSA = "alsa_output.pci-0000_00_1f.3.analog-stereo"


class FakePactl:
    def __init__(self, chrome_playing=True, default=ALSA, null_becomes_default=False):
        self.default = default
        self.null_becomes_default = null_becomes_default
        self.sinks = {"57": ALSA}
        self.modules: dict[str, tuple[str, str]] = {}
        self.clients = {"77": "chrome", "1695": "chrome", "1527": "pavucontrol"}
        # índice → [sink, client, owner_module, muted]
        self.inputs: dict[str, list] = {}
        if chrome_playing:
            self.inputs["1696"] = ["57", "1695", None, False]
        self.next_id = 536870916
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        cmd = args[0]
        if args[:3] == ("list", "short", "modules"):
            return "\n".join(f"{i}\t{n}\t{a}" for i, (n, a) in self.modules.items())
        if args[:3] == ("list", "short", "sinks"):
            return "\n".join(f"{i}\t{n}\tPipeWire\tfloat32le 2ch 48000Hz\tRUNNING" for i, n in self.sinks.items())
        if args[:3] == ("list", "short", "clients"):
            return "\n".join(f"{i}\tPipeWire\t{n}" for i, n in self.clients.items())
        if args[:2] == ("list", "sink-inputs"):
            out = []
            for i, (sink, client, owner, muted) in self.inputs.items():
                out += [f"Sink Input #{i}", f"\tDriver: PipeWire", f"\tOwner Module: {owner or 'n/a'}",
                        f"\tClient: {client}", f"\tSink: {sink}", f"\tMute: {'yes' if muted else 'no'}",
                        "\tProperties:", '\t\tmedia.name = "Playback"', ""]
            return "\n".join(out)
        if cmd == "get-default-sink":
            return self.default + "\n"
        if cmd == "set-default-sink":
            self.default = args[1]
            return ""
        if cmd == "load-module":
            mid = str(self.next_id)
            self.next_id += 1
            self.modules[mid] = (args[1], " ".join(args[2:]))
            if args[1] == "module-null-sink":
                self.sinks["1474"] = "debate"
                if self.null_becomes_default:
                    self.default = "debate"
            if args[1] == "module-loopback":
                sink_name = next(a.split("=", 1)[1] for a in args if a.startswith("sink="))
                sink_idx = next(k for k, v in self.sinks.items() if v == sink_name)
                self.inputs["1481"] = [sink_idx, "n/a", mid, False]
            return mid + "\n"
        if cmd == "unload-module":
            name, _ = self.modules.pop(args[1])
            if name == "module-loopback":
                self.inputs = {k: v for k, v in self.inputs.items() if v[2] != args[1]}
            if name == "module-null-sink":
                self.sinks.pop("1474")
            return ""
        if cmd == "move-sink-input":
            idx = next(k for k, v in self.sinks.items() if v == args[2])
            self.inputs[args[1]][0] = idx
            return ""
        if cmd == "set-sink-input-mute":
            self.inputs[args[1]][3] = args[2] == "1"
            return ""
        raise AssertionError(f"chamada inesperada: {args}")


def test_parse_real_short_tables():
    clients = parse_short_table("77\tPipeWire\tchrome\n1527\tPipeWire\tpavucontrol\n")
    assert clients["77"][2] == "chrome"


def test_setup_from_scratch_routes_chrome():
    fake = FakePactl()
    routing = AudioRouting(fake)
    actions = routing.setup()
    st = routing.status()
    assert st.ok and st.virtual_sink and st.loopback
    assert (st.browsers_total, st.browsers_on_debate) == (1, 1)
    assert st.loopback_muted is False
    # A cópia aponta para a saída real, por nome, nunca "a padrão".
    loop_args = next(a for n, a in fake.modules.values() if n == "module-loopback")
    assert f"sink={ALSA}" in loop_args.split()
    assert any("movida" in a for a in actions)


def test_setup_is_idempotent():
    fake = FakePactl()
    routing = AudioRouting(fake)
    routing.setup()
    routing.setup()
    assert sum(1 for n, _ in fake.modules.values() if n == "module-null-sink") == 1
    assert sum(1 for n, _ in fake.modules.values() if n == "module-loopback") == 1


def test_default_sink_restored_if_virtual_sink_steals_it():
    fake = FakePactl(null_becomes_default=True)
    AudioRouting(fake).setup()
    assert fake.default == ALSA


def test_refuses_when_default_is_already_debate():
    fake = FakePactl(default="debate")
    with pytest.raises(AudioSetupError, match="próprio 'Debate'"):
        AudioRouting(fake).setup()


def test_no_browser_playing_is_warned_not_failed():
    routing = AudioRouting(FakePactl(chrome_playing=False))
    actions = routing.setup()
    assert any("nenhum navegador" in a for a in actions)
    st = routing.status()
    assert st.ok and st.browsers_total == 0


def test_listening_toggle_mutes_only_the_copy():
    fake = FakePactl()
    routing = AudioRouting(fake)
    routing.setup()
    routing.set_listening(False)
    assert routing.status().loopback_muted is True
    assert fake.inputs["1696"][3] is False  # o navegador (o que é gravado) intacto
    routing.set_listening(True)
    assert routing.status().loopback_muted is False


def test_browser_back_on_speakers_is_detected():
    fake = FakePactl()
    routing = AudioRouting(fake)
    routing.setup()
    fake.inputs["1696"][0] = "57"  # página recarregada voltou aos alto-falantes
    st = routing.status()
    assert not st.ok and st.browsers_on_debate == 0


def test_teardown_removes_copy_then_sink():
    fake = FakePactl()
    routing = AudioRouting(fake)
    routing.setup()
    routing.teardown()
    assert fake.modules == {}
    unloads = [c for c in fake.calls if c[0] == "unload-module"]
    assert len(unloads) == 2


def test_listen_without_route_is_clear_error():
    with pytest.raises(AudioSetupError, match="audio-setup"):
        AudioRouting(FakePactl()).set_listening(False)


def test_parse_sink_inputs_without_client():
    out = "Sink Input #1481\n\tOwner Module: 536870917\n\tClient: n/a\n\tSink: 57\n\tMute: yes\n"
    [inp] = parse_sink_inputs(out, {})
    assert inp.client == "-" and inp.owner_module == "536870917" and inp.muted


def test_real_output_from_operator_machine():
    # Saída real de 21/10/2026: o Chrome recria a conexão (1696 → 2022) ao
    # pausar e voltar, e o PipeWire a devolve ao "Debate" pelo nome do programa.
    long = """Sink Input #1481
	Driver: PipeWire
	Owner Module: 536870917
	Client: n/a
	Sink: 57
	Mute: no
Sink Input #2022
	Driver: PipeWire
	Owner Module: n/a
	Client: 2021
	Sink: 1474
	Mute: no
	Properties:
		application.name = "Google Chrome"
		module-stream-restore.id = "sink-input-by-application-name:Google Chrome"
"""
    clients = parse_short_table("77\tPipeWire\tchrome\n2021\tPipeWire\tchrome\n")
    loop, chrome = parse_sink_inputs(long, clients)
    assert loop.client == "-" and loop.owner_module == "536870917"
    assert chrome.client == "chrome" and chrome.sink == "1474" and chrome.owner_module is None
