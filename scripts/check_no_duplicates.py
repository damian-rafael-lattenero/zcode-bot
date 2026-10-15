"""check_no_duplicates.py — gate CI anti-duplicación (F5).

Lección del refactor del 14/10 (commits b7a3d75..57dc336): copiar cuerpos
para "modularizar" terminó con UN concepto definido en 3-5 módulos (esc,
MODEL_SEL, send, AtomicStore...) y dos versiones que divergieron
silenciosamente. Regla de la casa: UN solo cuerpo vivo por concepto.

Fallará si dos módulos de src/zcode_bot definen el MISMO símbolo de
nivel superior (def/class/asignación UPPER_CASE). Los shims declarados
(re-export desde la fuente única) van en ALLOWED_SHIMS.
"""
import ast
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src",
                   "zcode_bot")

# shims de compatibilidad DECLARADOS (re-exportan, no redefinen)
ALLOWED_SHIMS = {
    "shim",  # todo archivo cuyo docstring empiece con "shim de compatibilidad"
}


def top_level_names(path):
    """Nombres públicos definidos a nivel de módulo."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    names.add(t.id)
    return names


def is_declared_shim(path):
    with open(path, encoding="utf-8") as f:
        head = f.read(400).lower()
    return "shim de compatibilidad" in head


def main():
    definiciones = {}  # nombre -> [módulos]
    for root, _dirs, files in os.walk(SRC):
        for fn in sorted(files):
            if not fn.endswith(".py") or fn == "__init__.py":
                continue
            path = os.path.join(root, fn)
            modulo = os.path.relpath(path, SRC).replace(os.sep, ".")[:-3]
            if is_declared_shim(path):
                continue
            for name in top_level_names(path):
                definiciones.setdefault(name, []).append(modulo)
    dups = {n: ms for n, ms in definiciones.items() if len(ms) > 1}
    if dups:
        print("✗ conceptos con MÁS de un cuerpo vivo:")
        for n, ms in sorted(dups.items()):
            print("   %-24s %s" % (n, ", ".join(sorted(ms))))
        sys.exit(1)
    print("✓ sin duplicaciones (%d símbolos, una fuente por concepto)"
          % len(definiciones))


if __name__ == "__main__":
    main()
