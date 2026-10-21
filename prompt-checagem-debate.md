# Prompt — Sistema de checagem de fatos do debate presidencial

## Contexto

Você vai construir, do zero, um sistema que acompanha um debate presidencial e publica uma linha do tempo de checagens de fatos, com fontes, alguns minutos depois de cada afirmação.

- **Alvo:** debate da TV Globo do 2º turno, sexta-feira 23/10/2026, a partir de ~21h20 (horário de Brasília), duração ~2h. Candidatos: Luiz Inácio Lula da Silva e Flávio Bolsonaro. Mediador: César Tralli. Os nomes ficam em configuração, nunca fixos no código.
- **Prazo duro:** o sistema precisa passar por um teste completo, de ponta a ponta, até **quinta-feira 22/10 à noite**. Se não passar, ele não vai ao ar.
- **Máquina:** Linux, que é onde a captura e o processamento rodam. O debate será assistido pelo Globoplay no navegador dessa máquina.
- **Onde você (agente) roda:** numa sessão do Claude Code na nuvem, com dois repositórios novos no GitHub (veja "Repositórios" abaixo). **A sua máquina virtual não tem áudio nem acesso ao Globoplay**, e a rede pode ser restrita. Por isso:
  - Tudo o que depende de áudio real ou de chaves de API reais é **testado pelo Eudes na máquina Linux dele**. Para cada etapa, entregue os comandos exatos para ele rodar localmente (`git pull`, instalação, execução) e o que ele deve observar.
  - Na nuvem, teste com **simulações** (*mocks*: respostas falsas das APIs) e arquivos de áudio de exemplo gerados por você.
  - **Nunca peça nem grave chaves de API no repositório.** Elas ficam só no `.env` local do Eudes.
- **Operador:** uma única pessoa (Eudes). Ele tem conhecimento técnico, mas não é especialista. Use termos moderados e explique qualquer termo técnico na primeira vez que aparecer.
- **Orçamento de API:** teto de US$ 100 no total, somando testes e o dia do debate.

## Repositórios

O projeto é dividido em **dois repositórios**:

| Repositório | Conteúdo |
|---|---|
| **`fact-checking-backend`** | Python: captura de áudio, transcrição, detecção de intervalo, atribuição de falante, detecção de afirmações, verificação, fila com veto, **painel do operador** (FastAPI, só localhost), modo pós-debate, controle de custo, envio do `timeline.json` e do `ads.json` para a hospedagem, testes e relatório gabarito |
| **`fact-checking-frontend`** | Página pública estática: linha do tempo, gráfico por candidato, espaço publicitário, seção "Como checamos", configuração de deploy na hospedagem estática e teste de carga |

- **O contrato entre os dois é feito pelos arquivos `timeline.json` e `ads.json`.** Antes de escrever qualquer código, defina o formato exato desses arquivos com JSON Schema (uma descrição formal de quais campos existem e de que tipo são). O schema mora no backend, em `schemas/`, e é **copiado de forma idêntica** para o frontend. O backend valida o arquivo **antes de enviar**, e o frontend tolera campos ausentes sem quebrar. Qualquer mudança no schema é feita nos dois repositórios no mesmo passo.
- Inclua no frontend um `timeline.json` e um `ads.json` de exemplo, válidos pelo schema, para a página funcionar e ser testada sem o backend.
- Se a sessão na nuvem tiver acesso aos dois repositórios, trabalhe nos dois. Se tiver acesso a só um, avise o Eudes no início e diga o que precisa ser feito numa sessão separada no outro.
- **Cada repositório tem um `README.md` com o passo a passo para rodar localmente no Linux**, do zero: clonar, instalar dependências, criar o `.env` a partir do `.env.example`, rodar e testar. O Eudes vai clonar e rodar seguindo só esse README.

## Forma de trabalhar (obrigatório)

