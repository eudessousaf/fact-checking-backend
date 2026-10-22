"""Ponto de entrada único: `factcheck <comando>`."""

from __future__ import annotations

import argparse
import ipaddress
from datetime import timedelta
import logging
import signal
import sys
from pathlib import Path

from factcheck.capture.sources import AudioSetupError
from factcheck.config import ConfigError, load_debate_config, load_settings
from factcheck.contract import ContractError, validate_file
from factcheck.log import setup_logging

log = logging.getLogger("factcheck")


def cmd_validate(args: argparse.Namespace) -> int:
    ok = True
    for f in args.files:
        try:
            validate_file(Path(f))
            print(f"OK      {f}")
        except ContractError as exc:
            ok = False
            print(f"INVÁLIDO {f}")
            for p in exc.problems:
                print(f"   - {p}")
    return 0 if ok else 1


def cmd_check_config(_args: argparse.Namespace) -> int:
    settings = load_settings()
    debate = load_debate_config(settings.debate_config_path)
    print(f"Ambiente:   {settings.environment}")
    print(f"Dados em:   {settings.data_dir}")
    print(f"Debate:     {debate.title} ({debate.broadcaster})")
    for c in debate.candidates:
        print(f"Candidato {c.id}: {c.name}")
    print(f"Mediador:   {debate.mediator_name}")
    if not settings.panel_password:
        print("AVISO: PANEL_PASSWORD vazio no .env — o painel não sobe sem senha.")
    return 0


def resume_offset(settings, debate_start_in_file: float | None) -> float:
    """Segundo do arquivo de onde continuar, quando o ao vivo caiu no meio.

    A conta: (última fala já processada − início do debate) + onde o debate
    começa no arquivo − margem. A margem repete alguns segundos de propósito:
    afirmação repetida é descartada pelo índice de duplicatas; um buraco não
    teria conserto.
    """
    from datetime import datetime

    from factcheck.db import connect, transaction

    if debate_start_in_file is None:
        raise ConfigError("--retomar exige --inicio-no-arquivo SEGUNDOS (onde o debate começa neste arquivo)")
    conn = connect(settings.db_path)
    try:
        state = conn.execute("SELECT started_at FROM debate_state WHERE id = 1").fetchone()
        last = conn.execute(
            "SELECT MAX(s.wall_at) FROM segments s JOIN chunks c ON c.id = s.chunk_id "
            "WHERE c.status = 'detected' AND s.wall_at IS NOT NULL").fetchone()[0]
        if not state["started_at"] or not last:
            raise ConfigError("não há ao vivo processado para retomar (falta o início do debate ou nenhuma fala "
                              "com horário). Rode sem --retomar e informe o início no painel.")
        started = datetime.fromisoformat(state["started_at"])
        elapsed = (datetime.fromisoformat(last) - started).total_seconds()
        start = max(0.0, debate_start_in_file + elapsed - RESUME_MARGIN_S)
        with transaction(conn):
            conn.execute("UPDATE debate_state SET file_anchor_at = ?, mode = 'pos_debate', auto_publish = 0 "
                         "WHERE id = 1", ((started - timedelta(seconds=debate_start_in_file)).isoformat(),))
    finally:
        conn.close()
    log.info("retomando do segundo %.0f do arquivo (última fala processada: %s); modo pós-debate ligado",
             start, last)
    return start


RESUME_MARGIN_S = 20.0


