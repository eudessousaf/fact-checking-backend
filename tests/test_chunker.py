from factcheck.capture.chunker import Chunker
from factcheck.capture.levels import SilenceTracker, pcm_to_samples, rms_dbfs
from factcheck.capture.sources import SAMPLE_RATE

SECOND = b"\x01\x00" * SAMPLE_RATE


def run(seconds: float, feed_s: float = 0.1, **kw):
    out = []
    ch = Chunker(30, 2, out.append, **kw)
    total = int(seconds / feed_s)
    piece = SECOND[: int(SAMPLE_RATE * feed_s) * 2]
    for _ in range(total):
        ch.feed(piece)
    ch.flush()
    return out


def test_overlap_and_final_partial():
    chunks = run(95)
    assert [(c.start_s, c.end_s) for c in chunks] == [
        (0, 30), (28, 58), (56, 86), (84, 95)]
    assert [c.idx for c in chunks] == [1, 2, 3, 4]


def test_no_extra_chunk_for_overlap_only():
    # 58 s = dois blocos exatos; o resto é só a sobreposição já entregue.
    chunks = run(58)
    assert [(c.start_s, c.end_s) for c in chunks] == [(0, 30), (28, 58)]


def test_tiny_tail_is_dropped_but_bigger_tail_kept():
    assert len(run(58.5)) == 2
    assert len(run(60)) == 3


def test_start_sample_and_first_idx_for_resume():
    chunks = run(30, first_idx=7, start_sample=600 * SAMPLE_RATE)
    assert chunks[0].idx == 7 and chunks[0].start_s == 600 and chunks[0].end_s == 630


def test_feed_size_does_not_change_cut_points():
    a = [(c.start_sample, c.end_sample) for c in run(95, feed_s=0.1)]
    b = [(c.start_sample, c.end_sample) for c in run(95, feed_s=1.0)]
    assert a == b


def test_levels_and_silence():
    assert rms_dbfs(pcm_to_samples(b"\x00\x00" * 1600)) == -96.0
    loud = pcm_to_samples((b"\xff\x7f" + b"\x01\x80") * 800)
    assert rms_dbfs(loud) > -0.1
    t = SilenceTracker(-50, 20)
    for _ in range(199):
        t.feed(-90, 0.1)
    assert not t.alert
    t.feed(-90, 0.1)
    t.feed(-90, 0.1)
    assert t.alert
    t.feed(-20, 0.1)
    assert not t.alert and t.silent_for_s == 0
