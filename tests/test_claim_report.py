#!/usr/bin/env python3
"""Tests for explicit claim binding and the bounded report.

The five texts in EXPECTED_EMPTY were written out before the renderer existed, by
someone other than its author. The other expected texts are literals in this file.
Receipt fixtures come from tests/test_claim_evidence.py, which writes them by hand.

Run: python3 tests/test_claim_report.py
"""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, HERE)

import claim_evidence as ce  # noqa: E402
import claim_report as cr  # noqa: E402
import test_claim_evidence as fx  # noqa: E402  (hand-written receipts and their digests)

REPORT = os.path.join(ROOT, "scripts", "claim_report.py")
CHECKER = os.path.join(ROOT, "scripts", "claim_evidence.py")
RUNNER = os.path.join(ROOT, "scripts", "pytest_evidence.py")
HAVE_PYTEST = importlib.util.find_spec("pytest") is not None

LIMITS = [
    "A receipt is not authenticated: whoever can write it can write a consistent false one.",
    "Declared files are a comparison of snapshots taken before and after the run. They are not the bytes "
    "pytest loaded, not its dependencies, and not an atomic snapshot.",
    "The verdict holds for this run and this selection only.",
]
TAIL = (
    "Limits:\n"
    "- A receipt is not authenticated: whoever can write it can write a consistent false one.\n"
    "- Declared files are a comparison of snapshots taken before and after the run. They are not the bytes "
    "pytest loaded, not its dependencies, and not an atomic snapshot.\n"
    "- The verdict holds for this run and this selection only.\n"
    "Binding does not add evidence or authenticate the receipt.\n"
)
EXPECTED_EMPTY = [
    ("supported", "recorded_selection_passed", "all_selected_items_passed",
     'Verdict: supported\nClaim kind: "recorded_selection_passed"\nScope: {}\n'
     'Reasons: ["all_selected_items_passed"]\nEvidence: []\n' + TAIL),
    ("contradicted", "recorded_selection_passed", "selected_item_failed",
     'Verdict: contradicted\nClaim kind: "recorded_selection_passed"\nScope: {}\n'
     'Reasons: ["selected_item_failed"]\nEvidence: []\n' + TAIL),
    ("insufficient", "recorded_selection_passed", "no_receipt_for_run",
     'Verdict: insufficient\nClaim kind: "recorded_selection_passed"\nScope: {}\n'
     'Reasons: ["no_receipt_for_run"]\nEvidence: []\n' + TAIL),
    ("unchecked", "other_kind", "unknown_claim_kind",
     'Verdict: unchecked\nClaim kind: "other_kind"\nScope: {}\n'
     'Reasons: ["unknown_claim_kind"]\nEvidence: []\n' + TAIL),
    ("insufficient", "recorded_selection_passed", "conflicting_receipts",
     'Verdict: insufficient\nClaim kind: "recorded_selection_passed"\nScope: {}\n'
     'Reasons: ["conflicting_receipts"]\nEvidence: []\n' + TAIL),
]
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64


def assessment(verdict="supported", kind="recorded_selection_passed", reasons=("all_selected_items_passed",),
               scope=None, evidence=()):
    return {"verdict": verdict, "claim_kind": kind, "reasons": list(reasons), "scope": scope or {},
            "evidence": [{"receipt_sha256": d} for d in evidence], "limits": list(LIMITS)}


def scope(run_id="run-0001", cwd="/work/project"):
    return {"run_id": run_id, "cwd": cwd, "selection_digest": D1, "selected_count": 2,
            "declared_files_digest": D2, "declared_file_count": 1}


