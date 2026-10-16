import datetime as dt
import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from quizbot import config, http
from quizbot.solver import solve_question
from quizbot.solver.llm import APISolver, ClaudeCLISolver, LLMSolver, safe_eval

# --- HTTP retry ---------------------------------------------------------------------


class FakeResp(io.BytesIO):
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, retry_after=None):
    hdrs = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("u", code, "x", hdrs, None)


def test_retries_429_and_5xx_then_succeeds():
    seq = [_http_error(429, "2"), _http_error(503), TimeoutError(), FakeResp(b"ok")]
    sleeps = []

    def opener(req, t):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    res = http.get("https://example.invalid/u", opener=opener, sleep=sleeps.append, attempts=5)
    assert res.body == b"ok" and len(sleeps) == 3 and sleeps[0] == 2.0


def test_non_retryable_status_fails_fast():
    calls = []

    def opener(req, t):
        calls.append(1)
        raise _http_error(404)

    with pytest.raises(http.FetchError, match="not retryable"):
        http.get("https://example.invalid/u", opener=opener, sleep=lambda s: None)
    assert len(calls) == 1


def test_bounded_attempts():
    def opener(req, t):
        raise urllib.error.URLError("down")

    with pytest.raises(http.FetchError, match="3 attempts"):
        http.get("https://example.invalid/u", opener=opener, sleep=lambda s: None, attempts=3)


def test_backoff_is_bounded():
    assert all(0 <= http.backoff_delay(a) <= 30 for a in range(20))


# --- LLM fallback (mocked; no network) ------------------------------------------------

def _response(payload, stop="end_turn"):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)])


class FakeClient:
    def __init__(self, resp):
        self.resp, self.calls = resp, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


UNKNOWN_Q = "A widget costs $40.00 to make and $10.00 to ship. What is the total cost of 3 widgets?"
OPTS = ["$150.00", "$120.00", "$130.00", "$50.00"]


def test_llm_verified_when_expression_agrees():
    llm = APISolver(client=FakeClient(_response({"letter": "A", "result": "150", "expression": "3*(40+10)", "method": "m"})))
    r = solve_question(1, UNKNOWN_Q, OPTS, llm)
    assert (r.letter, r.status, r.confident) == ("A", "llm_verified", True)


def test_llm_letter_contradicting_its_expression_is_overridden_and_flagged():
    llm = APISolver(client=FakeClient(_response({"letter": "B", "result": "150", "expression": "3*(40+10)", "method": "m"})))
    r = solve_question(1, UNKNOWN_Q, OPTS, llm)
    assert r.letter == "A" and not r.confident


@pytest.mark.parametrize("resp", [
    _response("not json"), _response({"letter": "E", "result": "", "expression": "", "method": ""}),
    _response({"letter": "A"}, stop="refusal"), _response("{", stop="max_tokens"),
])
def test_invalid_llm_output_falls_back_to_flagged_guess(resp):
    r = solve_question(1, UNKNOWN_Q, OPTS, APISolver(client=FakeClient(resp)))
    assert r.status == "guess"


def test_llm_exception_falls_back():
    r = solve_question(1, UNKNOWN_Q, OPTS, APISolver(client=FakeClient(RuntimeError("boom"))))
    assert r.status == "guess"


def test_llm_budget_enforced():
    fc = FakeClient(_response({"letter": "A", "result": "150", "expression": "150", "method": "m"}))
    llm = APISolver(client=fc, max_calls=1)
    solve_question(1, UNKNOWN_Q, OPTS, llm)
    assert solve_question(2, UNKNOWN_Q, OPTS, llm).status == "guess" and len(fc.calls) == 1


def test_llm_not_called_for_template_questions():
    fc = FakeClient(_response({"letter": "B", "result": "", "expression": "", "method": ""}))
    q = "An item listed at $50.00 is discounted by 10%. What is the sale price?"
    assert solve_question(1, q, ["$45.00", "$55.00", "$5.00", "$40.00"], APISolver(client=fc)).letter == "A"
    assert fc.calls == []


def test_llm_request_shape_keeps_question_as_data():
    fc = FakeClient(_response({"letter": "A", "result": "150", "expression": "150", "method": "m"}))
    APISolver(client=fc).solve(UNKNOWN_Q, OPTS)
    kw = fc.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["output_config"]["format"]["type"] == "json_schema"
    assert "<question>" in kw["messages"][0]["content"] and "untrusted" in kw["system"]
    assert "tools" not in kw


@pytest.mark.parametrize("expr,val", [("3*(40+10)", 150.0), ("31500*1.12**7", 31500 * 1.12 ** 7), ("-2+1", -1.0)])
def test_safe_eval_arithmetic(expr, val):
    assert safe_eval(expr) == pytest.approx(val)


@pytest.mark.parametrize("expr", ["__import__('os').system('x')", "open('f')", "a+1", "9**9**9", "1/0", "", "[1]"])
def test_safe_eval_rejects_non_arithmetic(expr):
    assert safe_eval(expr) is None


# --- time policy ----------------------------------------------------------------------

