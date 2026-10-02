#!/usr/bin/env python3
"""Tests for the pytest receipt runner, against real pytest subprocesses.

Each test writes a small project into a temporary directory, runs
scripts/pytest_evidence.py on it, and assesses the receipt it wrote. Expected node
ids and digests are literals worked out by hand, not read back from the runner.

Needs pytest in the interpreter that runs this file.
Run: python3 tests/test_pytest_evidence.py
"""

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import claim_evidence as ce  # noqa: E402

RUNNER = os.path.join(ROOT, "scripts", "pytest_evidence.py")
HAVE_PYTEST = importlib.util.find_spec("pytest") is not None

SOURCE = "def add(a, b):\n    return a + b\n"
PASSING = "from src.calc import add\n\ndef test_a():\n    assert add(1, 2) == 3\n\ndef test_b():\n    assert add(0, 0) == 0\n"


def sha(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "sha256:" + hashlib.sha256(data).hexdigest()


class TestReportReading(unittest.TestCase):
    """The parent's reading of the child's report file. Needs no pytest."""

    def read(self, text, limit=10**6):
        import pytest_evidence as pe
        with tempfile.NamedTemporaryFile("wb", suffix=".jsonl", delete=False) as fh:
            fh.write(text if isinstance(text, bytes) else text.encode("utf-8"))
            path = fh.name
        try:
            return pe._read_report(path, limit)
        finally:
            os.unlink(path)

    def test_a_line_with_a_repeated_key_is_malformed_and_its_last_value_is_not_kept(self):
        good = '{"event": "session_finish", "exitstatus": 0}'
        report = self.read('{"event": "session_finish", "exitstatus": 1, "exitstatus": 0}\n' + good + "\n")
        self.assertEqual(report, {"present": True, "truncated": False, "malformed_lines": 1,
                                  "events": [{"event": "session_finish", "exitstatus": 0}]})

    def test_lines_that_cannot_be_decoded_are_counted(self):
        report = self.read("{ not json\n" + "[" * 100000 + "]" * 100000 + '\n{"x": NaN}\n')
        self.assertEqual((report["malformed_lines"], report["events"]), (3, []))

    def test_a_line_with_a_damaged_byte_is_malformed_and_is_not_repaired(self):
        """Decoding with replacement turned a broken byte into U+FFFD and kept the line."""
        good = b'{"event": "session_finish", "exitstatus": 0}\n'
        bad = b'{"event": "session_start", "pytest_version": "9.\xff1"}\n'
        report = self.read(bad + good)
        self.assertEqual((report["malformed_lines"], report["events"]),
                         (1, [{"event": "session_finish", "exitstatus": 0}]))

    def test_a_line_nested_deeper_than_any_report_event_is_malformed(self):
        """1,500 levels decode on Python 3.12 and then break serialisation of the receipt."""
        for depth in (1500, 40):
            line = '{"event": "deselected", "nodeids": ' + "[" * depth + "]" * depth + "}\n"
            report = self.read(line)
            self.assertEqual((report["malformed_lines"], report["events"]), (1, []), depth)
            json.dumps(report)
        ok = self.read('{"event": "deselected", "nodeids": ["a", "b"]}\n')
        self.assertEqual(ok["malformed_lines"], 0)

    def test_a_missing_report_is_not_present(self):
        import pytest_evidence as pe
        self.assertEqual(pe._read_report("/nonexistent/report.jsonl", 10)["present"], False)


@unittest.skipUnless(HAVE_PYTEST, "pytest is not installed in this interpreter; these tests run real pytest")
class RunnerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = os.path.realpath(self._tmp.name)
        self.receipts = os.path.join(self.cwd, "receipts")
        self.write("src/__init__.py", "")
        self.write("src/calc.py", SOURCE)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text):
        path = os.path.join(self.cwd, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_runner(self, pytest_args, declare=("src/calc.py",), extra=(), env=None):
        """Returns (process, summary dict or None, receipt dict or None)."""
        args = [sys.executable, RUNNER, "run", "--receipt-dir", self.receipts]
        for d in declare:
            args += ["--declare", d]
        args += list(extra) + ["--"] + ["-p", "no:cacheprovider"] + list(pytest_args)
        e = dict(os.environ)
        e.pop("PYTEST_ADDOPTS", None)
        if env:
            e.update(env)
        proc = subprocess.run(args, cwd=self.cwd, capture_output=True, text=True, env=e, timeout=120)
        summary = receipt = None
        if proc.stdout.strip():
            summary = json.loads(proc.stdout)
            with open(summary["receipt"], encoding="utf-8") as fh:
                receipt = json.load(fh)
        return proc, summary, receipt

    def assess(self, summary, receipt, kind="recorded_selection_passed"):
        claim = {"kind": kind, "run_id": summary["run_id"], "selection_digest": summary["selection_digest"],
                 "declared_files_digest": summary["declared_files_digest"]}
        return ce.assess(claim, [receipt])

    def verdict(self, pytest_args, **kw):
        proc, summary, receipt = self.run_runner(pytest_args, **kw)
        out = self.assess(summary, receipt)
        return proc, out["verdict"], out["reasons"], receipt


class TestOutcomes(RunnerCase):
    def test_passing_selection_is_supported_and_bound_to_its_node_ids_and_file(self):
        self.write("test_ok.py", PASSING)
        proc, summary, receipt = self.run_runner(["-q", "test_ok.py"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(summary["selection_digest"], sha(["test_ok.py::test_a", "test_ok.py::test_b"]))
        file_hash = hashlib.sha256(SOURCE.encode()).hexdigest()
        self.assertEqual(summary["declared_files_digest"], sha([["src/calc.py", file_hash, len(SOURCE)]]))
        self.assertEqual(receipt["cwd"], self.cwd)
        self.assertEqual(receipt["process"]["exit_code"], 0)
        out = self.assess(summary, receipt)
        self.assertEqual((out["verdict"], out["reasons"]), ("supported", ["all_selected_items_passed"]))
        self.assertEqual(out["scope"]["selected_count"], 2)

    def test_a_narrower_selection_has_a_different_identity(self):
        self.write("test_ok.py", PASSING)
        _, wide, wide_receipt = self.run_runner(["-q", "test_ok.py"])
        _, narrow, narrow_receipt = self.run_runner(["-q", "test_ok.py::test_a"])
        self.assertEqual(narrow["selection_digest"], sha(["test_ok.py::test_a"]))
        claim_for_wide = {"kind": "recorded_selection_passed", "run_id": narrow["run_id"],
                          "selection_digest": wide["selection_digest"],
                          "declared_files_digest": wide["declared_files_digest"]}
        out = ce.assess(claim_for_wide, [narrow_receipt])
        self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["selection_mismatch"]))
        # And a receipt of one run says nothing about another run.
        out = ce.assess(dict(claim_for_wide, run_id=wide["run_id"]), [narrow_receipt])
        self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["no_receipt_for_run"]))
        self.assertNotEqual(wide["run_id"], narrow["run_id"])
        self.assertEqual(self.assess(wide, wide_receipt)["verdict"], "supported")

    def test_assertion_failure_is_contradicted(self):
        self.write("test_bad.py", "def test_ok():\n    assert True\n\ndef test_bad():\n    assert 1 == 2\n")
        proc, verdict, reasons, _ = self.verdict(["-q", "test_bad.py"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual((verdict, reasons), ("contradicted", ["selected_item_failed"]))

    def test_teardown_failure_is_contradicted(self):
        self.write("test_td.py", "import pytest\n\n@pytest.fixture\ndef res():\n    yield 1\n    raise RuntimeError('td')\n\n"
                                 "def test_uses(res):\n    assert res == 1\n")
        proc, verdict, reasons, _ = self.verdict(["-q", "test_td.py"])
        self.assertEqual(proc.returncode, 1)
        self.assertEqual((verdict, reasons), ("contradicted", ["selected_item_failed"]))

    def test_collection_error_is_insufficient(self):
        self.write("test_ok.py", PASSING)
        self.write("test_broken.py", "def test_x(:\n    pass\n")
        proc, verdict, reasons, _ = self.verdict(["-q"])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(verdict, "insufficient")
        self.assertIn("collection_error", reasons)

    def test_zero_tests_is_insufficient(self):
        self.write("test_ok.py", PASSING)
        for args in (["-q", "test_ok.py", "-k", "matches_nothing"], ["-q", "src"]):
            proc, verdict, reasons, _ = self.verdict(args)
            self.assertEqual(proc.returncode, 5, args)
            self.assertEqual((verdict, reasons), ("insufficient", ["no_tests_collected"]), args)

    def test_skip_is_insufficient(self):
        self.write("test_skip.py", "import pytest\n\ndef test_ok():\n    assert True\n\n"
                                   "@pytest.mark.skip(reason='later')\ndef test_later():\n    assert False\n")
        proc, verdict, reasons, _ = self.verdict(["-q", "test_skip.py"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual((verdict, reasons), ("insufficient", ["skipped"]))

    def test_xfail_and_xpass_are_insufficient(self):
        self.write("test_xf.py", "import pytest\n\n@pytest.mark.xfail\ndef test_known():\n    assert 1 == 2\n")
        self.write("test_xp.py", "import pytest\n\n@pytest.mark.xfail\ndef test_surprise():\n    assert True\n")
        for name in ("test_xf.py", "test_xp.py"):
            proc, verdict, reasons, _ = self.verdict(["-q", name])
            self.assertEqual(proc.returncode, 0, name)
            self.assertEqual((verdict, reasons), ("insufficient", ["xfail_or_xpass"]), name)

    def test_timeout_kills_the_run_and_is_insufficient(self):
        self.write("test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(60)\n")
        start = time.time()
        proc, verdict, reasons, receipt = self.verdict(["-q", "test_slow.py"], extra=["--timeout", "2"])
        self.assertLess(time.time() - start, 30)
        self.assertEqual(proc.returncode, 124)
        self.assertEqual(verdict, "insufficient")
        self.assertIn("timed_out", reasons)
        self.assertEqual((receipt["process"]["timed_out"], receipt["process"]["completed"],
                          receipt["process"]["exit_code"]), (True, False, None))


class TestCompletionComesFromTheParent(RunnerCase):
    """The session-finish line is the child's account. The exit code is the parent's."""

    def test_a_passing_session_whose_process_then_exits_nonzero_is_not_supported(self):
        self.write("test_ok.py", PASSING)
        self.write("conftest.py", "import os\n\ndef pytest_unconfigure(config):\n    os._exit(3)\n")
        proc, verdict, reasons, receipt = self.verdict(["-q", "test_ok.py"])
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(receipt["report"]["events"][-1], {"event": "session_finish", "exitstatus": 0})
        self.assertEqual((verdict, reasons), ("insufficient", ["exit_status_mismatch"]))

    def test_a_failing_session_whose_process_then_exits_zero_is_not_contradicted(self):
        self.write("test_bad.py", "def test_bad():\n    assert 1 == 2\n")
        self.write("conftest.py", "import os\n\ndef pytest_unconfigure(config):\n    os._exit(0)\n")
        proc, verdict, reasons, _ = self.verdict(["-q", "test_bad.py"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual((verdict, reasons), ("insufficient", ["exit_status_mismatch"]))

    def test_a_process_that_dies_before_the_session_finishes_is_insufficient(self):
        self.write("test_die.py", "import os\n\ndef test_die():\n    os._exit(0)\n")
        proc, verdict, reasons, _ = self.verdict(["-q", "test_die.py"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(verdict, "insufficient")
        self.assertIn("report_not_finished", reasons)


class TestHostileReportLines(RunnerCase):
    """Lines the capture plugin would never write, appended to the report by code in the child."""

    def run_with_extra_line(self, line_expr):
        self.write("test_ok.py", PASSING)
        self.write("conftest.py", "import os\n\ndef pytest_sessionstart(session):\n"
                                  "    with open(os.environ['CLAIM_CHECK_REPORT_PATH'], 'ab') as fh:\n"
                                  "        fh.write(" + line_expr + ")\n")
        proc, summary, receipt = self.run_runner(["-q", "test_ok.py"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNotNone(summary, "the runner must still write a receipt and its summary line")
        out = self.assess(summary, receipt)
        return receipt, out

    def test_a_deeply_nested_line_still_yields_a_receipt_and_is_insufficient(self):
        receipt, out = self.run_with_extra_line("b'{\"event\": \"deselected\", \"nodeids\": ' + b'[' * 1500 + b']' * 1500 + b'}\\n'")
        self.assertEqual(receipt["report"]["malformed_lines"], 1)
        self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["report_malformed_lines"]))

    def test_a_line_with_a_damaged_byte_is_insufficient(self):
        receipt, out = self.run_with_extra_line("b'{\"event\": \"deselected\", \"nodeids\": [\"a\\xffb\"]}\\n'")
        self.assertEqual(receipt["report"]["malformed_lines"], 1)
        self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["report_malformed_lines"]))


class TestDeclaredFiles(RunnerCase):
    def test_a_file_changed_after_the_run_only_affects_the_current_files_claim(self):
        self.write("test_ok.py", PASSING)
        _, summary, receipt = self.run_runner(["-q", "test_ok.py"])
        current = "recorded_selection_passed_current_files"
        self.assertEqual(self.assess(summary, receipt, current)["verdict"], "supported")
        self.write("src/calc.py", SOURCE + "# edited\n")
        out = self.assess(summary, receipt, current)
        self.assertEqual((out["verdict"], out["reasons"]), ("insufficient", ["declared_files_changed_since_run"]))
        self.assertEqual(self.assess(summary, receipt)["verdict"], "supported")

    def test_a_file_changed_during_the_run_is_insufficient(self):
        self.write("test_edit.py", "def test_edits_source():\n    open('src/calc.py', 'a').write('# changed by the test\\n')\n")
        proc, verdict, reasons, _ = self.verdict(["-q", "test_edit.py"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual((verdict, reasons), ("insufficient", ["declared_files_changed_during_run"]))

    def test_a_file_removed_during_the_run_is_insufficient(self):
        self.write("test_rm.py", "import os\n\ndef test_removes_source():\n    os.unlink('src/calc.py')\n")
        proc, verdict, reasons, _ = self.verdict(["-q", "test_rm.py"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual((verdict, reasons), ("insufficient", ["declared_files_changed_during_run"]))

    def test_unsafe_or_missing_scope_is_refused_before_pytest_starts(self):
        self.write("test_marker.py", "def test_leaves_a_marker():\n    open('ran.marker', 'w').close()\n")
        os.symlink(os.path.join(self.cwd, "src", "calc.py"), os.path.join(self.cwd, "link.py"))
        for declare in ((), ("src/missing.py",), ("link.py",), ("../outside.py",), ("src",),
                        (os.path.join(self.cwd, "src", "calc.py"),), ("src/calc.py", "src/calc.py")):
            proc, summary, _ = self.run_runner(["-q", "test_marker.py"], declare=declare)
            self.assertEqual(proc.returncode, 64, declare)
            self.assertIsNone(summary, declare)
            self.assertFalse(os.path.exists(os.path.join(self.cwd, "ran.marker")), declare)
            self.assertEqual([f for f in os.listdir(self.receipts)] if os.path.isdir(self.receipts) else [], [], declare)


class _FaultyWriter:
    """Wraps the real file object the runner writes the receipt through, and fails once."""

    def __init__(self, real, fail):
        self.real, self.fail = real, fail

    def write(self, text):
        if self.fail == "write":
            self.real.write(text[: len(text) // 2])
            self.real.flush()
            raise OSError(28, "No space left on device")
        return self.real.write(text)

    def flush(self):
        return self.real.flush()

    def fileno(self):
        return self.real.fileno()

    def close(self):
        self.real.close()
        if self.fail == "close":
            raise OSError(5, "Input/output error")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class TestReceiptPublication(RunnerCase):
    """The final name `<run_id>.json` must never show a receipt that was not fully written.

    Runs the runner's own `run()` in this process, with a real pytest child, and injects
    one failure into the write of the receipt.
    """

    def run_in_process(self, *patches):
        import contextlib
        import io
        from unittest import mock
        import pytest_evidence as pe
        self.write("test_ok.py", PASSING)
        out, old = io.StringIO(), os.getcwd()
        os.chdir(self.cwd)
        try:
            with contextlib.ExitStack() as stack:
                for target, name, kwargs in patches:
                    stack.enter_context(mock.patch.object(getattr(pe, target), name, **kwargs))
                stack.enter_context(contextlib.redirect_stdout(out))
                code = pe.run(self.receipts, ["src/calc.py"], ["-q", "-p", "no:cacheprovider", "test_ok.py"],
                              60.0, 0, 10 ** 6)
        finally:
            os.chdir(old)
        return code, out.getvalue()

    def entries(self):
        return sorted(os.listdir(self.receipts)) if os.path.isdir(self.receipts) else []

    def faulty(self, fail):
        real = os.fdopen

        def fdopen(fd, mode="r", *a, **k):
            handle = real(fd, mode, *a, **k)
            # Only the receipt is opened for writing; declared files are opened for reading.
            return _FaultyWriter(handle, fail) if "w" in mode else handle
        return ("os", "fdopen", {"side_effect": fdopen})

    def test_a_write_that_stops_halfway_leaves_no_receipt_under_any_name(self):
        code, out = self.run_in_process(self.faulty("write"))
        self.assertEqual((code, out, self.entries()), (70, "", []))

    def test_a_failing_close_leaves_no_receipt_under_any_name(self):
        code, out = self.run_in_process(self.faulty("close"))
        self.assertEqual((code, out, self.entries()), (70, "", []))

    def test_a_failing_fsync_leaves_no_receipt_under_any_name(self):
        code, out = self.run_in_process(("os", "fsync", {"side_effect": OSError(5, "Input/output error")}))
        self.assertEqual((code, out, self.entries()), (70, "", []))

    def test_an_existing_receipt_with_the_same_name_is_not_overwritten(self):
        run_id = "a" * 32
        os.mkdir(self.receipts, 0o700)
        existing = os.path.join(self.receipts, run_id + ".json")
        with open(existing, "wb") as fh:
            fh.write(b"older receipt bytes")
        code, out = self.run_in_process(("secrets", "token_hex", {"return_value": run_id}))
        self.assertEqual((code, out, self.entries()), (70, "", [run_id + ".json"]))
        with open(existing, "rb") as fh:
            self.assertEqual(fh.read(), b"older receipt bytes")

    def test_without_a_fault_the_same_path_publishes_one_complete_private_receipt(self):
        code, out = self.run_in_process()
        summary = json.loads(out)
        self.assertEqual((code, self.entries()), (0, [summary["run_id"] + ".json"]))
        self.assertEqual(stat.S_IMODE(os.stat(summary["receipt"]).st_mode), 0o600)
        with open(summary["receipt"], encoding="utf-8") as fh:
            self.assertEqual(ce.loads_strict(fh.read())["run_id"], summary["run_id"])


class TestReceiptFile(RunnerCase):
    def test_receipt_is_private_and_holds_no_output_and_no_environment_values(self):
        self.write("test_loud.py", "def test_loud():\n    print('canary-output')\n    assert True\n")
        proc, summary, receipt = self.run_runner(["-q", "-s", "test_loud.py"], env={"CANARY_SECRET": "canary-env-value",
                                                                                 "PYTEST_ADDOPTS": "-p no:randomly"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("canary-output", proc.stderr)       # forwarded to the terminal
        self.assertEqual(stat.S_IMODE(os.stat(summary["receipt"]).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.receipts).st_mode), 0o700)
        with open(summary["receipt"], encoding="utf-8") as fh:
            raw = fh.read()
        for canary in ("canary-output", "canary-env-value", "no:randomly"):
            self.assertNotIn(canary, raw)
        self.assertEqual(receipt["env_names_present"], ["PYTEST_ADDOPTS"])
        self.assertEqual(os.listdir(self.receipts), [summary["run_id"] + ".json"])

    def test_forwarded_output_is_bounded_while_the_child_is_still_drained(self):
        self.write("test_flood.py", "def test_flood():\n    print('x' * 3000000)\n")
        proc, summary, receipt = self.run_runner(["-q", "-s", "test_flood.py"], extra=["--max-output-bytes", "1000"])
        self.assertEqual(proc.returncode, 0)
        self.assertLessEqual(len(proc.stderr), 1000)
        self.assertGreaterEqual(receipt["output"]["stdout_bytes"], 3000000)
        self.assertEqual((receipt["output"]["truncated"], receipt["output"]["forwarded_limit"]), (True, 1000))
        self.assertEqual(self.assess(summary, receipt)["verdict"], "supported")

    def test_a_report_larger_than_the_limit_is_not_read_as_complete(self):
        self.write("test_ok.py", PASSING)
        proc, verdict, reasons, receipt = self.verdict(["-q", "test_ok.py"], extra=["--max-report-bytes", "300"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual((receipt["report"]["truncated"], receipt["report"]["events"]), (True, []))
        self.assertEqual(verdict, "insufficient")
        self.assertIn("report_truncated", reasons)

    def test_an_edited_copy_beside_the_original_is_a_conflict(self):
        self.write("test_bad.py", "def test_bad():\n    assert 1 == 2\n")
        _, summary, receipt = self.run_runner(["-q", "test_bad.py"])
        edited = json.loads(json.dumps(receipt))
        edited["process"]["exit_code"] = 0
        claim = {"kind": "recorded_selection_passed", "run_id": summary["run_id"],
                 "selection_digest": summary["selection_digest"],
                 "declared_files_digest": summary["declared_files_digest"]}
        self.assertEqual(ce.assess(claim, [receipt, edited])["reasons"], ["conflicting_receipts"])
        self.assertEqual(ce.assess(claim, [edited])["reasons"], ["exit_status_mismatch"])
        self.assertEqual(ce.assess(claim, [receipt])["verdict"], "contradicted")

    def test_a_timeout_that_is_not_a_finite_number_is_refused_before_pytest_starts(self):
        """`inf` ran pytest with no limit and wrote `Infinity`, which the checker cannot read."""
        self.write("test_marker.py", "def test_leaves_a_marker():\n    open('ran.marker', 'w').close()\n")
        for value in ("inf", "Infinity", "-inf", "nan", "NaN"):
            proc, summary, _ = self.run_runner(["-q", "test_marker.py"], extra=["--timeout", value])
            self.assertEqual((proc.returncode, summary), (64, None), value)
            self.assertFalse(os.path.exists(os.path.join(self.cwd, "ran.marker")), value)
            self.assertFalse(os.path.exists(self.receipts), value)

    def test_a_receipt_that_would_hold_a_non_finite_number_is_not_written(self):
        """Second line of defence: called past the argument check, the runner refuses to
        serialise `Infinity` and leaves no receipt."""
        self.write("test_ok.py", PASSING)
        code = ("import sys; sys.path.insert(0, %r); import pytest_evidence as pe; "
                "sys.exit(pe.run(%r, ['src/calc.py'], ['-q', '-p', 'no:cacheprovider', 'test_ok.py'], "
                "float('inf'), 1000, 10**6))" % (os.path.join(ROOT, "scripts"), self.receipts))
        proc = subprocess.run([sys.executable, "-B", "-c", code], cwd=self.cwd, capture_output=True, text=True, timeout=120)
        self.assertEqual((proc.returncode, proc.stdout), (70, ""), proc.stderr[-300:])
        self.assertEqual([f for f in os.listdir(self.receipts) if not f.startswith(".")], [])

    def test_usage_errors_exit_64_without_a_receipt(self):
        self.write("test_ok.py", PASSING)
        for extra in (["--timeout", "0"], ["--timeout", "abc"], ["--max-output-bytes", "-1"]):
            proc, summary, _ = self.run_runner(["-q", "test_ok.py"], extra=extra)
            self.assertEqual((proc.returncode, summary), (64, None), extra)


if __name__ == "__main__":
    unittest.main(verbosity=2)
