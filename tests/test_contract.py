import copy
import json
from pathlib import Path

import pytest

from factcheck.contract import ContractError, count_sentences, validate_ads, validate_timeline
from factcheck.paths import REPO_ROOT
from factcheck.stats import compute_candidate_stats, largest_remainder_pct

EXAMPLES = REPO_ROOT / "examples"


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_examples_are_valid():
    validate_timeline(load("timeline.example.json"))
    validate_ads(load("ads.example.json"))
    validate_ads(load("ads.example-active.json"))


def first_published(data):
    return next(i for i in data["items"] if i["state"] == "publicado")


def test_sem_evidencia_can_never_be_published():
    data = load("timeline.example.json")
    first_published(data)["verdict"] = "SEM_EVIDENCIA"
    with pytest.raises(ContractError):
        validate_timeline(data)


def test_published_needs_two_sources():
    data = load("timeline.example.json")
    item = first_published(data)
    item["sources"] = item["sources"][:1]
    item["indicators"]["source_count"] = 1
    with pytest.raises(ContractError):
        validate_timeline(data)


def test_nao_e_bem_assim_requires_what_is_off():
    data = load("timeline.example.json")
    item = next(i for i in data["items"] if i.get("verdict") == "NAO_E_BEM_ASSIM")
    del item["what_is_off"]
    with pytest.raises(ContractError):
        validate_timeline(data)


def test_format_checker_is_active():
    # Sem o extra format-nongpl o jsonschema aceitaria isto calado.
    data = load("timeline.example.json")
    data["generated_at"] = "ontem à noite"
    with pytest.raises(ContractError):
        validate_timeline(data)


def test_stats_must_match_items():
    data = load("timeline.example.json")
    data["stats"]["by_candidate"][0]["FALSO"]["count"] += 1
    with pytest.raises(ContractError, match="stats"):
        validate_timeline(data)


def test_removed_item_does_not_count_and_hides_content():
    data = load("timeline.example.json")
    item = first_published(data)
    removed = {"id": item["id"], "state": "removido", "said_at": item["said_at"],
               "removed_at": "2026-10-23T23:00:00-03:00", "verdict": item["verdict"]}
    data["items"][data["items"].index(item)] = removed
    # O conteúdo retirado não pode voltar a público pela porta do removido.
    with pytest.raises(ContractError):
        validate_timeline(data)
    del removed["verdict"]
    data["stats"]["by_candidate"] = compute_candidate_stats(data["items"], ["A", "B"], 8)
    validate_timeline(data)


def test_explanation_sentence_limit():
    data = load("timeline.example.json")
    first_published(data)["explanation"] = "Um. Dois. Três. Quatro."
    with pytest.raises(ContractError, match="frases"):
        validate_timeline(data)


@pytest.mark.parametrize("text,expected", [
    ("O PIB cresceu 3,5% em 2023.", 1),
    ("Custou R$ 1.200 milhões. Não R$ 2 bilhões.", 2),
    ("Sem ponto final", 1),
    ("", 0),
])
def test_count_sentences(text, expected):
    assert count_sentences(text) == expected


def test_largest_remainder_sums_to_100():
    assert largest_remainder_pct({"V": 1, "N": 1, "F": 1}) == {"V": 34, "N": 33, "F": 33}
    assert sum(largest_remainder_pct({"V": 2, "N": 5, "F": 7}).values()) == 100
    assert largest_remainder_pct({"V": 0, "N": 0, "F": 0}) == {"V": 0, "N": 0, "F": 0}


def test_ads_active_requires_advertiser_and_relative_image():
    data = load("ads.example-active.json")
    bad = copy.deepcopy(data)
    del bad["advertiser"]
    with pytest.raises(ContractError):
        validate_ads(bad)
    bad = copy.deepcopy(data)
    bad["image_path"] = "https://terceiro.example/banner.png"
    with pytest.raises(ContractError):
        validate_ads(bad)


FRONTEND_SCHEMAS = REPO_ROOT.parent / "fact-checking-frontend" / "schemas"


@pytest.mark.skipif(not FRONTEND_SCHEMAS.exists(), reason="frontend não está clonado ao lado")
@pytest.mark.parametrize("name", ["timeline.schema.json", "ads.schema.json"])
def test_schema_identical_in_frontend(name):
    here = (REPO_ROOT / "schemas" / name).read_bytes()
    there = (FRONTEND_SCHEMAS / name).read_bytes()
    assert here == there, f"copie: cp schemas/*.json ../fact-checking-frontend/schemas/"
