"""F44 — el vigilante del CoT de la Mac CALIENTE por sesión, y el fix del
botón único.

El botón único (Damian, 20/10: "apareció un único botón de cot en toda la
conversación"): uhttp.send devolvía True y las tres fuentes lo guardaban
como message_id de la caja de botones → editMessageText(message_id=True)
fallaba mudo y la caja quedaba clavada en «CoT 1». Los tests de F41-F43
no lo vieron porque sus dobles de send devolvían un entero. Acá: el
contrato de send (devuelve el id), ph_de descarta un True, y las fuentes
conservan el ph en CADA reescritura del cache (el tap edita en el lugar).

F44: UN resume por SESIÓN — el vigilante vigila el turno que lo lanzó y
queda caliente esperando el próximo marker de la misma sesión (misma
conexión, sin segundo resume); single-flight del hook por pid-file con
heartbeat; corte por after_id y guardia monótona; /nuclear lo remata.
"""
import json
import os
import signal
import sys
import time
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "src"))

import support  # noqa: E402
from zcode_bot import razonamiento as rz  # noqa: E402
from zcode_bot import hooks as zc_hooks  # noqa: E402
from zcode_bot import nuclear as zc_nuclear  # noqa: E402
from zcode_bot.live_view import LiveTurnView  # noqa: E402
from zcode_bot.utils import http as zh  # noqa: E402

CFG = {"token": "T", "chat_id": 1, "enabled": True}
# F51c: reciente — el aviso de turno largo dispara con t0 viejo
# F51c: reciente (120s atrás: lejos del segundo del marker
# real del turno 2 en los pickups, lejos del umbral del aviso)
T0 = int(time.time()) - 120
CAP = rz.COT_CAJA_CAP
A = "a" * (CAP + 500)   # una razonada: 1 caja llena + cola de 500
B = "b" * 300           # otra razonada corta
KEEP_CORTO = 0.05       # keepalive real que vence enseguida (sleep parcheado)


# --------------------------------------------------------------- helpers

def _msg(rol, mid, *parts):
    return {"info": {"role": rol, "id": mid}, "parts": list(parts)}


def _r(texto):
    return {"type": "reasoning", "text": texto}


_TXT = {"type": "text", "text": "fin"}


class _ProtoGuion:
    """session/messages devuelve el paso siguiente del guion; cada paso
    puede traer la acción que haría el HOOK sobre el marker en ese
    momento: "rm" (on_stop lo consumió) o ("new", token) (prompt nuevo)."""

    def __init__(self, marker, pasos):
        self.marker, self.pasos = marker, list(pasos)
        self.pedidos, self.resumes, self.cerrado = [], 0, False

    def request(self, method, params=None, timeout=None):
        if method == "session/resume":
            self.resumes += 1
            return {"result": {}}
        if method == "session/messages":
            self.pedidos.append(dict(params or {}))
            msgs, accion = self.pasos.pop(0) if self.pasos else ([], None)
            if accion == "rm":
                try:
                    os.remove(self.marker)
                except OSError:
                    pass
            elif accion:
                with open(self.marker, "w") as f:
                    json.dump({"token": accion[1]}, f)
            return {"result": {"messages": msgs}}
        return {"result": {}}

    def close(self):
        self.cerrado = True


def _botones(kb):
    return [b["text"] for fila in (kb or {}).get("inline_keyboard", [])
            for b in fila]


# ------------------------------------------------------ contrato de send