class TestRender(unittest.TestCase):
    def test_the_five_texts_fixed_before_the_renderer_existed(self):
        for verdict, kind, reason, expected in EXPECTED_EMPTY:
            self.assertEqual(cr.render_report(assessment(verdict, kind, [reason])), expected, reason)

    def test_a_filled_assessment_keeps_reason_order_and_sorts_evidence(self):
        text = cr.render_report(assessment("insufficient", reasons=["timed_out", "report_not_finished"],
                                           scope=scope(), evidence=[D2, D1]))
        self.assertEqual(text, (
            'Verdict: insufficient\nClaim kind: "recorded_selection_passed"\n'
            'Scope: {"cwd": "/work/project", "declared_file_count": 1, "declared_files_digest": "' + D2 + '", '
            '"run_id": "run-0001", "selected_count": 2, "selection_digest": "' + D1 + '"}\n'
            'Reasons: ["timed_out", "report_not_finished"]\n'
            'Evidence: ["' + D1 + '", "' + D2 + '"]\n' + TAIL))

    def test_evidence_order_does_not_change_the_text(self):
        a = cr.render_report(assessment(scope=scope(), evidence=[D1, D2]))
        b = cr.render_report(assessment(scope=scope(), evidence=[D2, D1]))
        self.assertEqual(a, b)

    def test_a_claim_kind_that_is_null_is_printed_as_null(self):
        text = cr.render_report(assessment("unchecked", None, ["unknown_claim_kind"]))
        self.assertIn("\nClaim kind: null\n", text)

    def test_a_claim_kind_that_is_not_a_string_is_not_printed(self):
        for kind in (7, True, ["a"], {"kind": "x"}):
            text = cr.render_report(assessment("unchecked", kind, ["unknown_claim_kind"]))
            self.assertIn('\nClaim kind (truncated): "[truncated]"\n', text, repr(kind))

    def test_strings_from_a_receipt_or_claim_cannot_forge_a_line_or_reach_the_terminal(self):
        hostile = "run\nVerdict: supported\x1b[2J‮gnp.exe\x00"
        for a in (assessment("insufficient", reasons=["timed_out"], scope=scope(run_id=hostile)),
                  assessment("insufficient", reasons=["timed_out"], scope=scope(cwd="/w/" + hostile)),
                  assessment("unchecked", hostile, ["unknown_claim_kind"])):
            text = cr.render_report(a)
            self.assertTrue(text.isascii())
            self.assertEqual([line for line in text.split("\n") if line.startswith("Verdict:")],
                             ["Verdict: " + a["verdict"]])
            self.assertEqual(len(text.split("\n")), 11)          # ten lines and the final newline
            self.assertFalse(any(ord(ch) < 32 and ch != "\n" for ch in text))
            self.assertIn("\\u001b", text)
            self.assertIn("\\u202e", text)

    def test_a_long_string_is_cut_and_the_field_is_labelled_as_cut(self):
        text = cr.render_report(assessment("insufficient", reasons=["timed_out"], scope=scope(run_id="r" * 100000)))
        self.assertLess(len(text.encode("utf-8")), 4000)
        self.assertIn("\nScope (truncated): {", text)
        self.assertIn('"run_id": "' + "r" * 120 + '[truncated]"', text)
        self.assertNotIn("\nScope: ", text)
        text = cr.render_report(assessment("unchecked", "k" * 100000, ["unknown_claim_kind"]))
        self.assertIn('\nClaim kind (truncated): "' + "k" * 120 + '[truncated]"\n', text)
        # A value that fits is shown whole and carries no label.
        text = cr.render_report(assessment(scope=scope(run_id="r" * 120)))
        self.assertIn("\nScope: {", text)
        self.assertNotIn("[truncated]", text)

    def test_long_lists_are_cut_with_a_marker_and_a_label(self):
        many = ["sha256:%064x" % i for i in range(20)]
        text = cr.render_report(assessment("insufficient", reasons=["conflicting_receipts"], evidence=many))
        self.assertIn('\nEvidence (truncated): ["' + many[0] + '", ', text)
        self.assertIn(many[7], text)
        self.assertNotIn(many[8], text)
        self.assertIn(', "[truncated]"]\n', text)
        reasons = ["reason_%02d" % i for i in range(30)]
        text = cr.render_report(assessment("insufficient", reasons=reasons))
        self.assertIn("\nReasons (truncated): [", text)
        self.assertIn('"reason_15", "[truncated]"]', text)
        self.assertNotIn("reason_16", text)

    def test_the_largest_full_report_fits_the_default_budget(self):
        a = assessment("insufficient", "k" * 500, ["r" * 64] * 16, scope("i" * 500, "/" + "c" * 500),
                       ["sha256:%064x" % i for i in range(8)])
        a["scope"].update(selected_count=10 ** 30, declared_file_count=10 ** 30)
        text = cr.render_report(a)
        self.assertLessEqual(len(text.encode("utf-8")), cr.DEFAULT_MAX_BYTES)
        self.assertIn('"selected_count": "[truncated]"', text)

    def test_rendering_reads_no_file(self):
        a = assessment(kind="recorded_selection_passed_current_files", scope=scope(), evidence=[D1])
        with mock.patch("builtins.open", side_effect=AssertionError("opened a file")), \
                mock.patch.object(os, "open", side_effect=AssertionError("opened a file")), \
                mock.patch.object(os, "lstat", side_effect=AssertionError("looked at a file")):
            self.assertTrue(cr.render_report(a).startswith("Verdict: supported\n"))


