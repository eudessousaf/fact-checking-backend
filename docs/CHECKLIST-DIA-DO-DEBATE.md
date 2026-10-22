# Checklist do dia do debate — sexta, 23/10/2026

Debate da TV Globo a partir de ~21h20. Siga na ordem.

## Até 20h30 — preparação

- [ ] Computador na tomada, sem economia de energia nem suspensão automática.
- [ ] **Fone com fio ou alto-falantes do computador** — Bluetooth pode cair e,
      com ele, a cópia para você ouvir (a gravação não para, mas você fica surdo).
- [ ] `git pull` no backend e no frontend.
- [ ] `config/debate.toml` com os nomes certos e as vozes:
      `A` Lula, `B` Flávio Bolsonaro, **`M` César Tralli** (não o mediador da Band).
- [ ] Ouça as três vozes: `for v in data/voices/*.wav; do echo $v; ffplay -autoexit -nodisp $v; done`
- [ ] `ENVIRONMENT=producao` no `.env` (no teste era `teste`).
- [ ] Saldo: OpenAI ≥ o dobro do custo medido no teste; Claude ≥ US$ 20 livres.
- [ ] `factcheck claude-probe` responde (custa ~US$ 0,10).
- [ ] Página no ar: abrir a URL da CDN no celular, fora do Wi-Fi de casa.

## 20h45 — ligar, nesta ordem

1. Chrome com o **Globoplay logado**, na transmissão da Globo, **volume do player no máximo**.
2. Terminal 1: `factcheck audio-setup` → precisa dizer **Rota pronta: 1 conexão…**
3. Terminal 1: `factcheck panel` → abrir http://127.0.0.1:8765
4. Terminal 2: `factcheck capture` → painel em **OUVINDO**.
5. Terminal 3: `factcheck pipeline`.
6. No painel: **Ao vivo** (modo). Estado **AGUARDANDO** até o debate começar.

## Durante

- Quando o mediador abrir o debate: **Debate começou**.
- Intervalo: **Entrou em intervalo** assim que perceber; **Voltou ao debate** na volta.
  O sistema tenta sozinho, mas o botão sempre vence.
- Para não ouvir: **Silenciar meu som**. **Nunca** o volume do sistema, as teclas de
  volume ou o volume do player — os três silenciam a gravação.
- Fila: cada checagem publica sozinha em 3 min. Leia; **Bloquear** se tiver dúvida.
- Algo estranho na página: **Pausar publicação** primeiro, investigar depois.
- Olhe a "Transcrição recente": se o nome de quem fala estiver trocado,
  **Pausar publicação** e me chame — é o pior erro possível.
- Fim: **Debate encerrado**. Deixe o pipeline terminar a fila.

## Se algo cair

| sintoma no painel | o que fazer |
|---|---|
| CAPTURA PARADA | terminal 2: `factcheck capture` de novo (vira uma nova captura; nada se perde do que já foi gravado) |
| SILÊNCIO | Globoplay travou ou deslogou: recarregar a página, logar, dar play; depois `factcheck audio-setup` |
| NAVEGADOR FORA DA ROTA | `factcheck audio-setup` |
| fila parada | terminal 3: Ctrl+C e `factcheck pipeline` de novo — ele retoma do ponto onde parou |
| painel não abre | terminal 1: `factcheck panel` de novo (a captura e o pipeline não dependem dele) |
| TETO DE US$ 100 | as checagens param; a captura e a transcrição seguem; decidir se libera mais |

## Plano B — pós-debate

Se o ao vivo não funcionar, a página existe do mesmo jeito, só que depois:

1. No painel: **Pós-debate** (nada publica sozinho).
2. Fonte do áudio, a melhor disponível:
   - o backup do próprio sistema: `factcheck capture --file data/audio/run_NNNN/backup`
   - um arquivo do debate: `factcheck capture --file debate.mp4`
   - o ao vivo caiu no meio: `factcheck capture --file debate.mp4 --retomar --inicio-no-arquivo SEGUNDOS`
   - a reprise no Globoplay: o mesmo procedimento do ao vivo (leva as 2 horas)
3. `factcheck pipeline --workers 4`
4. No painel: informar o **início real do debate** e o segundo do arquivo em que ele começa.
5. Revisar a fila e **Aprovar todas da fila** (ou uma a uma).
