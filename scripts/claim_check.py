#!/usr/bin/env python3
"""claim-check — say what a transcript can and cannot show about the agent's claims.

Runs as a Claude Code `Stop` hook. Reads the session transcript, extracts claim
candidates from the final assistant message, and reports for each one what the
transcript holds about it.

Two steps, kept apart:
  extract  — heuristic. Regular expressions over prose. It misses claims and can
             misread them. A match is a candidate, not an established statement.
  assess   — deterministic. Given a claim kind and what was observed, the verdict
             is fixed.

A transcript holds tool requests. It does not hold results, exit codes or effects,
and it does not hold what hooks, subagents, scripts or the user's own shell did. So
this hook gives two verdicts and no others:
  insufficient  — a test claim. A matching request may have been seen; a request is
                  not a result.
  unchecked     — a commit, push or file claim. Nothing here can check it.

It never says `supported` or `contradicted`. Those need evidence that binds a result
to a run; see claim_evidence.py and pytest_evidence.py, which this hook does not read.

Design rules (deliberate, do not "fix" without reading README §Limits):
  * Never blocks. CLAIM_CHECK_ENFORCE is ignored: there is no verdict to block on.
  * Never crashes the session. Any internal error exits 0 silently.
  * Silent when no claim is recognised.
  * Conservative matching: a missed claim is cheaper than an invented one.
  * Command text and tool input never reach the report or the log.

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

def _manifest_version() -> str:
    """Single source of truth: the plugin manifest."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "0.0.0")
    except Exception:
        return "0.0.0"


_BASE_VERSION = _manifest_version()


def _version() -> str:
    """Version plus a digest of this file.

    The log reported v0.1.0 both before and after a behaviour change, so two
    different tools shared one identifier. The digest makes the record honest.
    """
    try:
        import hashlib

        with open(os.path.abspath(__file__), "rb") as fh:
            return f"{_BASE_VERSION}+{hashlib.sha256(fh.read()).hexdigest()[:8]}"
    except Exception:
        return _BASE_VERSION


VERSION = _version()

EXIT_OK = 0

# How long to wait for the transcript to flush the final assistant message.
# The transcript is written asynchronously and can lag the in-memory conversation.
SETTLE_TRIES = 3
SETTLE_SLEEP_S = 0.4

# ---------------------------------------------------------------------------
# Observation model
# ---------------------------------------------------------------------------


@dataclass
class Observed:
    """Tool requests seen in the transcript. Counts and edit paths, no command text.

    Every field counts requests. None of them says a command ran, finished or
    succeeded, and a count of zero does not say nothing happened.
    """

    shell_requests: int = 0
    test_requests: int = 0  # shell requests whose text names a test runner
    edit_request_paths: list[str] = field(default_factory=list)


TEST_CMD = re.compile(
    r"""\b(
        pytest | py\.test | tox | nox
      | cargo\s+(test|nextest)
      | go\s+test
      | (npm|pnpm|yarn|bun)\s+(run\s+)?test
      | jest | vitest | mocha | ava | playwright\s+test | cypress\s+run
      | (dotnet|swift|mvn|gradle(w)?)\s+test
      | rspec | minitest | phpunit | ctest
      | make\s+(test|check)
      | (deno|bun)\s+test | node\s+--test
      | python[0-9.]*\s+-m\s+(unittest|pytest)
    )\b""",
    re.VERBOSE | re.IGNORECASE,
)

# Running a test file directly — `python3 tests/test_foo.py`, `node x.test.js`.
# The interpreter is required so that `cat tests/test_foo.py` is not counted, and the
# path token may not contain whitespace so a heredoc body cannot be crossed.
TEST_FILE_CMD = re.compile(
    r"""\b(python[0-9.]*|node|deno|bun|ruby|perl|php)\s+
        (?:-\w+\s+)*
        [^\s;|&<>]*test[^\s;|&<>]*\.(py|js|mjs|cjs|ts|rb|pl|php)\b""",
    re.VERBOSE | re.IGNORECASE,
)

EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit", "str_replace_editor", "apply_patch"}
SHELL_TOOLS = {"Bash", "BashOutput", "shell", "run_command"}

# ---------------------------------------------------------------------------
# Claim extraction (heuristic)
#
# Conservative by construction. Each pattern targets a confident, checkable
# assertion; vague progress prose is deliberately not matched.
# ---------------------------------------------------------------------------

# Every action claim must have the agent as its subject. Without this,
# "Aegis pushed again" and "what a COMMITTED claim proves" both read as claims.
# Accepted subjects: an explicit first person, or a line-initial past-tense verb
# (the bullet-summary idiom: "- Updated `src/foo.py`").
# "and" or a comma continues a subject already established:
#   "Committed and pushed."            -> and
#   "…closed on head `abc`, pushed to" -> comma
# The comma also admits "Aegis reviewed it, pushed a fix" and "nothing edited,
# committed or pushed". The second is handled by NEGATIVE_BEFORE below.
SUBJECT = r"(?:\bI\s+(?:have\s+|'ve\s+|just\s+)?|\band\s+|,\s+|(?:^|\n)\s*(?:[-*+]\s*)?)"

CLAIM_TESTS_PASS = re.compile(
    r"""(
        \b(all\s+)?(the\s+)?tests?\b[^.\n]{0,40}\b(pass(es|ed|ing)?|are\s+green|succeed(ed)?)\b
      | \btest\s+suite\b[^.\n]{0,30}\b(pass(es|ed|ing)?|green)\b
      | \ball\s+(\d+\s+)?tests?\s+(are\s+)?(now\s+)?(pass(ing|ed)?|green)\b
    )""",
    re.VERBOSE | re.IGNORECASE,
)

CLAIM_TESTS_RUN = re.compile(
    r"\bI\s+(ran|executed|have\s+run)\b[^.\n]{0,30}\btests?\b", re.IGNORECASE
)

CLAIM_COMMITTED = re.compile(
    SUBJECT + r"(committed|commited|made\s+a\s+commit|created\s+a\s+commit)\b",
    re.IGNORECASE | re.MULTILINE,
)

CLAIM_PUSHED = re.compile(SUBJECT + r"(pushed)\b", re.IGNORECASE | re.MULTILINE)

# A sentence that negates the action is not a claim that it happened.
# "I haven't committed" must never be read as a commit claim.
NEGATED = re.compile(
    r"""\b(
        have\s*n[o']?t | has\s*n[o']?t | had\s*n[o']?t
      | did\s*n[o']?t | do\s*n[o']?t | does\s*n[o']?t
      | was\s*n[o']?t | were\s*n[o']?t | is\s*n[o']?t | are\s*n[o']?t
      | never | not\s+yet | no\s+longer | without | failed\s+to | unable\s+to
    )\b""",
    re.VERBOSE | re.IGNORECASE,
)

# "Nothing was edited, committed or pushed." is how a turn that only reports ends,
# and SUBJECT's comma reads it as a commit claim. A word rule, not a parser: a
# positive claim is dropped when one of these words comes before it in its sentence,
# when the sentence opens with "not", or when one directly follows it ("pushed
# nothing", "Committed: nothing"). The cost is known and tested: "There was no
# reason to wait, so I committed" is dropped too.
NEGATIVE_BEFORE = re.compile(r"\b(?:nothing|none|neither|nor|no)\b|^\W*not\b", re.IGNORECASE)
NEGATIVE_AFTER = re.compile(r"(?::\s*|\s+)(?:nothing|none|no)\b", re.IGNORECASE)

# A sentence reporting a failure is not a clean pass claim, even when it
# contains the word "passes" ("the only failure was X, which passes in isolation").
# But a COUNTED-ZERO failure is a pass statement: "49 tests pass, 0 fail" is a
# claim, and real transcripts phrase it that way.
FAILURE_CONTEXT = re.compile(
    r"""(?<!\b0\s)(?<!\bno\s)(?<!\bzero\s)
        \b(fail(s|ed|ing|ure|ures)?|error(s)?|broke(n)?|regress(ed|ion|ions)?)\b""",
    re.VERBOSE | re.IGNORECASE,
)

