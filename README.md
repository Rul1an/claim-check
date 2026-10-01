# claim-check

**It told you the tests passed. What is that based on?**

A Claude Code plugin with two separate parts:

1. A **Stop hook** that reads the final message, finds the claims in it, and says what the session
   transcript can and cannot show about each one. It never confirms a claim and never refutes one.
2. An **opt-in pytest runner** that writes a receipt of one run, and a checker that compares a
   precisely scoped claim with that receipt. This is the only place a `supported` or
   `contradicted` verdict comes from.

```
claim-check — what the final message claims, and what this transcript can show

  insufficient  All tests pass
                1 tool request naming a test runner was seen. A request is not a result:
                this transcript holds no exit code, report or effect.
  unchecked     claimed a commit
                no shell-tool request was seen. That does not show it did not happen:
                hooks, subagents, scripts and your own shell are not in view.

  note: claim-check reads tool requests in the transcript, not their results or effects.
        It cannot confirm or refute a claim. The labels above describe what was
        checkable here, not whether the claim is true.
```

## Install

```
/plugin marketplace add Rul1an/claim-check
/plugin install claim-check@claim-check
```

Works in the Claude Code CLI and the desktop app. The hook needs Python 3.9+ and nothing else: no
network calls, no configuration, no telemetry. The runner needs pytest in the interpreter you start
it with.

## Why

Agents claim completion they have not earned, and the cause is structural: training on human
feedback rewards answers that sound finished. "All tests pass" lands well whether or not a test
ran. The useful question is what the claim rests on, and the answer has to come from somewhere
other than the model's own account.

## Four verdicts

| Verdict | Meaning | Where it can come from |
|---|---|---|
| `supported` | a receipt, bound to the claim's exact scope, records every selected test passing | the checker only |
| `contradicted` | a receipt, bound to the same scope, records a selected test failing | the checker only |
| `insufficient` | there is something to look at, and it does not decide the claim | hook and checker |
| `unchecked` | nothing here can assess this kind of claim | hook and checker |

## Part 1: the Stop hook

At the end of a turn the hook reads the transcript and takes the final assistant message. Finding
claims in prose is heuristic; what it says about each claim is fixed by rule.

| Claim in the final message | Verdict | What the report states |
|---|---|---|
| "all tests pass", "the test suite passes", "I ran the tests" | `insufficient` | how many tool requests named a test runner |
| "I committed", "I pushed" | `unchecked` | how many shell-tool requests were seen; their text is not read |
| "I updated `path`", "I did not touch `path`" | `unchecked` | how many editing-tool requests named a path ending in `path` |

A transcript holds tool *requests*. It does not hold exit codes, test reports or effects, and it
does not hold what a hook, a subagent, a script or your own shell did. So:

- A request that was seen does not confirm anything. `echo pytest` names a test runner.
- A request that was not seen does not refute anything. Tests can run where the hook cannot look.
- Every recognised claim is reported. Silence means no claim was recognised, not that one checked
  out.

The hook is report-only. `CLAIM_CHECK_ENFORCE`, which made 0.2.0 hand contradicted claims back to
the agent, is ignored: the hook has no verdict it could block on.

### Limits of the hook

- **Claim detection is heuristic and English-only.** Hedged, conditional and instruction-shaped
  sentences are dropped. A message in another language is reported as unchecked, once per session.
- **Negation is a word rule.** A claim is dropped when `nothing`, `none`, `neither`, `nor` or `no`
  comes before it in its sentence, so "Nothing was edited, committed or pushed" is not a commit
  claim. The same rule drops "There was no reason to wait, so I committed". A dropped claim costs
  one line of report.
- **Only the current final message is assessed.** If a user turn or a tool request follows the
  last prose in the transcript, the hook says nothing.
- **Command text never reaches the report or the log.** Counts and the claimed sentence do.
- Requires `python3` on PATH. Untested on native Windows.

## Part 2: a pytest receipt, and a claim scoped to it

Nothing here runs unless you start it.

```bash
python3 scripts/pytest_evidence.py run \
  --receipt-dir .claim-check/receipts \
  --declare src/calc.py --declare pyproject.toml \
  -- -q tests/test_calc.py
```

The runner starts `python -m pytest` as an argument vector, with no shell, waits for it, and writes
`<receipt-dir>/<run_id>.json`. It prints one line with the receipt path, the run id, the selection
digest and the declared-file digest. Pytest's own output goes to stderr, capped at
`--max-output-bytes`; the child is still read to the end. `--timeout` (default 600 s) kills the
run. The exit code is pytest's; 124 on timeout; 64 when the run was refused.

A receipt records:

- **what the runner observed itself**: argv, working directory, start and end time, the exit code
  or signal it waited for, whether it killed the run;