def cmd_capture(args: argparse.Namespace) -> int:
    # Imports locais: `factcheck validate` não precisa carregar a captura.
    from factcheck.capture import sources
    from factcheck.capture.runner import CaptureParams, CaptureRunner, SourceSpec, now_local
    from factcheck.db import connect

    settings = load_settings()
    if args.list_sources:
        print(f"Servidor de som: {sources.detect_server()}")
        found = sources.list_monitor_sources()
        if not found:
            print("Nenhum monitor encontrado.")
            return 1
        for s in found:
            mark = "  <- padrão" if s.is_default else ""
            print(f"  {s.name}\n      {s.description} [{s.state}]{mark}")
        print("\nPara escolher um, ponha o nome em CAPTURE_SOURCE no .env ou use --source.")
        return 0

    stamp = now_local().strftime("%Y%m%d_%H%M%S")
    setup_logging(settings.data_dir / "logs" / f"capture_{stamp}.log")

    if args.file:
        path = Path(args.file).expanduser().resolve()
        if not path.exists():
            raise AudioSetupError(f"arquivo não encontrado: {path}")
        if path.is_dir():
            path = sources.backup_concat_list(path, settings.data_dir / "tmp" / f"{path.parent.name}.ffconcat")
            log.info("lendo as partes do backup em sequência (%s)", path)
        sources.check_ffmpeg(require_pulse=False)
        start = args.start
        if args.retomar:
            start = resume_offset(settings, args.inicio_no_arquivo)
        spec = SourceSpec("file", str(path), realtime=args.realtime, start_s=start)
    else:
        from factcheck.capture.routing import VIRTUAL_MONITOR

        sources.check_ffmpeg(require_pulse=True)
        server = sources.detect_server()
        monitor = sources.resolve_monitor(args.source or settings.capture_source,
                                          prefer=VIRTUAL_MONITOR)
        log.info("servidor de som: %s; capturando de %s (%s)", server, monitor.name, monitor.description)
        if monitor.name != VIRTUAL_MONITOR:
            log.warning("a saída virtual 'Debate' não está montada (factcheck audio-setup). "
                        "Assim, zerar o volume do sistema ou do player SILENCIA a captura.")
        spec = SourceSpec("pulse", monitor.name)

    params = CaptureParams(
        chunk_s=settings.chunk_seconds,
        overlap_s=settings.chunk_overlap_seconds,
        silence_threshold_dbfs=settings.silence_threshold_dbfs,
        silence_alert_s=settings.silence_alert_seconds,
    )
    runner = CaptureRunner(connect(settings.db_path), settings.audio_dir, spec, params)

    def _stop(signum, _frame):
        log.info("sinal %s recebido; encerrando a captura com segurança…", signal.Signals(signum).name)
        runner.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    runner.run()
    return 0


def cmd_audio_setup(args: argparse.Namespace) -> int:
    from factcheck.capture.routing import AudioRouting

    routing = AudioRouting()
    actions = routing.teardown() if args.desfazer else routing.setup()
    for a in actions:
        print(f"  - {a}")
    if not args.desfazer:
        st = routing.status()
        if not st.ok:
            print("\nATENÇÃO: a rota ficou incompleta (veja acima).")
        elif st.browsers_total == 0:
            # Sem navegador tocando não dá para confirmar a parte mais
            # importante; dizer "pronta" aqui seria afirmar o que não se viu.
            print("\nRota montada, mas SEM confirmação do navegador: dê play no Globoplay e "
                  "rode de novo até aparecer a conexão do navegador no 'Debate'.")
        else:
            print(f"\nRota pronta: {st.browsers_on_debate} conexão(ões) do navegador no 'Debate'.")
        print("Regra do dia: NÃO mexa no volume do sistema nem no do player do Globoplay.")
        print("Para silenciar só o seu som, use o botão do painel.")
    return 0


