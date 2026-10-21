from pathlib import Path

import pytest

from factcheck.config import ConfigError, load_debate_config
from factcheck.paths import REPO_ROOT


def test_example_debate_config_loads():
    cfg = load_debate_config(REPO_ROOT / "config" / "debate.example.toml")
    assert [c.id for c in cfg.candidates] == ["A", "B"]
    assert cfg.min_checks_to_display == 8


def test_missing_config_explains_what_to_do(tmp_path: Path):
    with pytest.raises(ConfigError, match="debate.example.toml"):
        load_debate_config(tmp_path / "nao-existe.toml")
