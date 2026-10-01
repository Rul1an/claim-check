#!/usr/bin/env python3
"""Tests for the typed assessment of a claim against pytest receipts.

Receipts here are written out by hand, field by field, so that no expectation is
computed by the code under test. Digests are recomputed with hashlib from the
definition in the README.

Run: python3 tests/test_claim_evidence.py
"""

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import claim_evidence as ce  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "claim_evidence.py")

RUN = "run-0001"
NODES = ["tests/test_a.py::test_one", "tests/test_a.py::test_two"]
FILE_HASH = hashlib.sha256(b"x = 1\n").hexdigest()
DECLARED = [{"path": "src/a.py", "sha256": FILE_HASH, "size": 6}]


def sha(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "sha256:" + hashlib.sha256(data).hexdigest()


SELECTION_DIGEST = sha(sorted(NODES))
DECLARED_DIGEST = sha([["src/a.py", FILE_HASH, 6]])


def phase(nodeid, when, outcome="passed", xfail=False):
    return {"event": "phase", "nodeid": nodeid, "when": when, "outcome": outcome, "xfail": xfail}


def passing_receipt(cwd="/work/project"):
    events = [{"event": "session_start", "run_id": RUN, "pytest_version": "9.1.1", "python_version": "3.12.12",
               "rootdir": cwd, "inifile": None,
               "invocation_args": ["-p", "pytest_capture", "-q", "tests/test_a.py"]},
              {"event": "selected", "nodeids": list(NODES)}]
    for node in NODES:
        events += [phase(node, "setup"), phase(node, "call"), phase(node, "teardown")]
    events.append({"event": "session_finish", "exitstatus": 0})
    return {
        "schema": "claim-check.pytest-receipt.v1",
        "run_id": RUN,
        "cwd": cwd,
        "argv": ["/usr/bin/python3", "-m", "pytest", "-p", "pytest_capture", "-q", "tests/test_a.py"],
        "env_names_present": [],
        "process": {"completed": True, "timed_out": False, "exit_code": 0, "signal": None,
                    "started_at": 1790000000.0, "ended_at": 1790000001.5, "timeout_seconds": 600.0},
        "output": {"stdout_bytes": 120, "stderr_bytes": 0, "forwarded_limit": 200000, "truncated": False},
        "report": {"present": True, "truncated": False, "malformed_lines": 0, "events": events},
        "declared_files": {"pre": copy.deepcopy(DECLARED), "post": copy.deepcopy(DECLARED)},
        "selection_digest": SELECTION_DIGEST,
        "declared_files_digest": DECLARED_DIGEST,
    }


def failing_receipt(when="call"):
    r = passing_receipt()
    for e in r["report"]["events"]:
        if e["event"] == "phase" and e["nodeid"] == NODES[1] and e["when"] == when:
            e["outcome"] = "failed"
    r["report"]["events"][-1]["exitstatus"] = 1
    r["process"]["exit_code"] = 1
    return r


def claim(kind="recorded_selection_passed", **over):
    c = {"kind": kind, "run_id": RUN, "selection_digest": SELECTION_DIGEST, "declared_files_digest": DECLARED_DIGEST}
    c.update(over)
    return c


def events_of(r):
    return r["report"]["events"]


def unreachable(cwd, paths):
    raise AssertionError("the recorded kind must not read current files")


class TestVerdicts(unittest.TestCase):
    def assess(self, receipt, c=None, **kw):
        return ce.assess(c or claim(), [receipt], **kw)

    def test_every_selected_item_passing_all_three_phases_is_supported(self):
        out = self.assess(passing_receipt(), check_current=unreachable)
        self.assertEqual((out["verdict"], out["reasons"]), ("supported", ["all_selected_items_passed"]))
        self.assertEqual(out["scope"], {"run_id": RUN, "cwd": "/work/project", "selection_digest": SELECTION_DIGEST,
                                        "selected_count": 2, "declared_files_digest": DECLARED_DIGEST,
                                        "declared_file_count": 1})
        self.assertEqual(len(out["evidence"]), 1)
        self.assertTrue(any("not authenticated" in s for s in out["limits"]))

    def test_a_failed_phase_of_a_selected_item_is_contradicted(self):
        for when in ("call", "teardown"):
            out = self.assess(failing_receipt(when))
            self.assertEqual((out["verdict"], out["reasons"]), ("contradicted", ["selected_item_failed"]), when)

    def test_a_setup_failure_that_left_no_call_phase_is_still_contradicted(self):
        r = failing_receipt("setup")
        r["report"]["events"] = [e for e in events_of(r)
                                 if not (e["event"] == "phase" and e["nodeid"] == NODES[1] and e["when"] == "call")]
        self.assertEqual(self.assess(r)["verdict"], "contradicted")

    def test_unknown_claim_kind_is_unchecked(self):
        for c in (claim(kind="all_tests_pass"), claim(kind=7), {"run_id": RUN}, "all tests pass", None):
            out = ce.assess(c, [passing_receipt()])
            self.assertEqual((out["verdict"], out["reasons"]), ("unchecked", ["unknown_claim_kind"]))

    def test_a_claim_without_its_full_scope_is_insufficient(self):
        for over in ({"run_id": ""}, {"run_id": None}, {"selection_digest": "sha256:abc"},
                     {"selection_digest": None}, {"declared_files_digest": DECLARED_DIGEST[7:]}):
            out = ce.assess(claim(**over), [passing_receipt()])
            self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["scope_unspecified"]), over)


