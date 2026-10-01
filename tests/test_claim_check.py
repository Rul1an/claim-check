#!/usr/bin/env python3
"""Tests for the passive Stop hook.

The hook reads a transcript. A transcript holds tool requests, not results, so the
hook may say `insufficient` or `unchecked` and nothing stronger.

Run: python3 tests/test_claim_check.py
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import claim_check as cc  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "claim_check.py")


def assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def user(text):
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def tool_use(name, inp):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": inp}]},
    }


def tool_result(text, is_error=False):
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": text, "is_error": is_error}]},
    }


def run_hook(entries, env=None, session_id=None, raw_stdin=None):
    """Run the real hook script on a transcript. Returns (process, report text or "")."""
    e = dict(os.environ)
    e.pop("CLAIM_CHECK_ENFORCE", None)
    e.pop("CLAIM_CHECK_LOG", None)
    if env:
        e.update(env)
    path = None
    try:
        if raw_stdin is None:
            with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
                for entry in entries:
                    fh.write(json.dumps(entry) + "\n")
                path = fh.name
            payload = {"transcript_path": path}
            if session_id:
                payload["session_id"] = session_id
            raw_stdin = json.dumps(payload)
        proc = subprocess.run([sys.executable, SCRIPT], input=raw_stdin, capture_output=True, text=True, env=e)
    finally:
        if path:
            os.unlink(path)
    message = ""
    if proc.stdout.strip():
        try:
            message = json.loads(proc.stdout).get("systemMessage", "")
        except ValueError:
            message = proc.stdout
    return proc, message + proc.stderr


class TestTranscriptCannotDecide(unittest.TestCase):
    """Through the real entrypoint: no verdict stronger than the transcript can carry."""

    def assert_only(self, message, verdict):
        self.assertIn(verdict, message)
        for stronger in ("contradicted", "supported", "confirmed"):
            self.assertNotIn(stronger, message)

    def test_no_matching_request_is_insufficient_not_contradicted(self):
        proc, message = run_hook([tool_use("Edit", {"file_path": "src/a.py"}), assistant("All tests pass.")])
        self.assertEqual(proc.returncode, 0)
        self.assert_only(message, "insufficient")

    def test_a_runner_name_in_an_echo_is_not_a_test_run(self):
        proc, message = run_hook([tool_use("Bash", {"command": "echo pytest"}), assistant("All tests pass.")])
        self.assert_only(message, "insufficient")
        self.assertIn("A request is not a result", message)

    def test_a_request_whose_result_was_an_error_is_not_a_pass(self):
        _, message = run_hook([
            tool_use("Bash", {"command": "pytest -q"}),
            tool_result("3 failed, 1 passed", is_error=True),
            assistant("All tests pass."),
        ])
        self.assert_only(message, "insufficient")

    def test_a_later_successful_command_does_not_repair_a_failed_run(self):
        _, message = run_hook([
            tool_use("Bash", {"command": "pytest -q"}),
            tool_result("1 failed", is_error=True),
            tool_use("Bash", {"command": "ls"}),
            tool_result("a.py"),
            assistant("All tests pass."),
        ])
        self.assert_only(message, "insufficient")

    def test_an_observed_request_does_not_make_the_claim_silent(self):
        _, message = run_hook([tool_use("Bash", {"command": "pytest -q"}), assistant("I ran the tests.")])
        self.assert_only(message, "insufficient")
        self.assertIn("1 tool request", message)

    def test_commit_and_push_claims_are_unchecked(self):
        for entries in (
            [assistant("I committed the change and pushed it.")],
            [tool_use("Bash", {"command": "git -C /repo commit -m x && git push"}),
             assistant("I committed the change and pushed it.")],
        ):
            _, message = run_hook(entries)
            self.assert_only(message, "unchecked")
            self.assertNotIn("insufficient", message)

    def test_edit_claim_is_unchecked_with_or_without_a_request(self):
        for entries in (
            [tool_use("Edit", {"file_path": "/repo/src/other.py"}), assistant("I updated `src/config.py`.")],
            [tool_use("Edit", {"file_path": "/repo/src/config.py"}), assistant("I updated `src/config.py`.")],
            [tool_use("Bash", {"command": "ls"}), assistant("I created `notes.md`.")],
        ):
            _, message = run_hook(entries)
            self.assert_only(message, "unchecked")

    def test_untouched_claim_is_not_contradicted_by_a_same_named_file_elsewhere(self):
        _, message = run_hook([
            tool_use("Edit", {"file_path": "/repo/src/b/mod.rs"}),
            assistant("I did not touch `src/a/mod.rs`."),
        ])
        self.assert_only(message, "unchecked")
        self.assertIn("no editing-tool request", message)

    def test_untouched_claim_with_a_matching_edit_request_is_still_only_unchecked(self):
        """The request is reported as seen. Whether it changed the file is not in a transcript."""
        _, message = run_hook([
            tool_use("Edit", {"file_path": "/repo/src/secrets.py"}),
            tool_result("permission denied", is_error=True),
            assistant("I did not touch `src/secrets.py`."),
        ])
        self.assert_only(message, "unchecked")
        self.assertIn("1 editing-tool request", message)

    def test_prose_followed_by_a_user_turn_is_not_assessed(self):
        proc, message = run_hook([assistant("All tests pass."), user("thanks, now look at the parser")])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(message, "")

    def test_prose_followed_by_a_tool_request_is_not_assessed(self):
        for entries in (
            [assistant("All tests pass."), tool_use("Bash", {"command": "ls"})],
            [assistant("All tests pass."), tool_use("Bash", {"command": "ls"}), tool_result("a.py")],
            [{"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "All tests pass."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}],
        ):
            _, message = run_hook(entries)
            self.assertEqual(message, "")

    def test_saying_nothing_was_done_is_not_a_claim_that_it_was(self):
        for text in [
            "Nothing was edited, committed or pushed.",
            "Read-only: nothing edited, committed or pushed.",
            "So far nothing has been edited, committed, or pushed.",
            "As requested, nothing is staged, committed, or pushed.",
            "No changes have been staged, committed, or pushed.",
            "No files edited, committed or pushed.",
            "Neither edited, committed, nor pushed anything.",
            "Analysis only (nothing edited, committed, or pushed).",
            "I only read files; nothing changed, committed, or pushed.",
            "Status: nothing edited, committed, pushed.",
            "Not committed, pushed, or posted.",
            "This was read-only, and I pushed nothing.",
            "I only read the files, and committed no changes.",
            "Committed: nothing. Pushed: nothing.",
        ]:
            _, message = run_hook([assistant(text)])
            self.assertEqual(message, "", text)

    def test_enforce_variable_does_not_block(self):
        proc, message = run_hook([assistant("All tests pass. I committed.")], env={"CLAIM_CHECK_ENFORCE": "1"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        self.assert_only(message, "insufficient")

    def test_activity_outside_tool_requests_changes_nothing(self):
        """Hook-run commands, a compacted prefix, unknown or malformed tools: still insufficient."""
        shapes = [
            [{"type": "attachment", "attachment": {"type": "hook_success", "command": "pytest -q", "exitCode": 0}}],
            [{"type": "system", "subtype": "compact_boundary"}, user("summary: tests ran and passed")],
            [tool_use("mcp__ci__run", {"job": "tests"})],
            [tool_use("shell", {"command": ["bash", "-lc", "pytest -q"]})],
            [{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use"}]}}],
            [{"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": "pytest"}]}}],
        ]
        for prefix in shapes:
            proc, message = run_hook(prefix + [assistant("All tests pass.")])
            self.assertEqual(proc.returncode, 0, prefix)
            self.assert_only(message, "insufficient")

    def test_command_text_and_tool_input_reach_neither_report_nor_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "log.jsonl")
            _, message = run_hook(
                [tool_use("Bash", {"command": "API_KEY=canary-sk-live pytest -q --token=canary-t0p"}),
                 tool_use("mcp__x__y", {"secret": "canary-mcp"}),
                 tool_result("canary-output"),
                 assistant("All tests pass.")],
                env={"CLAIM_CHECK_LOG": log},
            )
            self.assertIn("insufficient", message)
            logged = open(log, encoding="utf-8").read()
            self.assertIn('"verdict": "insufficient"', logged)
            for text in (message, logged):
                self.assertNotIn("canary", text)


def assessments_for(entries):
    text, observed = cc.collect(entries)
    return cc.review(text, observed).assessments


def kinds(entries):
    text, _ = cc.collect(entries)
    return [c.kind for c in cc.extract(text)]


def verdicts(assessments):
    return sorted(a.verdict for a in assessments)


class TestExtraction(unittest.TestCase):
    """Heuristic and conservative: a missed claim is cheaper than an invented one."""

    def test_each_claim_kind_is_recognised(self):
        self.assertEqual(kinds([assistant("Fixed the bug. All tests pass and the feature is complete.")]), ["tests_pass"])
        self.assertEqual(kinds([assistant("I ran the tests.")]), ["tests_ran"])
        self.assertEqual(kinds([assistant("I committed the change.")]), ["committed"])
        self.assertEqual(kinds([assistant("Committed and pushed.")]), ["committed", "pushed"])
        self.assertEqual(kinds([assistant("I updated `src/config.py` with the new setting.")]), ["edited_file"])
        self.assertEqual(kinds([assistant("I did not touch `src/secrets.py`.")]), ["left_untouched"])

    def test_one_test_claim_per_message(self):
        self.assertEqual(kinds([assistant("All tests pass. The test suite passes. I ran the tests.")]), ["tests_pass"])

    def test_vague_progress_prose_is_not_a_claim(self):
        for text in [
            "I looked at the test setup and it seems reasonable.",
            "The tests directory contains a few files.",
            "Next step would be to run the tests.",
            "This should make the tests pass once you run them.",
            "You can run the tests to confirm they pass.",
            "If you run the suite the tests pass.",
            "I recommend you commit this.",
            "Try running the tests; they should be green now.",
        ]:
            self.assertEqual(kinds([assistant(text)]), [], f"should not extract: {text!r}")

    def test_hedged_sentence_does_not_mask_an_assertive_one(self):
        self.assertEqual(kinds([assistant("All tests pass. You can review the diff when you like.")]), ["tests_pass"])

    def test_markdown_structure_is_not_prose(self):
        for text in [
            "| E2E test of four CLI formats | Pass — all four verified |",
            "See the [updated](https://github.com/org/repo/commit/abc) notes.",
            "Reference: https://example.com/foo.py for context.",
            "```\nAll tests pass\n```",
        ]:
            self.assertEqual(kinds([assistant(text)]), [], f"should not extract: {text!r}")

    def test_quoted_example_in_inline_code_is_not_a_claim(self):
        self.assertEqual(kinds([assistant('And `"Committed and pushed."` has no subject before "pushed".')]), [])

    def test_prose_between_two_code_spans_is_kept(self):
        """Base paired the end of one span with the start of the next and lost the second claim."""
        self.assertEqual(kinds([assistant("I updated `src/a.py`. I did not touch `b.py`.")]),
                         ["edited_file", "left_untouched"])

    def test_negated_claim_is_not_a_claim(self):
        for text in ["I haven't committed.", "I did not run the tests.", "Tests were not run."]:
            self.assertEqual(kinds([assistant(text)]), [], text)

    def test_a_negative_word_after_the_claim_does_not_cancel_it(self):
        self.assertEqual(kinds([assistant("I committed and pushed, nothing left to do.")]), ["committed", "pushed"])
        self.assertEqual(kinds([assistant("All tests pass; no regressions were found.")]), ["tests_pass"])
        self.assertEqual(kinds([assistant("I committed the None check.")]), ["committed"])
        self.assertEqual(kinds([assistant("Nothing blocking. All tests pass.")]), ["tests_pass"])

    def test_a_negative_word_before_the_claim_drops_it_even_when_unrelated(self):
        """The bounded rule's known cost. Recorded so nobody reads it as an accident."""
        self.assertEqual(kinds([assistant("There was no reason to wait, so I committed and pushed.")]), [])

    def test_third_party_subject_is_not_the_agent(self):
        self.assertEqual(kinds([assistant("Aegis pushed again at 13:47Z with one commit.")]), [])

    def test_comma_continuation_is_a_claim(self):
        self.assertEqual(kinds([assistant("Both findings are closed on head `abc123`, pushed to the PR.")]), ["pushed"])

    def test_failure_report_is_not_a_pass_claim(self):
        self.assertEqual(kinds([assistant("All tests pass except one failure in the parser.")]), [])
        self.assertEqual(kinds([assistant("- **49 tests pass** (5 hermetic + 44 mutation), 0 fail")]), ["tests_pass"])

    def test_domain_is_not_a_file_path(self):
        self.assertEqual(kinds([assistant("I updated github.com entries in the list.")]), [])

    def test_unquoted_path_is_captured_whole(self):
        text, _ = cc.collect([assistant("I updated src/secrets.py as requested.")])
        self.assertEqual([c.path for c in cc.extract(text)], ["src/secrets.py"])

    def test_subagent_message_is_not_the_agents_claim(self):
        side = assistant("All tests pass.")
        side["isSidechain"] = True
        self.assertEqual(kinds([side, assistant("Investigation done.")]), [])

    def test_only_the_final_assistant_message_is_the_claim(self):
        self.assertEqual(kinds([
            assistant("All tests pass."),
            tool_use("Bash", {"command": "ls"}),
            assistant("Actually I could not run them."),
        ]), [])