def test_schedule_is_pacific_daylight_time():
    assert config.scheduled_release(49) == dt.datetime(2026, 10, 17, 21, 0, tzinfo=config.UTC)
    assert config.scheduled_release(168) == dt.datetime(2026, 10, 22, 20, 0, tzinfo=config.UTC)
    assert config.scheduled_release(168).astimezone(config.PACIFIC).hour == 13
    assert config.scheduled_release(10) is None


@pytest.mark.parametrize("hours,credit", [(0, 1.0), (2.99, 1.0), (3.0, 0.9), (5 + 20 / 60, 0.7), (11.5, 0.1), (12, 0.0), (30, 0.0)])
def test_timing_credit_table(hours, credit):
    t0 = dt.datetime(2026, 10, 17, 21, tzinfo=config.UTC)
    assert config.timing_credit(t0, t0 + dt.timedelta(hours=hours)) == credit


def test_revision_window_has_safety_margin():
    t0 = dt.datetime(2026, 10, 17, 21, tzinfo=config.UTC)
    assert config.can_revise(t0, t0 + dt.timedelta(hours=2, minutes=30))
    assert not config.can_revise(t0, t0 + dt.timedelta(hours=2, minutes=45))
    assert not config.can_revise(t0, t0 + dt.timedelta(hours=4))


def test_llm_only_evaluation_samples_every_category(practice_snapshot, practice_keys):
    from quizbot.evaluate import evaluate_llm_only

    class KeyedClient(FakeClient):
        """Answers with the official key, so the harness itself is what's under test."""
        def _create(self, **kw):
            self.calls.append(kw)
            return _response({"letter": "A", "result": "", "expression": "", "method": "m"})

    rep = evaluate_llm_only(practice_snapshot, practice_keys, APISolver(client=KeyedClient(None)), per_category=1)
    assert rep["sampled"] == 12 and rep["llm_calls"] == 12
    assert len({r["category"] for r in rep["rows"]}) == 12
    assert rep["correct"] == sum(r["key"] == "A" for r in rep["rows"])


# --- Claude subscription (headless CLI) backend -----------------------------------------

def _cli_stdout(structured, is_error=False, subtype="success"):
    return json.dumps({"type": "result", "subtype": subtype, "is_error": is_error,
                       "structured_output": structured, "result": json.dumps(structured)})


class FakeRun:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode, self.cmds = stdout, returncode, []

    def __call__(self, cmd, **kw):
        self.cmds.append((cmd, kw))
        return SimpleNamespace(stdout=self.stdout, returncode=self.returncode)


def test_cli_backend_verified_answer_and_safe_command():
    run = FakeRun(_cli_stdout({"letter": "A", "result": "150", "expression": "3*(40+10) = 150", "method": "m"}))
    r = solve_question(1, UNKNOWN_Q, OPTS, ClaudeCLISolver(binary="claude", runner=run))
    assert (r.letter, r.status) == ("A", "llm_verified")
    cmd, kw = run.cmds[0]
    assert cmd[cmd.index("--tools") + 1] == ""             # every tool disabled
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    assert "--strict-mcp-config" in cmd and "--json-schema" in cmd
    assert "shell" not in kw and kw["timeout"] > 0           # argv list, no shell


def test_cli_backend_wrong_letter_corrected_by_arithmetic():
    """Reproduces the real failure seen in testing: right arithmetic, wrong letter."""
    run = FakeRun(_cli_stdout({"letter": "C", "result": "150", "expression": "3*(40+10)=150...", "method": "m"}))
    r = solve_question(1, UNKNOWN_Q, OPTS, ClaudeCLISolver(runner=run))
    assert r.letter == "A" and not r.confident


@pytest.mark.parametrize("stdout,code", [("not json", 0), (_cli_stdout(None, is_error=True, subtype="error"), 1),
                                         (_cli_stdout({"letter": "Z", "expression": ""}), 0), ("", 137)])
def test_cli_backend_failures_fall_back(stdout, code):
    assert solve_question(1, UNKNOWN_Q, OPTS, ClaudeCLISolver(runner=FakeRun(stdout, code))).status == "guess"


def test_cli_timeout_falls_back():
    def slow(cmd, **kw):
        import subprocess
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    assert solve_question(1, UNKNOWN_Q, OPTS, ClaudeCLISolver(runner=slow)).status == "guess"


def test_backend_selection(monkeypatch):
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "QUIZ_USE_CLAUDE_CLI", "QUIZ_CLAUDE_BIN"):
        monkeypatch.delenv(k, raising=False)
    assert LLMSolver.from_env() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert isinstance(LLMSolver.from_env(), APISolver)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "y")
    monkeypatch.setenv("QUIZ_CLAUDE_BIN", "/usr/bin/true")
    assert isinstance(LLMSolver.from_env(), ClaudeCLISolver)  # subscription preferred when both exist


def test_safe_eval_accepts_equation_form():
    assert safe_eval("3*(40+10) = 150") == 150.0 and safe_eval("$1,000 * 0.92") == pytest.approx(920.0)
