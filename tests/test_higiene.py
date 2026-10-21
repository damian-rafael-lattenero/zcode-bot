"""Suite hygiene: anti-plain-rebind gate (ff53eb5 debt).

Lesson from the incident: `module.attr = lambda ...` in a test WITHOUT
cleanup lives forever in the xdist worker (the zc_* modules are
sys.modules singletons shared by every file that runs in that process)
and contaminates the next test — the latent bug that -n auto exposed
(the /task golden receiving test_f6's Mock). New house rule: EVERY test
rebind goes through support.patch (mock.patch.object + addCleanup);
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
import sys
import unittest

import support  # src on sys.path (deterministic)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)

# LHS = module alias (the fixture's zt/self.zt, a zc_* from sys.modules
# or the short aliases tests use to name package modules — R11 closed
# the blind spot of zp/_u/hi/hd/zd/repo/zh; W5 renamed the remaining
# Spanish-stem aliases to the plain module names, so those are watched
# now too), RHS = the typical replacement of a test double. Harmless
# locals (sub = mock.Mock(), reloj.sleep = ...) do not match the LHS.
REBIND_PLANO = re.compile(
    r"^\s+(?:self\.)?(?:zt|zc_[a-z_]+|zp|reasoning|spectator|handlers_[a-z_]+"
    r"|hi|hd|zd|repo|zh|_u)"
    r"\.[A-Za-z_]+(?:\.[A-Za-z_]+)*\s*=\s*"
    r"(?:lambda\b|mock\.(?:Mock|MagicMock)\b|boom\b|spy\w*\b|fake\w*\b|"
    r"prohibido\b|red_caida\b|types\.SimpleNamespace\b|type\()")


class TestGateRebinds(unittest.TestCase):
    def test_no_plain_rebinds_in_tests(self):
        worst = []
        for path_iter in sorted(glob.glob(os.path.join(TESTS_DIR, "test_*.py"))):
            with open(path_iter, encoding="utf-8") as f:
                for num, line in enumerate(f, 1):
                    if "noqa: rebind" in line:
                        continue
                    if REBIND_PLANO.match(line):
                        worst.append("%s:%d: %s" % (
                            os.path.basename(path_iter), num, line.strip()))
        self.assertEqual(
            worst, [],
            "Plain rebinds (no cleanup) — use support.patch(testcase, "
            "module, \"name\", value), or justify with `# noqa: rebind` "
            "if the binding targets an object owned by the test itself:\n"
            + "\n".join(worst))


class TestRegistrationGate(unittest.TestCase):
    """Registration gate (F28): every `_cmd_*` defined in the handler
    modules MUST have an entry in dispatch's REG table. A handler
    defined but unregistered is a dead command: the phone sends it,
    lookup does not find it and nobody answers — with no error in any
    log."""

    def test_every_cmd_in_handlers_is_registered(self):
        from zcode_bot.commands import dispatch as zc_dispatch
        from zcode_bot.commands import handlers_info, handlers_system
        from zcode_bot.commands import handlers_tasks
        defined = {}
        for mod in (handlers_tasks, handlers_info, handlers_system):
            for name, obj in vars(mod).items():
                if name.startswith("_cmd_") and callable(obj):
                    defined[name] = obj
        self.assertTrue(defined, "the guard went deaf: no _cmd_* at all")
        registrados = set(zc_dispatch.REG._by_name.values())
        dead = sorted(n for n, f in defined.items()
                         if f not in registrados)
        self.assertEqual(
            dead, [],
            "handlers _cmd_* without an entry in the REG table of "
            "commands/dispatch.py (dead command):\n  "
            + "\n  ".join(dead))


class TestBoundNames(unittest.TestCase):
    """Bound-names gate (R0a): every name used as an attribute BASE
    (`config.QUEUE_FILE`) must be bound in the module (import, def,
    assignment, parameter) or be a builtin. The real regression: F34b
    used `config` in commands.handlers_tasks without importing it —
    /qgo and /qdel broken for a whole day in production (errors.log
    19/10 12:55) with the suite green, because no test really called
    them. pyflakes is not installed (stdlib-only): this AST sweep covers
    that class of bug.

    It over-approximates bindings (any assignment in ANY scope counts as
    bound in the module): we prefer a false negative over a false
    positive that breaks the suite for no reason."""

    @staticmethod
    def _ligados(tree):
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(
                    node.ctx, (ast.Store, ast.Del)):
                out.add(node.id)
            elif isinstance(node, ast.arg):
                out.add(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    out.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef,
                                   ast.AsyncFunctionDef)):
                out.add(node.name)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                out.update(node.names)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                out.add(node.name)
        return out

    def test_no_unbound_attribute_base(self):
        src = os.path.join(TESTS_DIR, "..", "src", "zcode_bot")
        ligados_glob = set(dir(builtins))
        missing = []
        for path_iter in sorted(glob.glob(os.path.join(src, "**", "*.py"),
                                     recursive=True)):
            if "__pycache__" in path_iter:
                continue
            with open(path_iter, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path_iter)
            ligados = self._ligados(tree) | ligados_glob
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) \
                        and isinstance(node.value, ast.Name) \
                        and isinstance(node.value.ctx, ast.Load) \
                        and node.value.id not in ligados:
                    missing.append("%s:%d: %s.%s" % (
                        os.path.relpath(path_iter, src), node.lineno,
                        node.value.id, node.attr))
        self.assertEqual(
            missing, [],
            "Name used as an attribute base without being bound in "
            "the module (missing an import?):\n" + "\n".join(missing))


class TestSpecsCoverAll(unittest.TestCase):
    """R6 — the single declaration (dispatch.SPECS) and its derivatives:
    (1) EVERY _cmd_* handler registered (the gate above, in reverse);
    (2) SLOW_CMDS derived from the specs (name + alias of every slow);
    (3) the /help (curated text) mentions EVERY menu-visible command
    — the contract that used to live in 4 parallel lists."""

    def test_specs_register_slow_and_help_covers_the_visible(self):
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
                              "visible command missing from /help: %s"
                              % s.names[0])


class TestOnlyEnglish(unittest.TestCase):
    """P8 — English-only gate (F53 final): ZERO Spanish lines in the
    Python sources. Shares the detector with scripts/check_english.py
    (accents + stopwords; honors `# noqa: spanish` and its ALLOW dict),
    so the metric and the gate can never drift apart.

    `# noqa: spanish` is ONLY for Spanish that is load-bearing forever:
    permanent compat literals (validation.py's bilingual YES/NO words,
    the "si" legacy callback value, LEGACY_KINDS, _TOOL_FILTER_VALUES,  # noqa: spanish — the gate's own examples (quoted references)
    the bilingual .get("titulo")/"cajas" fallback reads, /cola alias)  # noqa: spanish — the gate's own examples (quoted references)
    or quoted history (Damian's remarks and old incident banners:
    «se tildó», «lo vi todo junto», 'reiniciá ZCode'...). Everything  # noqa: spanish — the gate's own examples (quoted references)
    else gets translated."""

    def test_no_spanish_in_python_sources(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        import check_english as _check_english  # the SHARED detector (same list as the CLI); aliased so a future scripts/check_english name clash can't shadow it
        targets = set(
            glob.glob(os.path.join(REPO_ROOT, "src", "zcode_bot", "**",
                                   "*.py"), recursive=True)
            + glob.glob(os.path.join(TESTS_DIR, "test_*.py"))
            + [os.path.join(TESTS_DIR, n) for n in
               ("support.py", "fake_server.py", "asynkit.py")]
            + glob.glob(os.path.join(REPO_ROOT, "scripts", "*.py"))
            + [os.path.join(REPO_ROOT, "src", "zcode-telegram.py"),
               os.path.join(REPO_ROOT, "src", "zcode_proto.py")])
        self.assertTrue(targets, "the gate went blind: nothing to check")
        worst = []
        for path in sorted(targets):
            if "__pycache__" in path \
                    or os.path.basename(path) == "check_english.py":
                continue  # the detector's own stopword list, exempt by design
            rel = os.path.relpath(path, REPO_ROOT)
            # relative paths keep check_english's ALLOW dict semantics;
            # the absolute fallback keeps the gate honest when the suite
            # runs from another cwd (spanish_lines swallows OSErrors)
            target = rel if os.path.isfile(rel) else path
            for n in _check_english.spanish_lines(target):
                worst.append("%s:%d" % (rel, n))
        self.assertEqual(
            worst, [],
            "Spanish lines in Python sources (F53 P8 English-only gate). "
            "Translate to English, or add `# noqa: spanish` ONLY if the "
            "Spanish is load-bearing forever — permanent compat literals "
            "(bilingual user input like si/dale/acepto, legacy callback "  # noqa: spanish — the gate's own examples (quoted references)
            "values, LEGACY_KINDS, old persisted keys read bilingually) "
            "or quoted history (Damian's remarks, old incident banners) — "
            "with a short reason:\n" + "\n".join(worst))


if __name__ == "__main__":
    unittest.main()
