"""process_update_sync (F2c): el cuerpo por-update extraído del loop del
poller. Es el seam que comparten el driver síncrono, el AsyncPoller (F2d)
y el webhook (F3) — su semántica queda congelada acá."""
import unittest

import support
import zc_config  # noqa: F401  (fixture de paths, via support)


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
        self.zt.send = lambda cfg, text, loud=None, markup=None: \
            (self.sends.append(text), True)[1]
        self.zt.react = lambda *a, **k: None
        self.zt.typing = lambda *a, **k: None
        self.zt.handle_command = lambda cfg, text: "cmd:" + text
        self.zt.handle_free_text = lambda cfg, text: "libre:" + text

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
        self.zt.handle_command = lambda cfg, text: None
        self._process(_msg(text="/noexiste"))
        self.assertIn("🤔 Comando desconocido", self.sends[0])

    async def test_handler_explota_avisa_sin_romper(self):
        def boom(cfg, text):
            raise RuntimeError("kaboom")
        self.zt.handle_command = boom
        self._process(_msg(text="/ping"))
        self.assertIn("⚠️ Error interno", self.sends[0])
        self.assertIn("kaboom", self.sends[0])

    async def test_respuesta_tupla_lleva_markup(self):
        markup = {"inline_keyboard": []}
        self.zt.handle_command = lambda cfg, text: ("con botones", markup)
        capturado = {}
        self.zt.send = lambda cfg, text, loud=None, markup=None: \
            capturado.update(text=text, markup=markup) or True
        self._process(_msg(text="/task"))
        self.assertEqual(capturado["markup"], markup)

    async def test_boton_de_pregunta_vencida_no_inyecta(self):
        self.zt._hq_pending = lambda: False
        self._process(_msg(text="si", _cb_data="yes"))
        self.assertIn("🤔 Ese botón ya venció", self.sends[0])


if __name__ == "__main__":
    unittest.main()
