#!/usr/bin/env python3
"""claim-check — compare what the agent said it did against what the session observed.

Runs as a Claude Code `Stop` hook. Reads the session transcript, extracts the final
assistant message (the claims) and the tool calls (the observations), and reports only
the mismatches.

Three outcomes, never two:
  contradicted  — the claim is inconsistent with what was observed
  unobservable  — hooks cannot see this; we say so instead of implying it is fine
  confirmed     — matched; printed only in --verbose

Design rules (deliberate, do not "fix" without reading README §Limits):
  * Never blocks by default. Set CLAIM_CHECK_ENFORCE=1 to hand findings back to the agent.
  * Never crashes the session. Any internal error exits 0 silently.
  * Silent when there is nothing to report.
  * Conservative matching: a missed claim is much cheaper than a false accusation.

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

# Exit codes per the Claude Code hook contract:
#   0 = success (stdout surfaces in transcript mode)
#   2 = blocking; stderr is fed back to the agent
EXIT_OK = 0
EXIT_BLOCK = 2

# How long to wait for the transcript to flush the final assistant message.
# The transcript is written asynchronously and can lag the in-memory conversation.
SETTLE_TRIES = 3
SETTLE_SLEEP_S = 0.4

# ---------------------------------------------------------------------------
# Observation model
# ---------------------------------------------------------------------------


@dataclass
class Observed:
    """What the session actually did, as far as hooks can see."""

    commands: list[str] = field(default_factory=list)
    edited_paths: set[str] = field(default_factory=set)
    # True when at least one tool call of a kind that can edit files was seen.
    saw_edit_tool: bool = False

    def ran_matching(self, pattern: re.Pattern[str]) -> list[str]:
        return [c for c in self.commands if pattern.search(c)]


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
# Found by installing the hook live: this project runs its own tests that way and
# the runner-name list above did not recognise it. The interpreter is required so
# that `cat tests/test_foo.py` is not mistaken for running them, and the path token
# may not contain whitespace so a heredoc body cannot be crossed.
TEST_FILE_CMD = re.compile(
    r"""\b(python[0-9.]*|node|deno|bun|ruby|perl|php)\s+
        (?:-\w+\s+)*
        [^\s;|&<>]*test[^\s;|&<>]*\.(py|js|mjs|cjs|ts|rb|pl|php)\b""",
    re.VERBOSE | re.IGNORECASE,
)

GIT_COMMIT_CMD = re.compile(r"\bgit\s+(-\S+\s+)*commit\b", re.IGNORECASE)
GIT_PUSH_CMD = re.compile(r"\bgit\s+(-\S+\s+)*push\b", re.IGNORECASE)

EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit", "str_replace_editor", "apply_patch"}
SHELL_TOOLS = {"Bash", "BashOutput", "shell", "run_command"}

# ---------------------------------------------------------------------------
# Claim detection
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
#   "…closed on head `abc`, pushed to" -> comma   (missed in a real session)
# Trade-off, recorded deliberately: a comma also admits "Aegis reviewed it,
# pushed a fix". Real transcripts showed the false NEGATIVE happening and the
# false positive not, so the comma stays until data says otherwise.
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
# "I haven't committed" must never be reported as a missing commit.
NEGATED = re.compile(
    r"""\b(
        have\s*n[o']?t | has\s*n[o']?t | had\s*n[o']?t
      | did\s*n[o']?t | do\s*n[o']?t | does\s*n[o']?t
      | was\s*n[o']?t | were\s*n[o']?t | is\s*n[o']?t | are\s*n[o']?t
      | never | not\s+yet | no\s+longer | without | failed\s+to | unable\s+to
    )\b""",
    re.VERBOSE | re.IGNORECASE,
)

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

# Phrases that make the whole message an explicit negative claim about files.
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
# nothing about whether they ran. Dropping these is the main defence against
# false accusations, which are far more expensive than missed claims.
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
# blocks in the final message, and every one of them produced a false positive
# before this existed: "| E2E test … | Pass" read as a passing suite, and
# "[updated](https://github.com/…)" read as a file edit.
FENCED_CODE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
TABLE_ROW = re.compile(r"^[ \t]*\|.*$", re.MULTILINE)
MD_LINK = re.compile(r"\[([^\]\n]*)\]\([^)\n]*\)")
BARE_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)

# An inline-code span containing whitespace is a quoted phrase, not a path.
# Found on real data: a message discussing this very plugin wrote
# `"Committed and pushed."` as an example and tripped its own push check.
# Single-token spans are kept, because that is where real paths live.
INLINE_CODE_PROSE = re.compile(r"`[^`\n]*\s[^`\n]*`")


def strip_markup(text: str) -> str:
    """Remove structure that is not a spoken claim."""
    text = FENCED_CODE.sub(" ", text)
    text = TABLE_ROW.sub(" ", text)
    text = MD_LINK.sub(r"\1", text)  # keep the link text, drop the target
    text = BARE_URL.sub(" ", text)
    text = INLINE_CODE_PROSE.sub(" ", text)
    return text


def assertive_sentences(text: str, drop_negated: bool = True) -> list[str]:
    """Sentences that assert completed work by the agent.

    Hedged, conditional and instructional sentences are always dropped. Negated
    ones are dropped for POSITIVE claims ("I haven't committed" must never be
    reported as a missing commit) but kept for the explicitly negative claim
    ("I did not touch X"), which is itself the thing being checked.
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


def assertive_text(text: str) -> str:
    """Back-compat view of :func:`assertive_sentences`."""
    return "\n".join(assertive_sentences(text))


@dataclass
class Finding:
    verdict: str  # "contradicted" | "unobservable" | "confirmed"
    claim: str
    detail: str


@dataclass
class Result:
    """Findings plus the denominator they were drawn from.

    Without the counts, "no findings" is indistinguishable from "nothing was
    checkable" — which is the exact confusion this tool exists to expose, and
    which it committed itself for nine live runs before this existed.
    """

    findings: list[Finding] = field(default_factory=list)
    claims_found: int = 0
    unverifiable: list[str] = field(default_factory=list)
    language: str = "en"

    @property
    def checkable(self) -> bool:
        return self.claims_found > 0


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


def collect(entries: list[dict]) -> tuple[str, Observed]:
    """Return (final assistant text, observations)."""
    observed = Observed()
    final_text_parts: list[str] = []

    for entry in entries:
        # A subagent's message is not the main agent's claim.
        if isinstance(entry, dict) and entry.get("isSidechain"):
            continue
        role = _entry_role(entry)
        is_assistant = role == "assistant"
        text_parts: list[str] = []

        for block in _iter_content_blocks(entry):
            btype = block.get("type")
            if btype == "tool_use":
                name = str(block.get("name") or "")
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                if name in SHELL_TOOLS:
                    cmd = inp.get("command") or inp.get("cmd") or ""
                    if isinstance(cmd, str) and cmd.strip():
                        observed.commands.append(cmd)
                elif name in EDIT_TOOLS:
                    observed.saw_edit_tool = True
                    p = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                    if isinstance(p, str) and p.strip():
                        observed.edited_paths.add(p.strip())
            elif btype == "text" and is_assistant:
                t = block.get("text")
                if isinstance(t, str):
                    text_parts.append(t)

        if is_assistant and text_parts:
            # Keep only the latest assistant prose; earlier turns are not the claim.
            final_text_parts = text_parts

    return "\n".join(final_text_parts).strip(), observed


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
# Checks
# ---------------------------------------------------------------------------


def _basename(p: str) -> str:
    return p.rstrip("/").split("/")[-1]


def review(text: str, observed: Observed) -> Result:
    """Full result: findings plus the denominator."""
    res = Result(language="en" if looks_english(text) else "other")
    if not text.strip():
        return res
    if res.language != "en":
        res.unverifiable.append(
            "the final message is not English; claim patterns are English-only"
        )
        return res
    res.findings = check(text, observed)
    res.claims_found = count_claims(text)
    return res


def count_claims(text: str) -> int:
    """How many claim-shaped statements were recognised at all."""
    n = 0
    sentences = assertive_sentences(text)
    for s in sentences:
        if not FAILURE_CONTEXT.search(s) and (
            CLAIM_TESTS_PASS.search(s) or CLAIM_TESTS_RUN.search(s)
        ):
            n += 1
        if CLAIM_COMMITTED.search(s) or CLAIM_PUSHED.search(s):
            n += 1
        n += sum(
            1 for m in CLAIM_EDITED_FILE.finditer(s) if looks_like_file(m.group("path"))
        )
    n += len(list(CLAIM_UNTOUCHED.finditer("\n".join(assertive_sentences(text, False)))))
    return n


def check(text: str, observed: Observed) -> list[Finding]:
    findings: list[Finding] = []
    raw_text = text
    sentences = assertive_sentences(text)
    if not sentences and not text.strip():
        return findings

    # 1 & 2 — tests. A sentence that also reports a failure is not a pass claim.
    for s in sentences:
        if FAILURE_CONTEXT.search(s):
            continue
        m = CLAIM_TESTS_PASS.search(s) or CLAIM_TESTS_RUN.search(s)
        if m:
            if not (observed.ran_matching(TEST_CMD) or observed.ran_matching(TEST_FILE_CMD)):
                findings.append(
                    Finding(
                        "contradicted",
                        m.group(0).strip(),
                        "no test command was observed in this session",
                    )
                )
            break  # one verdict per session, not one per phrasing

    # 3 — commit
    if any(CLAIM_COMMITTED.search(s) for s in sentences) and not observed.ran_matching(
        GIT_COMMIT_CMD
    ):
        findings.append(
            Finding("contradicted", "claimed a commit", "no `git commit` was observed")
        )

    # 4 — push
    if any(CLAIM_PUSHED.search(s) for s in sentences) and not observed.ran_matching(GIT_PUSH_CMD):
        findings.append(Finding("contradicted", "claimed a push", "no `git push` was observed"))

    # 5 — named file edits
    text = "\n".join(sentences)
    edited_basenames = {_basename(p) for p in observed.edited_paths}
    for m in CLAIM_EDITED_FILE.finditer(text):
        path = m.group("path")
        if not looks_like_file(path):
            continue
        if _basename(path) in edited_basenames:
            continue
        # A file can be written by a shell redirect or a subprocess; hooks see the
        # command string, not its effects. Only speak where we can be sure.
        if any(path in c or _basename(path) in c for c in observed.commands):
            continue
        verdict = "contradicted" if observed.saw_edit_tool else "unobservable"
        detail = (
            f"no edit to `{path}` was observed"
            if verdict == "contradicted"
            else f"no file-editing tool ran; an edit to `{path}` cannot be confirmed here"
        )
        findings.append(Finding(verdict, m.group(0).strip()[:80], detail))

    # 6 — explicit "did not touch X". Negation is the claim here, so this pass
    # reads the sentences that step 1-5 deliberately discarded.
    negative_text = "\n".join(assertive_sentences(raw_text, drop_negated=False))
    for m in CLAIM_UNTOUCHED.finditer(negative_text):
        path = m.group("path")
        if _basename(path) in edited_basenames:
            findings.append(
                Finding(
                    "contradicted",
                    m.group(0).strip()[:80],
                    f"`{path}` was edited in this session",
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

COVERAGE_NOTE = (
    "claim-check sees tool calls, not their effects: a shell redirect or a subprocess "
    "can change files with no trace here. Absence of an observation is not proof of absence."
)


def render(result: Result, enforce: bool) -> str:
    findings = result.findings
    contradicted = [f for f in findings if f.verdict == "contradicted"]
    unobservable = [f for f in findings if f.verdict == "unobservable"]

    lines = ["", "claim-check — what was said vs what this session observed", ""]
    for reason in result.unverifiable:
        lines.append(f"  unchecked     {reason}")
    if result.unverifiable:
        lines.append("")
    for f in contradicted:
        lines.append(f"  contradicted  {f.claim}")
        lines.append(f"                {f.detail}")
    for f in unobservable:
        lines.append(f"  unobservable  {f.claim}")
        lines.append(f"                {f.detail}")
    lines.append("")
    lines.append(f"  note: {COVERAGE_NOTE}")
    if enforce:
        lines.append("")
        lines.append("  Verify the claim or correct the statement before finishing.")
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

    # Do not re-fire on a stop that we ourselves caused.
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

    findings = result.findings
    _log(
        {
            "event": "ran",
            "final_text_chars": len(text),
            "commands": len(observed.commands),
            "edited_paths": len(observed.edited_paths),
            "language": result.language,
            "claims_found": result.claims_found,
            "unverifiable": result.unverifiable,
            "findings": [{"verdict": f.verdict, "detail": f.detail} for f in findings],
        }
    )

    if not findings and not result.unverifiable:
        return EXIT_OK

    enforce = os.environ.get("CLAIM_CHECK_ENFORCE", "").strip() in {"1", "true", "yes"}
    report = render(result, enforce)

    if enforce and any(f.verdict == "contradicted" for f in findings):
        sys.stderr.write(report)
        return EXIT_BLOCK

    # Report mode: bare stdout on exit 0 only surfaces in transcript view, so a
    # finding could be produced 500 times and never seen. The documented hook
    # envelope puts it where the user actually is.
    sys.stdout.write(
        json.dumps({"continue": True, "suppressOutput": False, "systemMessage": report})
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