class TestContratoSend(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.addCleanup(setattr, zh, "_cooldown_until", [0.0])
        zh._cooldown_until[0] = 0.0

    def test_send_devuelve_el_message_id(self):
        support.parchea(self, zh, "tg_api",
                        lambda cfg, m, params=None, timeout=35:
                        {"ok": True, "result": {"message_id": 777}})
        self.assertEqual(zh.send(CFG, "hola"), 777)
        self.assertEqual(zh._live_send(CFG, "hola"), 777)

    def test_send_sin_id_sigue_siendo_truthy(self):
        support.parchea(self, zh, "tg_api",
                        lambda cfg, m, params=None, timeout=35: {"ok": True})
        self.assertIs(zh.send(CFG, "hola"), True)
        support.parchea(self, zh, "tg_api",
                        lambda cfg, m, params=None, timeout=35: None)
        self.assertIs(zh.send(CFG, "hola"), True)

    def test_ph_de_descarta_lo_que_no_es_un_id(self):
        self.assertEqual(rz.ph_de(555), 555)
        self.assertIsNone(rz.ph_de(True))   # bool es subclase de int
        self.assertIsNone(rz.ph_de(False))
        self.assertIsNone(rz.ph_de(None))


# ------------------------------------------------ celu: el ph se conserva

class _FakeC:
    def __init__(self, evs):
        self.evs = evs

    def request(self, method, params=None, timeout=None):
        return {"result": {"events": [dict(e) for e in self.evs]}}


class _Th:
    def __init__(self, n=2):
        self.n = n

    def is_alive(self):
        self.n -= 1
        return self.n > 0


def _ev(eid, kind, delta=""):
    p = {"kind": kind}
    if delta:
        p["delta"] = delta
    return {"eventId": eid, "payload": p}


# una razonada que llena DOS cajas y cierra por tool (cola = caja 3)
_EVS = [_ev(1, "reasoning_delta", "x" * (2 * CAP + 400)),
        _ev(2, "tool_input_start"), _ev(3, "tool_call")]


class TestCeluConservaPh(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)

    def _view(self, send_ret):
        self.sends, self.edits = [], []
        deps = {
            "send": lambda cfg, text, loud=None, markup=None:
                (self.sends.append((text, markup)), send_ret)[1],
            "live_send": lambda cfg, text: 620,
            "live_edit": lambda cfg, mid, text, markup=None:
                (self.edits.append((mid, text, markup)), True)[1],
            "sleep": lambda s: None, "pwlog": lambda *a: None,
            "fetch_messages": lambda c_, sid, after_id=None, limit=40: [],
            "rss": lambda: -1, "rss_now": lambda: -1,
            "skip_mark": lambda sid: None,
        }
        cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1, cot_botones=True)
        return LiveTurnView(_FakeC(_EVS), cfg, support.T1, float(T0),
                            "testing", 777, _Th(2), deps=deps)

    async def test_todas_las_reescrituras_del_cache_llevan_el_ph(self):
        v = self._view(601)
        v.run()
        self.assertEqual(v.cot_rotulos, ["CoT 1.1", "CoT 1.2", "CoT 1.3"])
        self.assertEqual(len(self.sends), 1)  # la caja nace UNA vez…
        # …y crece editando SU message_id: 2ª caja, 3ª (cola) y cierre de
        # la razonada — los 3 edits al MISMO id (F49: ya no hay un 4º edit
        # de «índice final»; la razonada cierra con lo suyo y listo)
        self.assertEqual([e[0] for e in self.edits if e[2]],
                         [601, 601, 601])
        ultimo = [e for e in self.edits if e[2]][-1]  # el cierre de la razonada
        self.assertEqual(_botones(ultimo[2]), ["CoT 1.1", "CoT 1.2", "CoT 1.3"])
        self.assertIn("razonada 1", ultimo[1])
        with open(rz.cache_path(support.T1, T0)) as f:
            d = json.load(f)
        self.assertEqual(d["ph"], 601)        # antes: se perdía en la 2ª caja
        self.assertTrue(d["finalizado"])      # y el final tampoco lo traía
        self.assertEqual(len(d["cajas"]), 3)

    async def test_un_true_de_send_no_se_usa_como_message_id(self):
        v = self._view(True)  # el contrato viejo: salió, pero sin id
        v.run()
        self.assertIsNone(v.cot_ph)
        self.assertFalse(any(e[0] is True for e in self.edits))  # el bug
        with open(rz.cache_path(support.T1, T0)) as f:
            self.assertNotIn("ph", json.load(f))


# ------------------------------------------------ Mac: vigilante caliente

class _VigilanteBase(unittest.TestCase):
    SID = support.T1  # en el índice de tasks como "testing"

    def setUp(self):
        self.zt = support.fresh(self)
        self.sends, self.edits, self.naps = [], [], []
        support.parchea(self, rz.uhttp, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append((text, markup)), 777)[1])
        support.parchea(self, rz.uhttp, "_live_edit",
                        lambda cfg, ph, text, markup=None:
                        (self.edits.append((ph, text, markup)), True)[1])
        support.parchea(self, rz.time, "sleep", lambda s: self.naps.append(s))
        self.marker = rz.marker_path(self.SID)
        with open(self.marker, "w") as f:
            json.dump({"token": "tok1"}, f)

    def _vivir(self, pasos, keepalive, cfg=None):
        proto = _ProtoGuion(self.marker, pasos)
        support.parchea(self, rz.zc_proto, "make", lambda cwd=None, **kw: proto)
        rz.vivir(cfg or CFG, self.SID, "cv", T0, "/w", keepalive=keepalive)
        return proto

    def _caches(self):
        st = self.zt.config.STATE_DIR
        out = {}
        for n in sorted(os.listdir(st)):
            if n.startswith("cot-%s-" % self.SID) and n.endswith(".json"):
                with open(os.path.join(st, n)) as f:
                    d = json.load(f)
                out[d["t0"]] = d
        return out


