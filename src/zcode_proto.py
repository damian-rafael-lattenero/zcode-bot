#!/usr/bin/env python3
"""Minimal ZCode Protocol client (app-server --stdio).

This is the official headless channel: the desktop app talks to its runtimes
over this same protocol (spawnArgs ["app-server","--stdio"]). It runs with
no UI, no permissions and the screen locked.

Usage:
  zcode_proto.py list
  zcode_proto.py compact <sessionId> [instructions]
  zcode_proto.py send <sessionId> <text>
  zcode_proto.py stop <sessionId>

Requests the SERVER makes to the client (requestRuntimePreferences,
elicitation, etc.) are answered with -32601: the runtime uses defaults and
moves on (see fallback in zcode.cjs: code -32601/-32020 -> defaults).
"""
import json
import os
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

# Anti-Dock profile: the standalone runtime registers with LaunchServices
# as a Foreground app → bouncing tiles on every phone turn (and its MCP
# "npm exec" children too). With no LS registration the process is INVISIBLE
# to the Dock and the stdio protocol works the same (validated 2026-10-13:
# session/list, session/resume with 4 MCPs and session/events OK under
# sandbox). The sandbox is INHERITED by children → it covers the "exec"s
# too. Kill switch: delete ~/.zcode/hooks/nodock.sb to return to the direct
# spawn.
DEFAULT_ARGV = [ELECTRON, ZC, "app-server", "--stdio"]
DOCK_SB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nodock.sb")


def _spawn_argv(argv):
    """Effective spawn argv: if passed explicitly (tests/probes) it is
    used as-is; the production default gets wrapped with sandbox-exec
    when the nodock.sb profile exists."""
    if argv:
        return argv
    if not os.path.exists(DOCK_SB):
        return DEFAULT_ARGV
    return ["/usr/bin/sandbox-exec", "-f", DOCK_SB] + DEFAULT_ARGV


def _builtin_provider_config():
    """The builtin config declaring the coding plan providers. The app
    passes it to its children via ZCODE_BUILTIN_PROVIDER_CONFIG_FILE;
    without it the standalone runtime comes up with an EMPTY provider
    registry."""
    import glob
    cands = sorted(glob.glob(
        HOME + "/.zcode/v2/runtime/provider/*/*/endpoint-*/zcode-builtin.json"))
    return cands[-1] if cands else None


def _proto_env():
    env = dict(os.environ, ELECTRON_RUN_AS_NODE="1",
               ZCODE_BOT_RUNTIME="1")  # marker: deploy.sh defers the reload
    # when running INSIDE a bot turn (the gate would wait for its own
    # turn = deadlock; incident 2026-10-13 18:54)
    b = _builtin_provider_config()
    if b:
        env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = b
        env["ZCODE_BUILTIN_PROVIDER_BUNDLED_CONFIG_FILE"] = b
    # personal config EXCLUSIVE to the bot: zai-bot provider with the coding
    # plan api key (the app never sees this file; its own stays intact)
    bot_cfg = HOME + "/.zcode/hooks/bot-provider.json"
    if os.path.exists(bot_cfg):
        env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = bot_cfg
    else:
        env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = (
            HOME + "/.zcode/v2/provider_config.json")
    return env


