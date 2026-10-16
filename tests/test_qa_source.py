"""QA section C: source retrieval and CSV parsing."""

import random
import urllib.error

import pytest

from quizbot import http, source
from quizbot.http import FetchError, FetchResult
from tests.conftest import HEADER, practice_rows, to_csv

req = pytest.mark.req


def parse(rows):
    return source.parse_questions(to_csv(rows))


@req("C01")
def test_c01_one_complete_round():
    snap = parse(practice_rows({1}))
    rnd = snap.rounds[1]
    assert rnd.complete and len(rnd.questions) == 25
    assert rnd.questions[1]["question_text"].startswith("An investment of $31,500.00")
    assert [rnd.questions[1][f"option_{c}"] for c in "abcd"] == ["$62,175.41", "$57,960.00", "$69,636.46", "$26,460.00"]


@req("C02", "C24")
def test_c02_c24_multiple_rounds_any_order():
    rows = practice_rows({5, 1, 3})
    random.Random(1).shuffle(rows)
    snap = parse(rows)
    assert sorted(snap.rounds) == [1, 3, 5] and all(r.complete for r in snap.rounds.values())


@req("C03")
def test_c03_shuffled_questions_keep_mapping():
    rows = practice_rows({2})
    original = {r["q_index"]: r["question_text"] for r in rows}
    random.Random(7).shuffle(rows)
    rnd = parse(rows).rounds[2]
    assert [q["q_index"] for q in rnd.question_list()] == list(range(1, 26))
    assert all(rnd.questions[int(k)]["question_text"] == v for k, v in original.items())


@req("C04", "C05")
def test_c04_c05_quoted_commas_and_multiline():
    rows = practice_rows({1})
    rows[0]["question_text"] = 'Revenue of $1,000, costs of $500,\nand "quoted" text?'
    rows[0]["option_a"] = "$1,234,567.89"
    rnd = parse(rows).rounds[1]
    assert rnd.questions[1]["question_text"] == 'Revenue of $1,000, costs of $500,\nand "quoted" text?'
    assert rnd.questions[1]["option_a"] == "$1,234,567.89" and rnd.complete


@req("C06")
def test_c06_utf8_and_bom():
    rows = practice_rows({1})
    rows[0]["question_text"] = "Le coût est de 5 € — naïve façade ✓?"
    snap = source.parse_questions(b"\xef\xbb\xbf" + to_csv(rows))
    assert snap.rounds[1].questions[1]["question_text"] == "Le coût est de 5 € — naïve façade ✓?"
    assert snap.rounds[1].complete  # header not corrupted by the BOM


@req("C07")
def test_c07_missing_required_column_is_actionable():
    raw = to_csv(practice_rows({1})).replace(b"option_c", b"opt_c", 1)
    with pytest.raises(source.SourceError, match=r"missing columns \['option_c'\]"):
        source.parse_questions(raw)


@req("C08")
@pytest.mark.parametrize("code,retried", [(404, False), (429, True), (500, True), (502, True), (503, True)])
def test_c08_http_errors(code, retried):
    calls = []

    def opener(req, t):
        calls.append(1)
        raise urllib.error.HTTPError("u", code, "x", {}, None)

    with pytest.raises(FetchError):
        http.get("https://example.invalid/q.csv", opener=opener, sleep=lambda s: None, attempts=4)
    assert len(calls) == (4 if retried else 1)


@req("C08", "C10")
def test_c08_failed_source_is_never_no_new_rounds(remote_and_clone):
    """A dead source makes the run FAIL; it must not look like a quiet 'nothing new' run."""
    from quizbot.gitops import Repo
    from quizbot.pipeline import EXIT_FAILURE, Pipeline
    _, clone = remote_and_clone
    work = clone()

    def dead():
        raise source.SourceError("HTTP 503 from both endpoints")
    rep = Pipeline(work, "production", repo=Repo(work), loader=dead).run()
    assert rep.exit_code == EXIT_FAILURE and "source unavailable" in rep.errors[0]