class TestVigilanteCaliente(_VigilanteBase):
    def test_pickup_del_turno_siguiente_sin_segundo_resume(self):
        pasos = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], None),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            # fetch final del turno 1; en ese instante el hook escribe el
            # marker del turno 2 (prompt nuevo en la Mac)
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A)),
              _msg("assistant", "m3", _TXT)], ("new", "tok2")),
            # turno 2: solo lo posterior a m3 (afterMessageId)
            ([_msg("user", "m4", _TXT), _msg("assistant", "m5", _r(B))], "rm"),
            ([_msg("user", "m4", _TXT), _msg("assistant", "m5", _r(B), _TXT)],
             None),
        ]
        proto = self._vivir(pasos, keepalive=KEEP_CORTO)
        self.assertEqual(proto.resumes, 1)  # la clave de F44
        caches = self._caches()
        self.assertEqual(len(caches), 2)
        d1 = caches[T0]
        self.assertEqual([len(c) for c in d1["cajas"]], [CAP, 500])
        self.assertTrue(d1["finalizado"])
        self.assertEqual(d1["ph"], 777)        # F43c: el tap edita en el lugar
        d2 = next(d for t, d in caches.items() if t != T0)
        self.assertEqual([len(c) for c in d2["cajas"]], [300])
        self.assertTrue(d2["finalizado"])
        self.assertEqual(d2["titulo"], "testing")  # recalculado del índice
        # el turno 2 se leyó DESPUÉS del último mensaje del turno 1: en el
        # pickup el prompt nuevo puede no estar persistido y sin el corte
        # el último user sería el del turno anterior
        self.assertNotIn("afterMessageId", proto.pedidos[0])
        self.assertEqual(proto.pedidos[3]["afterMessageId"], "m3")
        self.assertEqual(proto.pedidos[4]["afterMessageId"], "m3")
        # dos cajas de botones (una por turno) y sus cierres propios (F49:
        # «razonada R — N cajas», ya no un índice «Razonamiento» con todas)
        self.assertEqual(len(self.sends), 2)
        finales = [t for _p, t, _k in self.edits if "razonada" in t]
        self.assertIn("2 cajas", finales[0])
        self.assertIn("1 caja", finales[1])
        self.assertFalse(any("Razonamiento" in t for _p, t, _k in self.edits))
        # al vencer el keepalive suelta todo: pid-file fuera, runtime cerrado
        self.assertFalse(os.path.exists(rz.pid_path(self.SID)))
        self.assertTrue(proto.cerrado)

    def test_keepalive_cero_un_turno_y_chau(self):
        pasos = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A), _TXT)],
             ("new", "tok2")),  # llega otro prompt: sin keepalive no es mío
        ]
        proto = self._vivir(pasos, keepalive=0)
        self.assertEqual(len(self._caches()), 1)
        self.assertTrue(proto.cerrado)
        self.assertTrue(all(s == rz.COT_VIVO_CAD_S for s in self.naps))

    def test_stop_en_idle_suelta_el_runtime_sin_consumir_el_marker(self):
        pasos = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A), _TXT)],
             None),
        ]
        self.zt._stop_request(self.SID)
        proto = self._vivir(pasos, keepalive=3600)  # colgaría una hora
        self.assertTrue(proto.cerrado)
        self.assertEqual(len(self._caches()), 1)
        # el marker de /stop es del dueño del runtime: acá solo se mira
        self.assertTrue(os.path.exists(self.zt._stop_file(self.SID)))

    def test_codigo_nuevo_deployado_suelta_el_runtime(self):
        # F48d: el vigilante caliente también se retira al ver el deploy —
        # el hook spawnea uno nuevo (código nuevo) en el próximo turno Mac
        pasos = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A), _TXT)],
             None),
        ]
        support.parchea(self, rz.config, "code_stamp", lambda: "otro")
        proto = self._vivir(pasos, keepalive=3600)  # colgaría una hora
        self.assertTrue(proto.cerrado)
        self.assertEqual(len(self._caches()), 1)
        self.assertFalse(os.path.exists(rz.pid_path(self.SID)))

    def test_task_no_current_o_worker_del_bot_sueltan(self):
        pasos = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A), _TXT)],
             None),
        ]
        cfg = dict(CFG, only_active_task=True)
        self.zt.set_active_bot_session({"sid": support.T2, "title": "CV",
                                        "ws": "/w"})
        proto = self._vivir(pasos, keepalive=3600, cfg=cfg)
        self.assertTrue(proto.cerrado)
        # y con la task current pero un worker del bot dueño de la sesión
        # (el celu la tomó: su LiveTurnView ya hace las cajas)
        self.zt.set_active_bot_session({"sid": self.SID, "title": "testing",
                                        "ws": "/w"})
        self.zt._workers().register(self.SID, {"titulo": "t", "pid": 1})
        with open(self.marker, "w") as f:
            json.dump({"token": "tok3"}, f)
        proto2 = self._vivir([(p[0], p[1]) for p in pasos], keepalive=3600,
                             cfg=cfg)
        self.assertTrue(proto2.cerrado)

    def test_guardia_monotona_nunca_menos_que_lo_mostrado(self):
        pasos = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], None),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([], None),  # el fetch final vino vacío
        ]
        self._vivir(pasos, keepalive=0)
        d = self._caches()[T0]
        self.assertEqual([len(c) for c in d["cajas"]], [CAP])  # lo vivo queda
        self.assertTrue(d["finalizado"])
        self.assertIn("1 caja", self.edits[-1][1])

    def test_marker_reemplazado_a_mitad_encadena_el_turno_nuevo(self):
        """Carrera on_stop → prompt nuevo entre dos polls: el turno 1 se
        cierra con lo visto en vivo (el historial ya es del siguiente) y
        el turno 2 se vigila de inmediato — aun con keepalive 0, porque la
        conexión ya está y el turno ya corre."""
        pasos = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))],
             ("new", "tok2")),
            ([_msg("user", "m3", _TXT), _msg("assistant", "m4", _r(B))], "rm"),
            ([_msg("user", "m3", _TXT), _msg("assistant", "m4", _r(B), _TXT)],
             None),
        ]
        proto = self._vivir(pasos, keepalive=0)
        self.assertEqual(proto.resumes, 1)
        caches = self._caches()
        self.assertEqual(len(caches), 2)
        self.assertEqual([len(c) for c in caches[T0]["cajas"]], [CAP])
        d2 = next(d for t, d in caches.items() if t != T0)
        self.assertEqual([len(c) for c in d2["cajas"]], [300])
        self.assertEqual(len(proto.pedidos), 3)  # sin fetch final del turno 1

    def test_pickup_sin_historial_nuevo_re_resume_una_vez(self):
        """Seguro contra una conexión que no refleje el store tras el
        pickup: si tras after_id no aparece NI el prompt nuevo en 5 polls,
        un session/resume (el costo de siempre, solo acá) la re-engancha."""
        turno1 = [
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))], "rm"),
            ([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A), _TXT)],
             ("new", "tok2")),
        ]
        vacios = [([], None)] * rz.COT_VIVO_VACIO_POLLS
        turno2 = [
            ([_msg("user", "m3", _TXT), _msg("assistant", "m4", _r(B))], "rm"),
            ([_msg("user", "m3", _TXT), _msg("assistant", "m4", _r(B), _TXT)],
             None),
        ]
        proto = self._vivir(turno1 + vacios + turno2, keepalive=KEEP_CORTO)
        self.assertEqual(proto.resumes, 2)
        d2 = next(d for t, d in self._caches().items() if t != T0)
        self.assertEqual([len(c) for c in d2["cajas"]], [300])
        # todos los fetches del turno 2 (5 vacíos + 1 con datos + el final)
        # leen DESPUÉS del último mensaje del turno 1
        self.assertEqual(sum(1 for p in proto.pedidos
                             if p.get("afterMessageId") == "m2"),
                         rz.COT_VIVO_VACIO_POLLS + 2)

    def test_otro_turno_por_token_y_tolerante_a_marker_a_mitad(self):
        self.assertFalse(rz._otro_turno((10, "tok1"), (10, "")))   # mitad de escritura
        self.assertFalse(rz._otro_turno((10, "tok1"), (10, "tok1")))
        self.assertTrue(rz._otro_turno((10, "tok1"), (10, "tok2")))  # mismo segundo
        self.assertTrue(rz._otro_turno((10, ""), (11, "")))         # sin tokens: mtime
        with open(self.marker, "w") as f:
            f.write('{"token": "tok')  # truncado
        mt, tok = rz._marker_estado(self.SID)
        self.assertEqual(tok, "")
        self.assertEqual(mt, int(os.path.getmtime(self.marker)))
        os.remove(self.marker)
        self.assertIsNone(rz._marker_estado(self.SID))

    def test_sucesor_en_el_pid_file_retira_al_vigilante(self):
        pasos = [([_msg("user", "m1", _TXT), _msg("assistant", "m2", _r(A))],
                  None)] * 3

        def send(cfg, text, loud=None, markup=None):
            # con la caja ya nacida, un sucesor toma el pid-file
            with open(rz.pid_path(self.SID), "w") as f:
                f.write(str(support.PID_FALSO))
            return 777
        support.parchea(self, rz.uhttp, "send", send)
        proto = self._vivir(pasos, keepalive=3600)
        self.assertTrue(proto.cerrado)
        self.assertEqual(len(proto.pedidos), 1)  # se retiró en el poll siguiente
        # el pid-file es del sucesor: no se borra
        self.assertEqual(rz.pid_leer(self.SID), support.PID_FALSO)


