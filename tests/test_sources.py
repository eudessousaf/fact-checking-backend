from factcheck.capture.sources import (
    describe_server,
    ffmpeg_file_cmd,
    parse_default_sink,
    parse_server_name,
    parse_sources,
)

PIPEWIRE_INFO = """Server String: /run/user/1000/pulse/native
Library Protocol Version: 35
Server Protocol Version: 35
Is Local: yes
Client Index: 120
Tile Size: 65472
User Name: eudes
Host Name: notebook
Server Name: PulseAudio (on PipeWire 1.0.5)
Server Version: 15.0.0
Default Sample Specification: float32le 2ch 48000Hz
Default Channel Map: front-left,front-right
Default Sink: alsa_output.pci-0000_00_1f.3.analog-stereo
Default Source: alsa_input.pci-0000_00_1f.3.analog-stereo
Cookie: 1234:abcd
"""

PULSE_INFO = PIPEWIRE_INFO.replace("PulseAudio (on PipeWire 1.0.5)", "pulseaudio")

SOURCES = """Source #45
	State: SUSPENDED
	Name: alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
	Description: Monitor of Built-in Audio Analog Stereo
	Driver: PipeWire
	Sample Specification: s32le 2ch 48000Hz
	Monitor of Sink: alsa_output.pci-0000_00_1f.3.analog-stereo
	Properties:
		device.description = "Monitor of Built-in Audio Analog Stereo"

Source #46
	State: SUSPENDED
	Name: alsa_input.pci-0000_00_1f.3.analog-stereo
	Description: Built-in Audio Analog Stereo
	Driver: PipeWire
	Monitor of Sink: n/a

Source #52
	State: RUNNING
	Name: alsa_output.pci-0000_01_00.1.hdmi-stereo.monitor
	Description: Monitor of HDMI Audio
	Driver: PipeWire
	Monitor of Sink: alsa_output.pci-0000_01_00.1.hdmi-stereo
"""


def test_server_detection():
    assert describe_server(parse_server_name(PIPEWIRE_INFO)) == "PipeWire"
    assert describe_server(parse_server_name(PULSE_INFO)) == "PulseAudio"


def test_only_monitors_listed_default_first():
    sources = parse_sources(SOURCES, parse_default_sink(PIPEWIRE_INFO))
    assert [s.name for s in sources] == [
        "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
        "alsa_output.pci-0000_01_00.1.hdmi-stereo.monitor",
    ]
    assert sources[0].is_default and not sources[1].is_default
    assert sources[1].state == "RUNNING"


def test_property_lines_do_not_override_description():
    sources = parse_sources(SOURCES, None)
    assert sources[0].description == "Monitor of Built-in Audio Analog Stereo"


def test_file_cmd_resume_and_realtime():
    from pathlib import Path
    cmd = ffmpeg_file_cmd(Path("/x/debate.mp4"), realtime=True, start_seconds=600)
    assert "-re" in cmd and cmd[cmd.index("-ss") + 1] == "600.000"
    assert cmd.index("-ss") < cmd.index("-i")  # -ss antes de -i: busca rápida
    assert cmd[-7:] == ["-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
