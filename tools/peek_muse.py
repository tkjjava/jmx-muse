"""只读取证：muse 页面现在的 DOM 状态（排障用，不点击不输入）。

## 为什么需要它

"发送后 45 秒页面没有任何反应"这类故障，只看 bridge 日志分不清是**消息没发出去**
还是**发出去但读不到回答**。2026-11-11 就是靠"手工往同一标签发一条消息能秒回"
才把范围缩到判据问题。那次用的是临时脚本，换个人复现不了。

这个工具把取证动作固定下来：

* 列出 Chrome 里所有标签页（含非 muse 的），标出**驱动会选中的那个**
  （`context.pages` 里第一个 URL 含 `muse.ai` 的）；
* 每个 muse 标签打印：标题、输入框数量、最后一条助手正文、最后一条消息 id、
  最后一条用户消息文本；
* 可选截图（`--screenshot`），以及 `--json` 机读输出。

## 用法

    python tools/peek_muse.py                  # 看第一个（驱动会选中的）muse 标签
    python tools/peek_muse.py --all-tabs       # 所有标签都列出来
    python tools/peek_muse.py --json           # 机读输出
    python tools/peek_muse.py --screenshot shot.png

## 边界（重要）

* **只读**：不点击、不输入、不刷新。它看到的是"当前这一刻"的页面；
* 如果 bridge 正在跑一个回合，读到的可能是中间态——取证时先确认没有在途请求；
* 需要 Chrome 以 `--remote-debugging-port` 启动，且 `playwright` 已安装；
* 连不上 CDP 时以退出码 2 结束（区别于"页面状态异常"的 1）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

JS_STATE = r"""
() => {
  const text = (el) => (el && (el.innerText || '')).trim();
  const composers = Array.from(document.querySelectorAll(
    'textarea, [contenteditable="true"][role="textbox"]'
  ));
  const assistants = Array.from(document.querySelectorAll(
    '[data-message-item][data-message-role="assistant"]'
  ));
  const users = Array.from(document.querySelectorAll(
    '[data-message-item][data-message-role="user"]'
  ));
  const lastAssistant = assistants[assistants.length - 1];
  const lastUser = users[users.length - 1];
  const bubbles = Array.from(document.querySelectorAll(
    '[data-hatch-assistant-message-body="true"]'
  ));
  return {
    title: document.title || '',
    url: location.href,
    composerCount: composers.length,
    composerValue: composers.length
      ? (('value' in composers[0]) ? composers[0].value : text(composers[0]))
      : '',
    assistantCount: assistants.length,
    lastAssistantText: text(lastAssistant).slice(0, 200),
    lastAssistantId: lastAssistant
      ? (lastAssistant.getAttribute('data-message-id') || '')
      : '',
    lastUserText: text(lastUser).slice(0, 120),
    bodyAssistantBubbles: bubbles.length,
    bodyLength: (document.body && document.body.innerText || '').length
  };
}
"""


def cdp_url() -> str:
    return os.environ.get("MUSE_TOOLS_CDP_URL", "http://127.0.0.1:9222").strip()


def collect(pages: list, want_all: bool, screenshot: str | None) -> dict:
    all_pages = [(i, p) for i, p in enumerate(pages)]
    muse = [(i, p) for i, p in all_pages if "muse" in (p.url or "")]
    report: dict = {"cdp": cdp_url(), "tabs_total": len(all_pages),
                    "muse_tabs": len(muse), "tabs": []}

    for index, page in (all_pages if want_all else muse):
        entry: dict = {"tab": index, "driver_pick": index == (muse[0][0] if muse else -1)}
        try:
            entry.update(page.evaluate(JS_STATE))
        except Exception as exc:  # 页面正在导航等瞬态
            entry["error"] = f"{type(exc).__name__}: {exc}"
        report["tabs"].append(entry)

    if screenshot:
        target = next((p for _, p in muse), None)
        if target is None:
            report["screenshot_error"] = "没有 muse 标签可截"
        else:
            try:
                target.screenshot(path=screenshot, full_page=False)
                report["screenshot"] = screenshot
            except Exception as exc:
                report["screenshot_error"] = f"{type(exc).__name__}: {exc}"
    return report


def print_human(report: dict) -> None:
    print(f"CDP: {report['cdp']}")
    print(f"标签总数 {report['tabs_total']}，其中 muse {report['muse_tabs']} 个")
    if report["muse_tabs"] == 0:
        print("没有 muse 标签：bridge 会去开新页，或者直接报'没有 muse.ai 的标签页'。")
    for tab in report["tabs"]:
        mark = " ← 驱动会选它" if tab.get("driver_pick") else ""
        print(f"\n--- tab#{tab['tab']}{mark} ---")
        if tab.get("error"):
            print(f"  读取失败：{tab['error']}")
            continue
        print(f"  url: {tab.get('url', '')[:100]}")
        print(f"  title: {tab.get('title', '')[:80]}")
        print(f"  输入框: {tab.get('composerCount')} 个"
              + (f"，当前内容 {tab.get('composerValue')!r}" if tab.get("composerValue") else "（空）"))
        print(f"  助手消息: {tab.get('assistantCount')} 条"
              f"（正文容器 {tab.get('bodyAssistantBubbles')} 个）")
        print(f"  最后一条助手 id: {tab.get('lastAssistantId') or '(读不到)'}")
        print(f"  最后一条助手正文: {tab.get('lastAssistantText')!r}")
        print(f"  最后一条用户消息: {tab.get('lastUserText')!r}")
    if report.get("screenshot"):
        print(f"\n截图: {report['screenshot']}")
    if report.get("screenshot_error"):
        print(f"\n截图失败: {report['screenshot_error']}")

    print("\n怎么用这份信息：")
    print("  * 输入框有残留文本 -> 上一轮「填了但没发出去」，看发送按钮与通道；")
    print("  * 最后一条助手 id 与发送前相同 -> 页面确实没有新消息（真没反应）；")
    print("  * 最后一条助手 id 变了但 bridge 报没反应 -> 判据问题（0.4.2 已修）；")
    print("  * 正文容器 0 个但助手消息 > 0 -> 前端改版，selectors.py 需要更新。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读取证 muse 页面 DOM 状态")
    parser.add_argument("--all-tabs", action="store_true", help="所有标签都列出来")
    parser.add_argument("--json", action="store_true", help="机读输出")
    parser.add_argument("--screenshot", metavar="PATH", help="把驱动会选的标签截一张图")
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺 playwright：pip install playwright（不需要下载浏览器，只连 CDP）",
              file=sys.stderr)
        return 2

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url())
            pages = [p for ctx in browser.contexts for p in ctx.pages]
            report = collect(pages, args.all_tabs, args.screenshot)
            browser.close()
    except Exception as exc:
        print(f"连不上 CDP {cdp_url()}：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("先确认 Chrome 带 --remote-debugging-port=9222 启动。", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print_human(report)
    return 0 if report["muse_tabs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
