# claim-check

**It told you the tests passed. Did they run?**

A Claude Code plugin that compares what the agent *said it did* against what the session *actually
observed*, and speaks up only when they disagree.

```
claim-check — what was said vs what this session observed

  contradicted  all tests pass
                no test command was observed in this session
  contradicted  claimed a commit
                no `git commit` was observed

  note: claim-check sees tool calls, not their effects: a shell redirect or a
        subprocess can change files with no trace here. Absence of an
        observation is not proof of absence.
```

Silent when everything checks out. Report-only by default; it never blocks unless you ask it to.

## Install

```
/plugin marketplace add Rul1an/claim-check
/plugin install claim-check@claim-check
```

Works in the Claude Code CLI and the desktop app — both read the same settings tree, so one install
covers both. Python 3.9+, standard library only, no network calls, no configuration, no telemetry.

## Why

Agents claim completion they have not earned, and the cause is structural rather than a bug:
training on human feedback rewards answers that sound finished. "All tests pass" lands well whether
or not a test ran. The fix is not a better prompt — it is checking the claim against something
outside the model's own account of itself.

That is the whole plugin. At the end of a turn it reads the session transcript, extracts the
confident statements from the final message, and checks each against the tool calls that actually
happened.

## What it checks

| Claim in the final message | Checked against |
|---|---|
| "all tests pass", "the suite is green" | did a recognised test command run? pytest · cargo test · go test · npm/pnpm/yarn/bun test · jest · vitest · mocha · playwright · rspec · phpunit · gradle/maven · `make test` · `python -m unittest` · a test file run directly |
| "I ran the tests" | same |
| "I committed" | was there a `git commit`? |
| "I pushed" | was there a `git push`? |
| "I updated `path`" | was `path` edited by an editing tool, or named in a shell command? |
| "I did not touch `path`" | was `path` edited anyway? |

Everything else is left alone on purpose.

## Three outcomes, never two

**contradicted** — the claim disagrees with what was observed. A test command either ran or it did
not.

**unobservable** — the claim is about something this session cannot see. Reported as such rather
than quietly counted as fine.

**confirmed** — matched. Not printed; a tool that congratulates you every turn becomes noise, and
noise gets uninstalled.

The distinction between *contradicted* and *unobservable* is the point. A checker with only two
states has to pretend that "I saw nothing" means "nothing happened" — which is the exact overclaim
this plugin exists to catch. That rule applies to its own output too.

## Limits, stated up front

Hooks see **tool calls, not their effects**. A `bash -c` that spawns a subprocess can write files,
open sockets and edit configuration with nothing visible at this layer.

- A missing observation is **not** proof that nothing happened. The note in every report says so,
  and it is not removable.
- File claims are only *contradicted* when an editing tool ran and the named path was not among its
  targets. With no editing tool at all, the verdict is *unobservable*.
- **Claim patterns are English-only.** A final message in another language is reported as
  *unchecked*, once per session — never silently treated as clean.
- Claim detection is conservative by design. Hedged, conditional and instruction-shaped sentences
  are dropped, so it misses real claims rather than inventing false ones.
- The transcript is written asynchronously. If the last message has not landed after a short wait,
  the plugin says nothing rather than judging a stale turn.
- Requires `python3` on PATH. Untested on native Windows, where the launcher is `py`.

## How it was tested

Synthetic fixtures were not enough, and saying so is the honest part of the record. The plugin was
run against **113 real Claude Code transcripts** and then installed live as a `Stop` hook. Each pass
found a class of defect the previous one could not:

| Found by | Defect |
|---|---|
| Unit tests | a greedy regex captured `s.py` out of `src/secrets.py`; conditional prose read as a claim |
| Real transcripts | markdown tables and links read as claims; `"I haven't committed"` reported as a *missing* commit; a third party's `"Aegis pushed again"` read as the agent's own claim |
| Its own live run | `python3 tests/test_x.py` not recognised as running tests — the way this very project runs its own suite |
| The live log | nine silent runs that looked clean were **unchecked**, not clean: that session was not in English |

Current state: **45 tests**, zero false positives across 113 real transcripts, and 12 of 12 mutants
caught — take a real session that legitimately claimed a passing suite, remove the test command from
its observations, and require the checker to notice.

```bash
python3 tests/test_claim_check.py
```

## Enforce mode (off by default)

```bash
export CLAIM_CHECK_ENFORCE=1
```

Contradicted claims are handed back to the agent, which then goes and does the thing it said it did.
Useful, and interrupting. Off unless you turn it on, because a check that fires on a normal turn
gets disabled within a day.

## Troubleshooting

Set `CLAIM_CHECK_LOG=/tmp/claim-check.jsonl` to append one line per run: how much of the final
message was read, how many commands and edits were observed, how many claims were recognised, and
what was reported. A silent hook and a broken hook look identical without it.

## Roadmap

- **Codex.** The design already targets the hook events both harnesses share. Codex observes shell
  commands, so the command-shaped claims port directly; file-edit claims will report *unobservable*
  there until its hook surface covers edits.
- More claim classes, one at a time, and only where they can be checked deterministically.
- Optional: write the claim/observation pairs out as a record for people who want to keep them.

Issues and pull requests welcome, particularly new false-positive cases — a real transcript this
gets wrong is the most useful thing you can send.

## Built by

The [Assay](https://github.com/Rul1an/assay) project, which asks the same question at a larger
scale: what can you actually conclude from a record of what an agent did, and where does the record
stop supporting the conclusion.

## License

[Apache-2.0](LICENSE).
