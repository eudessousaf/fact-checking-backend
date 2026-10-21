from __future__ import annotations

from pathlib import Path

# O pacote roda instalado em modo editável (`pip install -e .`), então a raiz
# do repositório fica dois níveis acima de src/factcheck. Os schemas moram lá,
# e não dentro do pacote, porque são copiados byte a byte para o frontend.
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "schemas"
