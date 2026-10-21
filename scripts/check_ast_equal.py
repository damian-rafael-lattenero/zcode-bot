"""check_ast_equal.py — proves two Python files differ only in comments
and/or docstrings. This is P1's safety contract (F53): translating prose
must never change code.

Usage:
  python3 scripts/check_ast_equal.py <ref_file> <new_file>

Comments never reach the AST, and docstrings are normalized to a marker, so
a comment/docstring-only rewrite compares equal by construction. Exit 0 =
identical; exit 1 = the code itself changed.

Batch use during P1 (verify every modified .py against HEAD):
  for f in $(git diff --name-only -- '*.py'); do
      git show "HEAD:$f" > /tmp/ref.py
      python3 scripts/check_ast_equal.py /tmp/ref.py "$f" || echo "CHANGED: $f"
  done
"""
import ast
import sys


def _normalize_docstrings(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body[0].value.value = "<docstring>"
    return tree


def same(ref_path, new_path):
    with open(ref_path, encoding="utf-8") as f:
        a = _normalize_docstrings(ast.parse(f.read(), filename=ref_path))
    with open(new_path, encoding="utf-8") as f:
        b = _normalize_docstrings(ast.parse(f.read(), filename=new_path))
    return ast.dump(a) == ast.dump(b)


def main():
    if len(sys.argv) != 3:
        print("usage: check_ast_equal.py <ref> <new>")
        sys.exit(2)
    if same(sys.argv[1], sys.argv[2]):
        print("OK ast-equal: %s" % sys.argv[2])
        return
    print("AST DIFFERS (code changed, not just prose): %s" % sys.argv[2])
    sys.exit(1)


if __name__ == "__main__":
    main()