class TestInsufficient(unittest.TestCase):
    """Each case changes one thing in a receipt that would otherwise be supported."""

    def check(self, change, reason, base=passing_receipt):
        r = base()
        change(r)
        out = ce.assess(claim(), [r])
        self.assertEqual(out["verdict"], "insufficient", reason)
        self.assertIn(reason, out["reasons"])

    def test_process_did_not_exit_zero(self):
        def exit1_but_no_failure(r):
            r["process"]["exit_code"] = 1
            events_of(r)[-1]["exitstatus"] = 1
        self.check(exit1_but_no_failure, "failure_exit_without_failed_item")
        self.check(lambda r: r["process"].update(exit_code=1), "exit_status_mismatch")
        self.check(lambda r: events_of(r)[-1].update(exitstatus=1), "exit_status_mismatch")
        self.check(lambda r: r["process"].update(timed_out=True, completed=False, exit_code=None), "timed_out")
        self.check(lambda r: r["process"].update(completed=False, exit_code=None), "process_not_completed")
        self.check(lambda r: r["process"].update(signal=9, exit_code=None), "killed_by_signal")

        def exit_n(n):
            def change(r):
                r["process"]["exit_code"] = n
                events_of(r)[-1]["exitstatus"] = n
            return change
        self.check(exit_n(2), "unsupported_exit_code")
        self.check(exit_n(5), "no_tests_collected")

    def test_a_boolean_is_not_a_number_and_a_number_is_not_a_boolean(self):
        self.check(lambda r: r["process"].update(exit_code=False), "malformed_receipt")
        self.check(lambda r: r["process"].update(completed=1), "malformed_receipt")
        self.check(lambda r: r["process"].update(timed_out=0), "malformed_receipt")
        self.check(lambda r: r["report"].update(malformed_lines=False), "malformed_receipt")
        self.check(lambda r: r["report"].update(present=1), "malformed_receipt")
        self.check(lambda r: r["declared_files"]["pre"][0].update(size=True), "malformed_receipt")
        self.check(lambda r: events_of(r)[-1].update(exitstatus=False), "malformed_report_event")
        self.check(lambda r: events_of(r)[2].update(xfail=0), "malformed_report_event")

    def test_report_that_is_not_complete(self):
        self.check(lambda r: r["report"].update(present=False, events=[]), "report_missing")
        self.check(lambda r: r["report"].update(truncated=True), "report_truncated")
        self.check(lambda r: r["report"].update(malformed_lines=1), "report_malformed_lines")
        self.check(lambda r: r["report"].update(events="oops"), "malformed_report")
        self.check(lambda r: events_of(r).pop(), "report_not_finished")
        self.check(lambda r: events_of(r).pop(0), "report_not_started")
        self.check(lambda r: events_of(r).pop(1), "selection_not_recorded")
        self.check(lambda r: events_of(r).insert(1, copy.deepcopy(events_of(r)[0])), "duplicate_report_event")
        self.check(lambda r: events_of(r).insert(2, copy.deepcopy(events_of(r)[1])), "duplicate_report_event")
        self.check(lambda r: events_of(r).append(copy.deepcopy(events_of(r)[-1])), "duplicate_report_event")
        self.check(lambda r: events_of(r).append(phase(NODES[0], "call")), "report_out_of_order")

    def test_unknown_or_mistyped_events(self):
        self.check(lambda r: events_of(r).insert(2, {"event": "warning", "text": "x"}), "unknown_report_event")
        self.check(lambda r: events_of(r).insert(2, "phase"), "unknown_report_event")
        self.check(lambda r: events_of(r).insert(2, {"event": 3}), "unknown_report_event")
        self.check(lambda r: events_of(r)[2].update(extra=1), "malformed_report_event")
        self.check(lambda r: events_of(r)[2].update(outcome="ok"), "malformed_report_event")
        self.check(lambda r: events_of(r)[2].update(when="run"), "malformed_report_event")
        self.check(lambda r: events_of(r)[2].pop("xfail"), "malformed_report_event")
        self.check(lambda r: events_of(r)[1].update(nodeids="tests/test_a.py::test_one"), "malformed_report_event")
        self.check(lambda r: events_of(r)[0].update(invocation_args=[1]), "malformed_report_event")

    def test_selection_that_is_not_fully_accounted_for(self):
        self.check(lambda r: events_of(r).pop(4), "missing_phase")                       # teardown of item one
        self.check(lambda r: events_of(r).insert(4, phase(NODES[0], "call")), "repeated_phase")
        self.check(lambda r: events_of(r).insert(4, phase("tests/test_b.py::test_x", "call")), "phase_for_unselected_item")
        self.check(lambda r: events_of(r)[1]["nodeids"].append(NODES[0]), "duplicate_selected_item")
        self.check(lambda r: events_of(r).insert(2, {"event": "collect_error", "nodeid": "tests/test_c.py"}),
                   "collection_error")

        def nothing_selected(r):
            r["report"]["events"] = [events_of(r)[0], {"event": "selected", "nodeids": []}, events_of(r)[-1]]
            r["selection_digest"] = sha([])
        r = passing_receipt()
        nothing_selected(r)
        out = ce.assess(claim(selection_digest=sha([])), [r])
        self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["no_tests_selected"]))

    def test_skip_xfail_and_xpass_are_not_passes(self):
        def skipped(r):
            r["report"]["events"] = [e for e in events_of(r) if not (e["event"] == "phase" and e["nodeid"] == NODES[0]
                                                                    and e["when"] == "call")]
            events_of(r)[2].update(outcome="skipped")
        self.check(skipped, "skipped")
        self.check(lambda r: events_of(r)[3].update(outcome="skipped", xfail=True), "xfail_or_xpass")
        self.check(lambda r: events_of(r)[3].update(xfail=True), "xfail_or_xpass")

    def test_identity_that_does_not_bind(self):
        self.check(lambda r: r.update(schema="claim-check.pytest-receipt.v2"), "unsupported_schema")
        self.check(lambda r: events_of(r)[0].update(run_id="run-0002"), "run_id_mismatch")
        self.check(lambda r: r.update(selection_digest=sha(["other"])), "inconsistent_receipt")
        self.check(lambda r: r.update(declared_files_digest=sha([])), "inconsistent_receipt")
        self.check(lambda r: events_of(r)[3].update(outcome="failed"), "inconsistent_receipt")  # failed item, exit 0
        self.check(lambda r: r["declared_files"]["post"][0].update(sha256="0" * 64), "declared_files_changed_during_run")
        self.check(lambda r: r["declared_files"].update(pre=[], post=[]), "declared_scope_empty")
        self.check(lambda r: r["declared_files"]["pre"].append(dict(DECLARED[0])), "malformed_receipt")
        self.check(lambda r: r.pop("cwd"), "malformed_receipt")

    def test_claim_scope_that_differs_from_the_receipt(self):
        other = "sha256:" + "1" * 64
        for over, reason in (({"selection_digest": other}, "selection_mismatch"),
                             ({"declared_files_digest": other}, "declared_files_mismatch")):
            for receipt in (passing_receipt(), failing_receipt()):
                out = ce.assess(claim(**over), [receipt])
                self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", [reason]))

    def test_a_failing_receipt_with_a_broken_report_is_not_contradicted(self):
        self.check(lambda r: events_of(r).insert(2, {"event": "warning"}), "unknown_report_event", base=failing_receipt)
        self.check(lambda r: events_of(r).insert(4, phase(NODES[0], "call")), "repeated_phase", base=failing_receipt)
        self.check(lambda r: r["process"].update(exit_code=2), "exit_status_mismatch", base=failing_receipt)