# "updated `src/foo.py`" / "created the file src/foo.py"
# The filler is LAZY: a greedy one eats into the path and captures a suffix
# ("s.py" out of "src/secrets.py"), which then never matches anything real.
CLAIM_EDITED_FILE = re.compile(
    r"""(?:\bI\s+(?:have\s+|'ve\s+|just\s+)?|(?:^|\n)[ \t]*(?:[-*+][ \t]*)?)
        (updated|created|added|modified|wrote|edited|changed)\b
        [^.\n`]{0,40}?
        [`"']?(?P<path>[\w./-]+\.[A-Za-z]{1,6})[`"']?""",
    re.VERBOSE | re.IGNORECASE | re.MULTILINE,
)

# "github.com" is not a file. Without a directory separator, only accept
# extensions that plausibly name a file rather than a top-level domain.
_TLD_LIKE = {
    "com", "org", "net", "io", "dev", "ai", "co", "app", "sh", "gov", "edu", "me", "info",
}


def looks_like_file(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if "/" in path:
        return True
    return bool(ext) and ext not in _TLD_LIKE

# An explicit negative claim about a file.
CLAIM_UNTOUCHED = re.compile(
    r"""\b(
        did\s*n[o']?t\s+(touch|modify|change|edit)
      | no\s+changes?\s+to
      | left\s+[^.\n]{0,30}?\s+untouched
    )\b[^.\n]{0,40}?[`"']?(?P<path>[\w./-]+\.[A-Za-z]{1,6})[`"']?""",
    re.VERBOSE | re.IGNORECASE,
)

# A sentence that hedges, instructs, or looks forward is not a claim about
# completed work. "This should make the tests pass once you run them" says
# nothing about whether they ran.
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
HEDGE = re.compile(
    r"""\b(
        should | would | could | will | wo n't | won't | shall
      | may | might | can | ca n't | cannot | can't
      | if | once | unless | whenever
      | next | plan | planning | intend | going\s+to | about\s+to
      | try | trying | please | feel\s+free | let\s+me\s+know
      | you\s+(can|should|may|might|need|want|could|will|must)
      | (before|after|when)\s+you
      | recommend | suggest | consider
    )\b""",
    re.VERBOSE | re.IGNORECASE,
)


# Markdown structure is not prose. Real sessions put URLs, tables and code
# blocks in the final message, and every one of them was read as a claim
# before this existed: "| E2E test … | Pass" read as a passing suite, and
# "[updated](https://github.com/…)" read as a file edit.
FENCED_CODE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
TABLE_ROW = re.compile(r"^[ \t]*\|.*$", re.MULTILINE)
MD_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")
BARE_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)

# An inline-code span containing whitespace is a quoted phrase, not a path.
# Single-token spans are kept, because that is where real paths live. Spans are
# taken in order, pair by pair: a pattern that asks for "a span with a space in it"
# pairs the closing backtick of one span with the opening backtick of the next and
# deletes the prose between two paths.
INLINE_CODE = re.compile(r"`[^`\n]*`")


def strip_markup(text: str) -> str:
    """Remove structure that is not a spoken claim."""
    text = FENCED_CODE.sub(" ", text)
    text = TABLE_ROW.sub(" ", text)
    text = MD_LINK.sub(r"\1", text)  # keep the link text, drop the target
    text = BARE_URL.sub(" ", text)
    text = INLINE_CODE.sub(lambda m: " " if re.search(r"\s", m.group(0)) else m.group(0), text)
    return text


def assertive_sentences(text: str, drop_negated: bool = True) -> list[str]:
    """Sentences that assert completed work by the agent.

    Hedged, conditional and instructional sentences are always dropped. Negated
    ones are dropped for POSITIVE claims but kept for the explicitly negative
    claim ("I did not touch X"), which is itself the thing being claimed.
    """
    out = []
    for s in SENTENCE_SPLIT.split(strip_markup(text)):
        s = s.strip()
        if not s or HEDGE.search(s):
            continue
        if drop_negated and NEGATED.search(s):
            continue
        out.append(s)
    return out


