#!/usr/bin/env python3
"""Tests for claim-check, including the negative controls that give it teeth.

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


def tool_use(name, inp):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": inp}]},
    }


def collect(entries):
    return cc.collect(entries)


def findings_for(entries):
    text, observed = collect(entries)
    return cc.check(text, observed)


def verdicts(fs):
    return sorted(f.verdict for f in fs)


class TestClaims(unittest.TestCase):
    # --- the headline case -------------------------------------------------

    def test_tests_claimed_but_never_run_is_contradicted(self):
        fs = findings_for([
            tool_use("Edit", {"file_path": "src/a.py"}),
            assistant("Fixed the bug. All tests pass and the feature is complete."),
        ])
        self.assertEqual(verdicts(fs), ["contradicted"])
        self.assertIn("no test command", fs[0].detail)

    def test_tests_claimed_and_run_is_silent(self):
        fs = findings_for([
            tool_use("Bash", {"command": "pytest -q"}),
            assistant("All tests pass."),
        ])
        self.assertEqual(fs, [])

    def test_various_test_runners_are_recognised(self):
        for cmd in [
            "cargo test --workspace",
            "npm test",
            "pnpm run test",
            "go test ./...",
            "npx vitest run",
            "make check",
            "gradlew test",
        ]:
            fs = findings_for([tool_use("Bash", {"command": cmd}), assistant("Tests are green.")])
            self.assertEqual(fs, [], f"{cmd!r} should count as running tests")

    # --- git ---------------------------------------------------------------

    def test_commit_claimed_without_commit(self):
        fs = findings_for([assistant("I committed the change.")])
        self.assertTrue(any("commit" in f.detail for f in fs))

    def test_commit_claimed_with_commit_is_silent(self):
        fs = findings_for([
            tool_use("Bash", {"command": "git commit -m 'x'"}),
            assistant("I committed the change."),
        ])
        self.assertEqual(fs, [])

    def test_push_claimed_without_push(self):
        fs = findings_for([
            tool_use("Bash", {"command": "git commit -m 'x'"}),
            assistant("Committed and pushed."),
        ])
        self.assertEqual(len(fs), 1)
        self.assertIn("push", fs[0].detail)

    # --- file edits --------------------------------------------------------

    def test_edit_claimed_and_observed_is_silent(self):
        fs = findings_for([
            tool_use("Write", {"file_path": "/repo/src/config.py"}),
            assistant("I updated `src/config.py` with the new setting."),
        ])
        self.assertEqual(fs, [])

    def test_edit_claimed_but_other_file_edited_is_contradicted(self):
        fs = findings_for([
            tool_use("Edit", {"file_path": "/repo/src/other.py"}),
            assistant("I updated `src/config.py`."),
        ])
        self.assertEqual(verdicts(fs), ["contradicted"])

    def test_edit_claimed_with_no_edit_tool_is_unobservable_not_contradicted(self):
        """The honesty rule: with no edit tool seen at all we cannot conclude a defect."""
        fs = findings_for([
            tool_use("Bash", {"command": "ls"}),
            assistant("I created `notes.md`."),
        ])
        self.assertEqual(verdicts(fs), ["unobservable"])

    def test_shell_written_file_is_not_flagged(self):
        fs = findings_for([
            tool_use("Bash", {"command": "echo hi > notes.md"}),
            assistant("I created `notes.md`."),
        ])
        self.assertEqual(fs, [])

    def test_untouched_claim_contradicted_when_edited(self):
        fs = findings_for([
            tool_use("Edit", {"file_path": "/repo/src/secrets.py"}),
            assistant("I did not touch `src/secrets.py`."),
        ])
        self.assertEqual(verdicts(fs), ["contradicted"])

    # --- conservatism: false accusations are the expensive failure ----------

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
            fs = findings_for([assistant(text)])
            self.assertEqual(fs, [], f"should not flag: {text!r}")

    def test_hedged_sentence_does_not_mask_an_assertive_one(self):
        """Filtering is per sentence, so a real claim beside a hedge still counts."""
        fs = findings_for([assistant("All tests pass. You can review the diff when you like.")])
        self.assertEqual(verdicts(fs), ["contradicted"])

    def test_markdown_structure_is_not_prose(self):
        """Regression from real transcripts: tables, links and URLs read as claims."""
        for text in [
            "| E2E test of four CLI formats | Pass — all four verified |",
            "See the [updated](https://github.com/org/repo/commit/abc) notes.",
            "Reference: https://example.com/foo.py for context.",
            "```\nAll tests pass\n```",
        ]:
            fs = findings_for([assistant(text)])
            self.assertEqual(fs, [], f"should not flag: {text!r}")

    def test_quoted_example_in_inline_code_is_not_a_claim(self):
        """Regression: a message discussing this plugin tripped its own push check."""
        fs = findings_for([assistant('And `"Committed and pushed."` has no subject before "pushed".')])
        self.assertEqual(fs, [])

    def test_negated_claim_is_never_reported_as_missing(self):
        """Regression: 'I haven't committed' was flagged as a missing commit."""
        for text in ["I haven't committed.", "I did not run the tests.", "Tests were not run."]:
            fs = findings_for([assistant(text)])
            self.assertEqual(fs, [], f"should not flag: {text!r}")

    def test_third_party_subject_is_not_the_agent(self):
        """Regression: 'Aegis pushed again' was read as the agent claiming a push."""
        fs = findings_for([assistant("Aegis pushed again at 13:47Z with one commit.")])
        self.assertEqual(fs, [])

    def test_failure_context_is_not_a_pass_claim(self):
        fs = findings_for([
            assistant("The only failure was test_x, which passes in isolation.")
        ])
        self.assertEqual(fs, [])

    def test_domain_is_not_a_file_path(self):
        fs = findings_for([
            tool_use("Edit", {"file_path": "/repo/a.py"}),
            assistant("I updated github.com entries in the list."),
        ])
        self.assertEqual(fs, [])

    def test_subagent_message_is_not_the_agents_claim(self):
        side = assistant("All tests pass.")
        side["isSidechain"] = True
        fs = findings_for([side, assistant("Investigation done.")])
        self.assertEqual(fs, [])


    def test_zero_failures_is_a_pass_claim(self):
        """Regression from real data: '49 tests pass, 0 fail' is a claim, not a failure report."""
        fs = findings_for([assistant("- **49 tests pass** (5 hermetic + 44 mutation), 0 fail")])
        self.assertEqual(verdicts(fs), ["contradicted"])


    def test_direct_test_file_invocation_counts_as_running_tests(self):
        """Found by running the hook live: this project runs tests this way."""
        for cmd in [
            "cd /repo && python3 tests/test_claim_check.py",
            "python -m unittest discover",
            "python3 -m pytest -q",
            "node --test",
            "bun test",
            "node src/foo.test.js",
        ]:
            fs = findings_for([tool_use("Bash", {"command": cmd}), assistant("All tests pass.")])
            self.assertEqual(fs, [], f"{cmd!r} should count as running tests")

    def test_reading_a_test_file_is_not_running_it(self):
        fs = findings_for([
            tool_use("Bash", {"command": "cat tests/test_foo.py"}),
            assistant("All tests pass."),
        ])
        self.assertEqual(verdicts(fs), ["contradicted"])


    # --- coverage denominator and language (added after the live run) --------

    def test_non_english_message_is_unchecked_not_clean(self):
        """The live run stayed silent on a Dutch session; silence read as 'clean'."""
        text = ("De hook werkt nu en ik heb de tests gedraaid, dus alles is groen. "
                "Verder heb ik de configuratie aangepast en het logbestand bekeken.")
        res = cc.review(text, cc.Observed())
        self.assertEqual(res.language, "other")
        self.assertTrue(res.unverifiable)
        self.assertIn("English-only", res.unverifiable[0])
        self.assertEqual(res.findings, [])

    def test_english_message_is_checked(self):
        res = cc.review("I ran the whole suite and all of the tests pass now.", cc.Observed())
        self.assertEqual(res.language, "en")
        self.assertEqual(res.unverifiable, [])
        self.assertEqual(verdicts(res.findings), ["contradicted"])

    def test_claims_found_is_the_denominator(self):
        obs = cc.Observed(commands=["pytest -q", "git commit -m x", "git push"])
        res = cc.review("I ran the tests and they pass. I committed and pushed.", obs)
        self.assertGreaterEqual(res.claims_found, 2)
        self.assertEqual(res.findings, [])          # all true
        self.assertTrue(res.checkable)              # and we can say so

    def test_no_claims_is_not_reported_as_clean(self):
        res = cc.review("Here is a summary of the architecture and its layers.", cc.Observed())
        self.assertEqual(res.claims_found, 0)
        self.assertFalse(res.checkable)

    def test_comma_continuation_is_a_claim(self):
        """Real session: '...closed on head `abc`, pushed to PR #1944' was missed."""
        fs = findings_for([assistant("Both findings are closed on head `abc123`, pushed to the PR.")])
        self.assertTrue(any("push" in f.detail for f in fs))

    def test_third_party_without_comma_still_not_a_claim(self):
        fs = findings_for([assistant("Aegis pushed again at 13:47Z with one commit.")])
        self.assertEqual(fs, [])

    def test_version_carries_a_digest(self):
        self.assertRegex(cc.VERSION, r"^\d+\.\d+\.\d+\+[0-9a-f]{8}$")

    def test_unquoted_path_is_captured_whole(self):
        """Regression: a greedy filler captured a suffix ('s.py') and matched nothing."""
        fs = findings_for([
            tool_use("Edit", {"file_path": "/repo/src/secrets.py"}),
            assistant("I updated src/secrets.py as requested."),
        ])
        self.assertEqual(fs, [])

    def test_only_the_final_assistant_message_is_the_claim(self):
        fs = findings_for([
            assistant("All tests pass."),           # earlier, superseded
            tool_use("Bash", {"command": "ls"}),
            assistant("Actually I could not run them."),
        ])
        self.assertEqual(fs, [])

    # --- transcript robustness --------------------------------------------

    def test_malformed_transcript_lines_are_skipped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(tool_use("Bash", {"command": "pytest"})) + "\n")
            fh.write("{ this is not json\n")
            fh.write(json.dumps(assistant("All tests pass.")) + "\n")
            path = fh.name
        try:
            text, observed = cc.collect(cc.read_transcript(path))
            self.assertIn("tests pass", text)
            self.assertEqual(len(observed.commands), 1)
        finally:
            os.unlink(path)

    def test_string_content_shape_is_tolerated(self):
        entries = [{"type": "assistant", "message": {"role": "assistant", "content": "All tests pass."}}]
        text, _ = collect(entries)
        self.assertIn("tests pass", text)