- **what pytest reported inside the child**: the selected node ids and every setup, call and
  teardown outcome, collection errors, deselection, the session's exit status;
- **declared files**: each `--declare` file's SHA-256 and size, read before the run and again
  after it.

Then state a claim with its scope and check it:

```json
{
  "kind": "recorded_selection_passed",
  "run_id": "…",
  "selection_digest": "sha256:…",
  "declared_files_digest": "sha256:…"
}
```

```bash
python3 scripts/claim_evidence.py assess --claim claim.json --receipt .claim-check/receipts/<run_id>.json
```

It prints the assessment as JSON. Exit code 0 `supported`, 1 `contradicted`, 2 `insufficient`,
3 `unchecked`, 64 unusable arguments.

Two claim kinds:

- `recorded_selection_passed` — every item of this selection passed in this run, and the declared
  files were the same before and after it. About the run, not about now.
- `recorded_selection_passed_current_files` — the same, and the declared files, re-read when you
  ask, still match. Edit a declared file and this becomes `insufficient`.

`supported` needs all of this: one receipt for the run; run id, selection digest and declared-file
digest equal to the claim's; the process completed with exit code 0 and pytest's own exit status
agrees; exactly one session start, one selection and one session finish, in order; at least one
selected item; every selected item with setup, call and teardown recorded once each as passed; no
skip, xfail or xpass; no collection error.

`contradicted` needs the same binding, exit code 1, and a selected item with a failed phase.

Everything else is `insufficient`, with reason codes: no tests collected, a skip, an xfail, a
collection error, a timeout, a missing or cut report, an unknown, repeated or mistyped report
line, a repeated or missing phase, an exit status that differs from the process exit code, a
different run, selection or declared-file identity, a declared file that changed, went missing or
became a symlink. Two different receipts for the same run are both listed and neither is used.

Digests, so you can recompute them: `selection_digest` is the SHA-256 of the sorted node ids as
compact JSON; `declared_files_digest` is the SHA-256 of the sorted `[path, sha256, size]` triples
as compact JSON.

### Limits of a receipt

- **A receipt is not authenticated.** The agent runs as you and can write any file you can. A
  hand-written receipt that is consistent with itself is accepted. `supported` means "consistent
  with what this runner records", not "this happened". The checker catches a receipt that
  contradicts itself and two receipts that disagree. It catches nothing else.
- **A passing selection is not "all tests pass".** The checker assesses a selection named by its
  node ids. It is never attached to a sentence in a message, and the hook does not read receipts.
  A run of one test supports a claim about that one test.
- **Declared files are a comparison of snapshots of the files you named.** They are not the bytes
  pytest loaded: imports from outside the list, bytecode caches, installed packages and a file
  edited and restored between the two reads are all outside it. The two reads are not atomic.
- **A result depends on things no receipt binds**: environment variables (only the names of a
  few pytest-related ones are recorded, never values), the network, the clock, test order.
- **A receipt is private.** It holds the pytest arguments and paths, which can carry secrets. It
  is written with mode 0600 in a directory created 0700, and nothing sends it anywhere.
- Plugins that move reporting out of the pytest process, such as pytest-xdist, are not handled;
  expect `insufficient`. Reruns repeat a phase and are `insufficient` too.
- The timeout kills the process group on POSIX. Elsewhere only the direct child is killed.

## Tests

```bash
python3 tests/test_claim_check.py        # the hook; stdlib only
python3 tests/test_claim_evidence.py     # the checker; stdlib only
python3 tests/test_pytest_evidence.py    # the runner; needs pytest, runs real pytest subprocesses
```

The runner tests cover a pass, an assertion failure, a teardown failure, a collection error, zero
tests, a skip, xfail and xpass, a timeout, and a process whose exit code disagrees with what pytest
reported.

0.2.0 said `contradicted` when no matching command appeared in the transcript and counted a
matching command as confirmation. Both were wrong for the reasons under Part 1, and 0.3.0 removes
them. 0.2.0's README also reported zero false positives across private transcripts; that figure
is withdrawn. It was not reproducible by anyone else, and the verdict it measured no longer exists.

## Troubleshooting

Set `CLAIM_CHECK_LOG=/tmp/claim-check.jsonl` to append one line per hook run: whether a current
final message was found, how many requests of each kind were counted, how many claims were
recognised, and each verdict. No command text. A silent hook and a broken hook look identical
without it.

## Built by

The [Assay](https://github.com/Rul1an/assay) project, which asks the same question at a larger
scale: what can you actually conclude from a record of what an agent did, and where does the record
stop supporting the conclusion.

## License

[Apache-2.0](LICENSE).