@dataclass
class Claim:
    """A claim candidate found in prose. Not an established statement."""

    kind: str  # tests_pass | tests_ran | committed | pushed | edited_file | left_untouched
    quote: str
    path: str = ""


def _cancelled(sentence: str, m: "re.Match[str]") -> bool:
    return bool(NEGATIVE_BEFORE.search(sentence[: m.start()]) or NEGATIVE_AFTER.match(sentence, m.end()))


def extract(text: str) -> list[Claim]:
    """Claim candidates in the final message, in a fixed order."""
    claims: list[Claim] = []
    sentences = assertive_sentences(text)

    # One test claim per message, not one per phrasing. A sentence that also
    # reports a failure is not a pass claim.
    for s in sentences:
        if FAILURE_CONTEXT.search(s):
            continue
        passed = CLAIM_TESTS_PASS.search(s)
        m = passed or CLAIM_TESTS_RUN.search(s)
        if m and not _cancelled(s, m):
            claims.append(Claim("tests_pass" if passed else "tests_ran", m.group(0).strip()[:80]))
            break

    for kind, pattern, quote in (
        ("committed", CLAIM_COMMITTED, "claimed a commit"),
        ("pushed", CLAIM_PUSHED, "claimed a push"),
    ):
        if any(not _cancelled(s, m) for s in sentences for m in pattern.finditer(s)):
            claims.append(Claim(kind, quote))

    for s in sentences:
        for m in CLAIM_EDITED_FILE.finditer(s):
            if looks_like_file(m.group("path")) and not _cancelled(s, m):
                claims.append(Claim("edited_file", m.group(0).strip()[:80], m.group("path")))

    # Negation is the claim here, so this pass reads the sentences the others discard.
    for s in assertive_sentences(text, drop_negated=False):
        for m in CLAIM_UNTOUCHED.finditer(s):
            claims.append(Claim("left_untouched", m.group(0).strip()[:80], m.group("path")))

    return claims


# ---------------------------------------------------------------------------
# Assessment (deterministic)
# ---------------------------------------------------------------------------


@dataclass
class Assessment:
    verdict: str  # "insufficient" | "unchecked"
    kind: str
    claim: str
    detail: str


def path_matches(requested: str, claimed: str) -> bool:
    """True when a requested path ends with the claimed path on a component boundary."""
    want = [p for p in claimed.replace("\\", "/").split("/") if p not in ("", ".")]
    have = [p for p in requested.replace("\\", "/").split("/") if p not in ("", ".")]
    return bool(want) and have[-len(want):] == want


def _requests(n: int, noun: str, qualifier: str = "") -> str:
    tail = f" {qualifier}" if qualifier else ""
    if n == 0:
        return f"no {noun}{tail} was seen"
    return f"1 {noun}{tail} was seen" if n == 1 else f"{n} {noun}s{tail} were seen"


_NOT_A_RESULT = "A request is not a result: this transcript holds no exit code, report or effect."
_NOT_ABSENCE = (
    "That does not show it did not happen: hooks, subagents, scripts and your own "
    "shell are not in view."
)


def assess_passive(claim: Claim, observed: Observed) -> Assessment:
    """What a transcript alone can say about one claim. Never supported or contradicted."""
    if claim.kind in ("tests_pass", "tests_ran"):
        verdict, n, what = "insufficient", observed.test_requests, ("tool request", "naming a test runner")
    elif claim.kind in ("committed", "pushed"):
        # Command text is not read for these: which request, if any, was a commit or
        # a push is not something a string match can settle.
        verdict, n, what = "unchecked", observed.shell_requests, ("shell-tool request",)
    elif claim.kind in ("edited_file", "left_untouched"):
        verdict = "unchecked"
        n = sum(1 for p in observed.edit_request_paths if path_matches(p, claim.path))
        what = ("editing-tool request", f"for a path ending in `{claim.path}`")
    else:
        return Assessment("unchecked", claim.kind, claim.quote, "this kind of claim is not assessed")
    detail = f"{_requests(n, *what)}. {_NOT_A_RESULT if n else _NOT_ABSENCE}"
    return Assessment(verdict, claim.kind, claim.quote, detail)


