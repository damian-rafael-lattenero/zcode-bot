"""proto — Factory for the ZCode Protocol client + per-PID stderr.

Pattern: Factory Method + RAII (context manager). Unifies the 4
ProtoClient boilerplates the monolith had (compact, worker, fork, api).

ONE stderr PER PID: before, every runtime appended to the same
proto-stderr.log (unreadable concurrent writes — discrepancy D4).
"""
import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zcode_proto as zp  # noqa: E402

from zcode_bot.utils.formatting import esc  # noqa: E402

STDERR_DIR = os.path.join(os.path.expanduser("~"), ".zcode", "hooks", "logs")


def stderr_path():
    os.makedirs(STDERR_DIR, exist_ok=True)
    return os.path.join(STDERR_DIR, "proto-stderr-%d.log" % os.getpid())


def proto_err(r, ctx):
    """Protocol error → chat message: '❌ ctx: {...}'. None if the
    response carries no error (usage: `if e := proto_err(r, "resume"): return e`).
    One living body (F1c): the pattern was repeated ~11 times in the
    gateway with a risk of format/truncation divergence."""
    if "error" not in r or not r["error"]:
        return None
    return "❌ %s: %s" % (ctx, esc(json.dumps(r["error"])[:150]))


def make(cwd=None, on_user_input=None, on_permission=None):
    """Creates a ProtoClient with the package's per-PID stderr."""
    return zp.ProtoClient(cwd=cwd, stderr_path=stderr_path(),
                          on_user_input=on_user_input,
                          on_permission=on_permission)


@contextlib.contextmanager
def client(cwd=None, on_user_input=None, on_permission=None):
    """Context manager: guarantees close() even if the body raises."""
    c = make(cwd=cwd, on_user_input=on_user_input, on_permission=on_permission)
    try:
        yield c
    finally:
        c.close()
