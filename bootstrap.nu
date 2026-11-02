#!/Users/lattenero/.local/bin/nu-nightly
# bootstrap.nu — prepares the bot runtime: Python 3.13 (uv standalone)
# + venv ~/.zcode/hooks/venv + pinned dependencies. Idempotent.
#
# Why uv (ADR-010, Oct 2026): the system Python 3.9 has been EOL since
# 2025; Homebrew-python exists for Homebrew formulas and may break on
# upgrades (pydevtools.com/handbook: "Use uv to manage Python instead");
# uv installs official standalone builds (python-build-standalone) in
# user-space, no sudo, native arm64.
#
# Migrated from bootstrap.sh on 2026-11-02. uv is a prerequisite now (no
# `curl | sh`: the old one-liner was the last embedded bash in this repo).

def main []: nothing -> nothing {
    let repo = $env.FILE_PWD
    cd $repo
    let home = $nu.home-dir
    let venv = ($home | path join ".zcode/hooks/venv")
    let py = ($venv | path join "bin/python3")

    if (which uv | is-empty) {
        print -e "✗ uv is not installed — install it with: brew install uv"
        print -e "  (the old script piped astral's installer through sh; that is the embedded bash this migration removes)"
        exit 1
    }

    print "▶ Python 3.13 (uv standalone)"
    ^uv python install 3.13

    if not ((do { ^test -x $py } | complete).exit_code == 0) {
        print $"▶ creating ($venv)"
        ^uv venv $venv --python 3.13
    }

    print "▶ dependencies (requirements.txt, pinned)"
    ^uv pip install --python $venv -r requirements.txt

    ^$py --version
    ^$py -c "import aiohttp; print('aiohttp', aiohttp.__version__)"
    print "✓ runtime ready. deploy.nu uses this venv automatically."
    print $"  \(LaunchAgents must point to ($py) — see docs/runbook.md\)"
}
