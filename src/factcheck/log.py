from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(log_file: Path | None = None, level: int = logging.INFO) -> None:
    """Log no terminal e, se pedido, em arquivo — o arquivo é o que sobra para
    entender o que aconteceu depois de uma queda no meio do debate."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(fh)
