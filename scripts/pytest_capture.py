"""pytest_capture — a pytest plugin that writes what pytest reports, one JSON line each.

Loaded by pytest_evidence.py inside the child process with `-p pytest_capture`. It
records the session start, collection errors, the selected node ids, each
setup/call/teardown outcome and the session's exit status. It records no traceback,
no captured output and no environment value.

Everything written here is the child's own account. It is not evidence that the
process finished: the parent records that from the exit code it waited for.
"""

import json
import os
import sys

_REPORT_PATH = os.environ.get("CLAIM_CHECK_REPORT_PATH")
_RUN_ID = os.environ.get("CLAIM_CHECK_RUN_ID")


def _emit(event):
    if not _REPORT_PATH or not _RUN_ID:
        return
    with open(_REPORT_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def pytest_sessionstart(session):
    import pytest

    config = session.config
    inifile = getattr(config, "inipath", None)
    _emit({
        "event": "session_start",
        "run_id": _RUN_ID,
        "pytest_version": pytest.__version__,
        "python_version": "%d.%d.%d" % sys.version_info[:3],
        "rootdir": str(config.rootpath),
        "inifile": str(inifile) if inifile else None,
        "invocation_args": [str(a) for a in config.invocation_params.args],
    })


def pytest_collectreport(report):
    if report.failed:
        _emit({"event": "collect_error", "nodeid": report.nodeid})


def pytest_deselected(items):
    _emit({"event": "deselected", "nodeids": [item.nodeid for item in items]})


def pytest_collection_finish(session):
    _emit({"event": "selected", "nodeids": [item.nodeid for item in session.items]})


def pytest_runtest_logreport(report):
    _emit({
        "event": "phase",
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "xfail": hasattr(report, "wasxfail"),
    })


def pytest_sessionfinish(session, exitstatus):
    _emit({"event": "session_finish", "exitstatus": int(exitstatus)})