def cmd_transcribe_probe(args: argparse.Namespace) -> int:
    """Manda UM arquivo para o provedor e mostra a resposta crua.

    Existe porque o formato da resposta não pôde ser conferido na nuvem: o
    resto da etapa 3 só é escrito depois de ver isto funcionando de verdade.
    """
    import json

    from factcheck.capture.runner import now_local
    from factcheck.transcription.base import SpeakerReference, TranscriptionError
    from factcheck.transcription.openai_diarize import OpenAIDiarizeTranscriber

    settings = load_settings()
    audio = Path(args.file).expanduser()
    if not audio.exists():
        raise ConfigError(f"arquivo não encontrado: {audio}")
    refs = []
    for item in args.ref or []:
        name, sep, path = item.partition("=")
        if not sep or not name or not Path(path).expanduser().exists():
            raise ConfigError(f"--ref precisa ser NOME=arquivo.wav existente, veio {item!r}")
        refs.append(SpeakerReference(name.strip(), Path(path).expanduser()))

    t = OpenAIDiarizeTranscriber(settings.openai_api_key, settings.transcription_model,
                                 settings.transcription_language)
    started = now_local()
    try:
        result = t.transcribe(audio, refs)
    except TranscriptionError as exc:
        log.error("%s", exc)
        return 1
    finally:
        t.close()
    elapsed = (now_local() - started).total_seconds()

    out_dir = settings.data_dir / "probes"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f"{audio.stem}_{started:%H%M%S}.json"
    raw_path.write_text(json.dumps(result.raw, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{len(result.segments)} falas em {elapsed:.1f} s de espera:\n")
    for seg in result.segments:
        print(f"  [{seg.start_s:6.1f}–{seg.end_s:6.1f}] {seg.speaker:>10}: {seg.text}")
    print(f"\nuso informado pela OpenAI: {json.dumps(result.usage, ensure_ascii=False) or '(nenhum)'}")
    print(f"resposta crua completa em: {raw_path}")
    return 0


def _uploaders(settings, extra_dir: str | None):
    from factcheck.hosting import SpacesUploader
    from factcheck.pipeline.publish import LocalDirUploader

    ups = []
    if extra_dir:
        ups.append(LocalDirUploader(Path(extra_dir).expanduser().resolve()))
    if settings.spaces_bucket:
        ups.append(SpacesUploader(settings.spaces_bucket, settings.spaces_region, settings.spaces_key,
                                  settings.spaces_secret, settings.spaces_prefix))
    else:
        log.warning("Spaces não configurado no .env: o timeline.json fica só em %s", settings.public_dir)
    return ups


def cmd_pipeline(args: argparse.Namespace) -> int:
    import threading

    from factcheck.capture.runner import now_local
    from factcheck.costs import TokenPrice
    from factcheck.db import connect
    from factcheck.llm.claude import Claude, ClaudePrices
    from factcheck.pipeline.publish import TimelinePublisher
    from factcheck.pipeline.runner import AnalysisWorker, PublishLoop, TranscribeWorkers, VerifyWorkers
    from factcheck.transcription.openai_diarize import OpenAIDiarizeTranscriber

    settings = load_settings()
    debate = load_debate_config(settings.debate_config_path)
    setup_logging(settings.data_dir / "logs" / f"pipeline_{now_local():%Y%m%d_%H%M%S}.log")
    stop = threading.Event()

    def _stop(signum, _frame):
        log.info("sinal %s recebido; terminando o que está em andamento…", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    threads = TranscribeWorkers(
        settings, debate,
        lambda: OpenAIDiarizeTranscriber(settings.openai_api_key, settings.transcription_model,
                                         settings.transcription_language),
        workers=args.workers).start(stop)

    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY vazio: só a TRANSCRIÇÃO vai rodar (sem intervalo, detecção e checagem)")
    else:
        prices = ClaudePrices(
            haiku=TokenPrice(settings.haiku_price_in_per_m, settings.haiku_price_out_per_m),
            sonnet=TokenPrice(settings.sonnet_price_in_per_m, settings.sonnet_price_out_per_m),
            web_search_per_1000=settings.web_search_usd_per_1000)
        claude = Claude(settings.anthropic_api_key, lambda: connect(settings.db_path), prices,
                        settings.detect_model, settings.verify_model, settings.verify_effort)
        threads += AnalysisWorker(settings, claude).start(stop)
        threads += VerifyWorkers(settings, debate, claude, settings.verify_workers).start(stop)
        publisher = TimelinePublisher(debate, settings.environment, settings.public_dir / "timeline.json",
                                      _uploaders(settings, args.publicar_em))
        threads += PublishLoop(settings, publisher).start(stop)
        log.info("fluxo completo no ar: transcrição → intervalo → detecção → verificação → publicação")

    while not stop.is_set():
        stop.wait(1.0)
    for t in threads:
        t.join(timeout=150)
    log.info("fluxo encerrado")
    return 0


def cmd_deploy_site(args: argparse.Namespace) -> int:
    from factcheck.hosting import SpacesUploader, deploy_site

    settings = load_settings()
    up = SpacesUploader(settings.spaces_bucket, settings.spaces_region, settings.spaces_key,
                        settings.spaces_secret, settings.spaces_prefix)
    public = Path(args.public_dir).expanduser().resolve()
    if not (public / "index.html").exists():
        raise ConfigError(f"{public} não tem index.html (é a pasta public/ do fact-checking-frontend?)")
    sent = deploy_site(up, public, include_data=args.com_exemplos)
    for name in sent:
        print(f"  enviado: {name}")
    print(f"\nPágina: {up.public_url('index.html')}")
    return 0


def cmd_costs(_args: argparse.Namespace) -> int:
    from factcheck.costs import summary, total_usd
    from factcheck.db import connect

    settings = load_settings()
    conn = connect(settings.db_path)
    for row in summary(conn):
        print(f"  {row['provider']:<10} {row['purpose']:<12} {row['calls']:>5} chamadas  "
              f"{row['input_tokens'] or 0:>9} entrada  {row['output_tokens'] or 0:>9} saída  US$ {row['usd']:.4f}")
    print(f"\nTotal estimado: US$ {total_usd(conn):.4f}  (Claude: US$ {total_usd(conn, 'anthropic'):.4f}; "
          f"teto das checagens: US$ {settings.budget_hard_stop_usd:.0f})")
    print("Os preços vêm do .env; o valor real é o do painel de cada provedor.")
    return 0


def cmd_panel(_args: argparse.Namespace) -> int:
    import uvicorn

    from factcheck.panel.app import create_app

    settings = load_settings()
    # O painel publica e despublica checagens: ele não pode ficar acessível
    # pela rede, nem por engano no .env.
    try:
        loopback = ipaddress.ip_address(settings.panel_host).is_loopback
    except ValueError:
        loopback = settings.panel_host == "localhost"
    if not loopback:
        raise ConfigError(f"PANEL_HOST={settings.panel_host} não é localhost; o painel só roda em 127.0.0.1")
    from factcheck.capture.runner import now_local

    # Tudo o que o operador faz no painel fica no arquivo, não só na tela.
    setup_logging(settings.data_dir / "logs" / f"panel_{now_local():%Y%m%d_%H%M%S}.log")
    app = create_app(settings)
    log.info("painel em http://%s:%d (usuário: qualquer; senha: PANEL_PASSWORD)",
             settings.panel_host, settings.panel_port)
    uvicorn.run(app, host=settings.panel_host, port=settings.panel_port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factcheck")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="valida timeline.json / ads.json contra o schema")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("check-config", help="confere .env e config/debate.toml")
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser("capture", help="captura o som do sistema (ou de um arquivo) em blocos")
    p.add_argument("--list-sources", action="store_true", help="lista os monitores de saída de som")
    p.add_argument("--source", help="nome do monitor (padrão: CAPTURE_SOURCE ou a saída padrão)")
    p.add_argument("--file", help="usa um arquivo de áudio/vídeo em vez do som do sistema")
    p.add_argument("--realtime", action="store_true", help="com --file: lê no ritmo real, como ao vivo")
    p.add_argument("--start", type=float, default=0.0, help="com --file: começa neste segundo")
    p.add_argument("--retomar", action="store_true",
                   help="com --file: continua de onde o ao vivo parou (exige --inicio-no-arquivo)")
    p.add_argument("--inicio-no-arquivo", type=float, metavar="SEGUNDOS",
                   help="com --retomar: em que segundo do arquivo o debate começa")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("audio-setup", help="monta a saída virtual 'Debate' e move o navegador para ela")
    p.add_argument("--desfazer", action="store_true", help="remove a saída virtual e a cópia")
    p.set_defaults(func=cmd_audio_setup)

    p = sub.add_parser("transcribe-probe", help="transcreve UM arquivo e mostra a resposta crua")
    p.add_argument("file", help="arquivo de áudio (ex.: um bloco de data/audio/run_NNNN/chunks/)")
    p.add_argument("--ref", action="append", metavar="NOME=arquivo.wav",
                   help="voz de referência (2–10 s); repita para cada pessoa, até 4")
    p.set_defaults(func=cmd_transcribe_probe)

    p = sub.add_parser("pipeline", help="roda o fluxo inteiro: transcrição → checagem → publicação")
    p.add_argument("--workers", type=int, default=2,
                   help="transcrições em paralelo (ao vivo: 2; arquivo inteiro: até 4)")
    p.add_argument("--publicar-em", metavar="PASTA",
                   help="também grava o timeline.json nesta pasta (ex.: ../fact-checking-frontend/public)")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("deploy-site", help="envia a página pública para o Spaces")
    p.add_argument("public_dir", help="a pasta public/ do fact-checking-frontend")
    p.add_argument("--com-exemplos", action="store_true",
                   help="envia também timeline.json e ads.json de exemplo (só no primeiro envio)")
    p.set_defaults(func=cmd_deploy_site)

    p = sub.add_parser("costs", help="mostra o gasto estimado por provedor e etapa")
    p.set_defaults(func=cmd_costs)

    p = sub.add_parser("panel", help="abre o painel do operador (só localhost)")
    p.set_defaults(func=cmd_panel)

    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, AudioSetupError) as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