class TestObservation(unittest.TestCase):
    """What is counted, and that it is only ever reported as a request."""

    def count(self, command, field):
        _, observed = cc.collect([tool_use("Bash", {"command": command})])
        return getattr(observed, field)

    def test_test_runner_names_are_counted_as_requests(self):
        for cmd in ["pytest -q", "cargo test --workspace", "npm test", "go test ./...", "make check",
                    "cd /repo && python3 tests/test_claim_check.py", "python -m unittest discover", "node --test"]:
            self.assertEqual(self.count(cmd, "test_requests"), 1, cmd)
        self.assertEqual(self.count("cat tests/test_foo.py", "test_requests"), 0)

    def test_commit_and_push_details_count_shell_requests_and_read_no_command_text(self):
        """`echo git commit` and a real commit give the same detail: the text is not interpreted."""
        details = []
        for cmd in ["git commit -m x && git push", "echo git commit", "ls"]:
            a = assessments_for([tool_use("Bash", {"command": cmd}), assistant("Committed and pushed.")])
            self.assertEqual(verdicts(a), ["unchecked", "unchecked"])
            details.append([x.detail for x in a])
        self.assertEqual(details[0], details[1])
        self.assertEqual(details[1], details[2])
        self.assertIn("1 shell-tool request was seen", details[0][0])

    def test_a_shell_request_is_counted_whatever_its_input_looks_like(self):
        for name, inp in [("shell", {"command": ["bash", "-lc", "x"]}), ("Bash", {"script": "x"}),
                          ("Bash", "x"), ("BashOutput", {"bash_id": "1"})]:
            entry = {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": name, "input": inp}]}}
            _, observed = cc.collect([entry])
            self.assertEqual((observed.shell_requests, observed.test_requests), (1, 0), name)

    def test_edit_requests_match_on_path_components_not_basenames(self):
        self.assertTrue(cc.path_matches("/repo/src/config.py", "src/config.py"))
        self.assertTrue(cc.path_matches("src/config.py", "./src/config.py"))
        self.assertFalse(cc.path_matches("/repo/src/b/mod.rs", "src/a/mod.rs"))
        self.assertFalse(cc.path_matches("/repo/xsrc/config.py", "src/config.py"))

    def test_details_state_requests_and_say_what_they_are_not(self):
        a = assessments_for([tool_use("Bash", {"command": "pytest -q"}), tool_use("Bash", {"command": "pytest -q"}),
                             assistant("All tests pass.")])
        self.assertEqual(verdicts(a), ["insufficient"])
        self.assertIn("2 tool requests", a[0].detail)
        a = assessments_for([assistant("All tests pass.")])
        self.assertIn("no tool request", a[0].detail)
        self.assertIn("does not show", a[0].detail)

    def test_passive_assessment_never_supports_or_contradicts(self):
        observed = cc.Observed(shell_requests=5, test_requests=3, edit_request_paths=["/r/a.py"])
        for kind, expected in [("tests_pass", "insufficient"), ("tests_ran", "insufficient"),
                               ("committed", "unchecked"), ("pushed", "unchecked"),
                               ("edited_file", "unchecked"), ("left_untouched", "unchecked"),
                               ("something_new", "unchecked")]:
            for obs in (observed, cc.Observed()):
                a = cc.assess_passive(cc.Claim(kind=kind, quote="q", path="a.py"), obs)
                self.assertEqual(a.verdict, expected, kind)

    def test_malformed_transcript_lines_are_skipped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(tool_use("Bash", {"command": "pytest"})) + "\n")
            fh.write("{ this is not json\n")
            fh.write(json.dumps(assistant("All tests pass.")) + "\n")
            path = fh.name
        try:
            text, observed = cc.collect(cc.read_transcript(path))
            self.assertIn("tests pass", text)
            self.assertEqual(observed.test_requests, 1)
        finally:
            os.unlink(path)

    def test_string_content_shape_is_tolerated(self):
        text, _ = cc.collect([{"type": "assistant", "message": {"role": "assistant", "content": "All tests pass."}}])
        self.assertIn("tests pass", text)


