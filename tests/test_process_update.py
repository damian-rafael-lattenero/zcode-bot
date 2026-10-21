"""process_update_sync (F2c): the per-update body extracted from the
poller's loop. It is the seam shared by the sync driver, the
AsyncPoller (F2d) and the webhook (F3) — its semantics are frozen
here."""
import unittest

import support  # sys.path of src (deterministic)


def _cfg(zt):
    return dict(zt.DEFAULTS, token="T", chat_id=1, owner_user_id=9,
                enabled=True, loud_all=False)


def _msg(**kw):
    m = {"chat": {"id": 1}, "from": {"id": 9}}
    m.update(kw)
    return m


class TestProcessUpdateSync(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zt = support.fresh(self)
        self.cfg = _cfg(self.zt)
        self.sends = []
        support.parchea(self, self.zt, "send",
                        lambda cfg, text, loud=None, markup=None:
                        (self.sends.append(text), True)[1])
        support.parchea(self, self.zt, "react", lambda *a, **k: None)
        support.parchea(self, self.zt, "typing", lambda *a, **k: None)
        support.parchea(self, self.zt, "handle_command",
                        lambda cfg, text: "cmd:" + text)
        support.parchea(self, self.zt, "handle_free_text",
                        lambda cfg, text: "libre:" + text)

    def _process(self, msg):
        self.zt.process_update_sync(self.cfg, msg)

    async def test_owner_pasado_comando_y_libre(self):
        self._process(_msg(text="/ping"))
        self._process(_msg(text="hacé esto"))
        self.assertEqual(self.sends, ["cmd:/ping", "libre:hacé esto"])

    async def test_no_owner_rechazado(self):
        self._process(_msg(text="/ping", **{"from": {"id": 666}}))
        self.assertEqual(self.sends, [])

    async def test_otro_chat_ignorado(self):
        self._process(_msg(text="/ping", chat={"id": -100123}))
        self.assertEqual(self.sends, [])

    async def test_adjunto_avisa_en_lugar_de_silencio(self):
        self._process(_msg(photo=[{}]))
        self.assertIn("📎", self.sends[0])

    async def test_comando_desconocido(self):
        support.parchea(self, self.zt, "handle_command",
                        lambda cfg, text: None)
        self._process(_msg(text="/noexiste"))
        self.assertIn("🤔 Unknown command", self.sends[0])

    async def test_handler_silencioso_no_es_desconocido(self):
        # F48a: None also means «the handler already replied on its own»
        support.parchea(self, self.zt, "handle_command",
                        lambda cfg, text: None)
        support.parchea(self, self.zt, "comando_conocido", lambda text: True)
        self._process(_msg(text="/last"))
        self.assertEqual(self.sends, [])

    async def test_tap_de_cajita_cot_en_el_lugar_no_regala_el_cartel(self):
        """The incident (Damian, 20/10 20:51: «me dice comando desconocido
        - /help cuando selecciono los botones de los thinking»): the tap
        edits the message in place, /cotb returns None and the gateway
        took it for a nonexistent command. REAL dispatch, without
        patching handle_command."""
        import json
        import os
        from zcode_bot import razonamiento as rz
        from zcode_bot.commands import dispatch as zc_dispatch
        from zcode_bot.utils import http as zh
        support.parchea(self, self.zt, "handle_command",
                        zc_dispatch.handle_command)
        sid, t0 = support.T1, 1760000000
        os.makedirs(os.path.dirname(rz.cache_path(sid, t0)), exist_ok=True)
        with open(rz.cache_path(sid, t0), "w") as f:
            json.dump({"sid": sid, "titulo": "cv", "t0": t0,
                       "cajas": ["uno"], "rotulos": ["CoT 1.1"],
                       "ph": 555, "phs": [555]}, f)
        edits = []
        support.parchea(self, zh, "_live_edit",
                        lambda cfg, ph, texto, markup=None:
                        (edits.append(ph), True)[1])
        self._process(_msg(text="/cotb %s %d 1" % (sid, t0),
                           _cb_data="ct:%s:%d:1" % (sid, t0)))
        self.assertEqual(edits, [555])   # the tap DID edit the box
        self.assertEqual(self.sends, [])  # and no banner dropped below
        # the truly nonexistent one still warns
        self._process(_msg(text="/noexiste"))
        self.assertIn("🤔 Unknown command", self.sends[0])

    async def test_handler_explota_avisa_sin_romper(self):
        def boom(cfg, text):
            raise RuntimeError("kaboom")
        support.parchea(self, self.zt, "handle_command", boom)
        self._process(_msg(text="/ping"))
        self.assertIn("⚠️ Internal error", self.sends[0])
        self.assertIn("kaboom", self.sends[0])

    async def test_respuesta_tupla_lleva_markup(self):
        markup = {"inline_keyboard": []}
        support.parchea(self, self.zt, "handle_command",
                        lambda cfg, text: ("con botones", markup))
        capturado = {}
        support.parchea(self, self.zt, "send",
                        lambda cfg, text, loud=None, markup=None:
                        capturado.update(text=text, markup=markup) or True)
        self._process(_msg(text="/task"))
        self.assertEqual(capturado["markup"], markup)

    async def test_boton_de_pregunta_vencida_no_inyecta(self):
        support.parchea(self, self.zt, "_hq_pending", lambda: False)
        self._process(_msg(text="si", _cb_data="yes"))
        self.assertIn("🤔 That button has expired", self.sends[0])


if __name__ == "__main__":
    unittest.main()
