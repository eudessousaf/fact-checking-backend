# fact-checking-backend

Captura o áudio do debate, transcreve, detecta e verifica afirmações, e envia
`timeline.json` para a hospedagem estática. O painel do operador roda só na
máquina local.

- Arquitetura: [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md)
- O que está pronto e o que falta: [`PROGRESSO.md`](PROGRESSO.md)
- Página pública: repositório `fact-checking-frontend`

## Requisitos (Linux)

| item | versão | como conferir |
|---|---|---|
| Python | **3.11 ou mais novo** (usa `tomllib`) | `python3 --version` |
| ffmpeg | com suporte a `pulse` | `ffmpeg -hide_banner -devices \| grep pulse` |
| PipeWire (com `pipewire-pulse`) **ou** PulseAudio | qualquer | `pactl info \| grep "Server Name"` |
| `pactl` | vem com `pulseaudio-utils` | `which pactl` |

No Ubuntu/Debian, o que faltar:

```bash
sudo apt install python3 python3-venv ffmpeg pulseaudio-utils
```

> `pulseaudio-utils` só instala o comando `pactl`; ele **não** troca o
> PipeWire pelo PulseAudio.

## Instalação, do zero

```bash
git clone https://github.com/eudessousaf/fact-checking-backend.git
cd fact-checking-backend
python3 -m venv .venv               # ambiente isolado de pacotes Python
source .venv/bin/activate           # repita este comando em todo terminal novo
pip install -e ".[dev]"

cp .env.example .env                # depois edite: no mínimo PANEL_PASSWORD
cp config/debate.example.toml config/debate.toml
```

## Conferir que está tudo certo

```bash
pytest -q                           # testes automáticos
factcheck check-config              # lê .env e config/debate.toml
factcheck validate examples/*.json  # valida os arquivos de exemplo
```

## Segredos

Chaves de API ficam **só** no `.env`, que está no `.gitignore`. Nunca as
coloque em outro arquivo nem em mensagem de commit.
