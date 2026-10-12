#!/usr/bin/env python3
"""Mini-cliente del ZCode Protocol (app-server --stdio).

Es la vía headless oficial: la app desktop habla con sus runtimes por este
mismo protocolo (spawnArgs ["app-server","--stdio"]). Corre sin UI, sin
permisos y con la pantalla bloqueada.

Uso:
  zcode_proto.py list
  zcode_proto.py compact <sessionId> [instrucciones]
  zcode_proto.py send <sessionId> <texto>
  zcode_proto.py stop <sessionId>

Los requests que el SERVER hace al cliente (requestRuntimePreferences,
elicitation, etc.) se contestan con -32601: el runtime usa defaults y sigue
(ver fallback en zcode.cjs: code -32601/-32020 -> defaults).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

ELECTRON = "/Applications/ZCode.app/Contents/MacOS/ZCode"
ZC = "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
MODEL = {"modelId": "GLM-5.3", "providerId": "account:zai-individual-coding-plan"}
HOME = os.path.expanduser("~")


def _builtin_provider_config():
    """El config builtin que declara los providers del coding plan. La app
    se lo pasa a sus hijos via ZCODE_BUILTIN_PROVIDER_CONFIG_FILE; sin esto
    el runtime standalone levanta con el registry de providers VACIO."""
    import glob
    cands = sorted(glob.glob(
        HOME + "/.zcode/v2/runtime/provider/*/*/endpoint-*/zcode-builtin.json"))
    return cands[-1] if cands else None


def _proto_env():
    env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
    b = _builtin_provider_config()
    if b:
        env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = b
        env["ZCODE_BUILTIN_PROVIDER_BUNDLED_CONFIG_FILE"] = b
    # config personal EXCLUSIVO del bot: provider zai-bot con la api key del
    # coding plan (la app no ve este archivo; el suyo queda intacto)
    bot_cfg = HOME + "/.zcode/hooks/bot-provider.json"
    if os.path.exists(bot_cfg):
        env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = bot_cfg
    else:
        env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = (
            HOME + "/.zcode/v2/provider_config.json")
    return env


class ProtoClient:
    """Cliente stdio del ZCode Protocol. OJO al framing: los frames se
    delimitan por PUNTO ('.') y el server corta en el PRIMER punto — todo
    punto dentro del JSON propio debe ir escapado como \\u002e (parsea
    igual), y las respuestas del server se leen con raw_decode."""

    def __init__(self, cwd=None, stderr_path=None, on_user_input=None,
                 on_permission=None):
        env = _proto_env()
        self.on_user_input = on_user_input
        self.on_permission = on_permission
        errf = open(stderr_path, "a") if stderr_path else subprocess.DEVNULL
        self.p = subprocess.Popen(
            [ELECTRON, ZC, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errf, cwd=cwd or None, env=env)  # binario: os.read no bloquea
        self.responses = {}
        self.events = []
        self.lock = threading.Lock()
        self.wlock = threading.Lock()  # serializa escrituras a stdin (requests desde varios hilos)
        self._n = 0
        self.t0 = time.time()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        import codecs
        import os as _os
        dec = json.JSONDecoder()
        dec8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = ""
        fd = self.p.stdout.fileno()
        while True:
            try:
                chunk = _os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += dec8.decode(chunk)
            while True:
                b = buf.lstrip(" .\n\r\t")
                if not b:
                    buf = ""
                    break
                try:
                    obj, idx = dec.raw_decode(b)
                except ValueError:
                    buf = b  # JSON incompleto: seguir acumulando
                    break
                self._dispatch(obj)
                buf = b[idx:]
        resto = buf.strip().strip(".")
        if resto:
            try:
                obj, _ = dec.raw_decode(resto)
                self._dispatch(obj)
            except ValueError:
                pass

    def _dispatch(self, raw):
        if isinstance(raw, dict):
            msg = raw
        else:
            raw = raw.strip()
            if not raw:
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                return
        if not isinstance(msg, dict):
            return
        with self.lock:
            if "id" in msg and ("result" in msg or "error" in msg):
                if not str(msg["id"]).startswith("server-"):
                    self.responses[msg["id"]] = msg
                    return
            m = msg.get("method") or ""
            if m:
                # guardar SOLO method+ts: nada consume params (wait_event no
                # tiene callers) y los session/update del resume traen MBs —
                # acumularlos infló el poller a 500-900MB y macOS lo mató
                self.events.append((time.time() - self.t0, {"method": m}))
                if len(self.events) > 300:
                    del self.events[:-150]
            # requests del server -> method-not-found (fallback defaults)
            if "id" in msg and m and not m.startswith(
                    ("startup/", "process/", "computer-use/")):
                if m == "interaction/requestUserInput" and self.on_user_input:
                    try:
                        res = self.on_user_input(msg.get("params") or {})
                    except Exception:  # noqa: BLE001
                        res = None
                    if isinstance(res, dict):
                        self._send({"id": msg["id"], "result": res})
                        return
                if m == "interaction/requestPermission" and self.on_permission:
                    try:  # solo aviso: el -32601 de abajo queda como estaba
                        self.on_permission(msg.get("params") or {})
                    except Exception:  # noqa: BLE001
                        pass
                self._send({"id": msg["id"],
                            "error": {"code": -32601,
                                      "message": "Method not found"}})

    def _send(self, obj):
        with self.wlock:
            self._send_locked(obj)

    def _send_locked(self, obj):
        """Escribe a stdin asumiendo wlock YA tomado (request lo toma:
        Lock no es reentrante — anidar with wlock acá era un deadlock)."""
        try:
            s = json.dumps(obj)
            assert not re.search(r":\s*-?\d+\.\d+", s), "floats no soportados"
            s = s.replace(".", "\\u002e")
            self.p.stdin.write((s + "\n").encode())
            self.p.stdin.flush()
        except (BrokenPipeError, ValueError, AssertionError):
            pass

    def request(self, method, params=None, timeout=200):
        with self.wlock:
            self._n += 1
            rid = "c%d" % self._n
            frame = {"id": rid, "method": method}
            if params is not None:
                frame["params"] = params
            self._send_locked(frame)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if rid in self.responses:
                    return self.responses.pop(rid)
            if self.p.poll() is not None:
                return {"error": {"code": -1, "message": "app-server murió"}}
            time.sleep(0.3)
        return {"error": {"code": -2, "message": "timeout %s" % method}}

    def wait_event(self, pred, timeout=200):
        """Espera un evento del server que cumpla pred(msg)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                for i, (ts, msg) in enumerate(self.events):
                    if pred(msg):
                        return self.events.pop(i)[1]
            time.sleep(0.5)
        return None

    def close(self):
        try:
            self.request("session/close", {}, timeout=5)
        except Exception:
            pass
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.terminate()
        except Exception:
            pass


