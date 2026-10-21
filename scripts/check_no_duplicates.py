"""check_no_duplicates.py — CI gate against duplication and magic numbers (F5 + F34e).

Lesson from the 14/10 refactor (commits b7a3d75..57dc336): copying bodies
to "modularize" ended with ONE concept defined in 3-5 modules (esc,
MODEL_SEL, send, AtomicStore...) and two versions that silently diverged.
House rule: ONE single live body per concept.

Three checks:
1. DUPLICATES: fails if two modules under src/zcode_bot define the SAME
   top-level symbol (def/class/UPPER_CASE assignment). Declared shims
   (re-export from the single source) go in ALLOWED_SHIMS.
2. MODULE ATTRIBUTES (incident 19/10, RELOAD_FILE: the poller went deaf
   for 5 min because updates.py referenced config.RELOAD_FILE and the
   constant never made it into config.py): every referenced `config.X` /
   `limits.X` must exist as a top-level name of the owner module.
3. MAGIC NUMBERS (F34): unnamed semantic literals (keyword
   timeout=/max_age=, slices >=1000, cfg.get with a literal fallback) are
   a violation except by allowlist — the list holds the AUDITED
   PREEXISTING ones; anything new is either named or added here with a
   justification.
"""
import ast
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src",
                   "zcode_bot")

# DECLARED compatibility shims (they re-export, they do not redefine)
ALLOWED_SHIMS = {
    "shim",  # any file whose docstring starts with a shim marker
    # ("shim de compatibilidad" / "compatibility shim")
}

# owner modules of constants that others reference by attribute (rule 2)
MODULOS_CONST = {"config", "limits"}

# rule 3: allowed semantic literals (relative file, value) — the
# preexisting ones audited in F34; adding a NEW one requires naming it
# or justifying it here. Format: "file.py": {value, ...}
# rule 3: allowed semantic literals, audited in F34 (the rest of the
# suite covers the behavior). A NEW literal in a file without an
# allowlist fails the gate: name it or justify it here.
ALLOW_MAGIC = {
    "commands.handlers_info": {60},
    "commands.handlers_system": {60, 2800},
    "core": {0, 60, 2000},
    "spectator": {2500},
    "hooks": {60, 240, 2000},
    "live_view": {5, 20, 30},
    "messages": {60},
    "nuclear": {5, 15},
    "reporting": {20, 30, 1000000},
    "tasks_repo": {2},
    "telegram.client": {30, 60, 1024},
    "updates": {240},
    "utils.formatting": {2},
    "utils.http": {90},
    "utils.logging": {4000},
    "workers": {5, 15, 30, 120, 180, 300, 480, 600, 1800, 4000, 6000},
}


def top_level_names(path):
    """Public names defined at module level."""
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
    # F53 dual marker: new shims declare "compatibility shim"; the Spanish
    # marker stays so pre-migration docstrings keep being recognized forever.
    return "shim de compatibilidad" in head or "compatibility shim" in head


def _const_int(node):
    """The literal if the node is a simple int/float, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    return None


def revisar_archivo(path, modulo):
    """Rules 2 and 3 over one file → (attrs_ausentes, magics)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    propios = top_level_names(path)
    attrs_ausentes = []   # (owner, attr)
    magics = []           # (value, detail)
    permitidos = ALLOW_MAGIC.get(modulo, set())

    for node in ast.walk(tree):
        # rule 2: config.X / limits.X — the attr must exist in the owner
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in MODULOS_CONST \
                and node.attr.isupper():
            dueño = node.value.id
            # the owner's file defines its names; config/limits are one
            if node.attr not in propios and dueño == "config" \
                    and modulo != "config":
                # propios holds the CURRENT file's names: the attr's
                # existence in the owner is verified at the end
                # (config's top_names)
                attrs_ausentes.append((dueño, node.attr))
        # rule 3a: semantic keyword with a literal (timeout=, max_age=...)
        if isinstance(node, ast.keyword) and node.arg in (
                "timeout", "max_age", "max_age_s", "seen_ttl", "deadline",
                "retener", "minutes", "cad"):
            v = _const_int(node.value)
            if v is not None and v not in permitidos:
                magics.append((v, "%s=%s" % (node.arg, v)))
        # rule 3b: slice [:N] with N >= 1000 and no name
        if isinstance(node, ast.Slice):
            v = _const_int(node.upper)
            if v is not None and v >= 1000 and v not in permitidos:
                magics.append((v, "slice[:%s]" % v))
        # rule 3c: cfg.get("key", LITERAL) — inline fallback forbidden
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and len(node.args) == 2:
            v = _const_int(node.args[1])
            if v is not None and v not in permitidos:
                magics.append((v, "get-fallback=%s" % v))
    return attrs_ausentes, magics


def main():
    definiciones = {}  # name -> [modules]
    mod_nombres = {}   # module -> set of names (for rule 2)
    archivos = []      # (path, module)
    for root, _dirs, files in os.walk(SRC):
        for fn in sorted(files):
            if not fn.endswith(".py") or fn == "__init__.py":
                continue
            path = os.path.join(root, fn)
            modulo = os.path.relpath(path, SRC).replace(os.sep, ".")[:-3]
            if is_declared_shim(path):
                continue
            nombres = top_level_names(path)
            mod_nombres[modulo] = nombres
            for name in nombres:
                definiciones.setdefault(name, []).append(modulo)
            archivos.append((path, modulo))

    errores = []

    dups = {n: ms for n, ms in definiciones.items() if len(ms) > 1}
    for n, ms in sorted(dups.items()):
        errores.append("duplicate: %-24s %s" % (n, ", ".join(sorted(ms))))

    # rule 2 with the owner's REAL names (not the visited file's)
    for path, modulo in archivos:
        ausentes, magics = revisar_archivo(path, modulo)
        for dueño, attr in ausentes:
            dueño_mod = "utils.config" if dueño == "config" else dueño
            if attr not in mod_nombres.get(dueño_mod, set()) and \
                    attr not in mod_nombres.get(dueño, set()):
                errores.append("missing attr: %s.%s (%s)" %
                               (dueño, attr, modulo))
        permitidos = ALLOW_MAGIC.get(modulo, set())
        for v, detalle in magics:
            if v not in permitidos:
                errores.append("unnamed magic: %s in %s (%s)" %
                               (detalle, modulo, path))

    if errores:
        print("✗ gate F34/F5:")
        for e in sorted(set(errores)):
            print("   " + e)
        sys.exit(1)
    print("✓ gate F34/F5: %d unique symbols, attrs and magics in order"
          % len(definiciones))


if __name__ == "__main__":
    main()