@req("C09")
@pytest.mark.parametrize("exc", [TimeoutError(), ConnectionResetError(), urllib.error.URLError("reset")])
def test_c09_timeouts_bounded(exc):
    calls = []

    def opener(req, t):
        calls.append(1)
        raise exc

    with pytest.raises(FetchError, match="3 attempts"):
        http.get("https://example.invalid/q.csv", opener=opener, sleep=lambda s: None, attempts=3)
    assert len(calls) == 3


@req("C10")
@pytest.mark.parametrize("body", [b"", b"\n\n", (",".join(HEADER) + "\n").encode()])
def test_c10_empty_200_rejected(body):
    with pytest.raises(source.SourceError):
        source.parse_questions(body)


@req("C11")
@pytest.mark.parametrize("body", [b"<!DOCTYPE html><html><body>Rate limit</body></html>",
                                  b"  <html>404</html>", b'{"message": "Not Found"}'])
def test_c11_html_or_error_page_rejected(body):
    with pytest.raises(source.SourceError):
        source.parse_questions(body)


@req("C12")
@pytest.mark.parametrize("cut", ["mid_quoted_field", "missing_fields"])
def test_c12_truncated(cut):
    raw = to_csv(practice_rows({1}))
    if cut == "mid_quoted_field":
        raw = raw[: raw.rfind(b'"')]  # stops inside the last quoted field
    else:
        last = raw.rstrip(b"\n").rsplit(b"\n", 1)
        raw = last[0] + b"\n" + b",".join(last[1].split(b",")[:5]) + b"\n"
    with pytest.raises(source.SourceError):
        source.parse_questions(raw)


@req("C13", "C18")
@pytest.mark.parametrize("field", ["option_a", "option_d", "question_text"])
def test_c13_c18_empty_option_or_text(field):
    rows = practice_rows({1})
    rows[4][field] = "   "
    rnd = parse(rows).rounds[1]
    assert not rnd.complete and any("empty" in p for p in rnd.problems)


@req("C14")
@pytest.mark.parametrize("bad", ["0", "-1", "26", "four", ""])
def test_c14_bad_q_index_makes_round_not_ready_but_others_fine(bad):
    rows = practice_rows({1, 2})
    rows[3]["q_index"] = bad  # row 3 belongs to round 1
    snap = parse(rows)
    assert not snap.rounds[1].complete and snap.rounds[2].complete


@req("C15")
@pytest.mark.parametrize("bad", ["", "abc", "0", "-3"])
def test_c15_bad_round_id_row_rejected(bad):
    rows = practice_rows({1, 2})
    rows[0]["round_id"] = bad
    snap = parse(rows)
    assert any("invalid round_id" in w for w in snap.warnings)
    assert not snap.rounds[1].complete and 1 in snap.rounds[1].missing_indices
    assert snap.rounds[2].complete


@req("C16", "C17")
def test_c16_c17_conflicting_duplicates_quarantined():
    rows = practice_rows({1})
    dup = dict(rows[6], option_b="$999.99")  # same round/q_index, different options
    rnd = parse(rows + [dup]).rounds[1]
    assert not rnd.complete and 7 in rnd.quarantined and 7 not in rnd.questions
    assert any("conflicting duplicate" in p for p in rnd.problems)


@req("C16")
def test_c16_identical_duplicate_rows_are_harmless_and_logged():
    rows = practice_rows({1})
    snap = parse(rows + [rows[6]])
    assert snap.rounds[1].complete and any("duplicate" in w for w in snap.warnings)


