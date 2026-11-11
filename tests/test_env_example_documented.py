"""`.env.example` 必须覆盖代码真正读取的每一个 MUSE_* 变量。

为什么值得一条测试：这份示例是使用者唯一能看到的配置面。少写一个变量，对应功能
就只能靠读源码才发现（`MUSE_REF_LOCAL_FILES`、`MUSE_REF_PRIVATE_FETCH` 这类"默认
拒绝"的安全开关尤其危险——不知道它存在的人不会去确认它关着）；多写一个变量更糟：
照着配了却毫无作用。

2026-11-11 实测的漂移：代码读 47 个，示例只写了 22 个，其中两个安全开关完全没被
提到；反向没有凭空多写的项。本测试把两个方向都钉住。

判定方式是静态扫描源码里"被当作配置读取"的 `MUSE_*` 字面量：
  * 出现在 `env_*` / `_flag` / `_env_bytes` / `os.environ.get` / `os.getenv` 的调用参数里；
  * 或 `store.py` 用 `DB_ENV = "MUSE_BRIDGE_DB"` 这种变量名间接读取。
注释与文案里提到的名字不算配置。
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "muse_bridge"
ENV_EXAMPLE = REPO / ".env.example"

_LITERAL = re.compile(r"""['"]([A-Z][A-Z0-9_]{2,})['"]""")
_READ_CONTEXT = re.compile(
    r"(?:env\w*|_flag|_raw|_env_bytes|environ\.get|getenv|environ)\s*[\(\[]\s*$"
)
_INDIRECT = re.compile(r"""DB_ENV\s*=\s*['"]([A-Z0-9_]+)['"]""")


def names_read_by_code(source: Path = SOURCE) -> set[str]:
    """源码里被当作配置读取的 MUSE_* 名字。"""
    names: set[str] = set()
    for path in sorted(source.rglob("*.py")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            for match in _LITERAL.finditer(line):
                name = match.group(1)
                if not name.startswith("MUSE_"):
                    continue
                before = line[: match.start()]
                tail = line[match.end() :].lstrip()
                if _READ_CONTEXT.search(before) or tail.startswith((",", "]")):
                    names.add(name)
    store = source / "store.py"
    if store.exists():
        for match in _INDIRECT.finditer(store.read_text(encoding="utf-8", errors="replace")):
            names.add(match.group(1))
    return names


def names_documented(path: Path = ENV_EXAMPLE) -> set[str]:
    """`.env.example` 里真正赋值的变量名（注释掉的示例不算）。"""
    documented: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key.startswith("MUSE_"):
            documented.add(key)
    return documented


def test_every_configured_variable_is_documented():
    missing = sorted(names_read_by_code() - names_documented())
    assert not missing, "代码读取但 .env.example 没写：「" + "」「".join(missing) + "」"


def test_example_declares_no_phantom_variable():
    extra = sorted(names_documented() - names_read_by_code())
    assert not extra, "示例声明了但代码从不读取：「" + "」「".join(extra) + "」"


def test_scanner_sees_the_indirectly_read_variable():
    """守住扫描器本身：`MUSE_BRIDGE_DB` 只以变量名形式出现，漏了它上面两条测试就失明。"""
    assert "MUSE_BRIDGE_DB" in names_read_by_code()


def test_scanner_ignores_comment_and_prose_mentions(tmp_path: Path):
    """注释里提到的名字不能算配置，否则测试会把文档里的讨论误判成配置项。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        textwrap.dedent(
            """
            import os

            # 下面这些只出现在注释/文档字符串里，不是配置：
            #   MUSE_ONLY_IN_COMMENTS
            \"\"\"也提到 MUSE_ONLY_IN_DOCSTRING 这个名字。\"\"\"

            REAL = os.environ.get("MUSE_REALLY_READ", "0")
            """
        ),
        encoding="utf-8",
    )
    assert names_read_by_code(pkg) == {"MUSE_REALLY_READ"}
