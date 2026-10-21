"""Suite hygiene: anti-plain-rebind gate (ff53eb5 debt).

Lesson from the incident: `module.attr = lambda ...` in a test WITHOUT
cleanup lives forever in the xdist worker (the zc_* modules are
sys.modules singletons shared by every file that runs in that process)
and contaminates the next test — the latent bug that -n auto exposed
(the /task golden receiving test_f6's Mock). New house rule: EVERY test
rebind goes through support.parchea (mock.patch.object + addCleanup);
plain rebinds are forbidden except with explicit justification on the
line (`# noqa: rebind`).

The gate checks the SOURCE CODE of tests/ (soporte.py stays out: its
job IS to rebind the fixture).
"""
import ast
import builtins
import glob
import os
import re
import unittest

import support  # src on sys.path (deterministic)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

# LHS = module alias (the fixture's zt/self.zt, a zc_* from sys.modules
# or the short aliases tests use to name package modules — R11 closed
# the blind spot of zp/_u/rz/hi/hd/zd/esp/repo/zh), RHS = the typical
# replacement of a test double. Harmless locals (sub = mock.Mock(),
# reloj.sleep = ...) do not match the LHS.
REBIND_PLANO = re.compile(
    r"^\s+(?:self\.)?(?:zt|zc_[a-z_]+|zp|rz|hi|hd|zd|esp|repo|zh|_u)"
    r"\.[A-Za-z_]+(?:\.[A-Za-z_]+)*\s*=\s*"
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
    """Registration gate (F28): every `_cmd_*` defined in the handler
    modules MUST have an entry in dispatch's REG table. A handler
    defined but unregistered is a dead command: the phone sends it,
    lookup does not find it and nobody answers — with no error in any
    log."""

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


class TestNombresSinImportar(unittest.TestCase):
    """Bound-names gate (R0a): every name used as an attribute BASE
    (`config.ARCH_COLA`) must be bound in the module (import, def,
    assignment, parameter) or be a builtin. The real regression: F34b
    used `config` in commands.handlers_tareas without importing it —
    /qgo and /qdel broken for a whole day in production (errors.log
    19/10 12:55) with the suite green, because no test really called
    them. pyflakes is not installed (stdlib-only): this AST sweep covers
    that class of bug.

    It over-approximates bindings (any assignment in ANY scope counts as
    bound in the module): we prefer a false negative over a false
    positive that breaks the suite for no reason."""

    @staticmethod
    def _ligados(arbol):
        out = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Name) and isinstance(
                    nodo.ctx, (ast.Store, ast.Del)):
                out.add(nodo.id)
            elif isinstance(nodo, ast.arg):
                out.add(nodo.arg)
            elif isinstance(nodo, (ast.Import, ast.ImportFrom)):
                for a in nodo.names:
                    out.add((a.asname or a.name).split(".")[0])
            elif isinstance(nodo, (ast.ClassDef, ast.FunctionDef,
                                   ast.AsyncFunctionDef)):
                out.add(nodo.name)
            elif isinstance(nodo, (ast.Global, ast.Nonlocal)):
                out.update(nodo.names)
            elif isinstance(nodo, ast.ExceptHandler) and nodo.name:
                out.add(nodo.name)
        return out

    def test_ninguna_base_de_atributo_sin_ligar(self):
        src = os.path.join(TESTS_DIR, "..", "src", "zcode_bot")
        ligados_glob = set(dir(builtins))
        faltantes = []
        for ruta in sorted(glob.glob(os.path.join(src, "**", "*.py"),
                                     recursive=True)):
            if "__pycache__" in ruta:
                continue
            with open(ruta, encoding="utf-8") as f:
                arbol = ast.parse(f.read(), filename=ruta)
            ligados = self._ligados(arbol) | ligados_glob
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Attribute) \
                        and isinstance(nodo.value, ast.Name) \
                        and isinstance(nodo.value.ctx, ast.Load) \
                        and nodo.value.id not in ligados:
                    faltantes.append("%s:%d: %s.%s" % (
                        os.path.relpath(ruta, src), nodo.lineno,
                        nodo.value.id, nodo.attr))
        self.assertEqual(
            faltantes, [],
            "Nombre usado como base de atributo sin estar ligado en el "
            "módulo (¿falta un import?):\n" + "\n".join(faltantes))


class TestSpecsCubrenTodo(unittest.TestCase):
    """R6 — the single declaration (dispatch.SPECS) and its derivatives:
    (1) EVERY _cmd_* handler registered (the gate above, in reverse);
    (2) SLOW_CMDS derived from the specs (name + alias of every slow);
    (3) the /help (curated text) mentions EVERY menu-visible command
    — the contract that used to live in 4 parallel lists."""

    def test_specs_registran_slow_y_el_help_cubre_lo_visible(self):
        from zcode_bot.commands import dispatch as zd
        registrados = set(zd.REG._by_name.values())
        for s in zd.SPECS:
            for n in s.names:
                self.assertIn(zd.REG._by_name[n], registrados)
        self.assertEqual(
            zd.SLOW_CMDS,
            {n for s in zd.SPECS if s.slow for n in s.names})
        # the menu derives from the specs: visible ones in order, no repeats
        visibles = [s.names[0] for s in zd.SPECS if s.menu_desc]
        self.assertEqual(len(visibles), len(set(visibles)))
        help_txt = zd._cmd_help({}, "")
        for s in zd.SPECS:
            if s.menu_desc and s.names[0] != "help":
                self.assertIn("/%s" % s.names[0], help_txt,
                              "comando visible ausente del /help: %s"
                              % s.names[0])


if __name__ == "__main__":
    unittest.main()
