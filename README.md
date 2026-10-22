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

## Captura de áudio (etapa 2)

Use **dois terminais**, os dois dentro da pasta do projeto e com
`source .venv/bin/activate`.

**Antes de capturar, com o Globoplay já tocando**, monte a saída virtual
"Debate" (precisa ser refeito depois de reiniciar o computador):

```bash
factcheck audio-setup            # cria "Debate", a cópia para ouvir, e move o navegador
factcheck audio-setup --desfazer # remove tudo
```

> **Não mexa no volume do sistema nem no do player do Globoplay**: os dois
> silenciam a gravação (medido em 21/10/2026). Para não ouvir, use o botão
> "Silenciar meu som" do painel — ele cala só a cópia para os alto-falantes.

```bash
# 1. Qual servidor de som e quais saídas existem
factcheck capture --list-sources

# 2. Terminal A — painel (abra http://127.0.0.1:8765; usuário qualquer,
#    senha = PANEL_PASSWORD do .env)
factcheck panel

# 3. Terminal B — captura do som do sistema (Ctrl+C para parar)
factcheck capture
```

Os blocos ficam em `data/audio/run_NNNN/chunks/` e a gravação completa em
`data/audio/run_NNNN/backup/`. Log de cada execução em `data/logs/`.

Sem navegador, para testar o fluxo com um arquivo:

```bash
factcheck capture --file caminho/do/video.mp4 --realtime   # no ritmo real
factcheck capture --file caminho/do/audio.wav              # o mais rápido possível
```

**Se não houver som:** o Globoplay provavelmente está tocando numa saída
diferente da padrão (HDMI, fone Bluetooth). Rode `--list-sources`, veja qual
está `RUNNING` enquanto o vídeo toca, e use `--source NOME` (ou ponha o nome
em `CAPTURE_SOURCE` no `.env`).

## Vozes de referência (obrigatórias)

Sem elas, os rótulos de falante trocam de um bloco para o outro (medido em
22/10/2026) e nada é publicado sozinho. São 2 a 10 segundos de cada pessoa
falando **sozinha**, recortados de qualquer gravação feita com
`factcheck capture`:

```bash
mkdir -p data/voices
# -ss = segundo onde começa, -t = duração
ffmpeg -y -ss 17.7 -t 7.5 -i data/audio/run_0007/chunks/chunk_00001.wav data/voices/flavio.wav
ffplay -autoexit -nodisp data/voices/flavio.wav     # OUÇA: voz trocada troca os candidatos
```

Os caminhos ficam em `[voices]` no `config/debate.toml` (A, B e M = mediador).

## Chaves no `.env`

| chave | para quê |
|---|---|
| `PANEL_PASSWORD` | senha do painel |
| `OPENAI_API_KEY` | transcrição |
| `ANTHROPIC_API_KEY` | intervalo, detecção e verificação |
| `SPACES_BUCKET`, `SPACES_REGION`, `SPACES_KEY`, `SPACES_SECRET` | envio da página e do `timeline.json` |

Antes do primeiro uso real, confira as duas APIs (custa centavos):

```bash
factcheck transcribe-probe data/audio/run_0007/chunks/chunk_00002.wav --ref "Flávio=data/voices/flavio.wav"
factcheck claude-probe
```

## O sistema completo: três terminais

| terminal | comando | o que faz |
|---|---|---|
| 1 | `factcheck panel` | painel em http://127.0.0.1:8765 |
| 2 | `factcheck capture` | grava (depois de `factcheck audio-setup`) |
| 3 | `factcheck pipeline` | transcreve, detecta intervalo, extrai e verifica afirmações, publica |

Para testar a página localmente com o `timeline.json` real:

```bash
factcheck pipeline --publicar-em ../fact-checking-frontend/public
# em outro terminal, no frontend: npm run serve
```

## Pós-debate (plano B)

Mesmo fluxo, outra fonte de áudio. No painel, clique em **Pós-debate**:
nada publica sozinho, você aprova.

```bash
# arquivo de áudio/vídeo seu (o mais rápido possível)
factcheck capture --file debate.mp4
# a gravação de backup do próprio sistema (a pasta com as partes)
factcheck capture --file data/audio/run_0007/backup
# o ao vivo caiu no meio: continua do ponto em que parou
factcheck capture --file debate.mp4 --retomar --inicio-no-arquivo 312
factcheck pipeline --workers 4
```

No pós-debate, informe no painel o **início real do debate** e **em que
segundo do arquivo** ele começa: é isso que dá às checagens o horário da
fala ("22h07"), e não o do processamento.

## Página pública no Spaces

```bash
factcheck deploy-site ../fact-checking-frontend/public --com-exemplos   # só na 1ª vez
factcheck deploy-site ../fact-checking-frontend/public                  # depois
factcheck costs                                                          # gasto estimado
```
