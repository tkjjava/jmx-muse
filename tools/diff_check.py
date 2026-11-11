"""Differential check: the stream-usage tests must FAIL before the fix and PASS after.

A test that passes both ways proves nothing. This toggles the real flag in the app module
(wants_stream_usage forced to False = the pre-fix behaviour: no usage frame on streams),
runs the ASGI tests against that simulated pre-fix state and against the fixed state, and
asserts the results differ. The file is restored in every case (finally + a re-read guard).

Each bridge declares its own anchor: hark's wants_stream_usage is a multi-line bool() whose
last argument line can become `and False` (the closing paren is on the next line), while
muse's is a single line where that replacement would comment out the trailing paren — so
muse swaps the whole inner expression for `False`.

Usage: run from a bridge root, or `python diff_check.py <bridge-root-dir>`.
"""
import subprocess
import sys
from pathlib import Path

# pkg -> (app_path, unique_anchor, pre_fix_replacement)
TARGETS = {
    'hark_bridge': ('hark_bridge/app.py',
                    'and self.stream_options.get("include_usage")',
                    'and False'),
    'muse_bridge': ('muse_bridge/app.py',
                    'isinstance(self.stream_options, dict) and self.stream_options.get("include_usage")',
                    'False'),
}


def _pytest(root: Path, test: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, '-m', 'pytest', test, '-q', '--tb=no',
                           '-p', 'no:cacheprovider'], cwd=str(root),
                          capture_output=True, text=True, encoding='utf-8')


def run(pkg: str, rel: str, old: str, new: str, root: Path) -> bool:
    p = root / rel
    original = p.read_text(encoding='utf-8')
    if original.count(old) != 1:
        print(f'[{pkg}] anchor count {original.count(old)} != 1 — refusing to guess')
        return False
    test = 'tests/test_stream_usage.py'
    try:
        p.write_text(original.replace(old, new), encoding='utf-8')
        pre = _pytest(root, test)
        p.write_text(original, encoding='utf-8')
        post = _pytest(root, test)
    finally:
        if p.read_text(encoding='utf-8') != original:
            p.write_text(original, encoding='utf-8')
    tail_pre = (pre.stdout.strip().splitlines() or ['(no output)'])[-1]
    tail_post = (post.stdout.strip().splitlines() or ['(no output)'])[-1]
    print(f'[{pkg}] PRE-FIX  rc={pre.returncode}  {tail_pre}')
    print(f'[{pkg}] POST-FIX rc={post.returncode} {tail_post}')
    if pre.returncode != 0 and post.returncode == 0:
        print(f'[{pkg}] differential CONFIRMED: the tests catch the billing bug.')
        return True
    print(f'[{pkg}] differential NOT proven.')
    return False


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    results = []
    for pkg, (rel, old, new) in TARGETS.items():
        if (root / rel).exists():
            results.append(run(pkg, rel, old, new, root))
    if not results:
        print(f'no bridge app found under {root}')
        return 2
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
