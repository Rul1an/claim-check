#!/usr/bin/env python3
"""claim_evidence — assess one typed claim against pytest receipts.

A typed claim names its scope: one run, one selection, one declared-file identity.
A receipt is what pytest_evidence.py recorded about one run. `assess` compares the
two and returns one of four verdicts:

  supported     the receipt is complete and consistent, binds to the claim's scope,
                and records every selected item passing setup, call and teardown
  contradicted  the same binding, and the receipt records a selected item failing
  insufficient  anything that keeps the receipt from deciding; the reasons are kept
  unchecked     a claim kind this module does not assess

What a verdict does not mean:
  * A receipt is not authenticated. Whoever can write it can write a consistent
    false one. `supported` means "consistent with a receipt", not "it happened".
  * Declared files are a comparison of snapshots. They are not the bytes pytest
    loaded, not its dependencies, and not an atomic snapshot.
  * The verdict holds for one run and one selection. It says nothing about tests
    outside the selection, and it is never attached to prose such as "all tests
    pass" by this code.

Nothing here runs a test, reads a transcript or touches the network.
Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
from typing import Any, Callable

SCHEMA = "claim-check.pytest-receipt.v1"
KIND_RECORDED = "recorded_selection_passed"
KIND_CURRENT = "recorded_selection_passed_current_files"
KINDS = (KIND_RECORDED, KIND_CURRENT)

MAX_DECLARED_FILE_BYTES = 64 * 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
# A receipt nests five levels (receipt, report, events, one event, its node ids).
# Anything deeper than this is not a receipt, a claim or a report line.
MAX_JSON_DEPTH = 16

EXIT_CODES = {"supported": 0, "contradicted": 1, "insufficient": 2, "unchecked": 3}
EXIT_USAGE = 64

LIMITS = [
    "A receipt is not authenticated: whoever can write it can write a consistent false one.",
    "Declared files are a comparison of snapshots taken before and after the run. They are "
    "not the bytes pytest loaded, not its dependencies, and not an atomic snapshot.",
    "The verdict holds for this run and this selection only.",
]

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PHASES = ("setup", "call", "teardown")
_OUTCOMES = ("passed", "failed", "skipped")


# ---------------------------------------------------------------------------
# Strict types. `True == 1` in Python, so every check is on the exact type.
# ---------------------------------------------------------------------------


def _is_int(x: Any) -> bool:
    return type(x) is int


def _is_bool(x: Any) -> bool:
    return type(x) is bool


def _is_str(x: Any) -> bool:
    return type(x) is str


def _is_str_list(x: Any) -> bool:
    return type(x) is list and all(type(i) is str for i in x)


def _is_number(x: Any) -> bool:
    return type(x) in (int, float) and x == x and x not in (float("inf"), float("-inf"))


def depth_ok(value: Any, limit: int = MAX_JSON_DEPTH) -> bool:
    """True when no list or dict in `value` sits more than `limit` levels deep.

    Walks with its own stack. How deep the JSON decoder or encoder can recurse
    differs between Python versions, so the limit is stated here and not left to them.
    """
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if type(item) in (list, dict):
            if depth > limit:
                return False
            children = item.values() if type(item) is dict else item
            stack.extend((child, depth + 1) for child in children)
    return True


def loads_strict(text: str) -> Any:
    """json.loads, refusing input that would hide a conflict or is not JSON.

    A repeated key is two statements about one member; json.loads keeps the last
    and drops the other without a trace. NaN and infinities are not JSON. Nesting
    beyond MAX_JSON_DEPTH is refused whether or not the decoder got through it.
    Raises ValueError, or RecursionError where the decoder did not get through.
    """

    def pairs(items: list) -> dict:
        out: dict = {}
        for key, value in items:
            if key in out:
                raise ValueError("repeated key")
            out[key] = value
        return out

    def constant(name: str) -> Any:
        raise ValueError("not a JSON number")

    def number(literal: str) -> float:
        value = float(literal)
        if not _is_number(value):
            raise ValueError("not a finite number")
        return value

    value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    if not depth_ok(value):
        raise ValueError("nested too deeply")
    return value


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_selection(nodeids: list[str]) -> str:
    """Identity of a selection: the set of node ids, order ignored."""
    return _sha256(_canonical(sorted(nodeids)))


def digest_declared(entries: list[dict]) -> str:
    """Identity of a declared-file snapshot: each path with its content hash and size."""
    return _sha256(_canonical(sorted([e["path"], e["sha256"], e["size"]] for e in entries)))


def digest_receipt(receipt: Any) -> str:
    return _sha256(_canonical(receipt))


# ---------------------------------------------------------------------------
# Declared files
# ---------------------------------------------------------------------------


class DeclaredFileError(Exception):
    """A declared file cannot be snapshotted safely. The message names why."""


def declared_path_ok(path: Any) -> bool:
    """Structural only, no filesystem: a relative POSIX path that stays under its base."""
    return (
        _is_str(path) and path != "" and "\x00" not in path and "\\" not in path
        and not path.startswith("/") and all(part not in ("", ".", "..") for part in path.split("/"))
    )


def cwd_ok(cwd: Any) -> bool:
    """Structural only, no filesystem: an absolute POSIX path already in normal form."""
    return (
        _is_str(cwd) and cwd.startswith("/") and not cwd.startswith("//")
        and "\x00" not in cwd and posixpath.normpath(cwd) == cwd
    )


def snapshot_declared(cwd: str, paths: list[str]) -> list[dict]:
    """Read each declared file under `cwd` and return path, sha256 and size.

    Refuses anything whose identity is ambiguous: an absolute path, a `..`, a
    duplicate, a symlink at any component below `cwd`, or a non-regular file.
    """
    if not cwd_ok(cwd) or os.path.realpath(cwd) != cwd:
        raise DeclaredFileError("cwd is not an absolute, normal path free of symlinks")
    seen: set[str] = set()
    out: list[dict] = []
    for raw in paths:
        if not declared_path_ok(raw):
            raise DeclaredFileError("declared path is not a relative POSIX path that stays under cwd")
        parts = raw.split("/")
        if raw in seen:
            raise DeclaredFileError("declared path is listed twice")
        seen.add(raw)
        current = cwd
        for i, part in enumerate(parts):
            current = os.path.join(current, part)
            try:
                st = os.lstat(current)
            except (OSError, ValueError):
                raise DeclaredFileError("declared file is missing") from None
            if stat.S_ISLNK(st.st_mode):
                raise DeclaredFileError("declared path crosses a symlink")
            last = i == len(parts) - 1
            if last and not stat.S_ISREG(st.st_mode):
                raise DeclaredFileError("declared path is not a regular file")
            if not last and not stat.S_ISDIR(st.st_mode):
                raise DeclaredFileError("declared path crosses a non-directory")
        h = hashlib.sha256()
        size = 0
        try:
            fd = os.open(current, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_DECLARED_FILE_BYTES:
                        raise DeclaredFileError("declared file is larger than the limit")
                    h.update(chunk)
        except OSError:
            raise DeclaredFileError("declared file cannot be read") from None
        out.append({"path": raw, "sha256": h.hexdigest(), "size": size})
    return out


def _declared_entries_ok(entries: Any) -> bool:
    if type(entries) is not list:
        return False
    paths = []
    for e in entries:
        if type(e) is not dict or set(e) != {"path", "sha256", "size"}:
            return False
        if not declared_path_ok(e["path"]) or not _is_str(e["sha256"]) or not _HEX64.match(e["sha256"]):
            return False
        if not _is_int(e["size"]) or e["size"] < 0:
            return False
        paths.append(e["path"])
    return len(paths) == len(set(paths))


# ---------------------------------------------------------------------------
# Report events
# ---------------------------------------------------------------------------


def _event_ok(e: dict) -> bool:
    name = e.get("event")
    if name == "session_start":
        return (
            set(e) == {"event", "run_id", "pytest_version", "python_version", "rootdir", "inifile", "invocation_args"}
            and all(_is_str(e[k]) for k in ("run_id", "pytest_version", "python_version", "rootdir"))
            and (e["inifile"] is None or _is_str(e["inifile"]))
            and _is_str_list(e["invocation_args"])
        )
    if name == "collect_error":
        return set(e) == {"event", "nodeid"} and _is_str(e["nodeid"])
    if name in ("deselected", "selected"):
        return set(e) == {"event", "nodeids"} and _is_str_list(e["nodeids"])
    if name == "phase":
        return (
            set(e) == {"event", "nodeid", "when", "outcome", "xfail"}
            and _is_str(e["nodeid"])
            and e["when"] in _PHASES and _is_str(e["when"])
            and e["outcome"] in _OUTCOMES and _is_str(e["outcome"])
            and _is_bool(e["xfail"])
        )
    if name == "session_finish":
        return set(e) == {"event", "exitstatus"} and _is_int(e["exitstatus"])
    return False


_KNOWN_EVENTS = ("session_start", "collect_error", "deselected", "selected", "phase", "session_finish")


def _read_events(events: Any) -> tuple[dict, list[str]]:
    """Validate the event list. Returns (view, reasons); any reason means not complete."""
    reasons: list[str] = []
    view: dict = {"run_id": None, "selected": [], "phases": {}, "collect_errors": 0, "exitstatus": None}
    if type(events) is not list:
        return view, ["malformed_report"]
    names = []
    for e in events:
        if type(e) is not dict or not _is_str(e.get("event")) or e.get("event") not in _KNOWN_EVENTS:
            reasons.append("unknown_report_event")
            names.append(None)
            continue
        if not _event_ok(e):
            reasons.append("malformed_report_event")
            names.append(None)
            continue
        names.append(e["event"])
    if reasons:
        return view, _unique(reasons)

    for name, missing in (("session_start", "report_not_started"), ("selected", "selection_not_recorded"),
                          ("session_finish", "report_not_finished")):
        n = names.count(name)
        if n == 0:
            reasons.append(missing)
        elif n > 1:
            reasons.append("duplicate_report_event")
    if reasons:
        return view, _unique(reasons)
    # pytest collects before it runs anything, so the selection precedes every phase.
    first_phase = names.index("phase") if "phase" in names else len(names)
    if names[0] != "session_start" or names[-1] != "session_finish" or names.index("selected") > first_phase:
        return view, ["report_out_of_order"]

    for e in events:
        name = e["event"]
        if name == "session_start":
            view["run_id"] = e["run_id"]
        elif name == "selected":
            view["selected"] = list(e["nodeids"])
        elif name == "collect_error":
            view["collect_errors"] += 1
        elif name == "session_finish":
            view["exitstatus"] = e["exitstatus"]
    selected = view["selected"]
    if len(set(selected)) != len(selected):
        reasons.append("duplicate_selected_item")
    chosen = set(selected)
    for e in events:
        if e["event"] != "phase":
            continue
        if e["nodeid"] not in chosen:
            reasons.append("phase_for_unselected_item")
            continue
        phases = view["phases"].setdefault(e["nodeid"], {})
        if e["when"] in phases:
            reasons.append("repeated_phase")
            continue
        # One item runs setup, then call only after a setup that passed, then teardown.
        position = _PHASES.index(e["when"])
        if (
            any(_PHASES.index(w) > position for w in phases)
            or (position > 0 and "setup" not in phases)
            or (e["when"] == "call" and phases["setup"][0] != "passed")
        ):
            reasons.append("phase_out_of_order")
        phases[e["when"]] = (e["outcome"], e["xfail"])
    return view, _unique(reasons)


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------


def _assess_receipt(claim: dict, receipt: dict, check_current: Callable[[str, list[str]], list[dict]]) -> tuple[str, list[str], dict]:
    scope: dict = {}
    if receipt.get("schema") != SCHEMA or not _is_str(receipt.get("schema")):
        return "insufficient", ["unsupported_schema"], scope

    process, report, declared = receipt.get("process"), receipt.get("report"), receipt.get("declared_files")
    argv = receipt.get("argv")
    well_typed = (
        cwd_ok(receipt.get("cwd"))
        # The runner starts exactly this: an interpreter, then pytest with the capture plugin.
        and _is_str_list(argv) and len(argv) >= 5 and argv[0] != ""
        and argv[1:5] == ["-m", "pytest", "-p", "pytest_capture"]
        and _is_str_list(receipt.get("env_names_present"))
        and type(process) is dict and type(report) is dict and type(declared) is dict
        and _is_bool(process.get("completed")) and _is_bool(process.get("timed_out"))
        and (process.get("exit_code") is None or _is_int(process.get("exit_code")))
        and (process.get("signal") is None or _is_int(process.get("signal")))
        and all(_is_number(process.get(k)) for k in ("started_at", "ended_at", "timeout_seconds"))
        and _is_bool(report.get("present")) and _is_bool(report.get("truncated"))
        and _is_int(report.get("malformed_lines"))
        and _declared_entries_ok(declared.get("pre")) and _declared_entries_ok(declared.get("post"))
        and _is_str(receipt.get("selection_digest")) and _is_str(receipt.get("declared_files_digest"))
    )
    if not well_typed:
        return "insufficient", ["malformed_receipt"], scope

    reasons: list[str] = []
    if not report["present"]:
        reasons.append("report_missing")
    if report["truncated"]:
        reasons.append("report_truncated")
    if report["malformed_lines"] != 0:
        reasons.append("report_malformed_lines")
    view, event_reasons = _read_events(report.get("events"))
    if report["present"]:
        reasons.extend(event_reasons)
    if process["timed_out"]:
        reasons.append("timed_out")
    if not process["completed"]:
        reasons.append("process_not_completed")
    if process["signal"] is not None:
        reasons.append("killed_by_signal")
    if not declared["pre"]:
        reasons.append("declared_scope_empty")
    if declared["pre"] != declared["post"]:
        reasons.append("declared_files_changed_during_run")
    if reasons:
        return "insufficient", _unique(reasons), scope

    selection = digest_selection(view["selected"])
    declared_digest = digest_declared(declared["pre"])
    scope = {
        "run_id": receipt["run_id"],
        "cwd": receipt["cwd"],
        "selection_digest": selection,
        "selected_count": len(view["selected"]),
        "declared_files_digest": declared_digest,
        "declared_file_count": len(declared["pre"]),
    }
    if receipt["selection_digest"] != selection or receipt["declared_files_digest"] != declared_digest:
        reasons.append("inconsistent_receipt")
    if view["run_id"] != receipt["run_id"]:
        reasons.append("run_id_mismatch")
    if claim["selection_digest"] != selection:
        reasons.append("selection_mismatch")
    if claim["declared_files_digest"] != declared_digest:
        reasons.append("declared_files_mismatch")
    if reasons:
        return "insufficient", reasons, scope

    exit_code = process["exit_code"]
    if view["exitstatus"] != exit_code:
        reasons.append("exit_status_mismatch")
    if view["collect_errors"]:
        reasons.append("collection_error")
    if reasons:
        return "insufficient", reasons, scope

    failed = [n for n in view["selected"] if any(o == "failed" for o, _ in view["phases"].get(n, {}).values())]
    if exit_code == 1:
        if not failed:
            return "insufficient", ["failure_exit_without_failed_item"], scope
        verdict, reasons = "contradicted", ["selected_item_failed"]
    elif exit_code == 0:
        if failed:
            return "insufficient", ["inconsistent_receipt"], scope
        if not view["selected"]:
            return "insufficient", ["no_tests_selected"], scope
        for n in view["selected"]:
            phases = view["phases"].get(n, {})
            if any(x for _, x in phases.values()):
                reasons.append("xfail_or_xpass")
            elif any(o == "skipped" for o, _ in phases.values()):
                reasons.append("skipped")
            elif set(phases) != set(_PHASES):
                reasons.append("missing_phase")
        if reasons:
            return "insufficient", _unique(reasons), scope
        verdict, reasons = "supported", ["all_selected_items_passed"]
    elif exit_code == 5:
        return "insufficient", ["no_tests_collected"], scope
    else:
        return "insufficient", ["unsupported_exit_code"], scope

    if claim["kind"] == KIND_CURRENT:
        try:
            now = check_current(receipt["cwd"], [e["path"] for e in declared["post"]])
        except DeclaredFileError:
            return "insufficient", ["declared_file_unsafe_or_missing"], scope
        if now != declared["post"]:
            return "insufficient", ["declared_files_changed_since_run"], scope
    return verdict, reasons, scope


def assess(claim: Any, receipts: list, *, check_current: Callable[[str, list[str]], list[dict]] = snapshot_declared) -> dict:
    """Assess one typed claim against the receipts supplied for it."""

    def result(verdict: str, reasons: list[str], scope: dict | None = None, evidence: list[str] | None = None) -> dict:
        return {
            "verdict": verdict,
            "claim_kind": claim.get("kind") if type(claim) is dict else None,
            "reasons": reasons,
            "scope": scope or {},
            "evidence": [{"receipt_sha256": d} for d in (evidence or [])],
            "limits": list(LIMITS),
        }

    if type(claim) is not dict or not _is_str(claim.get("kind")) or claim["kind"] not in KINDS:
        return result("unchecked", ["unknown_claim_kind"])
    if (
        not _is_str(claim.get("run_id")) or not claim["run_id"]
        or not _is_str(claim.get("selection_digest")) or not _DIGEST.match(claim["selection_digest"])
        or not _is_str(claim.get("declared_files_digest")) or not _DIGEST.match(claim["declared_files_digest"])
    ):
        return result("insufficient", ["scope_unspecified"])

    reasons: list[str] = []
    matching: dict[str, dict] = {}
    for receipt in receipts:
        # Depth first: hashing or printing a deeper value can exhaust the interpreter's stack.
        if type(receipt) is not dict or not depth_ok(receipt):
            reasons.append("unreadable_receipt")
        elif not _is_str(receipt.get("run_id")):
            reasons.append("malformed_receipt")
        elif receipt["run_id"] == claim["run_id"]:
            matching[digest_receipt(receipt)] = receipt
    evidence = sorted(matching)
    if reasons:
        return result("insufficient", _unique(reasons), evidence=evidence)
    if not matching:
        return result("insufficient", ["no_receipt_for_run"])
    if len(matching) > 1:
        # Two different records of one run. Neither is picked; both are kept.
        return result("insufficient", ["conflicting_receipts"], evidence=evidence)
    verdict, reasons, scope = _assess_receipt(claim, matching[evidence[0]], check_current)
    return result(verdict, reasons, scope, evidence)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_json(path: str) -> Any:
    """Parsed JSON, or None when the file is unreadable, too large, not JSON, nested too
    deeply to decode, or states one key twice."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            return None
        return loads_strict(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claim_evidence.py", description="Assess a typed claim against pytest receipts.")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("assess", help="print the assessment as JSON; the exit code is the verdict")
    p.add_argument("--claim", required=True, help="JSON file with kind, run_id, selection_digest, declared_files_digest")
    p.add_argument("--receipt", action="append", default=[], help="receipt JSON file; repeat for more")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    if args.command != "assess":
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    claim = _load_json(args.claim)
    if claim is None:
        sys.stderr.write("claim file is unreadable or not JSON\n")
        return EXIT_USAGE
    # An unreadable receipt stays in the list as None: it may be the record that
    # disagrees, so it makes the result insufficient and is never skipped.
    outcome = assess(claim, [_load_json(path) for path in args.receipt])
    sys.stdout.write(json.dumps(outcome, indent=2, sort_keys=True) + "\n")
    return EXIT_CODES[outcome["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
