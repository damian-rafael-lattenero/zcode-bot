#!/usr/bin/env python3
"""Probe de contexto real por sesión (subproceso desechable de /ctx).

Recibe por argv un JSON {workspacePath: [sessionId, ...]}, abre UN cliente
de protocolo por workspace, hace resume de cada sesión y reporta el
contextUsed/contextUsed de su proyección (el estado PERSISTIDO: tras un
compact muestra el tamaño compactado aunque la app tenga la task abierta
con su vista en memoria). Imprime {"sid": {"used": n, "window": n}} y sale.
Aislado a propósito: un resume de una sesión gigante infla ESTE proceso,
no el poller. Timeout del padre: 90s.
"""
import json
import sys
import time

sys.path.insert(0, "/Users/lattenero/.zcode/hooks")
import zcode_proto as zp


def main():
    plan = json.loads(sys.argv[1])
    out = {}
    for ws, sids in plan.items():
        try:
            c = zp.ProtoClient(cwd=ws, stderr_path="/dev/null")
        except Exception:  # noqa: BLE001
            continue
        time.sleep(1.5)
        for sid in sids:
            try:
                r = c.request("session/resume", {"sessionId": sid}, timeout=20)
                if "error" in r:
                    out[sid] = {"error": str(r["error"].get("message", ""))[:60]}
                    continue
                proj = (r.get("result") or {}).get("projection") or {}
                used = proj.get("contextUsed")
                win = proj.get("contextWindow")
                if isinstance(used, (int, float)):
                    out[sid] = {"used": int(used),
                                "window": int(win or 0)}
            except Exception as exc:  # noqa: BLE001
                out[sid] = {"error": str(exc)[:60]}
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
