#!/usr/bin/env python3
"""claim_report — bind a claim to one named receipt, and print one assessment as text.

    python3 scripts/claim_report.py bind   --kind KIND --receipt RECEIPT.json
    python3 scripts/claim_report.py report --claim CLAIM.json --receipt RECEIPT.json [--receipt ...]
                                           [--max-bytes N]

Both are opt-in and both sit on top of claim_evidence.assess(), which they do not
change. Neither reads a sentence, a transcript or a directory: the kind and every
receipt are named by whoever runs the command.

bind copies the scope out of the receipt it is given: the run id, and the digests of
the recorded selection and of the declared files, recomputed from the receipt's
content. It is a way to say "this run, this selection" without computing digests by
hand. A claim bound this way always matches that receipt's identity, so binding adds
no evidence, and it works just as well for a run that failed or never finished. What
the run showed is decided only by `report` (or `claim_evidence.py assess`).

report runs the assessment once and prints it in a fixed layout. Same input, same
bytes. The verdict, the limits and the last line are always printed in full; strings
that come from a receipt or a claim are escaped to ASCII and cut at a fixed length, and
everything that is cut is labelled.

Exit code of `report`: 0 supported, 1 contradicted, 2 insufficient, 3 unchecked,
64 unusable arguments or claim. Exit code of `bind`: 0, or 64 when the receipt gives
no usable identity.

Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claim_evidence as ce  # noqa: E402

DEFAULT_MAX_BYTES = 4000
TRUNCATED = "[truncated]"
DISCLAIMER = "Binding does not add evidence or authenticate the receipt."

# Fixed cuts. With these the full form has a known largest size, below the default budget.
MAX_STRING_CHARS = 120
MAX_REASONS = 16
MAX_EVIDENCE = 8
MAX_COUNT = 10 ** 15

_VERDICTS = ("supported", "contradicted", "insufficient", "unchecked")
_REASON_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCOPE_KEYS = {"run_id", "cwd", "selection_digest", "selected_count", "declared_files_digest", "declared_file_count"}


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


class BindError(Exception):
    """The receipt or the kind gives no claim to bind. The message names why."""


def bind_claim(kind: Any, receipt: Any) -> dict:
    """A claim of `kind` scoped to the run, selection and declared files of `receipt`.

    Checks identity only: that the receipt names one run, records exactly one
    well-formed selection and well-formed declared files, and that the digests it
    stores equal the ones recomputed here. It does not look at exit codes, phases or
    whether the session finished: a failed or incomplete run can be bound, and the
    assessment then says what that run showed.
    """
    if type(kind) is not str or kind not in ce.KINDS:
        raise BindError("unknown claim kind")
    if type(receipt) is not dict or not ce.json_value_ok(receipt):
        raise BindError("the receipt is not a JSON object")
    if receipt.get("schema") != ce.SCHEMA:
        raise BindError("unsupported receipt schema")
    run_id = receipt.get("run_id")
    if type(run_id) is not str or not run_id:
        raise BindError("the receipt names no run")

    report = receipt.get("report")
    events = report.get("events") if type(report) is dict else None
    if type(events) is not list:
        raise BindError("the receipt holds no report events")
    selections = [e for e in events if type(e) is dict and e.get("event") == "selected"]
    # The same structural rule the assessor applies to a selection event.
    if len(selections) != 1 or not ce._event_ok(selections[0]):
        raise BindError("the receipt does not record exactly one well-formed selection")
    nodeids = selections[0]["nodeids"]
    if len(set(nodeids)) != len(nodeids):
        raise BindError("the recorded selection names an item twice")

    declared = receipt.get("declared_files")
    pre = declared.get("pre") if type(declared) is dict else None
    if not ce._declared_entries_ok(pre):
        raise BindError("the declared files are missing or malformed")

    selection_digest = ce.digest_selection(nodeids)
    declared_digest = ce.digest_declared(pre)
    # Recomputed, then compared: a receipt whose stored digests contradict its own
    # content has no single identity to bind to.
    if receipt.get("selection_digest") != selection_digest or receipt.get("declared_files_digest") != declared_digest:
        raise BindError("the stored digests contradict the recorded selection or declared files")

    return {
        "kind": kind,
        "run_id": run_id,
        "selection_digest": selection_digest,
        "declared_files_digest": declared_digest,
        # Says where the scope came from. It is a note, not evidence: nothing reads it.
        "scope_source": "receipt",
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def _tail() -> str:
    return "Limits:\n" + "".join(f"- {limit}\n" for limit in ce.LIMITS) + DISCLAIMER + "\n"


def _line(label: str, value: Any, cut: bool) -> str:
    return f"{label}{' (truncated)' if cut else ''}: {_dumps(value)}\n"


def _compact(verdict: str) -> str:
    """Every variable field replaced by the marker. The caveats are not variable."""
    return (
        f"Verdict: {verdict}\n"
        + _line("Claim kind", TRUNCATED, True)
        + _line("Scope", TRUNCATED, True)
        + _line("Reasons", [TRUNCATED], True)
        + _line("Evidence", [TRUNCATED], True)
        + _tail()
    )


# The smallest budget that holds the verdict, every limit and the last line.
MIN_MAX_BYTES = max(len(_compact(v).encode("utf-8")) for v in _VERDICTS)


def _cut_string(value: str) -> tuple[str, bool]:
    if len(value) <= MAX_STRING_CHARS:
        return value, False
    return value[:MAX_STRING_CHARS] + TRUNCATED, True


def _checked(assessment: Any) -> dict:
    """Refuse anything that is not what claim_evidence.assess() returns."""
    problem = None
    if type(assessment) is not dict or set(assessment) != {"verdict", "claim_kind", "reasons", "scope", "evidence", "limits"}:
        problem = "members"
    elif type(assessment["verdict"]) is not str or assessment["verdict"] not in _VERDICTS:
        problem = "verdict"
    elif assessment["limits"] != ce.LIMITS:
        problem = "limits"
    elif type(assessment["reasons"]) is not list or not all(
        type(r) is str and _REASON_CODE.match(r) for r in assessment["reasons"]
    ):
        problem = "reasons"
    elif type(assessment["evidence"]) is not list or not all(
        type(e) is dict and set(e) == {"receipt_sha256"} and type(e["receipt_sha256"]) is str
        and _DIGEST.match(e["receipt_sha256"]) for e in assessment["evidence"]
    ):
        problem = "evidence"
    elif not ce.json_value_ok(assessment["claim_kind"]):
        problem = "claim_kind"
    else:
        scope = assessment["scope"]
        if type(scope) is not dict or (scope and (
            set(scope) != _SCOPE_KEYS
            or type(scope["run_id"]) is not str or type(scope["cwd"]) is not str
            or not all(type(scope[k]) is str and _DIGEST.match(scope[k]) for k in ("selection_digest", "declared_files_digest"))
            or not all(type(scope[k]) is int and scope[k] >= 0 for k in ("selected_count", "declared_file_count"))
        )):
            problem = "scope"
    if problem:
        raise ValueError(f"not an assessment as claim_evidence.assess() returns it ({problem})")
    return assessment


def render_report(assessment: Any, *, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """The assessment as text, in at most `max_bytes` bytes of UTF-8 (in fact ASCII).

    Pure: reads no file and no clock. Two forms. The full one applies the fixed cuts
    above and labels each field it cut. If that does not fit the budget, the compact
    one replaces every variable field by the marker. Both carry the verdict, every
    limit and the last line unchanged; a budget too small for that is refused.
    """
    if type(max_bytes) is not int or max_bytes < MIN_MAX_BYTES:
        raise ValueError(f"max_bytes must be an integer of at least {MIN_MAX_BYTES}")
    a = _checked(assessment)

    kind = a["claim_kind"]
    if kind is None:
        kind_shown, kind_cut = None, False
    elif type(kind) is str:
        kind_shown, kind_cut = _cut_string(kind)
    else:
        kind_shown, kind_cut = TRUNCATED, True  # a nested value from the claim file is not printed

    scope_shown, scope_cut = {}, False
    for key, value in a["scope"].items():
        if type(value) is str:
            value, cut = _cut_string(value)
        else:
            value, cut = (TRUNCATED, True) if value > MAX_COUNT else (value, False)
        scope_shown[key] = value
        scope_cut = scope_cut or cut

    reasons = a["reasons"]  # the assessor's order is kept
    reasons_cut = len(reasons) > MAX_REASONS
    reasons_shown = reasons[:MAX_REASONS] + ([TRUNCATED] if reasons_cut else [])

    evidence = sorted(e["receipt_sha256"] for e in a["evidence"])
    evidence_cut = len(evidence) > MAX_EVIDENCE
    evidence_shown = evidence[:MAX_EVIDENCE] + ([TRUNCATED] if evidence_cut else [])

    full = (
        f"Verdict: {a['verdict']}\n"
        + _line("Claim kind", kind_shown, kind_cut)
        + _line("Scope", scope_shown, scope_cut)
        + _line("Reasons", reasons_shown, reasons_cut)
        + _line("Evidence", evidence_shown, evidence_cut)
        + _tail()
    )
    return full if len(full.encode("utf-8")) <= max_bytes else _compact(a["verdict"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claim_report.py", description="Bind a claim to a receipt; print an assessment.",
                                     allow_abbrev=False)
    sub = parser.add_subparsers(dest="command")
    b = sub.add_parser("bind", allow_abbrev=False, help="print a claim scoped to one receipt; says nothing about the verdict")
    b.add_argument("--kind", required=True)
    b.add_argument("--receipt", required=True, action="append")
    r = sub.add_parser("report", allow_abbrev=False, help="assess once and print the result; the exit code is the verdict")
    r.add_argument("--claim", required=True)
    r.add_argument("--receipt", action="append", default=[])
    r.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return ce.EXIT_USAGE

    if args.command == "bind":
        if len(args.receipt) != 1:
            sys.stderr.write("bind takes exactly one --receipt\n")
            return ce.EXIT_USAGE
        try:
            claim = bind_claim(args.kind, ce._load_json(args.receipt[0]))
        except BindError as exc:
            sys.stderr.write(f"cannot bind: {exc}\n")
            return ce.EXIT_USAGE
        sys.stdout.write(json.dumps(claim, indent=2, sort_keys=True) + "\n")
        return 0

    if args.command == "report":
        if args.max_bytes < MIN_MAX_BYTES:
            sys.stderr.write(f"--max-bytes must be at least {MIN_MAX_BYTES}: the limits are never cut\n")
            return ce.EXIT_USAGE
        claim = ce._load_json(args.claim)
        if claim is None:
            sys.stderr.write("claim file is unreadable or not JSON\n")
            return ce.EXIT_USAGE
        # An unreadable receipt stays in the list as None, as in claim_evidence.py.
        outcome = ce.assess(claim, [ce._load_json(path) for path in args.receipt])
        sys.stdout.write(render_report(outcome, max_bytes=args.max_bytes))
        return ce.EXIT_CODES[outcome["verdict"]]

    parser.print_usage(sys.stderr)
    return ce.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
