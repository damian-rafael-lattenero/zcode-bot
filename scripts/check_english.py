"""check_english.py — F53 Spanish detector: the migration's progress metric.

Modes:
  python3 scripts/check_english.py            report: per-file Spanish line
                                              counts, worst first
  python3 scripts/check_english.py --check    exit 1 on any Spanish line
                                              (allowlist + noqa respected)
  python3 scripts/check_english.py -v         also print each flagged line

A line is Spanish if it contains an accented char (á é í ó ú ñ ü ¿ ¡, any
case) or one of the stopwords below as a whole word. The stopword list is
deliberately conservative: words that also exist in English (no, me, hay,
in, a...) are excluded — accents already catch most Spanish prose, the list
only adds the frequent unaccented words and the Spanish identifiers that P4
renames (texto, cajas, guardar...), so the metric tracks every phase.

Escape hatches:
  * per line: `# noqa: spanish` (same convention as test_higiene's rebind
    gate). Used from P3 on for the permanent compat literals (classify()'s
    Spanish answers, the "si" legacy callback value).
  * per path: ALLOW maps a path prefix to substrings that neutralize a
    flagged line. This file itself is exempt by construction.

F53 closes when `--check` passes on every tracked file.
"""
import re
import subprocess
import sys

ACCENT = re.compile(r"[áéíóúñÁÉÍÓÚÑüÜ¿¡]")

# Whole-word, unambiguously Spanish. Keep it conservative: a word that is
# also an English word must NOT go in here.
STOPWORDS = re.compile(
    r"\b("
    r"que|de|el|la|los|las|un|una|unos|unas|para|por|con|sin|del|al|lo|se|"
    r"ya|si|sus|mas|pero|como|cuando|donde|porque|muy|tambien|este|esta|"
    r"estos|estas|ese|esa|eso|esos|esas|aquel|ellos|ellas|nosotros|"
    r"turno|turnos|cola|colas|sesion|sesiones|archivo|archivos|mensaje|"
    r"mensajes|respuesta|respuestas|codigo|boton|botones|caja|cajas|rotulo|"
    r"rotulos|aviso|avisos|vigilante|tarea|tareas|numero|linea|lineas|"
    r"texto|titulo|guardar|cerrar|vivir|capturar|mostrar|parchea|esperar|"
    r"cada|hasta|desde|luego|antes|despues|ahora|hace|segun|menos|muerto|"
    r"muertos|viva|vivos|vacia|vacio|vacios|lleno|llena|forma|manera|"
    r"sobre|entre|toda|todo|todos|todas|nada|algo|alguien|dos|tres|cuatro|"
    r"cinco|seis|siete|ocho|nueve|diez|hola|gracias|bien|malo|ver|dar|"
    r"hacer|decir|queda|tiene|llama|debe|deberia|puede|podria|mientras|"
    r"ademas|sino|aqui|alli|mismo|nuestro|nuestra|sopla|huerfano|huerfanos|"
    r"razon|razones|razonadas|rezagadas|vencio|elegir|eleccion|aprobado|"
    r"rechazado|cancelado|cancelada|limpia|limpio|arbol|carpeta|llave|"
    r"llaves|llamada|llamadas|recibido|enviado|leyendo|escribiendo|corriendo"
    r")\b"
)

# Permanent line-level exemptions per path prefix, populated as F53 phases
# land (P3 compat literals, P6 dual markers). Empty during migration.
ALLOW = {
    # "src/zcode_bot/utils/validation.py": ("dale", "acepto"),
}

SKIP_EXTENSIONS = (".png", ".lock", ".db", ".icloud")


def tracked_files(root="."):
    out = subprocess.run(["git", "ls-files"], cwd=root, check=True,
                         capture_output=True, text=True)
    files = []
    for rel in out.stdout.splitlines():
        if not rel or rel.endswith(SKIP_EXTENSIONS):
            continue
        # this file carries the stopword list itself — exempt by construction
        if rel == "scripts/check_english.py":
            continue
        files.append(rel)
    return files


def line_is_allowed(path, line):
    if "# noqa: spanish" in line:
        return True
    for prefix, subs in ALLOW.items():
        if path.startswith(prefix):
            if any(s in line for s in subs):
                return True
    return False


def spanish_lines(path):
    """Flagged line numbers in one file (1-based)."""
    hits = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                if ACCENT.search(line) or STOPWORDS.search(line):
                    if not line_is_allowed(path, line):
                        hits.append(n)
    except OSError:
        pass
    return hits


def main():
    check = "--check" in sys.argv
    verbose = "-v" in sys.argv
    report = {}
    for path in tracked_files():
        hits = spanish_lines(path)
        if hits:
            report[path] = hits
    total = sum(len(h) for h in report.values())
    for path, hits in sorted(report.items(), key=lambda kv: -len(kv[1])):
        if verbose:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for n in hits:
                print("%s:%d: %s" % (path, n, lines[n - 1].rstrip()))
        else:
            print("%5d  %s" % (len(hits), path))
    print("— %d files, %d Spanish lines" % (len(report), total))
    if check and report:
        sys.exit(1)


if __name__ == "__main__":
    main()