1. **Trabalhe de forma autônoma pelas etapas**, com um commit por etapa e mensagem clara (ex.: `etapa 3: transcrição com diarização`). Ao fim de cada etapa, registre num arquivo `PROGRESSO.md`, em cada repositório, o que foi feito, como testar e o que ficou pendente.
   - **Paradas obrigatórias:** pare e peça ao Eudes que teste localmente **depois da etapa 2 (captura de áudio)** e **depois da etapa 3 (transcrição)**. Essas etapas não podem ser validadas na nuvem e são as de maior risco. Não construa o resto em cima delas sem confirmação.
   - Também pare antes de qualquer escolha que dependa do Eudes: provedor de transcrição, hospedagem, audiência esperada.
2. **Nunca assuma que algo existe.** Verifique pacotes, versões, nomes de modelos e parâmetros de API na documentação oficial antes de usar.
3. Depois de cada modificação, revise: imports quebrados, erros de tipo, dependências faltando, falhas não tratadas.
4. Escreva código limpo e modular, fácil de depurar, com logs claros. Trabalhe como uma equipe sênior de produção.
5. Antes de cada etapa, diga em poucas linhas o que pode quebrar.
6. Segredos (chaves de API) ficam em `.env`, que deve estar no `.gitignore`, junto com um `.env.example` sem valores.
7. Se algo do plano abaixo se mostrar inviável ou pior do que uma alternativa, **diga e proponha a alternativa antes de mudar**.

## Arquitetura

```
[Globoplay no navegador]
        │ som do sistema
        ▼
1. CAPTURA ─ ffmpeg lendo o "monitor" do PipeWire/PulseAudio
        │ blocos de áudio de ~30s com pequena sobreposição + gravação completa de backup
        ▼
2. TRANSCRIÇÃO ─ API com suporte a pt-BR e diarização (separação de quem fala)
        │ texto com rótulos de falante (Falante 1, 2, 3)
        ▼
2.5 DETECÇÃO DE INTERVALO ─ classifica cada bloco: debate / intervalo comercial / vinheta
        │ só blocos "debate" seguem adiante
        ▼
3. ATRIBUIÇÃO ─ o operador mapeia, no painel, Falante 1/2/3 → Candidato A / Candidato B / Mediador
        ▼
4. DETECÇÃO ─ modelo barato extrai só AFIRMAÇÕES FACTUAIS VERIFICÁVEIS
        ▼
5. VERIFICAÇÃO ─ modelo mais capaz + busca na web → veredito em categoria + fontes
        ▼
6. FILA COM VETO ─ publicação automática após N minutos, a menos que o operador bloqueie
        ▼
7. PUBLICAÇÃO ─ timeline.json enviado para hospedagem estática → página pública
```

### Linguagem e estrutura

- Back-end em **Python 3.11+**, com **FastAPI** para o painel do operador e **SQLite** como banco local.
- Cada etapa do fluxo é um módulo separado, com interface clara, para que um provedor possa ser trocado sem reescrever o resto. Isso vale especialmente para o provedor de transcrição.
- O fluxo precisa ser **retomável**: se o processo cair, ele volta de onde parou sem duplicar checagens.
- Falhas de API usam nova tentativa com espera crescente. Uma falha numa checagem nunca derruba o sistema inteiro.

### 1. Captura de áudio (Linux)

- Use o *monitor source* da saída de som (PipeWire com camada PulseAudio). Não precisa de cabo virtual. Liste as fontes disponíveis e deixe o operador escolher qual usar.
- O áudio vai em mono, 16 kHz, cortado em blocos de ~30s com ~2s de sobreposição.
- Grave também o áudio completo em disco como backup.
- Mostre no painel um **indicador de "ouvindo"** (nível de áudio e último bloco recebido) e um alerta se ficar mais de 20s em silêncio. Esse silêncio indica que o Globoplay travou ou deslogou.

### 2. Transcrição

- Escolha um provedor com **pt-BR e diarização**. Compare 2–3 opções em preço, qualidade em português e diarização, e apresente a escolha ao Eudes antes de integrar.
- Use processamento em lote por bloco, não streaming. O atraso de minutos é aceitável, e o lote é mais simples e robusto.
- Cuide da sobreposição entre blocos para não duplicar frases.

### 2.5 Detecção de intervalo comercial