class TestStoredIdentityAndOrder(unittest.TestCase):
    """A receipt whose stored identity or event order could not come from a run is not complete.

    All for the recorded kind, which reads no current file: `unreachable` fails if it tries.
    """

    def reasons(self, change):
        r = passing_receipt()
        change(r)
        c = claim(selection_digest=ce.digest_selection(NODES),
                  declared_files_digest=sha(sorted([e["path"], e["sha256"], e["size"]] for e in r["declared_files"]["pre"])))
        r["declared_files_digest"] = c["declared_files_digest"]
        out = ce.assess(c, [r], check_current=unreachable)
        self.assertEqual(out["verdict"], "insufficient")
        return out["reasons"]

    def test_declared_paths_that_name_no_file_under_cwd_are_malformed(self):
        for path in ["../outside.py", "/absolute.py", "./src/a.py", "", "src\x00a.py", "src//a.py", "src/../a.py",
                     "src\\a.py", "src/"]:
            def change(r, path=path):
                for side in ("pre", "post"):
                    r["declared_files"][side][0]["path"] = path
            self.assertEqual(self.reasons(change), ["malformed_receipt"], repr(path))

    def test_cwd_that_is_not_an_absolute_normal_path_is_malformed(self):
        for cwd in ["relative", "", "/recorded/../project", "/recorded/./project", "/recorded//project",
                    "/recorded/project/", "/recorded\x00/project"]:
            self.assertEqual(self.reasons(lambda r, cwd=cwd: r.update(cwd=cwd)), ["malformed_receipt"], repr(cwd))

    def test_missing_or_mistyped_invocation_context_is_malformed(self):
        for change in (lambda r: r.pop("argv"), lambda r: r.update(argv="python -m pytest"), lambda r: r.update(argv=[]),
                       lambda r: r.update(argv=["", "-m", "pytest", "-p", "pytest_capture"]),
                       lambda r: r.update(argv=["/usr/bin/python3", "-c", "pass"]),
                       lambda r: r.pop("env_names_present"), lambda r: r.update(env_names_present=[1]),
                       lambda r: r["process"].pop("started_at"), lambda r: r["process"].update(ended_at=True),
                       lambda r: r["process"].update(timeout_seconds="600")):
            self.assertEqual(self.reasons(change), ["malformed_receipt"])

    def test_phases_before_the_selection_are_out_of_order(self):
        def change(r):
            ev = events_of(r)
            r["report"]["events"] = [ev[0]] + ev[2:5] + [ev[1]] + ev[5:]
        self.assertEqual(self.reasons(change), ["report_out_of_order"])

    def test_phases_of_one_item_must_run_setup_call_teardown(self):
        def reorder(order):
            def change(r):
                ev = events_of(r)
                r["report"]["events"] = ev[:2] + [ev[i] for i in order] + ev[5:]
            return change
        for order in ([4, 3, 2], [3, 2, 4], [2, 4, 3]):
            self.assertEqual(self.reasons(reorder(order)), ["phase_out_of_order"], order)
        # A call after a setup that did not pass cannot happen either.
        self.assertEqual(self.reasons(lambda r: events_of(r)[2].update(outcome="skipped")), ["phase_out_of_order"])

    def test_phases_of_different_items_may_interleave(self):
        r = passing_receipt()
        ev = events_of(r)
        r["report"]["events"] = ev[:2] + [ev[2], ev[5], ev[3], ev[6], ev[4], ev[7]] + ev[8:]
        self.assertEqual(ce.assess(claim(), [r], check_current=unreachable)["verdict"], "supported")

    def test_current_files_claim_with_a_nul_path_is_insufficient_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = passing_receipt(os.path.realpath(tmp))
            for side in ("pre", "post"):
                r["declared_files"][side][0]["path"] = "bad\x00.py"
            d = sha([["bad\x00.py", FILE_HASH, 6]])
            r["declared_files_digest"] = d
            out = ce.assess(claim(kind="recorded_selection_passed_current_files", declared_files_digest=d), [r])
            self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["malformed_receipt"]))
            with self.assertRaises(ce.DeclaredFileError):
                ce.snapshot_declared(os.path.realpath(tmp), ["bad\x00.py"])


