# Progresso

## Etapa 1 — estrutura e contrato (21/10/2026)

**Feito**

- Estrutura Python (`src/factcheck`), `pyproject.toml`, `.env.example`,
  `config/debate.example.toml` (nomes dos candidatos e do mediador ficam
  aqui, nunca no código).
- JSON Schema de `timeline.json` e `ads.json` em `schemas/`, copiados
  idênticos para o frontend.
- `factcheck/contract.py`: validação por schema + conferências semânticas
  (stats × itens, percentuais, indicadores × fontes, limite de 3 frases).
- `factcheck/stats.py`: cálculo do gráfico por candidato (maior resto, para
  os percentuais somarem 100).
- Exemplos válidos em `examples/`, com candidatos **fictícios** — um exemplo
  com nomes reais e checagens inventadas seria material enganoso se vazasse.
- CLI: `factcheck validate`, `factcheck check-config`.

**Como testar**: seção "Conferir que está tudo certo" do README.

**Decisões do contrato que vale conhecer**

- `mode` (`ao_vivo`/`pos_debate`) e `status` (`aguardando`/`em_andamento`/
  `intervalo`/`encerrado`) são campos separados: um debate checado depois
  continua tendo intervalos e fim.
- `sequence` só cresce: a página ignora um arquivo mais velho que o que já
  mostra (cache atrasado da CDN).
- Item removido mantém só id, horários e candidato; o conteúdo retirado não
  volta a público.
- Anúncio só aceita imagem por caminho relativo (`ads/...`): a imagem não
  pode vir de servidor de terceiros.

## Etapa 2 — captura de áudio (21/10/2026)

**Feito**

- `factcheck capture`: lê o monitor da saída de som (PipeWire ou PulseAudio,
  detectado pelo `pactl info`) com ffmpeg, em mono 16 kHz.
- Blocos de 30 s com 2 s de sobreposição, contados em amostras de áudio, não
  no relógio do computador. Cada bloco vai para o banco com deslocamento e,
  no ao vivo, horário de parede.
- Gravação completa em partes de 10 min (`backup_0001.wav`, …).
- Nível em dBFS e alerta de silêncio (padrão: 20 s abaixo de -50 dBFS).
- Se o ffmpeg cair no ao vivo, a captura tenta de novo sozinha (1, 2, 4, 8,
  10 s) sem parar o resto.
- A mesma captura lê **arquivo** (`--file`, com `--realtime` e `--start`): é
  a entrada do pós-debate e o que permitiu testar tudo na nuvem.
- Painel mínimo (`factcheck panel`): senha obrigatória, recusa subir fora de
  localhost, indicador OUVINDO / SILÊNCIO / CAPTURA PARADA / RECONECTANDO.

**Testado na nuvem (sem áudio real)**

- 34 testes automáticos, incluindo ponta a ponta com arquivo gerado: tom de
  70 s + 25 s de silêncio → blocos 0–30, 28–58, 56–86, 84–95 s; backup com
  exatamente 95 s; alerta de silêncio disparado.
- Captura e painel rodando juntos: o painel acompanhou nível, silêncio e
  blocos ao vivo; responde 401 sem senha.
- Processo de captura morto à força (`kill -9`): em 5 s o painel passa a
  dizer CAPTURA PARADA, e não continua mostrando "ouvindo".
- Ctrl+C no terminal: encerra em ordem, grava o último bloco parcial.

**NÃO testado — depende da máquina do Eudes**

- Leitura do monitor real do PipeWire/PulseAudio (`-f pulse`).
- Som do **Globoplay** especificamente. Ele usa DRM (proteção contra cópia);
  o esperado é que o DRM bloqueie a imagem, não o som que sai pela placa —
  mas isso precisa ser visto, não suposto.
- Se o volume do sistema ou o mudo afetam o que o monitor recebe.

**Pendente**: o roteiro de teste está em "Teste da etapa 2" abaixo.

### Teste da etapa 2 (na máquina do Eudes)

1. `factcheck capture --list-sources` — anote a saída.
2. Terminal A: `factcheck panel`; abra http://127.0.0.1:8765.
3. Toque um vídeo **do Globoplay** (qualquer conteúdo) no navegador.
4. Terminal B: `factcheck capture`. Esperado: painel em OUVINDO, barra de
   nível mexendo; a cada ~28 s uma linha `bloco 0000N` no terminal.
5. Pause o vídeo por 25 s. Esperado: SILÊNCIO no painel e aviso no terminal.
   Volte a tocar: o aviso some.
6. Baixe o volume do sistema para ~20% e depois ponha no mudo, 30 s em cada.
   Anote o nível (dBFS) nos dois casos.
7. Ctrl+C no terminal B. Esperado: `encerrada (stopped)`.
8. Ouça um bloco: `aplay data/audio/run_0001/chunks/chunk_00002.wav`. Deve
   ser o áudio do vídeo, claro, sem picotes.

Mande: a saída do passo 1, o nível típico com o vídeo tocando, os níveis do
passo 6, e se o passo 8 soou bem.

### Resultado do teste da etapa 2 (21/10/2026, máquina do Eudes)

Ubuntu com PipeWire 1.6.2, Python 3.14, ffmpeg 8.0. 32 testes passando lá.