class TestResult(unittest.TestCase):
    def test_non_english_message_is_unchecked_not_clean(self):
        text = ("De hook werkt nu en ik heb de tests gedraaid, dus alles is groen. "
                "Verder heb ik de configuratie aangepast en het logbestand bekeken.")
        res = cc.review(text, cc.Observed())
        self.assertEqual(res.language, "other")
        self.assertIn("English-only", res.unverifiable[0])
        self.assertEqual(res.assessments, [])

    def test_claims_found_counts_what_was_assessed(self):
        res = cc.review("I ran the tests and they pass. I committed and pushed.", cc.Observed())
        self.assertEqual(res.claims_found, 3)
        self.assertEqual(len(res.assessments), 3)

    def test_no_claims_means_nothing_to_assess(self):
        res = cc.review("Here is a summary of the architecture and its layers.", cc.Observed())
        self.assertEqual(res.claims_found, 0)
        self.assertEqual(res.assessments, [])

    def test_version_carries_a_digest(self):
        self.assertRegex(cc.VERSION, r"^\d+\.\d+\.\d+\+[0-9a-f]{8}$")

    def test_script_version_matches_the_manifest(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            man = json.load(fh)
        self.assertTrue(cc.VERSION.startswith(man["version"] + "+"))


class TestProcessContract(unittest.TestCase):
    """The hook must never break a session, whatever it is handed."""

    def test_hostile_stdin_exits_zero_and_silent(self):
        for raw in ["", "not json at all", json.dumps({"session_id": "x"}),
                    json.dumps({"transcript_path": "/nonexistent/nope.jsonl"}), json.dumps([1, 2])]:
            proc, message = run_hook([], raw_stdin=raw)
            self.assertEqual(proc.returncode, 0, raw)
            self.assertEqual(message, "", raw)

    def test_stop_hook_active_is_a_noop(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(assistant("All tests pass.")) + "\n")
            path = fh.name
        try:
            proc, message = run_hook([], raw_stdin=json.dumps({"transcript_path": path, "stop_hook_active": True}))
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(message, "")
        finally:
            os.unlink(path)

    def test_report_uses_the_hook_envelope(self):
        """Bare stdout on exit 0 only shows in transcript view; the envelope reaches the user."""
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(assistant("I ran the suite and all of the tests pass.")) + "\n")
            path = fh.name
        try:
            proc = subprocess.run([sys.executable, SCRIPT], input=json.dumps({"transcript_path": path}),
                                  capture_output=True, text=True)
            env = json.loads(proc.stdout)
            self.assertTrue(env["continue"])
            self.assertIn("insufficient", env["systemMessage"])
            self.assertIn("note:", env["systemMessage"])
        finally:
            os.unlink(path)

    def test_language_limit_is_reported_once_per_session(self):
        dutch = ("De hook werkt nu en ik heb de tests gedraaid, dus alles is groen. "
                 "Verder heb ik de configuratie aangepast en het logbestand bekeken.")
        sid = "sess-%d-%s" % (os.getpid(), os.urandom(4).hex())
        _, first = run_hook([assistant(dutch)], session_id=sid)
        _, second = run_hook([assistant(dutch)], session_id=sid)
        self.assertIn("English", first)
        self.assertEqual(second, "")

    def test_session_without_a_claim_is_silent(self):
        proc, message = run_hook([tool_use("Bash", {"command": "pytest -q"}), assistant("Here is the summary.")])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(message, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
