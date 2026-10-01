#!/usr/bin/env python3
"""pytest_evidence — run pytest once, on purpose, and write a receipt of that run.

    python3 scripts/pytest_evidence.py run --receipt-dir DIR --declare FILE [--declare FILE ...]
        [--timeout SECONDS] [--max-output-bytes N] [--max-report-bytes N] -- <pytest arguments>

Opt-in. Nothing starts this for you, and the Stop hook never does.

The receipt records, for one run:
  * what this process observed itself: the argument vector it started (no shell),
    the working directory, when the child started and ended, the exit code or signal
    it waited for, whether it had to kill the child on timeout;
  * what pytest reported inside the child (see pytest_capture.py): the selected node
    ids and each setup/call/teardown outcome;
  * declared files: each `--declare` file read before the run and again after it.

What the receipt is not:
  * not authenticated — anyone who can write the receipt directory can write one;
  * not proof of which bytes pytest loaded. Declared files are a comparison of two
    snapshots of the files you named. Imports, bytecode caches, installed packages
    and files edited and restored between the snapshots are outside it;
  * not private by content: it holds the pytest arguments and paths, which can
    carry secrets. It is written with mode 0600 and is meant to stay on this machine.

Exit code: pytest's own; 124 when the run was killed on timeout; 128+N when the child
died on signal N; 64 when the run was refused or the arguments are unusable; 70 when
the receipt could not be written.

Stdlib only in this process. Python 3.9+. POSIX process groups are used for the
timeout; on other systems only the direct child is killed.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claim_evidence as ce  # noqa: E402

EXIT_USAGE = 64
EXIT_INTERNAL = 70
EXIT_TIMEOUT = 124

DEFAULT_TIMEOUT = 600.0
DEFAULT_MAX_OUTPUT_BYTES = 200_000
DEFAULT_MAX_REPORT_BYTES = 8 * 1024 * 1024

# Names only. Their values can change what pytest runs and can hold secrets.
_ENV_NAMES = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTHONPATH")


class _Drain(threading.Thread):
    """Read a pipe to its end in fixed-size chunks, forwarding at most `limit` bytes.

    The pipe is always read to EOF so the child never blocks on a full pipe, and
    nothing is accumulated: bytes past the limit are counted and dropped.
    """

    def __init__(self, pipe, sink, budget):
        super().__init__(daemon=True)
        self.pipe, self.sink, self.budget = pipe, sink, budget
        self.total = 0

    def run(self):
        fd = self.pipe.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.total += len(chunk)
            allowed = self.budget.take(len(chunk))
            if allowed:
                try:
                    self.sink.write(chunk[:allowed])
                    self.sink.flush()
                except (OSError, ValueError):
                    pass


class _Budget:
    def __init__(self, limit):
        self.left, self.lock, self.exhausted = limit, threading.Lock(), False

    def take(self, n):
        with self.lock:
            allowed = min(n, self.left)
            self.left -= allowed
            if allowed < n:
                self.exhausted = True
            return allowed


def _read_report(path, max_bytes):
    """Bounded read of the child's report. Returns the `report` member of the receipt."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError:
        return {"present": False, "truncated": False, "malformed_lines": 0, "events": []}
    if len(data) > max_bytes:
        # A cut report is not a shorter report. Nothing from it is kept.
        return {"present": True, "truncated": True, "malformed_lines": 0, "events": []}
    events, malformed = [], 0
    for line in data.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            # Strict: a line that states one key twice is counted as malformed, so the
            # conflict is still visible when the receipt is assessed.
            events.append(ce.loads_strict(line))
        except (ValueError, RecursionError):
            malformed += 1
    return {"present": True, "truncated": False, "malformed_lines": malformed, "events": events}


def _kill(proc):
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass


