"""Higiene de la suite: gate anti-rebinds planos (deuda ff53eb5).

Lección del incidente: `modulo.nombre = lambda ...` en un test SIN cleanup
vive para siempre en el worker de xdist (los módulos zc_* son singletons
de sys.modules compartidos por todos los archivos que corran en ese
proceso) y contamina al test siguiente — el bug latente que expuso -n auto
(el golden de /task recibiendo el Mock de test_f6). Regla nueva de la
casa: TODO rebind de tests pasa por support.parchea (mock.patch.object +
addCleanup); el rebind plano se prohíbe salvo justificación explícita en
la línea (`# noqa: rebind`).

El gate es de CÓDIGO FUENTE de tests/ (soporte.py queda afuera: su
trabajo ES rebindear el fixture).
"""
import glob
import os
import re
import unittest

import support  # sys.path de src (determinista)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

# LHS = alias de módulo (zt/self.zt del fixture o un zc_* de sys.modules),
# RHS = el reemplazo típico de un doble de test. Los locals inofensivos
# (sub = mock.Mock(), reloj.sleep = ...) no matchean el LHS.
REBIND_PLANO = re.compile(
    r"^\s+(?:self\.)?(?:zt|zc_[a-z_]+)\.[A-Za-z_]+(?:\.[A-Za-z_]+)*\s*=\s*"
    r"(?:lambda\b|mock\.(?:Mock|MagicMock)\b|boom\b|spy\w*\b|fake\w*\b|"
    r"prohibido\b|red_caida\b|types\.SimpleNamespace\b|type\()")


class TestGateRebinds(unittest.TestCase):
    def test_sin_rebinds_planos_en_tests(self):
        peores = []
        for ruta in sorted(glob.glob(os.path.join(TESTS_DIR, "test_*.py"))):
            with open(ruta, encoding="utf-8") as f:
                for nro, linea in enumerate(f, 1):
                    if "noqa: rebind" in linea:
                        continue
                    if REBIND_PLANO.match(linea):
                        peores.append("%s:%d: %s" % (
                            os.path.basename(ruta), nro, linea.strip()))
        self.assertEqual(
            peores, [],
            "Rebinds planos (sin cleanup) — usar support.parchea(testcase, "
            "modulo, \"nombre\", valor), o justificar con `# noqa: rebind` "
            "si el binding es sobre un objeto del propio test:\n"
            + "\n".join(peores))


class TestGuardiaRegistro(unittest.TestCase):
    """Gate de registro (F28): todo `_cmd_*` definido en los módulos de
    handlers DEBE tener entrada en la tabla REG de dispatch. Un handler
    definido y no registrado es un comando muerto: el celu lo manda,
    lookup no lo encuentra y nadie responde — sin error en ningún log."""

    def test_todo_cmd_de_handlers_esta_en_el_reg(self):
        from zcode_bot.commands import dispatch as zc_dispatch
        from zcode_bot.commands import handlers_info, handlers_sistema
        from zcode_bot.commands import handlers_tareas
        definidos = {}
        for mod in (handlers_tareas, handlers_info, handlers_sistema):
            for nombre, obj in vars(mod).items():
                if nombre.startswith("_cmd_") and callable(obj):
                    definidos[nombre] = obj
        self.assertTrue(definidos, "la guardia quedó sorda: ningún _cmd_*")
        registrados = set(zc_dispatch.REG._by_name.values())
        muertos = sorted(n for n, f in definidos.items()
                         if f not in registrados)
        self.assertEqual(
            muertos, [],
            "handlers _cmd_* sin entrada en la tabla REG de "
            "commands/dispatch.py (comando muerto):\n  "
            + "\n  ".join(muertos))


if __name__ == "__main__":
    unittest.main()