- **Por que importa:** durante o intervalo podem passar comerciais e **inserções de propaganda eleitoral** dos próprios candidatos, cheias de afirmações. Nada disso é fala do debate e **nunca pode ser checado como se fosse**. Atribuir a um candidato algo dito num comercial é um erro grave.
- **Três sinais, nesta ordem de prioridade:**
  1. **Botão manual do operador** ("Entrou em intervalo" / "Voltou ao debate"). Ele sempre vence os sinais automáticos.
  2. **Frases de transição do mediador**, como "vamos para o intervalo", "voltamos ao debate" ou "fim do bloco". Detectar por regras simples + modelo barato.
  3. **Classificação de cada bloco** pelo modelo barato (debate / comercial / vinheta / indefinido). Use como pistas: falantes que não são os mapeados, nomes de marcas, jingles, mudança brusca de assunto e diarização com muitos falantes novos.
- **Regra de segurança:** um bloco classificado como "indefinido" é tratado como intervalo (não é checado) e gera alerta no painel.
- **Retomada:** sair do intervalo exige o sinal manual ou dois blocos seguidos classificados como "debate" com falantes já mapeados.
- **Durante o intervalo:** a captura e a transcrição continuam, para detectar a volta, mas nenhuma afirmação é extraída. O painel mostra o estado atual em destaque. A página pública mostra o status "Intervalo comercial".
- **Teste:** o debate da Band tem intervalos. O relatório de teste deve listar os intervalos detectados com início e fim, para o Eudes conferir.

### 3. Atribuição de falante

- **Errar quem disse o quê é o pior erro possível.**
- No painel, o operador mapeia os rótulos do provedor para Candidato A, Candidato B ou Mediador, e pode corrigir a qualquer momento.
- Trechos com falante indefinido ou de baixa confiança **nunca são publicados automaticamente**. Eles ficam presos na fila até o operador resolver.
- Falas do mediador não são checadas.

### 4. Detecção de afirmações

- Use um modelo barato e rápido, como o Claude Haiku 5.5 (ID `claude-haiku-5-5`; confirme na documentação), com saída em JSON estruturado.
- Extraia **apenas afirmações factuais verificáveis**: números, datas, fatos sobre leis, votações, gastos, indicadores ou ações passadas.
- **Descarte** opiniões, promessas, previsões, ataques retóricos e juízos de valor. Por exemplo, "você destruiu o país" não é checável, mas "o desemprego subiu 3 pontos no seu governo" é.
- Para cada afirmação, registre: falante, horário no debate, trecho literal da transcrição e a afirmação reformulada de forma neutra e verificável.
- Evite checar a mesma afirmação duas vezes quando ela for repetida.

### 5. Verificação

- Use um modelo mais capaz, como o Claude Sonnet 5.5 (ID `claude-sonnet-5-5`; confirme na documentação), com a ferramenta de **busca na web** da API (confirme a versão atual da ferramenta na documentação da Anthropic).
- **Títulos públicos das checagens: apenas três** (não use porcentagem de confiança em nenhum lugar):
  - **VERDADEIRO**: a afirmação confere com os dados.
  - **FALSO**: a afirmação contradiz os dados.
  - **NÃO É BEM ASSIM**: tem base real, mas está exagerada, imprecisa, fora de contexto ou omite algo que muda o sentido.
- **Status interno, que nunca aparece como título público:** `SEM_EVIDENCIA`, para quando as fontes não bastam para nenhum dos três títulos. Esses itens **não são publicados** e **não entram no gráfico**. Eles aparecem só no painel e no relatório, para revisão.
  - **Motivo:** forçar uma afirmação sem evidência num dos três títulos é o erro mais provável do sistema. "Não é bem assim" não pode virar a gaveta do "não sei", porque esse título insinua que o candidato distorceu algo.
