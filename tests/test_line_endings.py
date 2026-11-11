"""交付物文件格式哨兵：不得出现 \\r\\r\\n 行尾。

这类污染在 Python 里完全合法（多出来的都是空行），pytest 全绿也发现不了；
它曾经让 6 个文件（含两个 README）每行之间多一个空行、markdown 每行渲染成
独立段落，同时悄悄打断了变异台的跨行锚点。这里钉死它。
"""

from __future__ import annotations

from pathlib import Path

BAD = b'\r\r\n'
ROOT = Path(__file__).resolve().parent.parent
SKIP = {'__pycache__', '.pytest_cache', '.ruff_cache', 'data', '.git', 'node_modules'}
EXTS = {'.py', '.md', '.toml', '.json', '.txt', '.example', '.cfg', '.ini'}


def _shipped_files():
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file():
            continue
        if any(part in SKIP for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix not in EXTS and path.name != '.env.example':
            continue
        yield path


def test_no_doubled_carriage_returns():
    offenders = [str(p.relative_to(ROOT)) for p in _shipped_files() if BAD in p.read_bytes()]
    assert offenders == [], f'这些文件的行尾被写成 \\r\\r\\n：{offenders}'
