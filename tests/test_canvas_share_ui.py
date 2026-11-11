"""画布「分享」面板的前后端契约静态检查（零构建前端没有单元测试，这里只钉契约）。

为什么需要这一层：`muse_bridge/canvas/` 是零构建 ES module（没有 npm 测试链），
分享面板的 DOM id、`data-share-action` 到 app action 的映射、api.js 的
`DELETE /shares/{token}` 三者只要有一处被改名/删掉，页面就会静默坏掉。真机
验收在 `work/canvas_re/qa_canvas_share.py`（headless Chrome + 真 bridge），
本测试只做"改名就会红"的廉价护栏。

覆盖：
1. `index.html` 里 `#share-panel` / `#share-body` / `#share-status` / `#share-close`
   存在，且面板默认 hidden（不能在启动时闪出来）；
2. `ui.js` 渲染出的每个 `data-share-action` 都有处理分支，且对应 app action 名
   真的在 `app.js` 的 action map 里；
3. `api.js` 有 `revokeShare` 且用 `DELETE` 打 `/shares/<token>`；
4. 分享 URL 必须带 `?share=` capability 参数，且在面板里以只读输入框呈现。
"""

from __future__ import annotations

import re
from pathlib import Path

CANVAS = Path(__file__).resolve().parents[1] / "muse_bridge" / "canvas"


def read(name: str) -> str:
    return (CANVAS / name).read_text(encoding="utf-8")


def test_share_panel_markup_exists_and_starts_hidden() -> None:
    html = read("index.html")
    for element_id in (
        "share-panel",
        "share-body",
        "share-status",
        "share-close",
    ):
        assert f'id="{element_id}"' in html, f"index.html 缺 #{element_id}"
    panel = html[html.index('id="share-panel"') - 40 : html.index('id="share-panel"') + 120]
    assert "hidden" in panel, "分享面板必须在 HTML 里默认 hidden（避免启动闪层）"
    assert 'data-share-close' in html, "分享面板需要遮罩关闭入口"


def test_share_actions_are_all_wired() -> None:
    ui = read("js/ui.js")
    app = read("js/app.js")
    rendered = set(re.findall(r'''data-share-action=["'](\w+)''', ui))
    assert rendered, "ui.js 没有渲染任何 data-share-action"
    handlers = set(re.findall(r'action === "(\w+)"', ui))
    assert rendered <= handlers, f"未处理的分享动作：{sorted(rendered - handlers)}"
    mapping = {
        "copy": "copyShare",
        "rotate": "rotateShare",
        "revoke": "revokeShare",
        "create": "createShare",
        "retry": "refreshShare",
    }
    declared = set(
        re.findall(r"(?:^|\s)(\w+):\s*\(\)\s*=>\s*this\.", app, re.MULTILINE)
    )
    for ui_action, app_action in mapping.items():
        if ui_action in rendered:
            assert app_action in declared, f"app action map 缺 {app_action}"
            assert f"{app_action}(" in app, f"app.js 没有实现 {app_action}"


def test_api_client_revokes_with_delete() -> None:
    api = read("js/api.js")
    assert "async revokeShare(token)" in api
    block = api[api.index("async revokeShare(token)") :]
    block = block[: block.index("\n  }") + 4]
    assert "DELETE" in block, "revokeShare 必须用 DELETE"
    assert "/shares/${encodeURIComponent(token)}" in block
    # 撤销是写操作：请求默认带 key（_request 的 _headers 已处理），不能是裸 fetch
    assert "_request(" in block


def test_share_url_is_capability_param() -> None:
    ui = read("js/ui.js")
    assert "const absolute = `${window.location.origin}${share.url || \"\"}`" in ui
    assert 'id="share-url-input"' in ui
    assert "readonly" in ui
    app = read("js/app.js")
    assert "/canvas/?share=" in app, "只读分享入口必须仍带 ?share="
