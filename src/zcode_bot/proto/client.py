"""ProtoClient — cliente del ZCode Protocol (app-server --stdio).

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
import codecs
import select
import subprocess
import sys
import threading
import time

ELECTRON = "/Applications/ZCode.app/Contents/MacOS/ZCode"
ZC = "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
MODEL = {"modelId": "GLM-5.3", "providerId": "account:zai-individual-coding-plan"}
HOME = os.path.expanduser("~")

# Perfil anti-Dock: el runtime standalone se registra en LaunchServices como
# app Foreground → tiles rebotando por cada turno del celular (y sus hijos
# MCP "npm exec" también). Sin registro LS el proceso es INVISIBLE para el
# Dock y el protocolo por stdio funciona igual (validado 2026-10-13:
# session/list, session/resume con 4 MCP y session/events OK bajo sandbox).
# El sandbox se HEREDA a los hijos → cubre los "exec" también. Kill switch:
# borrar ~/.zcode/hooks/nodock.sb vuelve al spawn directo.
DEFAULT_ARGV = [ELECTRON, ZC, "app-server", "--stdio"]
DOCK_SB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nodock.sb")


def _spawn_argv(argv):
    """argv efectivo del spawn: si viene explícito (tests/probes) se usa
    tal cual; el default de producción se envuelve con sandbox-exec cuando
    existe el perfil nodock.sb."""
    if argv:
        return argv
    if not os.path.exists(DOCK_SB):
        return DEFAULT_ARGV
    return ["/usr/bin/sandbox-exec", "-f", DOCK_SB] + DEFAULT_ARGV


def _builtin_provider_config():
    """El config builtin que declara los providers del coding plan. La app
    se lo pasa a sus hijos via ZCODE_BUILTIN_PROVIDER_CONFIG_FILE; sin esto
    el runtime standalone levanta con el registry de providers VACIO."""
    import glob
    cands = sorted(glob.glob(
        HOME + "/.zcode/v2/runtime/provider/*/*/endpoint-*/zcode-builtin.json"))
    return cands[-1] if cands else None


def _proto_env():
    env = dict(os.environ, ELECTRON_RUN_AS_NODE="1",
               ZCODE_BOT_RUNTIME="1")  # marker: deploy.sh difiere la recarga
    # si corre DENTRO de un turno del bot (la puerta esperaría su propio
    # turno = deadlock; incidente 2026-10-13 18:54)
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
                 on_permission=None, argv=None):
        env = _proto_env()
        self.on_user_input = on_user_input
        self.on_permission = on_permission
        errf = open(stderr_path, "a") if stderr_path else subprocess.DEVNULL
        self.p = subprocess.Popen(
            _spawn_argv(argv),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errf, cwd=cwd or None, env=env)  # binario: os.read no bloquea
        self.responses = {}
        self.abandoned = set()  # rids cuyo request() ya timeouteó: su
        # respuesta tardía se DESCARTA en _dispatch. Sin esto quedaba
        # retenida para siempre — cada árbol parseado de un session/events
        # lento suma decenas de MB y los polls cada 2 s apilaban GBs
        # (pico real medido: 3561MB con frames de 15MB, 2026-10-13)
        self.events = []
        self.max_frame = 0  # bytes del frame más grande visto (diagnóstico
        # anti-OOM: el replay de session/events de una sesión con imágenes
        # pesa GBs — el gateway usa esto para trippear el streaming en vivo)
        self.lock = threading.Lock()
        # el reader notifica en esta Condition (mismo lock) al guardar una
        # respuesta o al morir el server: request() espera reactivamente,
        # sin sondear self.responses con sleeps
        self._cond = threading.Condition(self.lock)
        self.dead = False  # reader terminó (EOF/OSError): server caído
        self.wlock = threading.Lock()  # serializa escrituras a stdin (requests desde varios hilos)
        self._n = 0
        self.t0 = time.time()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        """Data flow puro: pipe → _frames (decode + delimitación) →
        _dispatch. Todo el estado del stream vive en el generador."""
        try:
            for obj in self._frames(self.p.stdout.fileno()):
                self._dispatch(obj)
        finally:
            # el server se fue (EOF/OSError): despertar a los request()
            # bloqueados — la muerte se anuncia, no se sondea
            with self._cond:
                self.dead = True
                self._cond.notify_all()

    def _frames(self, fd):
        """Generador del stream: cede cada objeto JSON completo que llega
        por el pipe y termina en EOF. El kernel bloquea en os.read (cero
        CPU); el único select es la señal de fin probable de un frame
        mudo: sin '\\n' crudo (json.dumps escapa los de adentro →
        delimitador confiable) o '.', 250 ms de silencio del pipe lo
        cierran. raw_decode sobre un frame INCOMPLETO escanea todo lo
        acumulado: reintentar por cada chunk de 64KB es O(n²) (~1.8GB de
        escaneo por frame de 15MB). El fallback de VOLUMEN (reintentar
        cada 512KB) se probó y MAL: reintentar joins a mitad de frame →
        O(n²) igual que antes (pico medido 681MB por frame de 12MB)."""
        dec = json.JSONDecoder()
        dec8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pend = []   # trozos decodificados sin unir aún

        def cerrados():
            """Parsea lo acumulado y devuelve los objetos completos; deja
            en pend solo el resto incompleto."""
            buf = "".join(pend)
            pend.clear()
            objs = []
            while True:
                b = buf.lstrip(" .\n\r\t")
                if not b:
                    return objs
                if len(b) > self.max_frame:
                    self.max_frame = len(b)
                try:
                    obj, idx = dec.raw_decode(b)
                except ValueError:
                    pend.append(b)  # JSON incompleto: seguir acumulando
                    return objs
                objs.append(obj)
                buf = b[idx:]

        while True:
            if pend:
                # hay resto sin cerrar: esperar poco (señal de fin probable)
                rl, _, _ = select.select([fd], [], [], 0.25)
                if not rl:
                    yield from cerrados()
                    continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            txt = dec8.decode(chunk)
            if not txt:
                continue
            pend.append(txt)
            if "\n" in txt or "." in txt:
                yield from cerrados()
        yield from cerrados()  # EOF: flush final (última respuesta sin \n)

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
                rid = str(msg["id"])
                if rid.startswith("server-"):
                    pass  # pushes del server: caen al manejo de method abajo
                elif rid in self.abandoned:
                    # su request() ya timeouteó: NADIE lo va a popear —
                    # retener el árbol (decenas de MB) es el leak del pico
                    # de RSS. Descartar y liberar el turno del set.
                    self.abandoned.discard(rid)
                else:
                    if len(self.responses) >= 8:  # defensa: nunca >8 vivas
                        for viejo in list(self.responses):
                            del self.responses[viejo]
                            break
                    self.responses[rid] = msg
                    self._cond.notify_all()  # request() espera esto, no sondea
                    return
            m = msg.get("method") or ""
            if m:
                # guardar SOLO method+ts: nada consume params y los
                # session/update del resume traen MBs — acumularlos infló
                # el poller a 500-900MB y macOS lo mató
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
        with self._cond:
            while True:
                # espera REACTIVA: el reader notifica al guardar la respuesta
                # y al morir el server — despertar inmediato, cero sondeo
                rest = deadline - time.time()
                if self._cond.wait_for(lambda: rid in self.responses,
                                       max(0.0, rest)):
                    return self.responses.pop(rid)
                if self.dead or self.p.poll() is not None:
                    return {"error": {"code": -1, "message": "app-server murió"}}
                if rest <= 0:
                    break
        # timeout: si la respuesta llega tarde, _dispatch la descarta
        # (antes quedaba retenida en self.responses para siempre)
        with self.lock:
            self.abandoned.add(rid)
            if len(self.abandoned) > 64:
                self.abandoned.clear()  # paranoia: no crecer sin límite
        return {"error": {"code": -2, "message": "timeout %s" % method}}

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
        try:
            self.p.wait(timeout=5)  # sin wait quedaba zombie hasta el GC
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