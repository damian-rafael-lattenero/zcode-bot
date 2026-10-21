"""conftest — the ONE place that puts ../src and tests/ on sys.path.

Pytest loads this before any test module, so the per-file
`TESTS_DIR = …; sys.path.insert(…)` boilerplate is gone. The house
rule that once forbade conftest (determinism) is covered by running
the suite from the repo root AND from tests/ in both xdist modes —
the paths here are __file__-relative, never cwd-relative.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "src"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
