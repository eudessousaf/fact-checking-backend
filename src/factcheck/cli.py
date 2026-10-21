"""Ponto de entrada único: `factcheck <comando>`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factcheck")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="valida timeline.json / ads.json contra o schema")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("check-config", help="confere .env e config/debate.toml")
    p.set_defaults(func=cmd_check_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