- **Regras de evidência (o código valida, não só o prompt):**
  - Os três títulos exigem pelo menos **2 fontes independentes**, com preferência por fontes primárias como IBGE, Ipea, Banco Central, Tesouro, Portal da Transparência, Diário Oficial, Câmara, Senado e TSE, ou por checagens já publicadas por agências reconhecidas (Lupa, Aos Fatos, Estadão Verifica, Fato ou Fake, UOL Confere etc.).
  - "NÃO É BEM ASSIM" precisa dizer **explicitamente o que está errado ou faltando** (exagero, número diferente, contexto omitido). Sem isso, vira `SEM_EVIDENCIA`.
  - Se as exigências não forem atingidas, o status vira `SEM_EVIDENCIA` automaticamente.
  - Toda fonte citada precisa ser uma URL que de fato veio do resultado da busca. Nada de URL inventada.
- **Indicadores objetivos** (no lugar da porcentagem): número de fontes, se há fonte primária (sim/não) e se já existe checagem de agência (sim/não).
- A explicação deve ter até 3 frases, em linguagem simples e tom neutro.
- **Simetria entre candidatos:** o mesmo prompt e as mesmas regras para os dois. O nome do candidato não entra no prompt de verificação, só a afirmação. Mostre no painel quantas checagens há por candidato e por categoria, para o operador perceber desequilíbrios.
- Registre o custo de cada chamada. Acima de US$ 80 acumulados, avise no painel. Em US$ 100, pare as novas checagens.

### 6. Fila com veto (painel do operador)

- O painel é acessível **só localmente** (localhost), com senha simples definida no `.env`.
- Cada checagem pronta entra na fila com uma contagem regressiva configurável (padrão: 3 minutos) e é publicada automaticamente ao fim dela.
- O operador pode **bloquear**, **publicar agora**, **editar o veredito ou a explicação** e **despublicar** algo já publicado.
- Há um botão de **pausa geral**: interrompe toda publicação automática na hora.
- Tudo o que o operador faz fica registrado em log.

### 7. Página pública

- É uma página estática (HTML/CSS/JS simples ou React leve) que lê `timeline.json` periodicamente.
- **A página pública não é servida pela máquina do Eudes.** O back-end envia o `timeline.json` para uma hospedagem estática ou um storage com CDN (rede de servidores que distribui o arquivo) e cache curto. Proponha 2 opções e pergunte ao Eudes antes de configurar.

#### Conteúdo da página

- **Status no topo:** "Debate em andamento", "Intervalo comercial", "Debate encerrado" ou "Checagem feita após o debate, com base na gravação". O status vem do campo correspondente no `timeline.json`.
- **Aviso fixo:** checagem feita com auxílio de IA, pode conter erros, com um link ou e-mail para reportar erros.
- Cada item da linha do tempo tem como **título o veredito em destaque (VERDADEIRO / FALSO / NÃO É BEM ASSIM)**, com cor e ícone. Abaixo vêm: horário, candidato, trecho dito (marcado como "transcrição automática"), explicação curta, indicadores objetivos e links das fontes. As cores devem ser distinguíveis também por quem tem daltonismo; o ícone e o texto carregam o significado, não só a cor.
- **Transparência em correções:** um item despublicado aparece como "Checagem removida/corrigida pela equipe" com horário. Ele não some silenciosamente.
- A página deve funcionar bem no celular.
- **Sem áudio nem vídeo da transmissão.** Só texto.

#### Aguentar muitos acessos simultâneos (requisito obrigatório)

- **Nenhum servidor próprio no caminho do leitor.** Leitor → CDN → arquivos estáticos. O back-end só *envia* arquivos para lá. Ele nunca responde a visitantes.
- **Prefira provedores sem cobrança por tráfego de saída** (o volume de dados entregue aos leitores), ou com limite alto no plano gratuito. Confirme na documentação os limites e o que acontece quando são ultrapassados: se o site sai do ar ou se gera cobrança. Um site que cai no pico de audiência é o pior cenário.
- **`timeline.json` pequeno:** só as checagens publicadas, com texto enxuto, servido comprimido. Se passar de ~200 KB, divida em `timeline-latest.json` (últimos itens) + arquivo completo carregado sob demanda.
- **Atualização do leitor:** consulta a cada 20–30s com variação aleatória de alguns segundos, para milhares de navegadores não pedirem no mesmo instante. Use `ETag`/`If-None-Match` (o navegador pergunta "mudou?" e recebe resposta vazia se não mudou). Cache na CDN de ~10–15s.
- **Página leve:** sem frameworks pesados, sem fontes externas desnecessárias, imagens otimizadas. Nada de terceiros pode bloquear o carregamento da linha do tempo.
- **Falha graciosa:** se uma atualização falhar, a página mantém o que já mostrava e tenta de novo, sem tela de erro.
- **Teste de carga:** antes de sexta, simular acessos simultâneos (por exemplo, com k6) contra a URL real e reportar o resultado. Pergunte ao Eudes qual audiência ele espera para definir a meta do teste.

