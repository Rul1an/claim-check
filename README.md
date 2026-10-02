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
                1 tool request naming a test runner was seen. A request is not a result,
                and this hook does not evaluate tool results: nothing here binds an exit
                code or a report to the claim.
  unchecked     claimed a commit
                no shell-tool request was seen. That does not show it did not happen:
                hooks, subagents, scripts and your own shell are not in view.

  note: claim-check counts tool requests in the transcript. It does not evaluate their
        results or effects. It cannot confirm or refute a claim. The labels above
        describe what was checkable here, not whether the claim is true.
```

## Install

```
/plugin marketplace add Rul1an/claim-check
/plugin install claim-check@claim-check
```

Works in the Claude Code CLI and the desktop app. The hook needs Python 3.9+ and nothing else: no
network calls, no configuration, no telemetry. The runner needs pytest in the interpreter you start
it with. The runner and the checker are POSIX-only: the working directory and declared files are
handled as POSIX paths, and on Windows every run is refused.

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

The hook counts tool *requests*. It does not evaluate tool results, even where the transcript
carries an exit code or a test summary: nothing in the hook binds such a result to a claim. And a
transcript does not show what a hook, a subagent, a script or your own shell did. So:

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
`--max-output-bytes`; the child is still read to the end. `--timeout` (default 600 s, a positive
finite number) kills the run.

Runner exit codes: pytest's own when pytest ran to its end; 124 when the run was killed on timeout;
128+N when the child died on signal N; 64 when the run was refused or the arguments are unusable,
before pytest starts; 70 when the receipt directory cannot be used, pytest cannot be started, or
the receipt cannot be serialised or written. After 64 or 70 there is no receipt and no summary
line. 70 can also follow a pytest run that finished.

The receipt appears under `<run_id>.json` only once it is complete: it is written to a temporary
file in the same directory, flushed, synced and closed, and then linked to its final name, which
fails if that name exists. A write that fails leaves nothing under the final name. Removing the
temporary file is best effort; a run killed outright can leave a `.receipt-*.tmp` behind. This is
about what a reader can see, not about power loss: the directory is not synced.

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
agrees; exactly one session start, one selection and one session finish, with the selection before
any test phase; at least one selected item; every selected item with setup, call and teardown
recorded once each, in that order, as passed; no skip and no xfail-marked item; no collection error; the
launch argv, working directory and declared paths present and well-formed.

`contradicted` needs the same binding, exit code 1, a selected item with a failed phase, and no
xfail-marked item anywhere in the selection. One recorded failure is enough: the claim is that
every selected item passed, and a failed phase of a selected item refutes that whatever else the
run did. So the rest of the selection does not have to be accounted for. An item with no recorded
phases (never reached after `-x`) or a failed item with a phase missing still gives
`contradicted`. A missing phase stands in the way of `supported` only.

An xfail-marked item, whether it failed as expected, passed unexpectedly, or passed under
`strict=True` (which pytest itself reports as a failure with exit code 1), makes the receipt
`insufficient`, also when another item really failed. The strict case is recognised by the text
pytest puts in its report, `[XPASS(strict)]`; there is no other mark for it.

Everything else is `insufficient`, with reason codes: no tests collected, a skip, an xfail, a
collection error, a timeout, a missing or cut report, an unknown, repeated or mistyped report
line, a repeated or out-of-order phase, a missing phase where no selected item failed, a key stated twice in a receipt or a report line,
input that is not valid UTF-8 JSON or nests more than 16 levels, an exit status that differs from the process exit code, a
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
- **The snapshot's path checks are not a sandbox.** Refusing symlinks and `..` is a check on the
  path as it was when each file was read. Another process that swaps a directory or a file between
  the check and the read, or between the two reads, is not detected.
- **Declared files are a comparison of snapshots of the files you named.** They are not the bytes
  pytest loaded: imports from outside the list, bytecode caches, installed packages and a file
  edited and restored between the two reads are all outside it. The two reads are not atomic.
- **A result depends on things no receipt binds**: environment variables (only the names of a
  few pytest-related ones are recorded, never values), the network, the clock, test order.
- **A receipt is private, if its directory is.** It holds the pytest arguments and paths, which
  can carry secrets. The file is written with mode 0600 and nothing sends it anywhere. A receipt
  directory the runner creates gets mode 0700; one that already exists keeps the permissions it
  has, and the runner does not change them. Give it a directory only you can write: mode 0600 on
  the file does not stop someone who can write the directory from replacing the file.
- Plugins that move reporting out of the pytest process, such as pytest-xdist, are not handled;
  expect `insufficient`. Reruns repeat a phase and are `insufficient` too.
- The timeout kills the child's process group. A process that leaves that group outlives it.

## Part 3: bind a claim to a receipt, and print the assessment

Two conveniences on top of the checker. Neither changes a verdict.

```bash
python3 scripts/claim_report.py bind --kind recorded_selection_passed \
  --receipt .claim-check/receipts/<run_id>.json > claim.json
python3 scripts/claim_report.py report --claim claim.json --receipt .claim-check/receipts/<run_id>.json
```

`bind` writes the claim for you: the kind you name, and the run id, selection digest and
declared-file digest of the one receipt you name, recomputed from the receipt's content. It adds
`"scope_source": "receipt"` to say where the scope came from; the checker does not read that
member and it proves nothing. `bind` takes no sentence, no transcript and no directory.

**Binding is not evidence.** A claim bound to a receipt always matches that receipt's identity,
so binding cannot make a claim more true. It works the same for a run that failed, timed out or
changed its declared files; what the run showed is decided by `report`. `bind` refuses (exit 64)
only a receipt with no usable identity: not a JSON object, another schema, no run id, a report
that does not hold exactly one well-formed session start with that same run id, not exactly one
well-formed selection, malformed declared files, or stored digests that contradict the content.
A session finish is not required.

`report` runs the assessment once and prints it:

```
Verdict: contradicted
Claim kind: "recorded_selection_passed"
Scope: {"cwd": "/work/project", "declared_file_count": 1, "declared_files_digest": "sha256:…", "run_id": "…", "selected_count": 2, "selection_digest": "sha256:…"}
Reasons: ["selected_item_failed"]
Evidence: ["sha256:…"]
Limits:
- A receipt is not authenticated: whoever can write it can write a consistent false one.
- Declared files are a comparison of snapshots taken before and after the run. They are not the bytes pytest loaded, not its dependencies, and not an atomic snapshot.
- The verdict holds for this run and this selection only.
Binding does not add evidence or authenticate the receipt.
```

The exit code is the verdict, as for `claim_evidence.py assess`. The layout is fixed and the same
input gives the same bytes; receipt digests are sorted, reason codes are printed as the checker
gave them, in its order, with no explanation added. Strings that come from a receipt or a claim
are escaped to ASCII and cut at 120 characters, lists at 16 reasons and 8 digests, and a field
that was cut is labelled `(truncated)` and carries `[truncated]`. If the text does not fit
`--max-bytes` (default 4000) a compact form is printed in which every variable field is replaced
by `[truncated]`. The verdict, the three limits and the last line are never cut: a budget below
549 bytes is refused. The report names no test.

## Tests

```bash
python3 tests/test_claim_check.py        # the hook; stdlib only
python3 tests/test_claim_evidence.py     # the checker; stdlib only
python3 tests/test_pytest_evidence.py    # the runner; needs pytest, runs real pytest subprocesses
python3 tests/test_claim_report.py       # bind and report; one test needs pytest
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
