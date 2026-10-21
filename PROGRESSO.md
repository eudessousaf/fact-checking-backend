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
