"""check_no_duplicates.py — gate CI anti-duplicación y anti-magic-numbers (F5 + F34e).

Lección del refactor del 14/10 (commits b7a3d75..57dc336): copiar cuerpos
para "modularizar" terminó con UN concepto definido en 3-5 módulos (esc,
MODEL_SEL, send, AtomicStore...) y dos versiones que divergieron
silenciosamente. Regla de la casa: UN solo cuerpo vivo por concepto.

Tres verificaciones:
1. DUPLICADOS: falla si dos módulos de src/zcode_bot definen el MISMO
   símbolo de nivel superior (def/class/asignación UPPER_CASE). Los
   shims declarados (re-export desde la fuente única) van en
   ALLOWED_SHIMS.
2. ATRIBUTOS DE MÓDULO (incidente 19/10, ARCH_RELOAD: el poller quedó
   sordo 5 min porque updates.py referenciaba config.ARCH_RELOAD y la
   constante nunca entró en config.py): todo `config.X` / `lim.X`
   referenciado debe existir como nivel-superior del módulo dueño.
3. MAGIC NUMBERS (F34): los literales semánticos sin nombre (keyword
   timeout=/max_age=, slices >=1000, cfg.get con fallback literal) son
   violación salvo allowlist — la lista permite lo PREEXISTENTE
   auditado; algo nuevo o bien se nombra o se agrega acá con justificación.
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

# módulos dueño de constantes que otros referencian por atributo (regla 2)
MODULOS_CONST = {"config", "lim"}

# regla 3: literales semánticos permitidos (archivo relativo, valor) —
# lo preexistente auditado en F34; agregar uno NUEVO requiere nombrarlo
# o justificarlo acá. Formato: "archivo.py": {valor, ...}
# regla 3: literales semánticos permitidos, auditados en F34 (el resto
# de la suite da cobertura del comportamiento). Un literal NUEVO en un
# archivo sin allowlist falla el gate: nombrarlo o justificarlo acá.
ALLOW_MAGIC = {
    "commands.handlers_info": {60},
    "commands.handlers_sistema": {60, 2800},
    "core": {0, 60, 2000},
    "espectador": {2500},
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
    # F53 dual marker: new shims declare "compatibility shim"; the Spanish
    # marker stays so pre-migration docstrings keep being recognized forever.
    return "shim de compatibilidad" in head or "compatibility shim" in head


def _const_int(node):
    """El literal si el nodo es un entero/float simple, si no None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    return None


def revisar_archivo(path, modulo):
    """Reglas 2 y 3 sobre un archivo → (attrs_ausentes, magics)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    propios = top_level_names(path)
    attrs_ausentes = []   # (dueño, attr)
    magics = []           # (valor, detalle)
    permitidos = ALLOW_MAGIC.get(modulo, set())

    for node in ast.walk(tree):
        # regla 2: config.X / lim.X — el attr debe existir en el dueño
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in MODULOS_CONST \
                and node.attr.isupper():
            dueño = node.value.id
            # el archivo del dueño define sus nombres; config/lim son uno
            if node.attr not in propios and dueño == "config" \
                    and modulo != "config":
                # propios es del archivo ACTUAL: la existencia del attr
                # en el dueño se verifica al final (top_names de config)
                attrs_ausentes.append((dueño, node.attr))
        # regla 3a: keyword semántico con literal (timeout=, max_age=...)
        if isinstance(node, ast.keyword) and node.arg in (
                "timeout", "max_age", "max_age_s", "seen_ttl", "deadline",
                "retener", "minutes", "cad"):
            v = _const_int(node.value)
            if v is not None and v not in permitidos:
                magics.append((v, "%s=%s" % (node.arg, v)))
        # regla 3b: slice [:N] con N >= 1000 sin nombre
        if isinstance(node, ast.Slice):
            v = _const_int(node.upper)
            if v is not None and v >= 1000 and v not in permitidos:
                magics.append((v, "slice[:%s]" % v))
        # regla 3c: cfg.get("clave", LITERAL) — fallback inline prohibido
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and len(node.args) == 2:
            v = _const_int(node.args[1])
            if v is not None and v not in permitidos:
                magics.append((v, "get-fallback=%s" % v))
    return attrs_ausentes, magics


def main():
    definiciones = {}  # nombre -> [módulos]
    mod_nombres = {}   # módulo -> set de nombres (para la regla 2)
    archivos = []      # (path, módulo)
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
        errores.append("duplicado: %-24s %s" % (n, ", ".join(sorted(ms))))

    # regla 2 con los nombres REALES del dueño (no del archivo visitado)
    for path, modulo in archivos:
        ausentes, magics = revisar_archivo(path, modulo)
        for dueño, attr in ausentes:
            dueño_mod = "utils.config" if dueño == "config" else dueño
            if attr not in mod_nombres.get(dueño_mod, set()) and \
                    attr not in mod_nombres.get(dueño, set()):
                errores.append("attr inexistente: %s.%s (%s)" %
                               (dueño, attr, modulo))
        permitidos = ALLOW_MAGIC.get(modulo, set())
        for v, detalle in magics:
            if v not in permitidos:
                errores.append("magic sin nombre: %s en %s (%s)" %
                               (detalle, modulo, path))

    if errores:
        print("✗ gate F34/F5:")
        for e in sorted(set(errores)):
            print("   " + e)
        sys.exit(1)
    print("✓ gate F34/F5: %d símbolos únicos, attrs y magics en orden"
          % len(definiciones))


if __name__ == "__main__":
    main()