class TestReceiptSets(unittest.TestCase):
    def test_a_receipt_for_another_run_is_not_evidence(self):
        r = passing_receipt()
        r["run_id"] = "run-0002"
        events_of(r)[0]["run_id"] = "run-0002"
        out = ce.assess(claim(), [r])
        self.assertEqual((out["verdict"], out["reasons"], out["evidence"]), ("insufficient", ["no_receipt_for_run"], []))
        self.assertEqual(ce.assess(claim(), [])["reasons"], ["no_receipt_for_run"])

    def test_two_different_receipts_for_one_run_are_kept_and_neither_is_picked(self):
        for other in (failing_receipt(), passing_receipt(cwd="/elsewhere")):
            for order in ([passing_receipt(), other], [other, passing_receipt()]):
                out = ce.assess(claim(), order)
                self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["conflicting_receipts"]))
                self.assertEqual(len(out["evidence"]), 2)

    def test_the_same_receipt_twice_is_one_receipt(self):
        out = ce.assess(claim(), [passing_receipt(), passing_receipt()])
        self.assertEqual(out["verdict"], "supported")
        self.assertEqual(len(out["evidence"]), 1)

    def test_a_receipt_that_cannot_be_read_or_attributed_is_never_skipped(self):
        for bad, reason in ((None, "unreadable_receipt"), ("{", "unreadable_receipt"), ([], "unreadable_receipt"),
                            ({"schema": "x"}, "malformed_receipt"), ({"run_id": 1}, "malformed_receipt")):
            out = ce.assess(claim(), [passing_receipt(), bad])
            self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", [reason]))

    def test_an_edited_receipt_is_caught_only_where_it_contradicts_itself(self):
        """Flipping the exit code of a failing receipt leaves the failed phase behind."""
        r = failing_receipt()
        r["process"]["exit_code"] = 0
        events_of(r)[-1]["exitstatus"] = 0
        self.assertEqual(ce.assess(claim(), [r])["reasons"], ["inconsistent_receipt"])
        # Rewritten consistently, it is accepted: there is no authentication. The limit says so.
        for e in events_of(r):
            if e["event"] == "phase":
                e["outcome"] = "passed"
        out = ce.assess(claim(), [r])
        self.assertEqual(out["verdict"], "supported")
        self.assertIn("A receipt is not authenticated: whoever can write it can write a consistent false one.",
                      out["limits"])


class TestCurrentFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.cwd, "src"))
        self.path = os.path.join(self.cwd, "src", "a.py")
        with open(self.path, "wb") as fh:
            fh.write(b"x = 1\n")

    def tearDown(self):
        self.tmp.cleanup()

    def current(self):
        return claim(kind="recorded_selection_passed_current_files")

    def test_unchanged_files_keep_the_verdict(self):
        self.assertEqual(ce.assess(self.current(), [passing_receipt(self.cwd)])["verdict"], "supported")
        self.assertEqual(ce.assess(self.current(), [self.failing()])["verdict"], "contradicted")

    def failing(self):
        r = failing_receipt()
        r["cwd"] = self.cwd
        return r

    def test_a_file_changed_after_the_run_makes_both_verdicts_insufficient(self):
        with open(self.path, "wb") as fh:
            fh.write(b"x = 2\n")
        for receipt in (passing_receipt(self.cwd), self.failing()):
            out = ce.assess(self.current(), [receipt])
            self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["declared_files_changed_since_run"]))

    def test_the_recorded_kind_is_about_the_run_and_ignores_a_later_change(self):
        with open(self.path, "wb") as fh:
            fh.write(b"x = 2\n")
        self.assertEqual(ce.assess(claim(), [passing_receipt(self.cwd)])["verdict"], "supported")

    def test_a_file_that_is_gone_or_replaced_by_a_link_is_insufficient(self):
        os.unlink(self.path)
        out = ce.assess(self.current(), [passing_receipt(self.cwd)])
        self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["declared_file_unsafe_or_missing"]))
        real = os.path.join(self.cwd, "real.py")
        with open(real, "wb") as fh:
            fh.write(b"x = 1\n")
        os.symlink(real, self.path)
        out = ce.assess(self.current(), [passing_receipt(self.cwd)])
        self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["declared_file_unsafe_or_missing"]))

    def test_snapshot_returns_path_hash_and_size(self):
        self.assertEqual(ce.snapshot_declared(self.cwd, ["src/a.py"]), DECLARED)

    def test_snapshot_refuses_ambiguous_paths(self):
        os.symlink(os.path.join(self.cwd, "src"), os.path.join(self.cwd, "link"))
        for paths in (["/etc/hosts"], ["src/../src/a.py"], ["./src/a.py"], ["src//a.py"], ["src/a.py", "src/a.py"],
                      ["link/a.py"], ["src"], ["src/missing.py"], ["src/a.py/x"], [""], [3]):
            with self.assertRaises(ce.DeclaredFileError, msg=str(paths)):
                ce.snapshot_declared(self.cwd, paths)
        with self.assertRaises(ce.DeclaredFileError):
            ce.snapshot_declared(os.path.join(self.cwd, "link"), ["a.py"])
        with self.assertRaises(ce.DeclaredFileError):
            ce.snapshot_declared("relative", ["a.py"])