@dataclass
class Result:
    """Assessments plus the denominator they were drawn from.

    Without the count, "nothing reported" is indistinguishable from "nothing was
    recognised".
    """

    assessments: list[Assessment] = field(default_factory=list)
    claims_found: int = 0
    unverifiable: list[str] = field(default_factory=list)
    language: str = "en"


# Claim patterns are English-only. A non-English message is not "clean", it is
# unchecked, and must be reported as such.
EN_STOPWORDS = {
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "to", "of", "in",
    "it", "that", "this", "for", "with", "not", "no", "i", "you", "we", "he",
    "she", "they", "have", "has", "had", "be", "been", "do", "does", "did",
    "on", "at", "by", "from", "so", "but", "if", "as", "all", "can", "will",
}
WORD = re.compile(r"[a-zA-Z']+")


def looks_english(text: str) -> bool:
    words = [w.lower() for w in WORD.findall(text)]
    if len(words) < 12:
        return True  # too short to judge; do not cry wolf
    hits = sum(1 for w in words if w in EN_STOPWORDS)
    return (hits / len(words)) >= 0.12


def review(text: str, observed: Observed) -> Result:
    res = Result(language="en" if looks_english(text) else "other")
    if not text.strip():
        return res
    if res.language != "en":
        res.unverifiable.append(
            "the final message is not English; claim patterns are English-only"
        )
        return res
    claims = extract(text)
    res.claims_found = len(claims)
    res.assessments = [assess_passive(c, observed) for c in claims]
    return res


# ---------------------------------------------------------------------------
# Transcript reading
# ---------------------------------------------------------------------------


def _iter_content_blocks(entry: Any) -> Iterable[dict]:
    """Yield content blocks from a transcript entry, tolerating schema variation."""
    if not isinstance(entry, dict):
        return
    message = entry.get("message")
    candidates = []
    if isinstance(message, dict):
        candidates.append(message.get("content"))
    candidates.append(entry.get("content"))
    for content in candidates:
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    yield block
        elif isinstance(content, str):
            yield {"type": "text", "text": content}


