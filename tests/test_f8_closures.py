# -*- coding: utf-8 -*-
"""F8 — higiene de closures del gateway.

Incidente real (14:28, turno «Probando cot 1234»): cot_flush() asignaba
`cot_full = cot_full[nf:]` para liberar memoria, y esa asignación volvía
`cot_full` una variable LOCAL de la closure (sin nonlocal) — la primera
lectura explotaba con UnboundLocalError y el turno moría sin respuesta.

Regresión: análisis estático (ast) que prohibe asignar en una función
anidada un nombre que también asigna la función contenedora, salvo que
esté declarado nonlocal. F4a: vale para TODO el paquete zcode_bot (el
desmembramiento del gateway mueve closures a módulos nuevos — el gate
tiene que seguir viéndolas).
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
    """Nombres asignados en el cuerpo PROPIO de fn (sin bajar a funciones
    anidadas ni clases): una asignación acá es lo que compite con el scope
    externo cuando fn es una closure."""
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
    """[(funcion_anidada, funcion_externa, nombre), ...] por cada asignación
    que capture sin nonlocal un nombre asignado por la contenedora."""
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
        # sanity del detector: el patrón exacto del incidente debe saltar
        src = ("def outer():\n"
               "    buf = ''\n"
               "    def inner():\n"
               "        trozos = _f(buf)\n"      # lectura: ok
               "        buf = buf[1:]\n"          # asignación sin nonlocal: MAL
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
