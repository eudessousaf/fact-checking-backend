# Arquitetura

## Visão geral

```
[Globoplay no navegador] ──som──► 1. CAPTURA (ffmpeg ← monitor do PipeWire/PulseAudio)
                                     │ blocos de 30 s com 2 s de sobreposição + backup completo
                                     ▼
                                  2. TRANSCRIÇÃO (API em lote, pt-BR + diarização)
                                     ▼
                                  2.5 INTERVALO? (botão manual > frase do mediador > classificação)
                                     ▼ só blocos "debate"
                                  3. ATRIBUIÇÃO (operador mapeia falante → A / B / mediador)
                                     ▼
                                  4. DETECÇÃO de afirmações (modelo barato, JSON)
                                     ▼
                                  5. VERIFICAÇÃO (modelo capaz + busca na web; regras no código)
                                     ▼
                                  6. FILA COM VETO (painel, contagem regressiva)
                                     ▼
                                  7. PUBLICAÇÃO: timeline.json validado ──► CDN ──► leitores
```

O leitor **nunca** fala com esta máquina: ele lê arquivos estáticos na CDN
(rede de servidores que distribui os arquivos). Esta máquina só envia.

## Processos

| processo | comando | por que separado |
|---|---|---|
| captura | `factcheck capture` | é o único que não pode parar; uma falha no painel ou numa API não pode derrubá-lo |
| painel | `factcheck panel` | interface do operador, só em `127.0.0.1` |
| fluxo | (etapas 3–8) | transcreve, detecta, verifica e publica a partir do banco |

Eles conversam pelo **SQLite** (banco num arquivo único, `data/factcheck.sqlite`),
em modo WAL, que deixa um processo escrever enquanto outro lê. Tudo o que importa
fica no banco, e é isso que torna o fluxo **retomável**: cada bloco e cada
checagem tem estado, e o processo que volta continua do primeiro item não
concluído, sem refazer o que já foi feito.

## Fonte de áudio é uma peça trocável

O ao vivo e o pós-debate são o **mesmo fluxo** com fontes diferentes:

| fonte | quando |
|---|---|
| `pulse` — monitor da saída de som | ao vivo; reprise tocando no navegador |
| `file` — arquivo local | pós-debate; backup do próprio sistema; testes na nuvem |

As duas entregam o mesmo áudio (mono, 16 kHz, 16 bits) ao mesmo cortador de blocos.

## Contrato com o frontend

`schemas/timeline.schema.json` e `schemas/ads.schema.json` são a fonte da
verdade e são copiados **byte a byte** para o frontend. O teste
`test_schema_identical_in_frontend` e o `npm test` do frontend reprovam se
divergirem.

O backend valida em duas camadas antes de enviar (`factcheck/contract.py`):

1. **JSON Schema** — forma: campos, tipos, só três vereditos públicos,
   `SEM_EVIDENCIA` impossível de publicar, no mínimo 2 fontes,
   `what_is_off` obrigatório em NÃO É BEM ASSIM, imagem de anúncio só da
   própria CDN.
2. **Conferências que o schema não expressa** — `stats` batendo exatamente
   com os itens publicados, percentuais que somam 100, `display` coerente com
   o mínimo, indicadores coerentes com as fontes, explicação com até 3 frases.

Um item despublicado continua no arquivo como `state: "removido"`, **sem** o
conteúdo retirado — para não sumir em silêncio e para o conteúdo errado não
continuar público.

## Riscos de arquitetura já identificados

Ver `PROGRESSO.md`, seção "Riscos abertos".
