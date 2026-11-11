"""文档断言审计：行号引用还能不能解析、含糊时刻有多少。

## 为什么需要它

文档里的 `driver.py:233` 这种引用是**可失效的断言**：代码一改动，某个引用就可能
指到一行无关的注释，而读者不会去核对——它比"没有引用"更有害。2026-11-11 的 C3 轮
手工审过一次，结论是"仓库内引用 0 处陈旧"；可这个结论只对当天那个提交成立。

这个脚本把口径固定下来，随时可复查：

1. **面向本仓库的文档**（CHANGELOG / README / tools/README / USAGE / LIVE_RUNBOOK /
   roadmap / 本候选报告）：其中的 `file.py:123` 引用**必须**能解析到仓库内的非空行。
   解析不到、行号越界、指向空行，三种都算失效引用。
2. **逆向记录类文档**（PROTOCOL / FRONTEND_RE / CANVAS_* 等）：它们引用的是**上游
   产物**（`engine.py:554`、上游 `app.py:1100`），本仓库不持有，因此只统计条数，
   不判定——否则裸文件名回退会把上游 `app.py` 错认成本仓库 `muse_bridge/app.py`。
3. 顺带统计 `16:1x` 这类含糊时刻（含义见 CHANGELOG 顶部「时间约定」）。

## 用法

    python tools/doc_assertions.py              # 审计并打印结论
    python tools/doc_assertions.py --json       # 机读输出
    python tools/doc_assertions.py --strict     # 失效引用 或 含糊时刻 > 0 就失败

退出码：0 = 仓库内引用全部可解析；1 = 有失效引用（`--strict` 下含糊时刻也算）。

## 边界

* 只认 `file.py:123` / `file.py`:123 这类显式写法；中文"（第 123 行）"不算——
  它不是稳定的机器可读形式，强行解析只会制造假阳性；
* 行号审计证明的是"这一行存在且非空"，**不**证明该行仍写着文档声称的内容；
  后者只能靠人复核，脚本不冒充做到。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 面向本仓库的文档：这里的引用必须可解析。
REPO_DOCS = [
    REPO / "CHANGELOG.md",
    REPO / "README.md",
    REPO / "README.en.md",
    REPO / "tools" / "README.md",
    REPO / "docs" / "USAGE.md",
    REPO / "docs" / "LIVE_RUNBOOK.md",
    REPO / "docs" / "2026-11-11-roadmap.md",
    REPO / "docs" / "RELEASE_CANDIDATE.md",
]

# `file.py:123` 或 `file.py`:123 或 `file.py:123-456`。
# 路径部分必须以已知扩展名结尾：URL 里的 `127.0.0.1:8788`、`example.com:8080`
# 也是"字符 + 冒号 + 数字"，不限制会把外部引用数虚增。
CODE_SUFFIXES = (".py", ".md", ".toml", ".yaml", ".yml", ".json", ".js", ".ts", ".ps1")
REF = re.compile(
    r"`?(?P<path>[A-Za-z0-9_./\\-]+(?:" + "|".join(re.escape(s) for s in CODE_SUFFIXES) + r"))"
    r"[`:]+(?P<line>\d+)(?:\s*-\s*(?P<end>\d+))?`?"
)
VAGUE_TIME = re.compile(r"\b[01]?\d:\d{1,2}[xX]\b")


def repo_docs() -> list[Path]:
    return [p for p in REPO_DOCS if p.is_file()]


def upstream_docs() -> list[Path]:
    return [p for p in sorted((REPO / "docs").glob("*.md")) if p not in REPO_DOCS]


def _byname() -> dict[str, list[Path]]:
    byname: dict[str, list[Path]] = {}
    for path in REPO.rglob("*.py"):
        if {"__pycache__", ".git"} & set(path.parts):
            continue
        byname.setdefault(path.name, []).append(path)
    return byname


BYNAME = _byname()


def resolve_ref(raw_path: str) -> Path | None:
    """先按仓库内路径找；找不到再按 basename 找唯一命中（文档常写裸文件名）。"""
    norm = raw_path.replace("\\", "/").lstrip("/")
    direct = REPO / norm
    if direct.is_file():
        return direct
    hits = BYNAME.get(norm.rsplit("/", 1)[-1], [])
    if len(hits) == 1:
        return hits[0]
    for hit in hits:
        if hit.relative_to(REPO).as_posix().endswith(norm):
            return hit
    return None


def audit() -> dict:
    stale: list[dict] = []
    resolved = 0
    vague: dict[str, int] = {}
    vague_total = 0

    for doc in repo_docs():
        text = doc.read_text(encoding="utf-8", errors="replace")
        rel_doc = doc.relative_to(REPO).as_posix()
        for _match in VAGUE_TIME.finditer(text):
            vague[rel_doc] = vague.get(rel_doc, 0) + 1
            vague_total += 1
        for match in REF.finditer(text):
            target = resolve_ref(match.group("path").replace("\\", "/"))
            start = int(match.group("line"))
            end = int(match.group("end")) if match.group("end") else None
            if target is None:
                stale.append({"doc": rel_doc, "ref": match.group(0).strip("`"),
                              "reason": "无法解析到仓库内文件"})
                continue
            resolved += 1
            source = target.read_text(encoding="utf-8", errors="replace").splitlines()
            if start > len(source) or (end is not None and (end > len(source) or end < start)):
                stale.append({"doc": rel_doc, "ref": match.group(0).strip("`"),
                              "reason": f"越界（文件共 {len(source)} 行）"})
            elif not source[start - 1].strip():
                stale.append({"doc": rel_doc, "ref": match.group(0).strip("`"),
                              "reason": "指向空行"})

    # 逆向记录里的引用只统计，不判定（本仓库不持有那些上游产物）。
    upstream = 0
    for doc in upstream_docs():
        text = doc.read_text(encoding="utf-8", errors="replace")
        upstream += len(REF.findall(text))
        rel_doc = doc.relative_to(REPO).as_posix()
        for _match in VAGUE_TIME.finditer(text):
            vague[rel_doc] = vague.get(rel_doc, 0) + 1
            vague_total += 1

    return {
        "docs_scanned": len(repo_docs()) + len(upstream_docs()),
        "repo_docs": len(repo_docs()),
        "upstream_docs": len(upstream_docs()),
        "stale": stale,
        "stale_count": len(stale),
        "resolved_count": resolved,
        "upstream_ref_count": upstream,
        "vague_time_total": vague_total,
        "vague_time_by_doc": dict(sorted(vague.items(), key=lambda kv: -kv[1])),
    }


def _utf8_console() -> None:
    """Windows 控制台默认 GBK：带 ✓/✗ 的中文输出会 UnicodeEncodeError 崩在最后一行。

    审计本身已经跑完了，却在打印结论时抛异常——退出码因此变成 1，看起来像"文档失效"。
    这里统一把 stdout/stderr 换成 UTF-8（不支持时静默跳过，非 Windows 无影响）。
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(description="文档断言审计")
    parser.add_argument("--json", action="store_true", help="机读输出")
    parser.add_argument("--strict", action="store_true", help="含糊时刻 > 0 也算失败")
    args = parser.parse_args(argv)

    result = audit()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["stale"] else 1

    print(f"扫描 {result['docs_scanned']} 份文档"
          f"（面向本仓库 {result['repo_docs']} / 逆向记录 {result['upstream_docs']}）")
    print(f"\n[仓库内引用] 可解析 {result['resolved_count']} 条；失效 "
          f"{result['stale_count']} 条")
    for item in result["stale"]:
        print(f"  ✗ {item['doc']}：{item['ref']} —— {item['reason']}")
    if not result["stale"]:
        print("  ✓ 全部可解析到非空行")

    print(f"\n[上游产物引用] {result['upstream_ref_count']} 条（逆向记录里指向本仓库"
          "不持有的文件，只统计不判定）")

    print(f"\n[含糊时刻] {result['vague_time_total']} 处（含义见 CHANGELOG 顶部「时间约定」）")
    for doc, count in result["vague_time_by_doc"].items():
        print(f"  {doc}: {count}")

    failed = bool(result["stale"])
    if args.strict and result["vague_time_total"]:
        print("\n--strict：含糊时刻 > 0，按失败处理")
        failed = True
    print("\n结论：" + ("未通过" if failed else "通过") + "（行号引用层面）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