| pergunta | resultado medido |
|---|---|
| a captura lê o som do sistema? | sim — bipe a -17,6 dB pela saída interna |
| o som do Globoplay passa (DRM)? | sim — blocos entre -21 e -22 dBFS |
| alerta de silêncio | dispara aos 20 s e some quando o som volta |
| volume do **sistema** zerado | **silencia a gravação**: o Chrome corta o próprio som quando a saída padrão fica muda (Globoplay a -91 dB; bipe direto no "Debate", a -21 dB) |
| volume do **player** zerado | silencia a gravação — sem conserto, o som sai vazio dele |
| silenciar só a cópia para os alto-falantes | gravação intacta (-26 dB com o operador sem ouvir) |
| monitor do fone Bluetooth | não testado; deixou de importar com a saída virtual |

Três achados durante o teste, que viraram regra:

- O `pactl` em português escreve "Nome do servidor:"; o código já força
  `LC_ALL=C`, mas comandos manuais também precisam.
- Faltavam `ffmpeg`, `pulseaudio-utils` e `python3.14-venv` no Ubuntu.
- O operador PRECISA poder não ouvir durante o intervalo e voltar a ouvir.
  Isso levou à saída virtual abaixo.

## Etapa 2b — saída virtual "Debate" (21/10/2026)

O plano dizia "não precisa de cabo virtual". Para o uso real, precisa: sem
ela, o único jeito de não ouvir é zerar um volume, e os dois volumes
disponíveis silenciam a gravação.

**Feito**

- `factcheck audio-setup`: cria a saída "Debate" e a cópia para os
  alto-falantes, move o navegador para o "Debate". Pode rodar quantas vezes
  quiser; `--desfazer` remove. A cópia aponta para a saída real POR NOME —
  apontar para "a padrão" criaria um laço se a padrão virasse o "Debate".
- A captura usa `debate.monitor` sozinha quando ele existe, e avisa quando
  não existe.
- Painel: estado da rota (saída, cópia, navegador no "Debate"), alerta se o
  navegador sair da rota (ex.: página recarregada), botão "Silenciar meu
  som / Ouvir de novo", e a regra fixa na tela.
- Toda ação do painel exige o cabeçalho `X-Painel`: um site qualquer aberto
  no mesmo navegador não consegue acionar o painel.
- O painel grava log em arquivo (`data/logs/panel_*.log`).

**Teste na máquina do Eudes**

1. Reinicie nada; se a rota manual de hoje ainda existir, rode primeiro
   `factcheck audio-setup --desfazer`.
2. Globoplay tocando → `factcheck audio-setup`. Esperado: "Rota pronta".
3. `factcheck panel` e `factcheck capture` (sem `--source`): a captura deve
   dizer `capturando de debate.monitor`.
4. No painel, "Silenciar meu som" por 30 s: você não ouve, e o painel
   continua em OUVINDO, sem alerta de silêncio.

### Resultado do teste da etapa 2b (21/10/2026, máquina do Eudes)

- `factcheck audio-setup`: "Rota pronta: 1 conexão(ões) do navegador no
  'Debate'". O Chrome recria a conexão de som ao pausar (1696 → 2022), e o
  PipeWire a devolve sozinho ao "Debate" pelo nome do programa
  (`module-stream-restore`).
- `factcheck capture` sem `--source` escolheu `debate.monitor` sozinho.
- Blocos 1 a 4 com som: -26,3 a -26,7 dBFS. **Blocos 5 a 7 com "Silenciar
  meu som" ligado: -26,4, -26,2 e -26,2 dBFS** — a gravação não percebeu o
  silêncio do operador, e nenhum alerta disparou.

**Etapa 2 validada.** Fica para o checklist do dia: rodar `audio-setup` com o
Globoplay já tocando, depois de qualquer reinício do computador.

## Etapa 3 — transcrição: sondagem (21/10/2026)

**Escolha do Eudes:** OpenAI `gpt-4o-transcribe-diarize`, com US$ 5 de crédito
para começar. A API do Claude não aceita áudio (conferido na referência da
API), então o crédito da Anthropic fica para detecção e verificação.

Comparação apresentada: OpenAI (~US$ 0,006–0,017/min, reconhece a pessoa
pela voz entre blocos), Deepgram (~US$ 0,0043/min, pt-BR melhorado em
mai/2026, sem identidade entre blocos), AssemblyAI (~US$ 0,15–0,21/h,
identifica pelo CONTEXTO da fala — o tipo de palpite que não pode atribuir
fala a candidato).

**Feito**

- `transcription/base.py`: interface `Transcriber` — o resto do sistema
  não conhece fornecedor.
- `transcription/openai_diarize.py`: chamada direta por HTTP; até 4 vozes de
  referência; repete em erro de rede, 429 e 5xx (2, 4, 8 s); NÃO repete chave
  errada, pedido inválido nem falta de crédito (repetir gastaria e não
  resolveria). Falante ausente vira "?", nunca palpite.
- `factcheck transcribe-probe ARQUIVO [--ref NOME=voz.wav ...]`: manda um
  arquivo e guarda a resposta crua em `data/probes/`.

**Por que parar na sondagem:** a rede da nuvem não alcança a OpenAI, e o
formato da resposta veio de busca, não da documentação lida direto. O resto
da etapa (sobreposição entre blocos, fila no banco) só é escrito depois de
ver a resposta real.