class ProtoClient:
    """stdio client for the ZCode Protocol. MIND the framing: frames are
    delimited by DOT ('.') and the server cuts at the FIRST dot — every
    dot inside our own JSON must be escaped as \\u002e (it parses the
    same), and server responses are read with raw_decode."""

    def __init__(self, cwd=None, stderr_path=None, on_user_input=None,
                 on_permission=None, argv=None):
        env = _proto_env()
        self.on_user_input = on_user_input
        self.on_permission = on_permission
        errf = open(stderr_path, "a") if stderr_path else subprocess.DEVNULL
        self.p = subprocess.Popen(
            _spawn_argv(argv),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errf, cwd=cwd or None, env=env)  # binary: os.read does not block
        self.responses = {}
        self.abandoned = set()  # rids whose request() already timed out: their
        # late response is DISCARDED in _dispatch. Without this it stayed
        # retained forever — every parsed tree from a slow session/events
        # adds tens of MB and the 2 s polls piled up GBs
        # (real measured peak: 3561MB with 15MB frames, 2026-10-13)
        self.events = []
        self.max_frame = 0  # bytes of the largest frame seen (anti-OOM
        # diagnostic: the session/events replay of a session with images
        # weighs GBs — the gateway uses this to trip live streaming)
        self.lock = threading.Lock()
        # the reader notifies on this Condition (same lock) when it stores
        # a response or the server dies: request() waits reactively,
        # without polling self.responses with sleeps
        self._cond = threading.Condition(self.lock)
        self.dead = False  # reader finished (EOF/OSError): server down
        self.wlock = threading.Lock()  # serializes stdin writes (requests from several threads)
        self._n = 0
        self.t0 = time.time()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        """Pure data flow: pipe → _frames (decode + delimiting) →
        _dispatch. All the stream state lives in the generator."""
        try:
            for obj in self._frames(self.p.stdout.fileno()):
                self._dispatch(obj)
        finally:
            # the server is gone (EOF/OSError): wake the blocked
            # request()s — death is announced, not polled
            with self._cond:
                self.dead = True
                self._cond.notify_all()

    def _frames(self, fd):
        """Stream generator: yields each complete JSON object arriving
        through the pipe and ends at EOF. The kernel blocks in os.read
        (zero CPU); the only select is the probable-end signal for a mute
        frame: with no raw '\\n' (json.dumps escapes the inner ones →
        reliable delimiter) or '.', 250 ms of pipe silence closes it.
        raw_decode on an INCOMPLETE frame scans everything accumulated:
        retrying per 64KB chunk is O(n²) (~1.8GB of scanning per 15MB
        frame). The VOLUME fallback (retrying every 512KB) was tried and
        BAD: retrying joins mid-frame → O(n²) just like before
        (measured peak 681MB per 12MB frame)."""
        dec = json.JSONDecoder()
        dec8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = []   # decoded pieces not joined yet

        def flush_complete():
            """Parses what is accumulated and returns the complete
            objects; leaves only the incomplete remainder in pending."""
            buf = "".join(pending)
            pending.clear()
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
                    pending.append(b)  # incomplete JSON: keep accumulating
                    return objs
                objs.append(obj)
                buf = b[idx:]

        while True:
            if pending:
                # unclosed remainder: wait briefly (probable-end signal)
                rl, _, _ = select.select([fd], [], [], 0.25)
                if not rl:
                    yield from flush_complete()
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
            pending.append(txt)
            if "\n" in txt or "." in txt:
                yield from flush_complete()
        yield from flush_complete()  # EOF: final flush (last response without \n)

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
                    pass  # server pushes: they fall through to the method handling below
                elif rid in self.abandoned:
                # its request() already timed out: NOBODY will pop it —
                # retaining the tree (tens of MB) is the leak behind the
                # RSS peak. Discard it and free the slot in the set.
                    self.abandoned.discard(rid)
                else:
                    if len(self.responses) >= 8:  # defense: never more than 8 alive
                        for old in list(self.responses):
                            del self.responses[old]
                            break
                    self.responses[rid] = msg
                    self._cond.notify_all()  # request() waits on this, it does not poll
                    return
            m = msg.get("method") or ""
            if m:
                # store ONLY method+ts: nothing consumes params and the
                # session/update frames of a resume carry MBs — piling
                # them up ballooned the poller to 500-900MB and macOS
                # killed it
                self.events.append((time.time() - self.t0, {"method": m}))
                if len(self.events) > 300:
                    del self.events[:-150]
            # server requests -> method-not-found (defaults fallback)
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
                    try:  # notify only: the -32601 below stays as is
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
        """Writes to stdin assuming wlock is ALREADY held (request takes
        it: Lock is not reentrant — nesting with wlock here used to
        deadlock)."""
        try:
            if not _sin_floats(obj):
                raise ValueError("floats not supported")
            s = json.dumps(obj).replace(".", "\\u002e")
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
                # REACTIVE wait: the reader notifies when it stores the
                # response and when the server dies — immediate wakeup,
                # zero polling
                rest = deadline - time.time()
                if self._cond.wait_for(lambda: rid in self.responses,
                                       max(0.0, rest)):
                    return self.responses.pop(rid)
                if self.dead or self.p.poll() is not None:
                    return {"error": {"code": -1, "message": "app-server died"}}
                if rest <= 0:
                    break
        # timeout: if the response arrives late, _dispatch discards it
        # (before, it stayed retained in self.responses forever)
        with self.lock:
            self.abandoned.add(rid)
            if len(self.abandoned) > 64:
                self.abandoned.clear()  # paranoia: do not grow unbounded
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
            self.p.wait(timeout=5)  # without wait it stayed a zombie until GC
        except Exception:
            pass


def ws_of(sid, clients):
    for c in clients:
        if sid in c:
            return c
    return None


def _sin_floats(o):
    """True if the tree has NO NUMERIC floats. Only those cannot travel:
    numeric JSON does not accept \\u002e and the server cuts the frame at
    the dot (dots INSIDE strings are escaped by the replace in
    _send_locked and travel fine). The old guard was a regex over the
    json.dumps — ': 1.5' inside a STRING also matched, the
    AssertionError was silently swallowed and the turn's send was lost
    until the timeout (e.g. a prompt "measurement: 1.5 kg"; audited in
    the deep review of 20/10)."""
    if isinstance(o, float):
        return False
    if isinstance(o, dict):
        return all(_sin_floats(v) for v in o.values())
    if isinstance(o, (list, tuple)):
        return all(_sin_floats(v) for v in o)
    return True


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
            print("missing sessionId")
            return 1
        # workspace of the sid for the cwd (it matters for tools/files)
        r = c.request("session/list", {}, timeout=30)
        ws = None
        for s in (r.get("result") or {}).get("sessions", []):
            if s["sessionId"] == sid:
                ws = (s.get("workspace") or {}).get("workspacePath")
                break
        if ws:
            c.close()
            c = ProtoClient(cwd=ws)

        # activate the session (server requests get answered -32601
        # -> defaults) and bind the model with session/setModel
        r = c.request("session/resume", {"sessionId": sid}, timeout=60)
        if "error" in r:
            print("resume ERROR:", json.dumps(r["error"])[:300])
            return 2
        r = c.request("session/setModel",
                      {"sessionId": sid, "model": "GLM-5.3"}, timeout=30)
        if "error" in r:
            # full provider/model form
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
                print("missing text")
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

        print("unknown command:", cmd)
        return 1
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