def _entry_role(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    role = entry.get("type") or entry.get("role") or ""
    message = entry.get("message")
    if isinstance(message, dict):
        role = message.get("role") or role
    return str(role)


def read_transcript(path: str) -> list[dict]:
    entries: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a partially flushed line; skip it
    except OSError:
        return []
    return entries


def _observe(observed: Observed, block: dict) -> None:
    name = block.get("name")
    inp = block.get("input")
    if not isinstance(name, str):
        return
    if name in SHELL_TOOLS:
        observed.shell_requests += 1
        cmd = (inp.get("command") or inp.get("cmd")) if isinstance(inp, dict) else None
        if isinstance(cmd, str) and (TEST_CMD.search(cmd) or TEST_FILE_CMD.search(cmd)):
            observed.test_requests += 1
    elif name in EDIT_TOOLS and isinstance(inp, dict):
        p = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
        if isinstance(p, str) and p.strip():
            observed.edit_request_paths.append(p.strip())


def collect(entries: list[dict]) -> tuple[str, Observed]:
    """Return (current final assistant prose or "", observations).

    Prose is current only while nothing follows it on the main chain: a later user
    entry or a later tool request means the turn it closed is over or was never
    closed, and assessing it would judge a stale message.
    """
    observed = Observed()
    current: list[str] = []

    for entry in entries:
        # A subagent's message is not the main agent's claim.
        if isinstance(entry, dict) and entry.get("isSidechain"):
            continue
        role = _entry_role(entry)
        texts: list[str] = []
        requested = False

        for block in _iter_content_blocks(entry):
            btype = block.get("type")
            if btype == "tool_use":
                requested = True
                _observe(observed, block)
            elif btype == "text" and role == "assistant":
                t = block.get("text")
                if isinstance(t, str):
                    texts.append(t)

        if role == "assistant":
            if requested:
                current = []
            elif any(t.strip() for t in texts):
                current = texts
        elif role == "user":
            current = []

    return "\n".join(current).strip(), observed


def transcript_with_settle(path: str) -> tuple[str, Observed]:
    """Read the transcript, giving the async writer a moment to flush the last message."""
    text, observed = "", Observed()
    for attempt in range(SETTLE_TRIES):
        entries = read_transcript(path)
        text, observed = collect(entries)
        if text:
            return text, observed
        if attempt < SETTLE_TRIES - 1:
            time.sleep(SETTLE_SLEEP_S)
    return text, observed


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

COVERAGE_NOTE = (
    "claim-check reads tool requests in the transcript, not their results or effects. "
    "It cannot confirm or refute a claim. The labels above describe what was checkable "
    "here, not whether the claim is true."
)


def render(result: Result) -> str:
    lines = ["", "claim-check — what the final message claims, and what this transcript can show", ""]
    for reason in result.unverifiable:
        lines.append(f"  unchecked     {reason}")
    if result.unverifiable:
        lines.append("")
    for a in result.assessments:
        lines.append(f"  {a.verdict:<13} {a.claim}")
        lines.append(f"                {a.detail}")
    lines.append("")
    lines.append(f"  note: {COVERAGE_NOTE}")
    lines.append("")
    return "\n".join(lines)


def _already_reported_limit(session_id: str) -> bool:
    """True if this session was already told about a capability limit."""
    if not session_id:
        return False
    try:
        import tempfile

        marker = os.path.join(
            tempfile.gettempdir(), f"claim-check-limit-{re.sub(r'[^A-Za-z0-9_-]', '', session_id)}"
        )
        if os.path.exists(marker):
            return True
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("1")
        return False
    except Exception:
        return False


def _log(event: dict) -> None:
    """Append a heartbeat line when CLAIM_CHECK_LOG is set. Never raises.

    A silent hook is indistinguishable from a hook that never ran, which makes
    it impossible to tell "nothing to report" from "broken install".
    """
    path = os.environ.get("CLAIM_CHECK_LOG", "").strip()
    if not path:
        return
    try:
        event = dict(event, ts=time.strftime("%Y-%m-%dT%H:%M:%S"), version=VERSION)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    except Exception:
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        _log({"event": "bad_stdin"})
        return EXIT_OK

    if not isinstance(payload, dict):
        return EXIT_OK

    if payload.get("stop_hook_active"):
        return EXIT_OK

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return EXIT_OK

    try:
        text, observed = transcript_with_settle(transcript_path)
        result = review(text, observed)
    except Exception as exc:
        _log({"event": "internal_error", "error": type(exc).__name__})
        return EXIT_OK  # never break the session

    # A capability limit is not a per-turn finding. Reporting "not English" on
    # every turn would fire constantly for a non-English user and drown the
    # thing it exists to say, so it is said once per session.
    session_id = str(payload.get("session_id") or "")
    if result.unverifiable and _already_reported_limit(session_id):
        result.unverifiable = []

    _log(
        {
            "event": "ran",
            "current_message": bool(text),
            "final_text_chars": len(text),
            "shell_requests": observed.shell_requests,
            "test_requests": observed.test_requests,
            "edit_requests": len(observed.edit_request_paths),
            "language": result.language,
            "claims_found": result.claims_found,
            "unverifiable": result.unverifiable,
            "assessments": [{"verdict": a.verdict, "kind": a.kind} for a in result.assessments],
        }
    )

    if not result.assessments and not result.unverifiable:
        return EXIT_OK

    # Bare stdout on exit 0 only surfaces in transcript view. The documented hook
    # envelope puts the report where the user actually is.
    try:
        sys.stdout.write(
            json.dumps({"continue": True, "suppressOutput": False, "systemMessage": render(result)})
        )
    except Exception:
        pass
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
