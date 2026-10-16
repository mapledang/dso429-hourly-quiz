"""Registry of every QA requirement ID with its title and test level.

Levels: unit | integration (real git/HTTP plumbing, mocked network) | e2e (whole pipeline against a
temporary bare remote) | regression (official practice data) | static (config/workflow inspection)
| simulation (simulated clock / cloud schedule) | live (real GitHub, recorded in live_evidence.json)
| manual (cannot be automated; documented only).
"""

R = {}


def _add(level, items):
    for rid, title in items:
        R[rid] = {"title": title, "level": level}


_add("unit", [
    ("C01", "Valid CSV with one complete round"), ("C02", "Multiple complete rounds, any row order"),
    ("C03", "Shuffled questions indexed by round_id/q_index"), ("C04", "Quoted commas preserved"),
    ("C05", "Quoted multiline text preserved"), ("C06", "UTF-8 characters and BOM"),
    ("C07", "Missing required column rejected"), ("C10", "HTTP 200 empty body not an empty quiz"),
    ("C11", "HTML error page rejected"), ("C12", "Truncated CSV rejected"),
    ("C13", "Question with no options -> round incomplete"), ("C14", "Bad q_index (0,-1,26,text,missing)"),
    ("C15", "Missing/non-numeric round_id row rejected"), ("C16", "Duplicate q_index never silently chosen"),
    ("C17", "Same question with different options flagged"), ("C18", "Empty text/option not eligible"),
    ("C21", "Unknown extra column tolerated"), ("C22", "Duplicate headers rejected"),
    ("C23", "Complete rounds then an incomplete round"), ("C24", "Rounds out of chronological order"),
])
_add("integration", [
    ("C08", "HTTP 404/429/500/502/503 retry or fail"), ("C09", "Timeout / connection reset bounded retries"),
    ("C19", "Source changes during/between runs"), ("C20", "Transient source failure, other endpoint OK"),
    ("C25", "Stale but valid fetch reconciled"),
])
_add("e2e", [
    ("R01", "One new complete round"), ("R02", "Five rounds at once"), ("R03", "Older round missing"),
    ("R04", "Valid remote file: no replacement/commit"), ("R05", "Malformed existing file recovered"),
    ("R06", "Remote file absent locally respected"), ("R07", "Local unpushed file reconciled"),
    ("R08", "Crash after solve before push recovers"), ("R09", "Crash after push before manifest"),
    ("R10", "Idempotent second run"), ("R11", "Rounds arriving during a prior run"),
    ("R12", "Scheduled but unpublished round = awaiting"), ("R13", "Source down for hours then back"),
    ("R14", "Manifest missing"), ("R15", "Manifest corrupt"), ("R16", "Manifest says submitted, file missing"),
    ("R17", "Round disappears from source"), ("R18", "New round while another incomplete"),
    ("R19", "Round id outside 1-168"), ("R20", "Conflicting release timestamps"),
])
_add("unit", [(f"A{i:02d}", t) for i, t in enumerate([
    "Exact numeric match", "Rounding-close match", "Similar options compared fully",
    "No option matches -> flagged", "Identical options -> ambiguous", "Options reordered",
    "Independent letters per question", "Compound vs simple interest", "Monthly vs annual compounding",
    "Percent change vs percentage points", "Margin vs markup", "Weighted vs arithmetic average",
    "Negative/zero/undefined values", "Monetary rounding", "Units differ between question and options",
    "Ambiguous time period", "Conceptual question", "Prompt injection is data",
    "Model malformed JSON -> retry then reject", "Model returns incomplete answer set",
    "Model invalid letters", "Model extra fields/duplicate keys", "Wrong question-index mapping",
    "Model timeout / partial output", "Model rate limit", "Model disagrees with independent calc",
    "Repeated question, new numbers", "Option formats $ , % ()", "Irrelevant numbers in context",
    "Before/after tax wording"], start=1)])
_add("regression", [
    ("P01", "48 rounds / 1,200 questions covered"), ("P02", "Shuffled keys matched by id"),
    ("P03", "Invalid key letter flagged"), ("P04", "Missing key -> incomplete coverage"),
    ("P05", "Key for nonexistent question reported"), ("P06", "Per-question/round/overall accuracy"),
    ("P07", "Below threshold fails readiness"), ("P08", "Category-level analysis"),
    ("P09", "Regression suite runs on solver change (CI)"), ("P10", "Evaluation reproducible"),
    ("P11", "Evaluator alignment bug caught"), ("P12", "Keys unavailable -> BLOCKED"),
])
_add("unit", [(f"V{i:02d}", t) for i, t in enumerate([
    "Valid file", "Missing q25", "Extra q26", "Duplicate keys", "round_id != filename",
    "3/5-digit filename", "Wrong directory", "Malformed JSON", "Lowercase letter", "Trailing whitespace",
    "Integer/null answers", "Extra top-level field", "Interrupted write is atomic", "Only 24 questions",
    "Shifted answers detected by mapping tests", "Changed after validation before commit",
    "Changed after commit before push", "CRLF/encoding tolerant parse", "Valid file not replaced by malformed",
    "Misleading filename detected by audit"], start=1)])
