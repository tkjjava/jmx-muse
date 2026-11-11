"""额度面板文本解析（本轮新增的独立实现，不动上游、也不依赖上游）。

实测来源：muse2api `engine.py:910-968` 的 `_parse_quota`，以及 `engine.py:813-820`
记录的 muse.ai 额度面板形态（Settings → General → Usage 区块）：

    Free plan
    Weekly limit resets on Sep 30
    1% used
    Additional tokens / Never expires / 0% used (2B tokens left)

判定原则（与上游一致，故意保留三条容易写错的细节）：

1. **只采 Usage 到 Language 之间**。设置面板里账号资料也带数字，不切开就会把
   用户名/ID 里的数字当成用量。
2. **0..100 之外一律拒绝**。页面噪声、繁体全角、"101% used" 这种脏值不能上报，
   否则上游计费与面板显示都会被一个假数字带跑。
3. **单独的 plan 标签不算读到额度**。`found=False` 时调用方必须保留旧缓存，
   绝不能用空结果覆盖（上游 `engine.py:966-967` 的同一条结论）。

NFKC 归一是必需的：繁体/全角界面会把百分比写成 `１２．７５％`，
不归一就正则匹配不到，读出来永远是 found=False。
"""

from __future__ import annotations

import re
import unicodedata

# Usage 段的起点与终点关键词（中英繁三形，实测 engine.py:917,920）
USAGE_HEADS = ("usage", "用量", "使用量")
USAGE_TAILS = ("language", "语言", "語言")

# 额外额度区块的起点：从这里往后是 "Additional tokens ... (2B tokens left)"
_EXTRA_BLOCK = re.compile(
    r"^(?:(?:Additional|Extra)\s+tokens|(?:額外|额外)(?:的)?(?:符元|符記|限额|代幣|代币|令牌|憑證|凭证|tokens))",
    re.I,
)

_RESET_EN = re.compile(r"Weekly limit resets? on ([^\n]+)", re.I)
_RESET_ZH = re.compile(
    r"(?:每(?:週|周|星期)|週|周)(?:上限|限額|限额|額度|额度)(?:會|会|將|将)?(?:於|于|在)\s*([^\n]+?)\s*(?:重設|重置)"
)

_PLAN = re.compile(r".{1,60}?(?:\s+plan|計劃|计划|方案)", re.I)
_NEVER = re.compile(r"Never expires|永不過期|永不过期", re.I)

_PCT_AFTER = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:used\b|已(?:使)?用)", re.I)
_PCT_BEFORE = re.compile(r"已(?:使)?用\s*(\d+(?:\.\d+)?)\s*%", re.I)


def _percentage(line: str) -> int | float | None:
    """从一行里取百分比；越界（<0 或 >100）一律当作没读到。"""
    match = _PCT_AFTER.search(line) or _PCT_BEFORE.search(line)
    if match is None:
        return None
    value = float(match.group(1))
    if not 0 <= value <= 100:
        return None
    return int(value) if value.is_integer() else value


def parse_quota_text(text: str) -> dict:
    """把额度面板的 innerText 解析成结构化字段。

    返回的 dict 一定有 `raw` 与 `found`；`found=False` 时绝不会出现
    `weekly_used_pct`，调用方据此保留旧缓存（见模块文档字符串第 3 条）。
    """
    normalized = unicodedata.normalize("NFKC", text or "")
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]

    # 只采 Usage 之后的段落，并在 Language 处截断。
    start = next(
        (index + 1 for index, line in enumerate(lines) if line.lower() in USAGE_HEADS),
        0,
    )
    lines = lines[start:]
    end = next(
        (index for index, line in enumerate(lines) if line.lower() in USAGE_TAILS),
        len(lines),
    )
    lines = lines[:end]

    out: dict = {"raw": "\n".join(lines[:40])}
    text_block = "\n".join(lines)

    for line in lines:
        if _PLAN.fullmatch(line):
            out["plan"] = line
            break

    reset = _RESET_EN.search(text_block) or _RESET_ZH.search(text_block)
    if reset:
        out["weekly_reset"] = reset.group(1).strip()

    extra_at = next(
        (index for index, line in enumerate(lines) if _EXTRA_BLOCK.match(line)),
        len(lines),
    )

    for line in lines[:extra_at]:
        value = _percentage(line)
        if value is not None:
            out["weekly_used_pct"] = value
            break

    for line in lines[extra_at:]:
        value = _percentage(line)
        if value is None:
            continue
        out["extra_used_pct"] = value
        bracket = re.search(r"\(([^)]+)\)", line)
        if bracket:
            out["extra_left"] = bracket.group(1).strip()
        break

    if _NEVER.search(text_block):
        out["extra_expires"] = "never"

    # 与上游 engine.py:966-967 一致：光有 plan 标签不算读到额度。
    out["found"] = "weekly_used_pct" in out
    return out