def ws_of(sid, clients):
    for c in clients:
        if sid in c:
            return c
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    c = ProtoClient(cwd=os.path.expanduser("~/.zcode/workspace/default"))
    try:
        if cmd == "list":
            r = c.request("session/list", {}, timeout=30)
            for s in (r.get("result") or {}).get("sessions", []):
                print(s["sessionId"], "|", s.get("status"), "|",
                      (s.get("workspace") or {}).get("workspacePath", ""), "|",
                      s.get("title", ""))
            return 0

        sid = sys.argv[2] if len(sys.argv) > 2 else ""
        if not sid:
            print("falta sessionId")
            return 1
        # workspace del sid para el cwd (importa para tools/archivos)
        r = c.request("session/list", {}, timeout=30)
        ws = None
        for s in (r.get("result") or {}).get("sessions", []):
            if s["sessionId"] == sid:
                ws = (s.get("workspace") or {}).get("workspacePath")
                break
        if ws:
            c.close()
            c = ProtoClient(cwd=ws)

        # activar la sesión (los requests del server se contestan -32601
        # -> defaults) y bindear el modelo con session/setModel
        r = c.request("session/resume", {"sessionId": sid}, timeout=60)
        if "error" in r:
            print("resume ERROR:", json.dumps(r["error"])[:300])
            return 2
        r = c.request("session/setModel",
                      {"sessionId": sid, "model": "GLM-5.3"}, timeout=30)
        if "error" in r:
            # forma completa provider/model
            r = c.request(
                "session/setModel",
                {"sessionId": sid,
                 "model": "account:zai-individual-coding-plan/GLM-5.3"},
                timeout=30)
        if "error" in r:
            print("setModel ERROR:", json.dumps(r["error"])[:400])
            return 2

        if cmd == "compact":
            rest = " ".join(sys.argv[3:]).strip()
            params = {"sessionId": sid}
            if rest:
                params["instructions"] = rest
            r = c.request("session/compact", params, timeout=300)
            if "error" in r:
                print("compact ERROR:", json.dumps(r["error"])[:300])
                return 3
            print("compact OK:", json.dumps(r.get("result"))[:200])
            return 0

        if cmd == "send":
            text = " ".join(sys.argv[3:]).strip()
            if not text:
                print("falta texto")
                return 1
            r = c.request("session/send",
                          {"sessionId": sid, "content": text,
                           "modelSelection": MODEL}, timeout=600)
            if "error" in r:
                print("send ERROR:", json.dumps(r["error"])[:300])
                return 3
            print("send OK:", json.dumps(r.get("result"))[:300])
            return 0

        if cmd == "stop":
            r = c.request("session/stop", {"sessionId": sid}, timeout=30)
            print(json.dumps(r)[:300])
            return 0 if "error" not in r else 3

        print("comando desconocido:", cmd)
        return 1
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