def run(receipt_dir, declare, pytest_args, timeout, max_output_bytes, max_report_bytes):
    cwd = os.path.realpath(os.getcwd())
    if not declare:
        sys.stderr.write("refused: declare at least one file with --declare; a receipt with no declared scope is not written\n")
        return EXIT_USAGE
    try:
        pre = ce.snapshot_declared(cwd, list(declare))
    except ce.DeclaredFileError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return EXIT_USAGE

    try:
        os.makedirs(receipt_dir, mode=0o700, exist_ok=True)
        workdir = tempfile.mkdtemp(prefix=".run-", dir=receipt_dir)
    except OSError as exc:
        sys.stderr.write(f"cannot use the receipt directory: {exc.strerror}\n")
        return EXIT_INTERNAL

    run_id = secrets.token_hex(16)
    report_path = os.path.join(workdir, "report.jsonl")
    argv = [sys.executable, "-m", "pytest", "-p", "pytest_capture"] + list(pytest_args)
    env = dict(os.environ)
    scripts = os.path.dirname(os.path.abspath(__file__))
    env["PYTHONPATH"] = scripts + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["CLAIM_CHECK_RUN_ID"] = run_id
    env["CLAIM_CHECK_REPORT_PATH"] = report_path

    timed_out = False
    started_at = time.time()
    try:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=hasattr(os, "setsid"))
        except OSError as exc:
            sys.stderr.write(f"cannot start pytest: {exc.strerror}\n")
            return EXIT_INTERNAL
        budget = _Budget(max_output_bytes)
        # Both streams go to this process's stderr: stdout is kept for the one summary line.
        drains = [_Drain(proc.stdout, sys.stderr.buffer, budget), _Drain(proc.stderr, sys.stderr.buffer, budget)]
        for d in drains:
            d.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
            proc.wait()
        except BaseException:
            # Interrupted. The child has its own session and would outlive us.
            _kill(proc)
            proc.wait()
            raise
        ended_at = time.time()
        for d in drains:
            d.join(timeout=5)
        returncode = proc.returncode

        report = _read_report(report_path, max_report_bytes)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    try:
        post = ce.snapshot_declared(cwd, list(declare))
    except ce.DeclaredFileError:
        post = []  # gone, replaced or unreadable after the run: recorded as not equal to `pre`

    selected = [e["nodeids"] for e in report["events"]
                if type(e) is dict and e.get("event") == "selected" and type(e.get("nodeids")) is list
                and all(type(n) is str for n in e["nodeids"])]
    receipt = {
        "schema": ce.SCHEMA,
        "run_id": run_id,
        "cwd": cwd,
        "argv": argv,
        "env_names_present": sorted(n for n in _ENV_NAMES if n in os.environ),
        "process": {
            "completed": not timed_out,
            "timed_out": timed_out,
            "exit_code": None if timed_out or returncode < 0 else returncode,
            "signal": -returncode if not timed_out and returncode < 0 else None,
            "started_at": started_at,
            "ended_at": ended_at,
            "timeout_seconds": timeout,
        },
        "output": {
            "stdout_bytes": drains[0].total,
            "stderr_bytes": drains[1].total,
            "forwarded_limit": max_output_bytes,
            "truncated": budget.exhausted,
        },
        "report": report,
        "declared_files": {"pre": pre, "post": post},
        "selection_digest": ce.digest_selection(selected[0] if len(selected) == 1 else []),
        "declared_files_digest": ce.digest_declared(pre),
    }

    path = os.path.join(receipt_dir, run_id + ".json")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(receipt, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError as exc:
        sys.stderr.write(f"cannot write the receipt: {exc.strerror}\n")
        return EXIT_INTERNAL

    sys.stdout.write(json.dumps({
        "receipt": path,
        "run_id": run_id,
        "selection_digest": receipt["selection_digest"],
        "declared_files_digest": receipt["declared_files_digest"],
    }, sort_keys=True) + "\n")
    if timed_out:
        return EXIT_TIMEOUT
    return returncode if returncode >= 0 else 128 - returncode


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pytest_evidence.py", description="Run pytest once and write a receipt.")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("run")
    p.add_argument("--receipt-dir", required=True)
    p.add_argument("--declare", action="append", default=[], metavar="FILE",
                   help="a file, relative to the working directory, to snapshot before and after the run")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
    p.add_argument("--max-report-bytes", type=int, default=DEFAULT_MAX_REPORT_BYTES)
    p.add_argument("pytest_args", nargs=argparse.REMAINDER)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    if args.command != "run":
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    if not args.timeout > 0 or args.max_output_bytes < 0 or args.max_report_bytes < 0:
        sys.stderr.write("refused: --timeout must be positive and the byte limits must not be negative\n")
        return EXIT_USAGE
    pytest_args = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    return run(args.receipt_dir, args.declare, pytest_args, args.timeout, args.max_output_bytes, args.max_report_bytes)


if __name__ == "__main__":
    sys.exit(main())