class TestProcessContract(unittest.TestCase):
    """The hook must never break a session, whatever it is handed."""

    def _run(self, stdin_text, env=None):
        e = dict(os.environ)
        e.pop("CLAIM_CHECK_ENFORCE", None)
        if env:
            e.update(env)
        return subprocess.run(
            [sys.executable, SCRIPT], input=stdin_text, capture_output=True, text=True, env=e
        )

    def test_empty_stdin_exits_zero(self):
        self.assertEqual(self._run("").returncode, 0)

    def test_garbage_stdin_exits_zero(self):
        self.assertEqual(self._run("not json at all").returncode, 0)

    def test_missing_transcript_path_exits_zero(self):
        self.assertEqual(self._run(json.dumps({"session_id": "x"})).returncode, 0)

    def test_nonexistent_transcript_exits_zero(self):
        payload = {"transcript_path": "/nonexistent/nope.jsonl"}
        self.assertEqual(self._run(json.dumps(payload)).returncode, 0)

    def test_stop_hook_active_is_a_noop(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(assistant("All tests pass.")) + "\n")
            path = fh.name
        try:
            payload = {"transcript_path": path, "stop_hook_active": True}
            r = self._run(json.dumps(payload))
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout.strip(), "")
        finally:
            os.unlink(path)

    def test_report_mode_exits_zero_and_prints(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(assistant("All tests pass.")) + "\n")
            path = fh.name
        try:
            r = self._run(json.dumps({"transcript_path": path}))
            self.assertEqual(r.returncode, 0)
            env = json.loads(r.stdout)
            self.assertIn("contradicted", env["systemMessage"])
            self.assertIn("note:", env["systemMessage"])
        finally:
            os.unlink(path)

    def test_enforce_mode_blocks_with_exit_2_on_stderr(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(assistant("All tests pass.")) + "\n")
            path = fh.name
        try:
            r = self._run(json.dumps({"transcript_path": path}), {"CLAIM_CHECK_ENFORCE": "1"})
            self.assertEqual(r.returncode, 2)
            self.assertIn("contradicted", r.stderr)
        finally:
            os.unlink(path)


    def test_report_mode_emits_the_hook_envelope(self):
        """Bare stdout on exit 0 only shows in transcript view; the envelope reaches the user."""
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(assistant("I ran the suite and all of the tests pass.")) + "\n")
            path = fh.name
        try:
            r = self._run(json.dumps({"transcript_path": path}))
            self.assertEqual(r.returncode, 0)
            env = json.loads(r.stdout)
            self.assertTrue(env["continue"])
            self.assertIn("contradicted", env["systemMessage"])
        finally:
            os.unlink(path)

    def test_capability_limit_is_reported_once_per_session(self):
        """A non-English message must not produce a report every single turn."""
        dutch = ("De hook werkt nu en ik heb de tests gedraaid, dus alles is groen. "
                 "Verder heb ik de configuratie aangepast en het logbestand bekeken.")
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(assistant(dutch)) + "\n")
            path = fh.name
        sid = "sess-" + os.path.basename(path)
        try:
            first = self._run(json.dumps({"transcript_path": path, "session_id": sid}))
            second = self._run(json.dumps({"transcript_path": path, "session_id": sid}))
            self.assertIn("English", json.loads(first.stdout)["systemMessage"])
            self.assertEqual(second.stdout.strip(), "")
        finally:
            os.unlink(path)

    def test_script_version_matches_the_manifest(self):
        man = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
        self.assertTrue(cc.VERSION.startswith(man["version"] + "+"))

    def test_clean_session_is_silent_in_both_modes(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps(tool_use("Bash", {"command": "pytest -q"})) + "\n")
            fh.write(json.dumps(assistant("All tests pass.")) + "\n")
            path = fh.name
        try:
            for env in (None, {"CLAIM_CHECK_ENFORCE": "1"}):
                r = self._run(json.dumps({"transcript_path": path}), env)
                self.assertEqual(r.returncode, 0)
                self.assertEqual(r.stdout.strip(), "")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