class TestBudget(unittest.TestCase):
    def full(self):
        return assessment("insufficient", reasons=["timed_out"], scope=scope(), evidence=[D1])

    def test_a_budget_below_the_full_text_gives_the_compact_form_with_every_caveat(self):
        full = cr.render_report(self.full())
        compact = cr.render_report(self.full(), max_bytes=len(full.encode("utf-8")) - 1)
        self.assertEqual(compact, (
            'Verdict: insufficient\nClaim kind (truncated): "[truncated]"\nScope (truncated): "[truncated]"\n'
            'Reasons (truncated): ["[truncated]"]\nEvidence (truncated): ["[truncated]"]\n' + TAIL))
        self.assertEqual(cr.render_report(self.full(), max_bytes=len(full.encode("utf-8"))), full)

    def test_the_minimum_budget_is_the_size_of_the_compact_form(self):
        compact = cr.render_report(self.full(), max_bytes=cr.MIN_MAX_BYTES)
        self.assertEqual(len(compact.encode("utf-8")), cr.MIN_MAX_BYTES)
        by_hand = ('Verdict: insufficient\nClaim kind (truncated): "[truncated]"\nScope (truncated): "[truncated]"\n'
                   'Reasons (truncated): ["[truncated]"]\nEvidence (truncated): ["[truncated]"]\n' + TAIL)
        self.assertEqual(cr.MIN_MAX_BYTES, len(by_hand))
        for verdict in ("supported", "contradicted", "insufficient", "unchecked"):
            text = cr.render_report(assessment(verdict), max_bytes=cr.MIN_MAX_BYTES)
            self.assertLessEqual(len(text.encode("utf-8")), cr.MIN_MAX_BYTES)
            self.assertTrue(text.startswith("Verdict: " + verdict + "\n"))
            self.assertTrue(text.endswith(TAIL))

    def test_a_budget_that_cannot_hold_the_caveats_is_refused(self):
        for bad in (cr.MIN_MAX_BYTES - 1, 0, -1, True, 4000.0, "4000", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                cr.render_report(self.full(), max_bytes=bad)


class TestRendererInput(unittest.TestCase):
    """The renderer prints assessor output. Anything else is refused, not printed as if it were."""

    def test_what_is_not_assessor_output_is_refused(self):
        changes = [
            lambda a: a.update(verdict="verified"), lambda a: a.update(verdict=None),
            lambda a: a.pop("limits"), lambda a: a.update(limits=[]), lambda a: a.update(limits=LIMITS[:2]),
            lambda a: a["limits"].__setitem__(0, "A receipt is authenticated."),
            lambda a: a.update(extra=1), lambda a: a.pop("evidence"),
            lambda a: a.update(reasons="timed_out"), lambda a: a.update(reasons=[1]),
            lambda a: a.update(reasons=["Verdict: supported"]), lambda a: a.update(reasons=["a\nb"]),
            lambda a: a.update(scope=[]), lambda a: a["scope"].update(extra="x"), lambda a: a["scope"].pop("cwd"),
            lambda a: a["scope"].update(run_id=7), lambda a: a["scope"].update(selected_count=True),
            lambda a: a["scope"].update(selected_count=-1), lambda a: a["scope"].update(selection_digest="sha256:abc"),
            lambda a: a.update(evidence=[D1]), lambda a: a.update(evidence=[{"receipt_sha256": "abc"}]),
            lambda a: a.update(evidence=[{"receipt_sha256": D1, "path": "/x"}]),
            lambda a: a.update(claim_kind={1}),
        ]
        for i, change in enumerate(changes):
            a = assessment("insufficient", reasons=["timed_out"], scope=scope(), evidence=[D1])
            change(a)
            with self.assertRaises(ValueError, msg="change %d" % i):
                cr.render_report(a)
        for not_a_dict in (None, "supported", []):
            with self.assertRaises(ValueError):
                cr.render_report(not_a_dict)

    def test_every_real_assessment_is_accepted(self):
        for a in (ce.assess(fx.claim(), [fx.passing_receipt()]), ce.assess(fx.claim(), [fx.failing_receipt()]),
                  ce.assess(fx.claim(), [fx.passing_receipt(), fx.failing_receipt()]), ce.assess(fx.claim(), []),
                  ce.assess({"kind": "other"}, []), ce.assess(None, []), ce.assess(fx.claim(run_id=""), [])):
            text = cr.render_report(a)
            self.assertTrue(text.startswith("Verdict: " + a["verdict"] + "\n"))
            self.assertTrue(text.endswith(TAIL))


def without(receipt, *names):
    receipt["report"]["events"] = [e for e in receipt["report"]["events"] if e["event"] not in names]
    return receipt


def bindable_receipts():
    """Receipts that identify a run and a selection, whatever happened in the run."""
    timed_out = without(fx.passing_receipt(), "session_finish", "phase")
    timed_out["process"].update(completed=False, timed_out=True, exit_code=None)
    changed = fx.passing_receipt()
    changed["declared_files"]["post"][0]["sha256"] = "0" * 64
    no_argv = fx.passing_receipt()
    del no_argv["argv"]
    odd_event = fx.passing_receipt()
    odd_event["report"]["events"].insert(2, {"event": "warning"})
    return {"passed": fx.passing_receipt(), "failed": fx.failing_receipt(), "timed out": timed_out,
            "files changed during the run": changed, "no argv": no_argv, "unknown event": odd_event}


class TestBind(unittest.TestCase):
    def test_a_bound_claim_names_the_run_the_selection_and_the_declared_files(self):
        self.assertEqual(cr.bind_claim("recorded_selection_passed", fx.passing_receipt()), {
            "kind": "recorded_selection_passed", "run_id": "run-0001",
            "selection_digest": fx.SELECTION_DIGEST, "declared_files_digest": fx.DECLARED_DIGEST,
            "scope_source": "receipt"})
        self.assertEqual(cr.bind_claim("recorded_selection_passed_current_files", fx.passing_receipt())["kind"],
                         "recorded_selection_passed_current_files")

    def test_binding_is_about_identity_not_about_success(self):
        expected = {"passed": "supported", "failed": "contradicted", "timed out": "insufficient",
                    "files changed during the run": "insufficient", "no argv": "insufficient",
                    "unknown event": "insufficient"}
        for name, receipt in bindable_receipts().items():
            claim = cr.bind_claim("recorded_selection_passed", receipt)
            out = ce.assess(claim, [receipt])
            self.assertEqual(out["verdict"], expected[name], name)
            # Whatever the verdict, a claim bound to a receipt never mismatches that receipt's scope.
            for reason in ("selection_mismatch", "declared_files_mismatch", "no_receipt_for_run", "scope_unspecified"):
                self.assertNotIn(reason, out["reasons"], name)

    def test_a_bound_claim_does_not_travel_to_another_run(self):
        other = fx.passing_receipt()
        other["run_id"] = "run-0002"
        other["report"]["events"][0]["run_id"] = "run-0002"
        claim = cr.bind_claim("recorded_selection_passed", fx.passing_receipt())
        self.assertEqual(ce.assess(claim, [other])["reasons"], ["no_receipt_for_run"])

    def test_a_receipt_with_no_usable_identity_is_refused(self):
        def two_selections(r):
            r["report"]["events"].insert(2, copy.deepcopy(r["report"]["events"][1]))

        refused = {
            "not a dict": lambda r: None,
            "truncated report": lambda r: r["report"].update(truncated=True, events=[]),
            "no selection": lambda r: without(r, "selected"),
            "two selections": two_selections,
            "selection with a non-string": lambda r: r["report"]["events"][1]["nodeids"].append(3),
            "selection with an extra key": lambda r: r["report"]["events"][1].update(extra=1),
            "a node id twice": lambda r: r["report"]["events"][1]["nodeids"].append(fx.NODES[0]),
            "events not a list": lambda r: r["report"].update(events="x"),
            "no report": lambda r: r.pop("report"),
            "other schema": lambda r: r.update(schema="claim-check.pytest-receipt.v2"),
            "no run id": lambda r: r.pop("run_id"),
            "empty run id": lambda r: r.update(run_id=""),
            "run id not a string": lambda r: r.update(run_id=7),
            "declared path outside cwd": lambda r: r["declared_files"]["pre"][0].update(path="../x.py"),
            "declared files missing": lambda r: r.pop("declared_files"),
            "stored selection digest contradicts the selection": lambda r: r.update(selection_digest=D1),
            "stored declared digest contradicts the files": lambda r: r.update(declared_files_digest=D2),
            "stored digest missing": lambda r: r.pop("selection_digest"),
            "not a JSON value": lambda r: r.update(extra={1}),
        }
        for name, change in refused.items():
            receipt = fx.passing_receipt()
            change(receipt)
            self.assert_refused(None if name == "not a dict" else receipt, name)

    def assert_refused(self, receipt, name):
        try:
            cr.bind_claim("recorded_selection_passed", receipt)
        except cr.BindError:
            return
        except Exception as exc:  # a crash is not a refusal
            self.fail("%s: bind raised %s, not BindError" % (name, type(exc).__name__))
        self.fail("%s: bind accepted the receipt" % name)

    def test_malformed_identity_is_refused_even_when_the_stored_digests_agree_with_it(self):
        """Without this, the digest comparison alone would refuse these, and the structural
        checks would be untested."""
        twice = fx.passing_receipt()
        twice["report"]["events"][1]["nodeids"] = [fx.NODES[0], fx.NODES[0]]
        twice["selection_digest"] = fx.sha([fx.NODES[0], fx.NODES[0]])
        self.assert_refused(twice, "a node id twice, digest consistent")

        outside = fx.passing_receipt()
        outside["declared_files"]["pre"][0]["path"] = "../x.py"
        outside["declared_files_digest"] = fx.sha([["../x.py", fx.FILE_HASH, 6]])
        self.assert_refused(outside, "declared path outside cwd, digest consistent")

        negative = fx.passing_receipt()
        negative["declared_files"]["pre"][0]["size"] = -1
        negative["declared_files_digest"] = fx.sha([["src/a.py", fx.FILE_HASH, -1]])
        self.assert_refused(negative, "negative size, digest consistent")

    def test_only_a_known_kind_can_be_bound(self):
        for kind in ("all_tests_pass", "", None, 7, ["recorded_selection_passed"]):
            with self.assertRaises(cr.BindError, msg=repr(kind)):
                cr.bind_claim(kind, fx.passing_receipt())

    def test_the_digests_are_recomputed_and_not_copied_from_the_receipt(self):
        """A receipt whose stored digests agree with each other but not with its content is refused;
        one that stores correct digests yields exactly the recomputed values."""
        r = fx.passing_receipt()
        r["report"]["events"][1]["nodeids"] = ["tests/test_a.py::test_one"]
        with self.assertRaises(cr.BindError):
            cr.bind_claim("recorded_selection_passed", r)
        r["selection_digest"] = fx.sha(["tests/test_a.py::test_one"])
        self.assertEqual(cr.bind_claim("recorded_selection_passed", r)["selection_digest"],
                         fx.sha(["tests/test_a.py::test_one"]))


class CommandLine(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def path(self, name, content):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content if isinstance(content, str) else json.dumps(content))
        return p

    def run_cli(self, *args, script=REPORT):
        return subprocess.run([sys.executable, "-B", script] + list(args), capture_output=True, text=True)


class TestBindCommand(CommandLine):
    def test_bind_prints_the_claim_and_nothing_about_the_verdict(self):
        for name, receipt in bindable_receipts().items():
            proc = self.run_cli("bind", "--kind", "recorded_selection_passed", "--receipt", self.path("r.json", receipt))
            self.assertEqual((proc.returncode, proc.stderr), (0, ""), name)
            self.assertEqual(json.loads(proc.stdout), cr.bind_claim("recorded_selection_passed", receipt), name)
            for word in ("supported", "contradicted", "insufficient", "unchecked"):
                self.assertNotIn(word, proc.stdout, name)

    def test_bind_refusals_exit_64_with_nothing_on_stdout(self):
        good = self.path("good.json", fx.passing_receipt())
        dup = json.dumps(fx.passing_receipt()).replace('"exit_code": 0', '"exit_code": 1, "exit_code": 0')
        cases = [
            ["bind", "--kind", "all_tests_pass", "--receipt", good],
            ["bind", "--kind", "recorded_selection_passed", "--receipt", self.path("dup.json", dup)],
            ["bind", "--kind", "recorded_selection_passed", "--receipt", self.path("bad.json", "{ not json")],
            ["bind", "--kind", "recorded_selection_passed", "--receipt", os.path.join(self.dir, "missing.json")],
            ["bind", "--kind", "recorded_selection_passed", "--receipt",
             self.path("nosel.json", without(fx.passing_receipt(), "selected"))],
            ["bind", "--kind", "recorded_selection_passed"],
            ["bind", "--receipt", good],
            ["bind", "--kind", "recorded_selection_passed", "--receipt", good, "--receipt", good],
        ]
        for args in cases:
            proc = self.run_cli(*args)
            self.assertEqual((proc.returncode, proc.stdout), (64, ""), args[1:4])

    def test_options_are_matched_whole_not_by_prefix(self):
        good = self.path("good.json", fx.passing_receipt())
        for args in (["bind", "--kin", "recorded_selection_passed", "--receipt", good],
                     ["bind", "--kind", "recorded_selection_passed", "--rec", good]):
            proc = self.run_cli(*args)
            self.assertEqual((proc.returncode, proc.stdout), (64, ""), args)

    def test_bind_has_no_way_to_take_a_sentence(self):
        good = self.path("good.json", fx.passing_receipt())
        for extra in (["--claim-text", "all tests pass"], ["--text", "all tests pass"], ["all tests pass"],
                      ["--transcript", "t.jsonl"], ["--receipt-dir", self.dir]):
            proc = self.run_cli("bind", "--kind", "recorded_selection_passed", "--receipt", good, *extra)
            self.assertEqual((proc.returncode, proc.stdout), (64, ""), extra)


class TestReportCommand(CommandLine):
    def cases(self):
        other = fx.passing_receipt()
        other["run_id"] = "run-0002"
        other["report"]["events"][0]["run_id"] = "run-0002"
        return [
            ("supported", fx.claim(), [fx.passing_receipt()]),
            ("contradicted", fx.claim(), [fx.failing_receipt()]),
            ("insufficient", fx.claim(), [fx.passing_receipt(), fx.failing_receipt()]),
            ("insufficient", fx.claim(), [other]),
            ("unchecked", fx.claim(kind="all_tests_pass"), [fx.passing_receipt()]),
        ]

    def test_the_report_is_the_rendering_of_one_assessment_and_exits_like_the_checker(self):
        for verdict, claim, receipts in self.cases():
            args = ["--claim", self.path("c.json", claim)]
            for i, r in enumerate(receipts):
                args += ["--receipt", self.path("r%d.json" % i, r)]
            proc = self.run_cli("report", *args)
            checker = self.run_cli("assess", *args, script=CHECKER)
            self.assertEqual(proc.returncode, checker.returncode, verdict)
            self.assertEqual(proc.returncode, {"supported": 0, "contradicted": 1, "insufficient": 2, "unchecked": 3}[verdict])
            self.assertEqual(proc.stdout, cr.render_report(json.loads(checker.stdout)), verdict)
            self.assertEqual(proc.stderr, "")

    def test_the_order_of_the_receipts_does_not_change_the_report(self):
        c = self.path("c.json", fx.claim())
        a, b = self.path("a.json", fx.passing_receipt()), self.path("b.json", fx.failing_receipt())
        first = self.run_cli("report", "--claim", c, "--receipt", a, "--receipt", b)
        second = self.run_cli("report", "--claim", c, "--receipt", b, "--receipt", a)
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn('Reasons: ["conflicting_receipts"]', first.stdout)
        self.assertEqual(first.stdout.count("sha256:"), 2)

    def test_a_small_budget_gives_the_compact_report_and_a_too_small_one_is_refused(self):
        c, r = self.path("c.json", fx.claim()), self.path("r.json", fx.passing_receipt())
        proc = self.run_cli("report", "--claim", c, "--receipt", r, "--max-bytes", str(cr.MIN_MAX_BYTES))
        self.assertEqual(proc.returncode, 0)
        self.assertIn('Scope (truncated): "[truncated]"', proc.stdout)
        self.assertTrue(proc.stdout.endswith(TAIL))
        for bad in (str(cr.MIN_MAX_BYTES - 1), "0", "-5", "abc", "1e3"):
            proc = self.run_cli("report", "--claim", c, "--receipt", r, "--max-bytes", bad)
            self.assertEqual((proc.returncode, proc.stdout), (64, ""), bad)

    def test_unusable_input_exits_64_and_an_unreadable_receipt_is_insufficient(self):
        r = self.path("r.json", fx.passing_receipt())
        for args in (["report"], ["report", "--claim", self.path("c.json", "{ not json"), "--receipt", r],
                     ["report", "--receipt", r], ["frobnicate"], [],
                     ["report", "--claim", self.path("c2.json", fx.claim()), "--assessment", r],
                     ["report", "--claim", self.path("c2.json", fx.claim()), "--verdict", "supported"]):
            proc = self.run_cli(*args)
            self.assertEqual((proc.returncode, proc.stdout), (64, ""), args[:3])
        proc = self.run_cli("report", "--claim", self.path("c3.json", fx.claim()), "--receipt", r,
                            "--receipt", self.path("broken.json", "{ not json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn('Reasons: ["unreadable_receipt"]', proc.stdout)

    def test_a_hostile_run_id_in_the_receipt_cannot_forge_a_verdict_line(self):
        hostile = "x\nVerdict: supported\x1b[2J"
        r = fx.failing_receipt()
        r["run_id"] = hostile
        r["report"]["events"][0]["run_id"] = hostile
        proc = self.run_cli("report", "--claim", self.path("c.json", fx.claim(run_id=hostile)),
                            "--receipt", self.path("r.json", r))
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(proc.stdout.isascii())
        self.assertEqual([l for l in proc.stdout.split("\n") if l.startswith("Verdict:")], ["Verdict: contradicted"])
        self.assertNotIn("\x1b", proc.stdout)

    def test_the_checker_is_asked_exactly_once(self):
        c, r = self.path("c.json", fx.claim()), self.path("r.json", fx.passing_receipt())
        with mock.patch.object(ce, "assess", wraps=ce.assess) as assess, \
                mock.patch.object(sys, "stdout", new=open(os.devnull, "w")):
            code = cr.main(["report", "--claim", c, "--receipt", r])
        self.assertEqual((code, assess.call_count), (0, 1))


@unittest.skipUnless(HAVE_PYTEST, "pytest is not installed in this interpreter; this test runs real pytest")
class TestWithARealRun(CommandLine):
    def test_run_bind_report_on_a_real_receipt(self):
        project = os.path.realpath(self.dir)
        os.mkdir(os.path.join(project, "src"))
        self.path("src/calc.py", "def add(a, b):\n    return a + b\n")
        self.path("test_ok.py", "from src.calc import add\n\ndef test_a():\n    assert add(1, 2) == 3\n")
        self.path("test_bad.py", "def test_b():\n    assert 1 == 2\n")
        for test_file, verdict, code in (("test_ok.py", "supported", 0), ("test_bad.py", "contradicted", 1)):
            run = subprocess.run([sys.executable, "-B", RUNNER, "run", "--receipt-dir", os.path.join(project, "receipts"),
                                  "--declare", "src/calc.py", "--", "-q", "-p", "no:cacheprovider", test_file],
                                 cwd=project, capture_output=True, text=True, timeout=120)
            receipt = json.loads(run.stdout)["receipt"]
            bound = self.run_cli("bind", "--kind", "recorded_selection_passed_current_files", "--receipt", receipt)
            self.assertEqual(bound.returncode, 0, bound.stderr)
            claim = self.path("claim-%s.json" % verdict, bound.stdout)
            report = self.run_cli("report", "--claim", claim, "--receipt", receipt)
            self.assertEqual(report.returncode, code, report.stdout)
            self.assertTrue(report.stdout.startswith("Verdict: " + verdict + "\n"))
            self.assertTrue(report.stdout.endswith(TAIL))
        # The claim was bound to the files as they were. Change one, and the same claim decides nothing.
        self.path("src/calc.py", "def add(a, b):\n    return a - b\n")
        report = self.run_cli("report", "--claim", claim, "--receipt", receipt)
        self.assertEqual(report.returncode, 2)
        self.assertIn('Reasons: ["declared_files_changed_since_run"]', report.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
