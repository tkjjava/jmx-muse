"""tools/ 的可移植性与文档一致性钉子。

三条约束都来自真实踩过的坑：

1. **能编译**：本会话里新写的工具曾因为把中文引号写成 ASCII 双引号而 SyntaxError，
   而 pytest 只跑 `tests/`，工具坏了没人发现。这里对每个 `tools/*.py` 调 compile()。
2. **README 必须登记**：`tools/README.md` 是唯一的工具索引。加了脚本不登记，
   下一个人根本不知道它存在——这正是 `work/` 里那批一次性探针的老问题。
3. **源码不许留本机绝对路径**：早期探针把 `D:\\…` 写死在默认参数里，
   别人拿到直接跑不起来。允许文档字符串里**提到**这类路径（那是解释历史），
   只禁止它作为字符串常量**开头**（那才是可用的默认值）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
README = TOOLS / "README.md"
DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def tool_files() -> list[Path]:
    return sorted(TOOLS.glob("*.py"))


@pytest.mark.parametrize("path", tool_files(), ids=lambda p: p.name)
def test_tool_compiles(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")  # SyntaxError 会直接抛


def test_every_tool_is_documented_in_readme() -> None:
    documented = set(re.findall(r"`([a-z0-9_]+\.py)`", README.read_text(encoding="utf-8")))
    actual = {p.name for p in tool_files()}
    missing = sorted(actual - documented)
    assert not missing, f"tools/README.md 没登记这些脚本：{missing}"
    phantom = sorted(documented - actual)
    assert not phantom, f"tools/README.md 登记了不存在的脚本：{phantom}"


@pytest.mark.parametrize("path", tool_files(), ids=lambda p: p.name)
def test_no_absolute_path_constants(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and DRIVE_PATH.match(node.value)
    ]
    assert not offenders, f"{path.name} 里有本机绝对路径常量：{offenders[:3]}"


@pytest.mark.parametrize("path", tool_files(), ids=lambda p: p.name)
def test_tool_has_main_guard(path: Path) -> None:
    """静态检查入口形态：定义了 main()，且有 `if __name__ == "__main__"` 守卫。

    **刻意不去跑 `--help`**：这批工具里有一半不用 argparse（`--help` 会被当成
    普通参数，直接跑起真业务——本会话第一次写这条测试时就踩了，可能消耗上游额度）。
    这里只证明"入口存在且可被 import 而不执行"，不付出任何活网代价。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "main" in functions, f"{path.name} 没有 main() 入口"

    guarded = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            left = node.test.left
            if (isinstance(left, ast.Name) and left.id == "__name__"
                    and len(node.test.comparators) == 1
                    and isinstance(node.test.comparators[0], ast.Constant)
                    and node.test.comparators[0].value == "__main__"):
                guarded = True
    assert guarded, f"{path.name} 缺 `if __name__ == \"__main__\"` 守卫（import 会执行业务）"
