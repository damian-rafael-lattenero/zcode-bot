"""selfcheck — the structural ritual of deploy/CI, as AST asserts.

Replaces the old grep line-counts in deploy.sh (same strictness,
structural instead of textual — a decorator or a nested def can no
longer slip a 4th symbol past the facade gate). Called by deploy.sh
and ci.yml right after check_no_duplicates.

Both checks encode ADR-012 (dismemberment by delegation):
  1. running_turns is defined exactly ONCE at core.py module level
     (a half-finished move leaves two copies behind).
  2. gateway.py stays a PURE facade: exactly process_update_sync,
     runtime_async and main — anything else is logic creeping back
     into the composition root.
"""
import ast
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src", "zcode_bot")
FACADE = ("process_update_sync", "runtime_async", "main")


def _module_level_defs(path):
    """Names of module-level def/async def/class, in file order."""
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)
    return [n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))]


def main():
    errors = []
    core_defs = _module_level_defs(os.path.join(SRC, "core.py"))
    n = core_defs.count("running_turns")
    if n != 1:
        errors.append("running_turns defined %d× in core.py (expected 1; "
                      "half-finished move?)" % n)
    gw_defs = _module_level_defs(os.path.join(SRC, "gateway.py"))
    if tuple(gw_defs) != FACADE:
        errors.append("gateway must be a pure facade of exactly %s — found: "
                      "%s" % (", ".join(FACADE), ", ".join(gw_defs) or "none"))
    if errors:
        for e in errors:
            print("✗ selfcheck: %s" % e)
        return 1
    print("✓ selfcheck: running_turns ×1 in core; gateway a pure facade")
    return 0


if __name__ == "__main__":
    sys.exit(main())
