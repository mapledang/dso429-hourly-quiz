import pytest

from quizbot import source
from quizbot.http import FetchError, FetchResult
from tests.conftest import HEADER, practice_rows, to_csv


def test_practice_fixture_is_48_complete_rounds(practice_snapshot):
    assert sorted(practice_snapshot.rounds) == list(range(1, 49))
    assert all(r.complete for r in practice_snapshot.rounds.values())
    assert sum(len(r.questions) for r in practice_snapshot.rounds.values()) == 1200


def test_quoted_commas_and_multiline_text_preserved():
    rows = practice_rows({1})
    rows[0] = {**rows[0], "question_text": 'Line one, with "quotes",\nline two?'}
    snap = source.parse_questions(to_csv(rows))
    assert snap.rounds[1].questions[1]["question_text"] == 'Line one, with "quotes",\nline two?'
    assert snap.rounds[1].complete


def test_bom_is_handled():
    snap = source.parse_questions(b"\xef\xbb\xbf" + to_csv(practice_rows({1})))
    assert snap.rounds[1].complete


@pytest.mark.parametrize("raw", [b"", b"   \n", (",".join(HEADER) + "\n").encode()])
def test_empty_sources_raise_not_return_nothing(raw):
    with pytest.raises(source.SourceError):
        source.parse_questions(raw)


def test_missing_column_raises():
    raw = to_csv(practice_rows({1})).replace(b"option_d", b"option_x", 1)
    with pytest.raises(source.SourceError, match="missing columns"):
        source.parse_questions(raw)


def test_truncated_row_raises():
    raw = to_csv(practice_rows({1}))
    cut = raw[: raw.rfind(b",")]  # chop the final field off the last line
    with pytest.raises(source.SourceError):
        source.parse_questions(cut)


def test_non_utf8_raises():
    with pytest.raises(source.SourceError):
        source.parse_questions(to_csv(practice_rows({1})) + b"\xff\xfe")


def test_non_integer_ids_raise():
    rows = practice_rows({1})
    rows[3]["q_index"] = "four"
    with pytest.raises(source.SourceError):
        source.parse_questions(to_csv(rows))


def test_incomplete_round_detected():
    rows = [r for r in practice_rows({1}) if r["q_index"] != "17"]
    rnd = source.parse_questions(to_csv(rows)).rounds[1]
    assert not rnd.complete and rnd.missing_indices == [17]


def test_out_of_range_q_index_flags_round():
    rows = practice_rows({1})
    rows[0]["q_index"] = "26"
    assert not source.parse_questions(to_csv(rows)).rounds[1].complete


def test_exact_duplicate_row_is_tolerated_but_conflicting_is_not():
    rows = practice_rows({1})
    snap = source.parse_questions(to_csv(rows + [rows[4]]))
    assert snap.rounds[1].complete and snap.warnings
    conflicting = {**rows[4], "option_a": "$0.00"}
    assert not source.parse_questions(to_csv(rows + [conflicting])).rounds[1].complete


def test_inconsistent_release_time_flags_round():
    rows = practice_rows({1})
    rows[2]["released_at"] = "2026-10-15T22:00:00Z"
    assert not source.parse_questions(to_csv(rows)).rounds[1].complete


def test_timestamps_are_utc_aware(practice_snapshot):
    t = practice_snapshot.rounds[1].released_at
    assert t.utcoffset().total_seconds() == 0 and t.hour == 21


def test_out_of_order_and_non_contiguous_rounds():
    rows = list(reversed(practice_rows({3, 7})))
    snap = source.parse_questions(to_csv(rows))
    assert sorted(snap.rounds) == [3, 7] and all(r.complete for r in snap.rounds.values())


def test_content_hash_changes_when_question_changes():
    rows = practice_rows({1})
    h1 = source.parse_questions(to_csv(rows)).rounds[1].content_hash()
    rows[0]["option_b"] = "$1.00"
    assert source.parse_questions(to_csv(rows)).rounds[1].content_hash() != h1


def test_fetch_prefers_snapshot_with_more_complete_rounds():
    fresh, stale = to_csv(practice_rows({1, 2})), to_csv(practice_rows({1}))

    def fake(url, headers=None):
        return FetchResult(url, 200, fresh if "api.github.com" in url else stale, {})

    assert sorted(source.fetch_questions(fetch=fake).rounds) == [1, 2]


def test_fetch_falls_back_when_one_endpoint_fails():
    good = to_csv(practice_rows({1}))

    def fake(url, headers=None):
        if "api.github.com" in url:
            raise FetchError("HTTP 503")
        return FetchResult(url, 200, good, {})

    snap = source.fetch_questions(fetch=fake)
    assert 1 in snap.rounds and any("503" in w for w in snap.warnings)


def test_fetch_total_failure_raises():
    def fake(url, headers=None):
        raise FetchError("timeout")

    with pytest.raises(source.SourceError):
        source.fetch_questions(fetch=fake)