class TestDigests(unittest.TestCase):
    def test_selection_digest_ignores_order_and_nothing_else(self):
        self.assertEqual(ce.digest_selection(list(reversed(NODES))), SELECTION_DIGEST)
        self.assertNotEqual(ce.digest_selection(NODES[:1]), SELECTION_DIGEST)

    def test_declared_digest_covers_path_content_and_size(self):
        self.assertEqual(ce.digest_declared(DECLARED), DECLARED_DIGEST)
        for change in ({"path": "src/b.py"}, {"sha256": "0" * 64}, {"size": 7}):
            self.assertNotEqual(ce.digest_declared([dict(DECLARED[0], **change)]), DECLARED_DIGEST)


class TestCommandLine(unittest.TestCase):
    def run_cli(self, claim_obj, receipts, raw_claim=None):
        with tempfile.TemporaryDirectory() as tmp:
            claim_path = os.path.join(tmp, "claim.json")
            with open(claim_path, "w", encoding="utf-8") as fh:
                fh.write(raw_claim if raw_claim is not None else json.dumps(claim_obj))
            args = [sys.executable, SCRIPT, "assess", "--claim", claim_path]
            for i, r in enumerate(receipts):
                p = os.path.join(tmp, "r%d.json" % i)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(r if isinstance(r, str) else json.dumps(r))
                args += ["--receipt", p]
            return subprocess.run(args, capture_output=True, text=True)

    def test_exit_code_is_the_verdict(self):
        for receipts, c, code, verdict in (
            ([passing_receipt()], claim(), 0, "supported"),
            ([failing_receipt()], claim(), 1, "contradicted"),
            ([passing_receipt(), failing_receipt()], claim(), 2, "insufficient"),
            ([passing_receipt(), "{ not json"], claim(), 2, "insufficient"),
            ([], claim(), 2, "insufficient"),
            ([passing_receipt()], claim(kind="all_tests_pass"), 3, "unchecked"),
        ):
            proc = self.run_cli(c, receipts)
            self.assertEqual(proc.returncode, code, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["verdict"], verdict)

    def test_a_repeated_key_is_a_conflict_inside_one_file_and_is_not_resolved(self):
        """json.loads keeps the last of two members. The first one said the run failed."""
        for old, new in (('"exit_code": 0', '"exit_code": 1, "exit_code": 0'),
                         ('"exitstatus": 0', '"exitstatus": 1, "exitstatus": 0'),
                         ('"run_id": "run-0001", "schema"', '"run_id": "run-0002", "run_id": "run-0001", "schema"')):
            raw = json.dumps(passing_receipt(), sort_keys=True)
            self.assertEqual(raw.count(old), 1, old)
            proc = self.run_cli(claim(), [raw.replace(old, new)])
            self.assertEqual((proc.returncode, proc.stderr), (2, ""), old)
            self.assertEqual(json.loads(proc.stdout)["reasons"], ["unreadable_receipt"])
        proc = self.run_cli(None, [passing_receipt()], raw_claim=json.dumps(claim()).replace(
            '"run_id": "run-0001"', '"run_id": "run-0009", "run_id": "run-0001"'))
        self.assertEqual((proc.returncode, proc.stdout), (64, ""))

    def test_input_that_cannot_be_decoded_is_unreadable_not_a_traceback(self):
        deep = '{"run_id":"run-0001","nested":' + "[" * 100000 + "0" + "]" * 100000 + "}"
        for raw in (deep, "\x00", '{"run_id": NaN}', '{"a": 1e999}', ""):
            proc = self.run_cli(claim(), [raw])
            self.assertEqual((proc.returncode, proc.stderr), (2, ""), raw[:30])
            self.assertEqual(json.loads(proc.stdout)["reasons"], ["unreadable_receipt"])

    def test_unusable_invocations_exit_64_and_print_no_verdict(self):
        proc = self.run_cli(None, [passing_receipt()], raw_claim="{ not json")
        self.assertEqual((proc.returncode, proc.stdout), (64, ""))
        for args in ([], ["assess"], ["assess", "--claim", "/nonexistent/claim.json"], ["frobnicate"]):
            proc = subprocess.run([sys.executable, SCRIPT] + args, capture_output=True, text=True)
            self.assertEqual((proc.returncode, proc.stdout), (64, ""), args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