#### Gráfico de vereditos por candidato

- **Barras horizontais empilhadas, uma por candidato**, com a % de VERDADEIRO / NÃO É BEM ASSIM / FALSO, atualizadas junto com a linha do tempo.
- **Feito em SVG ou CSS puro, sem biblioteca de gráficos**, para manter a página leve no pico de acesso.
- **Os números vêm do back-end, dentro do `timeline.json`**, num bloco `stats`. Eles não são recalculados no navegador. Assim, página e painel mostram sempre a mesma coisa.
- **Contam só itens publicados.** Itens bloqueados, despublicados ou `SEM_EVIDENCIA` não contam. Quando algo é despublicado, o gráfico se recalcula.
- **Regras contra leitura enganosa (obrigatórias):**
  - **Sempre mostrar a quantidade absoluta ao lado da %**, por exemplo "FALSO 40% (4 de 10)". Porcentagem sem o total é proibida.
  - **Mínimo para exibir:** o gráfico de um candidato só aparece com pelo menos **8 checagens publicadas** (valor configurável). Antes disso, mostrar "Dados insuficientes para comparação (X checagens até agora)".
  - **Nota fixa sob o gráfico:** "Percentual sobre as afirmações checadas, não sobre tudo o que o candidato disse. Afirmações opinativas ou sem dados suficientes não entram na conta." Incluir link para uma seção "Como checamos" na própria página.
  - No painel, mostrar ao operador quantas afirmações de cada candidato foram detectadas, checadas, publicadas e descartadas como `SEM_EVIDENCIA`, para ele perceber se o sistema está checando um candidato muito mais que o outro.

### 8. Espaço publicitário (pronto, mas desligado por padrão)

- **Dois formatos configuráveis:** faixa fixa (rodapé ou topo) e modal (janela sobreposta).
- **Configuração em um arquivo `ads.json` na CDN, controlado pelo painel**, para ligar, desligar ou trocar o anúncio sem publicar uma nova versão do site. Campos: ativo (sim/não), formato, imagem, link, texto alternativo e anunciante.
- **Regras que o código precisa garantir:**
  - Todo anúncio tem o rótulo visível **"Publicidade"**.
  - **O modal nunca bloqueia a linha do tempo de forma persistente:** aparece no máximo uma vez por visita, só depois de alguns segundos, com botão de fechar grande e visível, e fecha com ESC ou clique fora. A faixa fixa não pode cobrir conteúdo; reserve espaço para ela no layout.
  - **A imagem do anúncio é servida pela mesma CDN** e carregada depois da linha do tempo. Se o anúncio falhar, a página segue normal.
  - **Sem scripts de redes de anúncio de terceiros nesta versão.** Eles pesam a página, podem exibir anúncios fora do controle do Eudes (inclusive políticos) e exigem banner de cookies pela LGPD (lei de proteção de dados). Se um dia forem adicionados, isso é outra etapa, com aviso de cookies.
  - **Campo obrigatório "anunciante" no painel.** O painel exibe um aviso fixo: **proibido aceitar anúncio de candidato, partido, coligação ou com conteúdo político-eleitoral.** (A lei eleitoral restringe propaganda paga na internet, e um site de checagem com anúncio político perde a credibilidade.)

### 9. Modo pós-debate (plano B)

Se o sistema não conseguir rodar ao vivo, por falha ou por não ficar pronto a tempo, ele precisa processar o debate **depois**, com o mesmo fluxo e as mesmas regras.