_add("integration", [(f"G{i:02d}", t) for i, t in enumerate([
    "Push succeeds and verified", "Transient push failure retried", "Non-fast-forward reconciled",
    "Missing auth explicit failure", "Read-only token detected", "Private repo warned by audit",
    "Default branch != main flagged", "Simultaneous pushes", "Push landed but client timed out",
    "Git success but file absent -> verify fails", "Remote content mismatch reported", "No empty commit",
    "No recursive workflow triggers", "Repo renamed/transferred refused", "Unrelated local files untouched",
    "Batch several rounds in one push", "One bad round, four good", "Remote changes after fetch",
    "Push retries exhausted visible", "GitHub unavailable then recovers"], start=1)])
_add("unit", [(f"T{i:02d}", t) for i, t in enumerate([
    "Push at +10 min credit", "Exactly 3h boundary", "3h + 1s", "2h59m eligible", "First push after 3h",
    "Two versions within 3h -> last", "One within, one after -> within", "Late-published round",
    "CSV lag 5 min", "UTC/Pacific midnight crossing", "DST via tz database", "Round 168 processed",
    "Late discovery reported", "Commit dates not used", "Deadline passed reported honestly"], start=1)])
_add("static", [
    ("CLOUD01", "Workflow YAML valid"), ("CLOUD02", "GitHub-hosted runners only"),
    ("CLOUD09", "Concurrency group serialises writers"), ("CLOUD13", "Cron matches Pacific cadence"),
    ("CLOUD16", "Artifact upload failure non-fatal"),
])
_add("simulation", [
    ("CLOUD06", "Delayed schedule reconciled"), ("CLOUD07", "Skipped schedule reconciled"),
    ("CLOUD08", "Timeout -> later reconciliation"), ("CLOUD10", "Disabled workflow detected (watchdog)"),
    ("CLOUD11", "Missing secret -> safe, explicit"), ("CLOUD12", "Read-only token -> push fails clearly"),
    ("CLOUD14", "Pushed but manifest not recorded"), ("CLOUD15", "Stale automation alert"),
    ("CLOUD17", "LLM local-only auth not assumed in cloud"), ("CLOUD18", "Success but no round -> coverage flags"),
    ("CLOUD20", "24 simulated hours: no duplicates/runaway"),
])
_add("live", [
    ("CLOUD03", "Runs with laptop off (scheduled run on GitHub infra)"),
    ("CLOUD04", "Independent of local Claude Code session"),
    ("CLOUD05", "Independent of local internet"), ("CLOUD19", "Manual recovery run"),
])
_add("unit", [(f"SEC{i:02d}", t) for i, t in enumerate([
    "Ignore-instructions text is data", "Request for API key", "Malicious strings never executed",
    "Auth errors redacted", "Secret env values never printed", "Path traversal impossible",
    "Dependency install pinned/safe", "API error bodies redacted", "Fabricated model success ignored",
    "Question cannot change workflow/credentials"], start=1)])
_add("e2e", [(f"F{i:02d}", t) for i, t in enumerate([
    "Before writing answer", "During atomic write", "After write before validation",
    "After validation before staging", "After staging before commit", "After commit before push",
    "Server got push, client didn't", "After push before verify", "After verify before manifest",
    "While fetching", "While solving q25", "Second of several rounds", "While writing logs",
    "Artifact upload", "Notifications", "Workflow terminated mid-run", "Invalid model output x3",
    "HTTP 429 until budget exhausted", "200 with malformed CSV", "Two workers same round"], start=1)])
_add("e2e", [(f"S{i:02d}", t) for i, t in enumerate([
    "New complete round end-to-end", "Five rounds together", "One incomplete round",
    "Network fails first attempt", "Push confirmation lost", "Two workers start together",
    "Three missed hourly runs", "One malformed answer gated", "Hard question -> fallback policy",
    "Final round late"], start=1)])
_add("e2e", [
    ("N01", "Every round classified into one defined state"), ("N02", "Unreleased never 'missing'"),
    ("N03", "Padding/validity/round_id/q1-q25/letters checked"), ("N04", "Visibility/owner/name/branch checked"),
    ("N05", "No fixtures or secrets committed"), ("N06", "Practice accuracy from real keys"),
    ("N07", "Latest cloud run recorded"), ("N08", "Missing/unverified listed"),
    ("N09", "Usage report stays a separate requirement"),
])
