"""Regras do fluxo com modelos simulados: intervalo, detecção, verificação e publicação."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from factcheck.capture.runner import now_local
from factcheck.config import load_debate_config
from factcheck.contract import validate_timeline
from factcheck.db import connect
from factcheck.llm.claude import LLMError, VerifyResult
from factcheck.paths import REPO_ROOT
from factcheck.pipeline import detect, interval, publish, verify

DEBATE = load_debate_config(REPO_ROOT / "config" / "debate.example.toml")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "db.sqlite")
    c.execute("UPDATE debate_state SET status = 'em_andamento', updated_at = ? WHERE id = 1",
              (now_local().isoformat(),))
    return c


def add_chunk(conn, idx, lines, run_id=None, wall=True):
    """lines: [(papel, texto, inicio, fim, incerto)] em segundos do bloco."""
    if run_id is None:
        run_id = conn.execute("SELECT id FROM capture_runs").fetchone()
        run_id = run_id[0] if run_id else conn.execute(
            "INSERT INTO capture_runs (source_kind, source_name, started_at, audio_dir) "
            "VALUES ('pulse', 'x', 'x', '')").lastrowid
    start = (idx - 1) * 28.0
    ws = (now_local() - timedelta(minutes=10) + timedelta(seconds=start)).isoformat() if wall else None
    cid = conn.execute(
        "INSERT INTO chunks (run_id, idx, segment, offset_start_s, offset_end_s, wall_start, path, mean_dbfs, "
        "status, created_at) VALUES (?, ?, 1, ?, ?, ?, 'x', -20, 'transcribed', ?)",
        (run_id, idx, start, start + 30, ws, now_local().isoformat())).lastrowid
    for role, text, a, b, unc in lines:
        conn.execute("INSERT INTO segments (chunk_id, run_id, start_s, end_s, wall_at, speaker_label, role, "
                     "uncertain, text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (cid, run_id, start + a, start + b, ws, f"REF_{role}" if role != "?" else "C", role,
                      int(unc), text))
    return cid


class FakeLLM:
    def __init__(self, labels=None, claims=None, fail=False):
        self.labels = labels or {}      # trecho de texto → rótulo
        self.claims = claims or []      # [(tag, quote, claim)]
        self.fail = fail
        self.calls = []

    def json_call(self, purpose, system, user, schema, max_tokens=4000):
        self.calls.append((purpose, user))
        if self.fail:
            raise LLMError("falha simulada")
        if purpose == "intervalo":
            for needle, label in self.labels.items():
                if needle in user:
                    return {"label": label, "reason": f"tem '{needle}'"}
            return {"label": "debate", "reason": "conversa no estúdio"}
        return {"claims": [{"speaker": t, "quote": q, "claim": c, "category": "numero"} for t, q, c in self.claims]}


def classify_all(conn, llm):
    while (c := interval.next_pending(conn)) is not None:
        interval.classify_one(conn, c, llm)


def effective(conn):
    return [r[0] for r in conn.execute(
        "SELECT cc.effective FROM chunk_class cc JOIN chunks c ON c.id = cc.chunk_id ORDER BY c.idx")]


DEBATE_LINES = [("M", "Candidato, sua resposta.", 0, 3, False), ("A", "O desemprego caiu para 6% em 2024.", 3, 12, False)]


# --- intervalo ------------------------------------------------------------------------------

def test_commercial_needs_two_debate_chunks_to_return(conn):
    add_chunk(conn, 1, DEBATE_LINES)
    add_chunk(conn, 2, [("?", "COMPRE AGORA o carro novo", 0, 20, True)])
    add_chunk(conn, 3, DEBATE_LINES)
    add_chunk(conn, 4, DEBATE_LINES)
    classify_all(conn, FakeLLM({"COMPRE": "comercial"}))
    assert effective(conn) == ["debate", "intervalo", "intervalo", "debate"]
    assert conn.execute("SELECT status FROM debate_state").fetchone()[0] == "em_andamento"


def test_mediator_break_phrase_switches_from_next_chunk(conn):
    add_chunk(conn, 1, [("M", "Vamos para o intervalo e voltamos já.", 20, 25, False)])
    add_chunk(conn, 2, DEBATE_LINES)
    classify_all(conn, FakeLLM())
    assert effective(conn) == ["debate", "intervalo"]
    assert conn.execute("SELECT status FROM debate_state").fetchone()[0] == "intervalo"


def test_undefined_is_interval(conn):
    add_chunk(conn, 1, [("?", "música e vozes", 0, 20, True)])
    classify_all(conn, FakeLLM({"música": "indefinido"}))
    assert effective(conn) == ["intervalo"]


def test_debate_without_known_speakers_does_not_end_interval(conn):
    conn.execute("UPDATE debate_state SET status = 'intervalo'")
    unknown = [("?", "conversa de estúdio sem voz conhecida", 0, 20, True)]
    add_chunk(conn, 1, unknown)
    add_chunk(conn, 2, unknown)
    classify_all(conn, FakeLLM())
    assert effective(conn) == ["intervalo", "intervalo"]


def test_manual_wins_both_ways(conn):
    add_chunk(conn, 1, DEBATE_LINES)
    conn.execute("UPDATE debate_state SET manual_interval = 'intervalo'")
    classify_all(conn, FakeLLM())
    conn.execute("UPDATE debate_state SET manual_interval = 'debate'")
    add_chunk(conn, 2, [("?", "COMPRE", 0, 20, True)])
    classify_all(conn, FakeLLM({"COMPRE": "comercial"}))
    assert effective(conn) == ["intervalo", "debate"]


def test_resume_button_returns_immediately(conn):
    conn.execute("UPDATE debate_state SET status = 'intervalo'")
    add_chunk(conn, 1, [("?", "COMPRE", 0, 20, True)])
    classify_all(conn, FakeLLM({"COMPRE": "comercial"}))
    conn.execute("UPDATE debate_state SET resume_after = ?", ((now_local() - timedelta(seconds=1)).isoformat(),))
    add_chunk(conn, 2, DEBATE_LINES)
    classify_all(conn, FakeLLM())
    assert effective(conn) == ["intervalo", "debate"]


def test_not_started_is_interval_without_spending(conn):
    conn.execute("UPDATE debate_state SET status = 'aguardando'")
    add_chunk(conn, 1, DEBATE_LINES)
    llm = FakeLLM()
    classify_all(conn, llm)
    assert effective(conn) == ["intervalo"] and llm.calls == []


def test_classification_failure_is_interval(conn):
    cid = add_chunk(conn, 1, DEBATE_LINES)
    chunk = interval.next_pending(conn)
    with pytest.raises(LLMError):
        interval.classify_one(conn, chunk, FakeLLM(fail=True))
    interval.classify_failed(conn, chunk, "falha")
    assert conn.execute("SELECT effective FROM chunk_class WHERE chunk_id = ?", (cid,)).fetchone()[0] == "intervalo"


# --- detecção -------------------------------------------------------------------------------

def detect_all(conn, llm):
    while (c := detect.next_pending(conn)) is not None:
        detect.detect_one(conn, c, llm)


def test_detection_sees_only_certain_candidate_speech(conn):
    add_chunk(conn, 1, [("M", "Pergunta do mediador com 50 números.", 0, 4, False),
                        ("A", "A inflação foi de 4% em 2023.", 4, 10, False),
                        ("B", "Mentira", 10, 11, True),
                        ("B", "O PIB cresceu 3% em 2024.", 12, 20, False)])
    classify_all(conn, FakeLLM())
    llm = FakeLLM()
    detect_all(conn, llm)
    trecho = llm.calls[-1][1].split("TRECHO A ANALISAR:")[1]
    assert "[CANDIDATO_1] A inflação" in trecho and "[CANDIDATO_2] O PIB" in trecho
    assert "mediador" not in trecho and "Mentira" not in trecho
    assert "Lula" not in llm.calls[-1][1] and "Flávio" not in llm.calls[-1][1]


def test_quote_must_exist_in_that_candidates_speech(conn):
    add_chunk(conn, 1, [("A", "A inflação foi de 4% em 2023.", 4, 10, False),
                        ("B", "O PIB cresceu 3% em 2024.", 12, 20, False)])
    classify_all(conn, FakeLLM())
    llm = FakeLLM(claims=[
        ("CANDIDATO_1", "a inflacao foi de 4% em 2023", "A inflação de 2023 foi de 4%"),   # ok (sem acento)
        ("CANDIDATO_1", "O PIB cresceu 3% em 2024", "O PIB de 2024 cresceu 3%"),          # é do outro
        ("CANDIDATO_2", "o desemprego zerou", "O desemprego zerou"),                       # inventada
    ])
    detect_all(conn, llm)
    rows = conn.execute("SELECT candidate_id, claim FROM claims").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("A", "A inflação de 2023 foi de 4%")]


def test_same_claim_twice_is_one_check(conn):
    add_chunk(conn, 1, [("A", "A inflação foi de 4% em 2023.", 4, 10, False)])
    add_chunk(conn, 2, [("A", "A inflação foi de 4% em 2023.", 4, 10, False)])
    classify_all(conn, FakeLLM())
    detect_all(conn, FakeLLM(claims=[("CANDIDATO_1", "A inflação foi de 4% em 2023", "A inflação de 2023 foi de 4%.")]))
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1


def test_interval_chunk_is_never_sent_to_detection(conn):
    add_chunk(conn, 1, [("A", "Vote 13! O salário mínimo dobrou.", 0, 20, False)])
    conn.execute("UPDATE debate_state SET manual_interval = 'intervalo'")
    classify_all(conn, FakeLLM())
    llm = FakeLLM(claims=[("CANDIDATO_1", "O salário mínimo dobrou", "x")])
    detect_all(conn, llm)
    assert llm.calls == [] and conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


# --- verificação ----------------------------------------------------------------------------

SEARCH = [{"url": "https://www.ibge.gov.br/estatisticas/x.html", "title": "IBGE"},
          {"url": "https://agenciadenoticias.ibge.gov.br/noticia", "title": "Agência IBGE"},
          {"url": "https://g1.globo.com/fato-ou-fake/noticia/x.ghtml", "title": "Fato ou Fake"},
          {"url": "https://www1.folha.uol.com.br/mercado/x.shtml", "title": "Folha"}]


def src(url, kind="outra"):
    return {"url": url, "title": "t", "publisher": "p", "kind": kind}


def test_sources_must_come_from_search():
    out = verify.validate({"status": "FALSO", "explanation": "Não confere.", "what_is_off": "",
                           "sources": [src(SEARCH[0]["url"]), src("https://inventado.com/x")]}, SEARCH)
    assert out.status == "SEM_EVIDENCIA" and any("não veio da busca" in n for n in out.notes)


def test_two_pages_of_same_org_are_not_independent():
    out = verify.validate({"status": "VERDADEIRO", "explanation": "Confere.", "what_is_off": "",
                           "sources": [src(SEARCH[0]["url"]), src(SEARCH[1]["url"])]}, SEARCH)
    assert out.status == "SEM_EVIDENCIA"


def test_valid_verdict_kinds_from_domain_and_trimmed_explanation():
    out = verify.validate({"status": "VERDADEIRO", "explanation": "Um. Dois. Três. Quatro.", "what_is_off": "x",
                           "sources": [src(SEARCH[0]["url"] + "#frag", "imprensa"), src(SEARCH[2]["url"], "imprensa"),
                                       src(SEARCH[3]["url"], "primaria")]}, SEARCH)
    assert out.status == "VERDADEIRO" and out.explanation == "Um. Dois. Três." and out.what_is_off == ""
    assert [s["kind"] for s in out.sources] == ["primaria", "agencia", "outra"]


def test_not_quite_needs_what_is_off():
    raw = {"status": "NAO_E_BEM_ASSIM", "explanation": "Exagero.", "what_is_off": "",
           "sources": [src(SEARCH[0]["url"]), src(SEARCH[3]["url"])]}
    assert verify.validate(raw, SEARCH).status == "SEM_EVIDENCIA"
    raw["what_is_off"] = "Foi 40%, não 100%."
    assert verify.validate(raw, SEARCH).status == "NAO_E_BEM_ASSIM"


class FakeVerifier:
    def __init__(self, raw):
        self.raw = raw
        self.users = []

    def verify(self, system, user, schema, max_searches=5, max_tokens=16000):
        self.users.append(user)
        return VerifyResult(self.raw, SEARCH, 2, 0.05, "claude-sonnet-5-5")


def make_claim(conn, wall=True, text="A inflação de 2023 foi de 4%"):
    cid = add_chunk(conn, 1, [("A", "x", 0, 5, False)], wall=wall)
    conn.execute("INSERT INTO claims (chunk_id, run_id, candidate_id, start_s, wall_at, quote, claim, dedup_key, "
                 "status, created_at) VALUES (?, 1, 'A', 3.0, ?, 'a inflação foi de 4%', ?, ?, 'detectada', ?)",
                 (cid, (now_local() - timedelta(minutes=5)).isoformat() if wall else None, text, text,
                  now_local().isoformat()))
    return verify.claim_next(conn)


GOOD = {"status": "FALSO", "explanation": "Foi 4,6%.", "what_is_off": "",
        "sources": [src(SEARCH[0]["url"]), src(SEARCH[3]["url"])]}


def test_verify_prompt_has_no_speaker_and_sets_countdown(conn):
    claim = make_claim(conn)
    v = FakeVerifier(GOOD)
    verify.verify_one(conn, claim, v, "2026-10-23", veto_minutes=3)
    assert "Lula" not in v.users[0] and "CANDIDATO" not in v.users[0]
    row = conn.execute("SELECT status, verdict, publish_at FROM claims").fetchone()
    assert (row["status"], row["verdict"]) == ("pronta", "FALSO") and row["publish_at"]


def test_post_debate_never_publishes_by_itself(conn):
    conn.execute("UPDATE debate_state SET mode = 'pos_debate'")
    verify.verify_one(conn, make_claim(conn), FakeVerifier(GOOD), "2026-10-23", 3)
    assert conn.execute("SELECT publish_at FROM claims").fetchone()[0] is None


# --- fila e publicação ------------------------------------------------------------------------

def ready(conn, wall=True):
    verify.verify_one(conn, make_claim(conn, wall=wall), FakeVerifier(GOOD), "2026-10-23", 3)
    return conn.execute("SELECT id FROM claims").fetchone()[0]


def test_countdown_pause_and_publish(conn):
    cid = ready(conn)
    assert publish.publish_due(conn) == 0                      # contagem ainda correndo
    conn.execute("UPDATE claims SET publish_at = ?", ((now_local() - timedelta(seconds=1)).isoformat(),))
    conn.execute("UPDATE debate_state SET publish_paused = 1")
    assert publish.publish_due(conn) == 0                      # pausa geral
    conn.execute("UPDATE debate_state SET publish_paused = 0")
    assert publish.publish_due(conn) == 1
    assert conn.execute("SELECT status FROM claims WHERE id = ?", (cid,)).fetchone()[0] == "publicada"


def test_operator_actions(conn):
    cid = ready(conn)
    publish.block(conn, cid)
    with pytest.raises(publish.PublishError):
        publish.unpublish(conn, cid, None)
    publish.publish_now(conn, cid)
    with pytest.raises(publish.PublishError, match="o que está errado"):
        publish.edit(conn, cid, "NAO_E_BEM_ASSIM", None, "")
    publish.edit(conn, cid, "NAO_E_BEM_ASSIM", "Foi 4,6%, não 4%.", "Diferença de 0,6 ponto.")
    publish.unpublish(conn, cid, "Corrigida pela equipe.")
    actions = [r[0] for r in conn.execute("SELECT action FROM operator_log ORDER BY id")]
    assert actions == ["bloquear", "publicar_agora", "editar", "despublicar"]


def test_timeline_is_valid_and_removed_hides_content(conn, tmp_path):
    cid = ready(conn)
    publish.publish_now(conn, cid)
    pub = publish.TimelinePublisher(DEBATE, "teste", tmp_path / "t.json", [publish.LocalDirUploader(tmp_path / "cdn")])
    assert pub.sync(conn) is True
    t = json.loads((tmp_path / "cdn" / "timeline.json").read_text())
    validate_timeline(t)
    assert t["items"][0]["verdict"] == "FALSO" and t["stats"]["by_candidate"][0]["FALSO"]["count"] == 1
    assert pub.sync(conn) is False                             # nada mudou: não reenvia
    publish.unpublish(conn, cid, None)
    assert pub.sync(conn) is True
    t2 = json.loads((tmp_path / "cdn" / "timeline.json").read_text())
    assert t2["sequence"] == t["sequence"] + 1
    assert t2["items"][0]["state"] == "removido" and "quote" not in t2["items"][0]
    assert t2["stats"]["by_candidate"][0]["total"] == 0


def test_file_mode_needs_debate_start_for_times(conn):
    cid = ready(conn, wall=False)
    with pytest.raises(publish.PublishError, match="início do debate"):
        publish.publish_now(conn, cid)
    conn.execute("UPDATE debate_state SET file_anchor_at = '2026-10-18T21:00:00-03:00'")
    publish.publish_now(conn, cid)
    row = conn.execute("SELECT * FROM claims").fetchone()
    assert publish.said_at(conn, row) == "2026-10-18T21:00:03-03:00"


def test_one_bad_item_does_not_block_the_others(conn, tmp_path):
    a = ready(conn)
    publish.publish_now(conn, a)
    conn.execute("UPDATE claims SET explanation = '' WHERE id = ?", (a,))   # defeito
    cid = add_chunk(conn, 2, [("B", "y", 0, 5, False)])
    conn.execute("INSERT INTO claims (chunk_id, run_id, candidate_id, start_s, wall_at, quote, claim, dedup_key, status, "
                 "verdict, explanation, sources_json, published_at, created_at) VALUES (?, 1, 'B', 1, ?, 'q', 'c', 'k2', "
                 "'publicada', 'VERDADEIRO', 'Confere.', ?, ?, ?)",
                 (cid, now_local().isoformat(), json.dumps([src(SEARCH[0]["url"], "primaria"), src(SEARCH[3]["url"])]),
                  now_local().isoformat(), now_local().isoformat()))
    pub = publish.TimelinePublisher(DEBATE, "teste", tmp_path / "t.json", [])
    pub.sync(conn)
    t = json.loads((tmp_path / "t.json").read_text())
    assert [i["candidate_id"] for i in t["items"]] == ["B"]