# ------------------------------------------------ hook: single-flight

class TestSingleFlightHook(unittest.TestCase):
    SID = support.T1

    def setUp(self):
        self.zt = support.fresh(self)
        self.spawns = []
        support.parchea(self, zc_hooks.subprocess, "Popen",
                        lambda args, **kw: self.spawns.append(args))
        support.parchea(self, zh, "send",
                        lambda cfg, text, loud=None, markup=None: True)
        self.zt.set_active_bot_session({"sid": self.SID, "title": "testing",
                                        "ws": "/w"})
        self.cfg = dict(self.zt.DEFAULTS, token="T", chat_id=1,
                        mac_cot_botones=True)

    def _prompt(self):
        self.zt.on_user_prompt(self.cfg, {"sessionId": self.SID, "cwd": "/w",
                                          "prompt": "p"})
        return [a for a in self.spawns if "--cot-vivo" in a]

    def test_vigilante_vivo_y_fresco_no_spawnea_otro(self):
        rz.pid_escribir(self.SID)  # este proceso, latido recién
        self.assertEqual(self._prompt(), [])
        self.assertTrue(rz.vigilante_activo(self.SID))

    def test_vigilante_muerto_spawnea(self):
        with open(rz.pid_path(self.SID), "w") as f:
            f.write(str(support.PID_FALSO))  # pid imposible en macOS
        self.assertFalse(rz.vigilante_activo(self.SID))
        self.assertEqual(len(self._prompt()), 1)

    def test_heartbeat_viejo_es_un_colgado_y_spawnea(self):
        rz.pid_escribir(self.SID)
        viejo = time.time() - rz.COT_VIVO_HB_FRESCO_S - 5
        os.utime(rz.pid_path(self.SID), (viejo, viejo))
        self.assertFalse(rz.vigilante_activo(self.SID))
        self.assertEqual(len(self._prompt()), 1)
        # y el latido lo vuelve a poner fresco
        rz.pid_latir(self.SID)
        self.assertTrue(rz.vigilante_activo(self.SID,
                                            proc_vivo=lambda pid: True))

    def test_sin_pid_file_spawnea(self):
        self.assertEqual(len(self._prompt()), 1)


# ------------------------------------------------ /nuclear los remata

class TestNuclearRemataVigilantes(unittest.TestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.senales = []

        def fake_kill(pid, sig):
            if sig == 0:
                raise ProcessLookupError(pid)  # ya murió: el remate no espera
            self.senales.append((pid, sig))
        support.parchea(self, zc_nuclear.os, "kill", fake_kill)
        support.parchea(self, zc_nuclear.time, "sleep", lambda s: None)
        with open(rz.pid_path(support.T1), "w") as f:
            f.write(str(support.PID_FALSO))

    def test_sigterm_al_vigilante_y_limpia_el_pid_file(self):
        self.zt._nuc_kill_own_runtimes(es_cot_vivo=lambda pid: True)
        self.assertIn((support.PID_FALSO, signal.SIGTERM), self.senales)
        self.assertFalse(os.path.exists(rz.pid_path(support.T1)))

    def test_pid_reciclado_no_se_toca_pero_el_archivo_se_limpia(self):
        self.zt._nuc_kill_own_runtimes(es_cot_vivo=lambda pid: False)
        self.assertEqual(self.senales, [])
        self.assertFalse(os.path.exists(rz.pid_path(support.T1)))


if __name__ == "__main__":
    unittest.main()