@req("C19", "C25")
def test_c19_c25_source_change_between_runs_revises_within_window(remote_and_clone, monkeypatch, practice_keys):
    """The run uses one consistent snapshot; a later change to the source is detected through the
    per-round content hash and re-solved (inside the 3h window)."""
    from quizbot import answers, config
    from quizbot.gitops import Repo
    from quizbot.pipeline import Pipeline
    from tests.conftest import as_round
    _, clone = remote_and_clone
    work = clone()
    rows = as_round(practice_rows({1}), 49, "2026-10-17T21:00:00Z")
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T21:10:00Z")
    Pipeline(work, "production", repo=Repo(work), loader=lambda: parse(rows)).run()
    new_q5 = next(r for r in practice_rows({9}) if r["q_index"] == "5")
    rows2 = [dict(new_q5, round_id="49", released_at="2026-10-17T21:00:00Z") if r["q_index"] == "5" else r for r in rows]
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T21:30:00Z")
    rep = Pipeline(work, "production", repo=Repo(work), loader=lambda: parse(rows2)).run()
    assert rep.revised == [49]
    got = answers.letters_of(Repo(work).remote_blob(config.answer_path(49)))
    assert got[5] == practice_keys[(9, 5)]


@req("C20")
def test_c20_one_endpoint_down_other_used():
    good = to_csv(practice_rows({1, 2}))

    def fetch(url, headers=None):
        if "api.github.com" in url:
            raise FetchError("HTTP 502")
        return FetchResult(url, 200, good, {})
    snap = source.fetch_questions(fetch=fetch)
    assert sorted(snap.rounds) == [1, 2] and any("502" in w for w in snap.warnings)


@req("C25")
def test_c25_stale_endpoint_loses_to_fresher_one():
    fresh, stale = to_csv(practice_rows({1, 2, 3})), to_csv(practice_rows({1}))

    def fetch(url, headers=None):
        return FetchResult(url, 200, stale if "raw.githubusercontent" in url else fresh, {})
    snap = source.fetch_questions(fetch=fetch)
    assert sorted(snap.rounds) == [1, 2, 3] and snap.sha256  # sha logged for traceability


@req("C21")
def test_c21_extra_column_tolerated():
    rows = practice_rows({1})
    raw = to_csv(rows).decode().splitlines()
    raw = [raw[0] + ",difficulty"] + [line + ",easy" for line in raw[1:]]
    snap = source.parse_questions(("\n".join(raw) + "\n").encode())
    assert snap.rounds[1].complete and "difficulty" not in snap.rounds[1].questions[1]


@req("C22")
def test_c22_duplicate_headers_rejected():
    raw = to_csv(practice_rows({1})).decode().splitlines()
    raw = [raw[0] + ",option_a"] + [line + ",X" for line in raw[1:]]
    with pytest.raises(source.SourceError, match="duplicate"):
        source.parse_questions(("\n".join(raw) + "\n").encode())


@req("C23")
def test_c23_complete_rounds_then_incomplete():
    rows = practice_rows({1, 2}) + [r for r in practice_rows({3}) if r["q_index"] != "12"]
    snap = parse(rows)
    assert snap.rounds[1].complete and snap.rounds[2].complete
    assert not snap.rounds[3].complete and snap.rounds[3].missing_indices == [12]


@req("C19")
def test_c19_changed_text_same_letter_is_recorded_once(remote_and_clone, monkeypatch):
    """Regression for a bug found by C19: a changed question whose letter did not change was
    re-solved on every later run because its new hash was never recorded."""
    from quizbot.gitops import Repo
    from quizbot.pipeline import Pipeline
    from tests.conftest import as_round, git
    _, clone = remote_and_clone
    work = clone()
    rows = as_round(practice_rows({1}), 49, "2026-10-17T21:00:00Z")
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T21:10:00Z")
    Pipeline(work, "production", repo=Repo(work), loader=lambda: parse(rows)).run()
    rows2 = [dict(r, question_text=r["question_text"] + " ") if r["q_index"] == "5" else r for r in rows]
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T21:20:00Z")
    first = Pipeline(work, "production", repo=Repo(work), loader=lambda: parse(rows2)).run()
    head = git(work, "rev-parse", "origin/main")
    monkeypatch.setenv("QUIZ_NOW", "2026-10-17T21:30:00Z")
    second = Pipeline(work, "production", repo=Repo(work), loader=lambda: parse(rows2)).run()
    git(work, "fetch", "--quiet")
    assert first.revised == [49] and second.revised == [] and git(work, "rev-parse", "origin/main") == head