- **Três entradas possíveis:**
  1. **Gravação de backup do próprio sistema** (da etapa de captura), se a captura chegou a rodar.
  2. **Arquivo de áudio local** fornecido pelo Eudes.
  3. **Reprodução da gravação no navegador** (por exemplo, a reprise no Globoplay), capturada pelo mesmo caminho do ao vivo. Isso não exige baixar nada, mas leva o tempo real do debate.
- **Retomada parcial:** se o ao vivo cair no meio, o sistema sabe o último trecho processado e continua a partir da gravação de backup **daquele ponto em diante**, sem duplicar checagens já feitas.
- **Velocidade:** com arquivo local, processar em lote o mais rápido que os limites das APIs permitirem, com processamento em paralelo controlado, sem estourar o limite de requisições nem o teto de custo. Mostrar no painel o progresso (% do debate processado) e uma estimativa de término.
- **Horários originais:** cada checagem usa o horário em que a fala aconteceu no debate (por exemplo, "22h07"), calculado a partir do início do debate informado pelo operador, não o horário do processamento.
- **Publicação mais segura:** no modo pós-debate não existe pressa. Por padrão, **nada publica sozinho**: o operador aprova cada item, ou aprova um lote depois de revisar. A contagem regressiva com veto pode ser ligada manualmente, se o Eudes quiser.
- **A página informa o modo:** um aviso como "Checagem feita após o debate, com base na gravação". A detecção de intervalo, a atribuição de falante e as regras de evidência continuam valendo do mesmo jeito.
- **Mesmo código:** o modo pós-debate não é um sistema paralelo. É só outra *fonte de áudio* alimentando o mesmo fluxo. Isso evita duas lógicas para manter e testar.

## Modo de teste (obrigatório antes de sexta)

- **Teste real:** tocar no navegador a gravação do debate da Band de 18/10/2026. A captura é exatamente a mesma do dia do debate, o que valida o fluxo inteiro.
- **Modo arquivo**, para desenvolvimento: alimentar o fluxo com um arquivo de áudio local.
- **Gabarito:** depois do teste, gerar um relatório com todas as checagens, para o Eudes comparar com as checagens já publicadas pelas agências sobre o debate da Band.
- O ambiente de teste publica numa página separada da de produção.

## Etapas (paradas obrigatórias depois da 2 e da 3)

1. Estrutura dos dois repositórios, `.env.example`, dependências, READMEs iniciais, **JSON Schema do `timeline.json` e do `ads.json`** e explicação da arquitetura. *(backend + frontend)*
2. Captura de áudio do monitor source + indicador de "ouvindo". Teste com um vídeo qualquer tocando no navegador.
3. Transcrição com diarização (comparação de provedores → escolha → integração). Teste com áudio real.
4. Detecção de intervalo comercial (botão manual + frases do mediador + classificação).
5. Painel do operador: mapeamento de falantes, estado debate/intervalo e fila.
6. Detecção de afirmações.
7. Verificação com busca, validação de evidências e controle de custo.
8. Fila com veto, pausa geral e despublicação.
9. Página pública (linha do tempo com os 3 títulos + gráfico por candidato + "Como checamos") *(frontend)* + envio do `timeline.json` para a hospedagem *(backend)* + teste de carga *(frontend)*.
10. Modo pós-debate: entrada por arquivo/backup, retomada parcial, aprovação manual por padrão.
11. Espaço publicitário (faixa e modal, desligado por padrão, controlado pelo painel).
12. Teste completo com o debate da Band, **ao vivo (reprodução no navegador) e pós-debate (arquivo)**, + relatório gabarito (incluindo intervalos detectados).
13. Checklist do dia do debate: o que ligar, em que ordem, o que fazer se algo cair e **como acionar o plano B**.

**Prioridade se faltar tempo:** o modo pós-debate vem antes do espaço publicitário. Se só der para entregar um modo funcionando, entregue o pós-debate. Ele é o que garante que a página exista de qualquer jeito.

Comece pela etapa 1. Escreva o código para **Python 3.11+** e para **PipeWire e PulseAudio** (detecte qual está em uso). Se uma versão específica for necessária, deixe isso explícito no README.
