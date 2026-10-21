# -*- coding: utf-8 -*-
"""F8 — gateway closure hygiene.

Real incident (14:28, turn «Probando cot 1234»): cot_flush() assigned
`cot_full = cot_full[nf:]` to free memory, and that assignment turned
`cot_full` into a LOCAL variable of the closure (no nonlocal) — the
first read blew up with UnboundLocalError and the turn died without a
response.

Regression: static analysis (ast) that forbids assigning, in a nested
function, a name that the enclosing function also assigns, unless it is
declared nonlocal. F4a: applies to the WHOLE zcode_bot package (the
gateway break-up moves closures into new modules — the gate must keep
seeing them).
"""
import ast
import os
import unittest

SRC_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "zcode_bot")


def _py_files():
    for dirpath, _dirs, files in os.walk(SRC_ROOT):
        if "__pycache__" in dirpath:
            continue
        for fn in sorted(files):
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _assigned_own(fn):
    """Names assigned in fn's OWN body (without descending into nested
    functions or classes): an assignment here is what competes with the
    outer scope when fn is a closure."""
    out = set()
    for stmt in fn.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign,
                                   ast.For, ast.AsyncFor)):
                targets = [getattr(node, "target", None)]
            elif isinstance(node, ast.NamedExpr):  # walrus :=
                targets = [node.target]
            else:
                continue
            for t in targets:
                if t is None:
                    continue
                for n in ast.walk(t):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        out.add(n.id)
    return out


def _nonlocal_declared(fn):
    out = set()
    for stmt in fn.body:
        if isinstance(stmt, ast.Nonlocal):
            out.update(stmt.names)
    return out


def _violations(source):
    """[(nested_function, enclosing_function, name), ...] for each
    assignment that captures without nonlocal a name assigned by the
    enclosing function."""
    bad = []

    def visit(fn, enclosing):
        own = _assigned_own(fn)
        declared = _nonlocal_declared(fn)
        for var in sorted(own):
            for enc_name, enc_assigned in enclosing:
                if var in enc_assigned and var not in declared:
                    bad.append((fn.name, enc_name, var))
        for stmt in fn.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(stmt, enclosing + [(fn.name, own)])

    tree = ast.parse(source)
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            visit(stmt, [])
    return bad


class TestClosureHygiene(unittest.IsolatedAsyncioTestCase):
    async def test_sin_capturas_sin_nonlocal(self):
        archivos = list(_py_files())
        self.assertGreater(len(archivos), 5,
                           "el walker no está viendo el paquete")
        for ruta in archivos:
            with open(ruta) as f:
                bad = _violations(f.read())
            self.assertEqual(
                bad, [],
                "%s: closures que asignan variables del scope externo sin "
                "nonlocal (bug del incidente cot_full): %r" % (ruta, bad))

    async def test_detector_encuentra_el_bug_original(self):
        # detector sanity check: the exact incident pattern must be flagged
        src = ("def outer():\n"
               "    buf = ''\n"
               "    def inner():\n"
               "        trozos = _f(buf)\n"      # read: ok
               "        buf = buf[1:]\n"          # assignment without nonlocal: BAD
               "        return trozos\n")
        self.assertEqual(_violations(src), [("inner", "outer", "buf")])

    async def test_detector_acepta_nonlocal(self):
        src = ("def outer():\n"
               "    buf = ''\n"
               "    def inner():\n"
               "        nonlocal buf\n"
               "        buf = buf[1:]\n")
        self.assertEqual(_violations(src), [])


if __name__ == "__main__":
    unittest.main()
