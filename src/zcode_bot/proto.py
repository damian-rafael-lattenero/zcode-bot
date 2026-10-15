"""proto — Factory del cliente del ZCode Protocol + stderr por PID.

Patrón: Factory Method + RAII (context manager). Unifica los 4 boilerplates
de ProtoClient que había en el monolito (compact, worker, fork, api).

stderr UNO POR PID: antes todos los runtimes appendeaban al mismo
proto-stderr.log (escritura concurrente ilegible — discrepancia D4).
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
    """Error de protocolo → mensaje para el chat: '❌ ctx: {...}'.
    None si la respuesta no trae error (uso: `if e := proto_err(r, "resume"): return e`).
    Un solo cuerpo vivo (F1c): el patrón estaba repetido ~11 veces en el
    gateway con riesgo de divergencia de formato/truncado."""
    if "error" not in r or not r["error"]:
        return None
    return "❌ %s: %s" % (ctx, esc(json.dumps(r["error"])[:150]))


def make(cwd=None, on_user_input=None, on_permission=None):
    """Crea un ProtoClient con el stderr por-PID del paquete."""
    return zp.ProtoClient(cwd=cwd, stderr_path=stderr_path(),
                          on_user_input=on_user_input,
                          on_permission=on_permission)


@contextlib.contextmanager
def client(cwd=None, on_user_input=None, on_permission=None):
    """Context manager: garantiza close() aunque el cuerpo lance."""
    c = make(cwd=cwd, on_user_input=on_user_input, on_permission=on_permission)
    try:
        yield c
    finally:
        c.close()
