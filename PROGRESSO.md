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
