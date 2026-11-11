"""选择器表的结构哨兵。

selectors.py 是 muse-bridge 唯一的"前端改版出血点"：表里少一项、或者写进一条
浏览器不认的选择器，故障要等到线上真正跑生成时才暴露。这里离线钉死四件事：

1. 每张表都非空、都是元组、元素都是非空字符串；
2. 每条选择器的括号/引号成对（能拼进 Playwright 的 `:is()` 且不抛）；
3. 同一张表内没有重复项（重复会让 `:is()` 变长但不带来任何候选）；
4. **旧候选一个都没丢**（SEND_BUTTONS 里那条无实测来源的 send-button 除外，
   它是本轮规格明确要求删除的），新增项按优先级排在前面。

第 4 条是重点：这张表的语义是"候选集合"，删一项就等于放弃一条兜底路径，
而前端改版时能救命的往往就是那条旧候选。
"""

from __future__ import annotations

import pytest

from muse_bridge import selectors

NEW_TABLES = (
    "ATTACHMENTS",
    "RESULT_MEDIA",
    "MEDIA_EXCLUDE",
    "HYDRATION",
    "COMPOSER_ROOT",
    "ERROR_NOTICES",
    "REMOVE_ATTACHMENT",
    "FILE_INPUTS",
    "THREAD_ROWS",
    "QUOTA_SETTINGS_ENTRY",
    "QUOTA_MENU_ITEM",
    "QUOTA_DIALOG",
    "UPLOAD_HINT",
)

ALL_TABLES = (
    "LOGGED_IN_MARKERS",
    "LOGGED_OUT_MARKERS",
    "COMPOSER_INPUTS",
    "SEND_BUTTONS",
    "ASSISTANT_MESSAGES",
    "STREAMING_MARKERS",
    "NEW_CHAT_BUTTONS",
    "REASONING_MESSAGES",
    *NEW_TABLES,
)

# 本轮之前就在表里的项。除 send-button 外，任何一项消失都是回归。
LEGACY_MUST_KEEP = {
    "LOGGED_IN_MARKERS": (
        '[data-testid="composer"]',
        "textarea[placeholder]",
        '[contenteditable="true"]',
        'button[aria-label*="Send"]',
        'button[aria-label*="发送"]',
    ),
    "LOGGED_OUT_MARKERS": (
        'a[href*="login"]',
        'button:has-text("Log in")',
        'button:has-text("登录")',
        'button:has-text("Sign in")',
    ),
    "COMPOSER_INPUTS": (
        "textarea[placeholder]",
        '[contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"]',
        "textarea",
    ),
    "SEND_BUTTONS": (
        'button[aria-label*="Send"]',
        'button[aria-label*="发送"]',
        'button[type="submit"]',
    ),
    "ASSISTANT_MESSAGES": (
        '[data-testid="assistant-message"]',
        '[data-role="assistant"]',
        'div[class*="assistant"]',
        'div[class*="message"][class*="ai"]',
    ),
    "STREAMING_MARKERS": (
        '[data-testid="streaming"]',
        'button[aria-label*="Stop"]',
        'button[aria-label*="停止"]',
        '[class*="cursor"][class*="blink"]',
    ),
    "NEW_CHAT_BUTTONS": (
        'button[aria-label*="New chat"]',
        'button[aria-label*="新对话"]',
        'a[href="/"]',
    ),
    "REASONING_MESSAGES": (
        '[data-testid="reasoning-content"]',
        '[data-testid="reasoning"] [class*="content"]',
        '[data-reasoning] p',
        '[class*="reasoning"] [class*="content"]',
        '[class*="thinking"] [class*="content"]',
    ),
}


def _quote_pairs_ok(candidate: str) -> bool:
    """属性值里的引号必须成对，而且不能出现裸的单引号包裹双引号那种混用。"""
    return (
        candidate.count('"') % 2 == 0
        and candidate.count("'") % 2 == 0
        and candidate.count("\\") % 2 == 0
    )


def _balanced(candidate: str) -> bool:
    depth_square = depth_round = 0
    for char in candidate:
        if char == "[":
            depth_square += 1
        elif char == "]":
            depth_square -= 1
        elif char == "(":
            depth_round += 1
        elif char == ")":
            depth_round -= 1
        if depth_square < 0 or depth_round < 0:
            return False
    return depth_square == 0 and depth_round == 0


@pytest.mark.parametrize("name", ALL_TABLES)
def test_table_is_non_empty_tuple_of_clean_strings(name: str):
    table = getattr(selectors, name)
    assert isinstance(table, tuple), f"{name} 必须是元组（防止运行时被改写）"
    assert table, f"{name} 不能是空表"
    for candidate in table:
        assert isinstance(candidate, str), f"{name} 里混进了非字符串：{candidate!r}"
        assert candidate.strip() == candidate and candidate.strip(), (
            f"{name} 里有空白项：{candidate!r}"
        )


@pytest.mark.parametrize("name", ALL_TABLES)
def test_every_candidate_is_a_usable_playwright_selector(name: str):
    table = getattr(selectors, name)
    assert len(set(table)) == len(table), f"{name} 有重复候选"
    for candidate in table:
        assert _balanced(candidate), f"{name}: 括号不成对 -> {candidate!r}"
        assert _quote_pairs_ok(candidate), f"{name}: 引号不成对 -> {candidate!r}"
        # first_match 就是 driver 实际使用的拼装方式：这里必须不抛。
        joined = selectors.first_match(table)
        assert joined.startswith(":is(") and joined.endswith(")"), joined
        assert joined.count("(") == joined.count(")"), joined


@pytest.mark.parametrize("name", ALL_TABLES)
def test_first_match_rejects_empty_table(name: str):
    with pytest.raises(ValueError):
        selectors.first_match(())


def test_legacy_candidates_survived():
    for name, legacy in LEGACY_MUST_KEEP.items():
        table = getattr(selectors, name)
        for candidate in legacy:
            assert candidate in table, f"{name} 丢了旧候选：{candidate!r}"


def test_fabricated_send_button_removed_and_zh_first():
    """`button[data-testid="send-button"]` 从来没有任何实测来源，本轮删掉；
    中文界面更常见，所以 发送 排在 Send 前面（engine.py:1075-1077 同时覆盖两者）。"""
    assert 'button[data-testid="send-button"]' not in selectors.SEND_BUTTONS
    assert selectors.SEND_BUTTONS[0] == 'button[aria-label*="发送"]'


def test_measured_candidates_come_first():
    """实测项必须排在猜测项之前：`:is()` 与逐条尝试都以顺序为优先级。"""
    # 2026-11-11 真机改判：助手正文容器（data-hatch-assistant-message-body）最精确，
    # 排第一；`data-role="assistant"` 在当前构建里一个都不存在（只在早先某次渲染
    # 出现过），只能垫底兜底；`hatch-chat-groupable-bubble` 两边气泡都命中，不能靠前。
    assert selectors.ASSISTANT_MESSAGES[0] == (
        '[data-hatch-assistant-message-body="true"]'
    )
    assert selectors.ASSISTANT_MESSAGES[1] == (
        '[data-message-item][data-message-role="assistant"]'
    )
    assert '[data-message-role="assistant"]' in selectors.ASSISTANT_MESSAGES[:3]
    assert '[data-message-item]' in selectors.ASSISTANT_MESSAGES
    assert selectors.STREAMING_MARKERS[0] == (
        '[data-testid="hatch-composer-stop-button"]'
    )
    assert selectors.COMPOSER_INPUTS[0] == "[data-hatch-composer-root] textarea"
    assert selectors.COMPOSER_INPUTS[1] == '[data-lexical-editor="true"]'


def test_attachment_prefix_family_is_exact():
    """`hatch-chat-attachment-presentation-` 必须保持前缀匹配（后缀是 image/video
    等类型片段），写成精确匹配就会一条都命中不了（engine.py:41 的 ATT_SEL）。"""
    assert selectors.ATTACHMENTS == ('[data-testid^="hatch-chat-attachment-presentation-"]',)
    assert selectors.REMOVE_ATTACHMENT[0] == 'button[aria-label*="Remove attachment"]'
    assert any("移除附件" in c for c in selectors.REMOVE_ATTACHMENT)
    assert selectors.UPLOAD_HINT == ('[class*="group/widget-presentation"]',)


def test_reasoning_table_is_still_marked_as_unconfirmed():
    """思考区候选至今没有活网确认，注释必须继续说清楚，不能让人误当实测项。"""
    path = selectors.__file__
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    block = source.split("REASONING_MESSAGES = (")[0]
    assert "未活网确认" in block, "REASONING_MESSAGES 的注释要标明这些只是候选"


# ---- 助手消息提取：真机 DOM 的角色判定 --------------------------------


def test_assistant_selectors_prefer_role_attributes():
    """真机实测：用户气泡同样带 `hatch-chat-groupable-bubble`，文本是"你：…"。

    2026-11-11 真机 muse.ai DOM 把顺序钉死：助手正文是
    `DIV[data-hatch-assistant-message-body="true"]`（最精确），用户消息项带
    `data-message-role` / `data-message-side` = "user" 且渲染 "你：\\n\\n<提问>"。
    `[data-role="assistant"]` 在当前构建里一个都不存在（早先某次渲染出现过），
    所以它只能排在正文容器之后当兜底。通用类名放首位时，"最后一条"在刚发出
    提问的瞬间就是用户气泡，回答里会混进提问原文。
    """
    order = list(selectors.ASSISTANT_MESSAGES)
    body = '[data-hatch-assistant-message-body="true"]'
    generic = 'div[class*="hatch-chat-groupable-bubble"]'
    assert order[0] == body
    assert body in order and generic in order, "旧兜底候选不能删：前端改版时它救命"
    assert order.index(generic) > order.index(body)
    assert '[data-role="assistant"]' in order
    assert order.index('[data-role="assistant"]') > order.index(body)


def test_user_bubble_class_never_outranks_role_criteria():
    """用户气泡同款类名（两边都命中）不得排在按角色/正文判定的候选前面。

    用户气泡的 inner_text 是 "你：\\n\\n<提问>"。通用类名一旦靠前，
    "最后一条"在刚发出提问的瞬间就是用户气泡，回答里就会粘上提问原文——
    这是旧 bug 的现场表现，不是理论风险。
    """
    order = list(selectors.ASSISTANT_MESSAGES)
    generic = 'div[class*="hatch-chat-groupable-bubble"]'
    role_based = [
        index
        for index, item in enumerate(order)
        if "data-message-role" in item or "data-role" in item
    ]
    assert role_based, "至少要有一条按角色判定的候选"
    assert order.index(generic) > max(role_based), (
        f"用户气泡同款类名排在了角色判据前面：{order}"
    )
    body = '[data-hatch-assistant-message-body="true"]'
    assert order.index(body) < order.index(generic), (
        "助手正文容器是当前构建里最精确的判据，必须在通用类名之前"
    )


class _FakeNode:
    def __init__(self, text: str, *, is_user: bool) -> None:
        self.text = text
        self.is_user = is_user

    async def evaluate(self, _js: str) -> bool:
        return self.is_user

    async def inner_text(self) -> str:
        return self.text


class _FakeLocator:
    def __init__(self, nodes: list[_FakeNode]) -> None:
        self.nodes = nodes

    async def count(self) -> int:
        return len(self.nodes)

    def nth(self, index: int) -> _FakeNode:
        return self.nodes[index]


class _FakePage:
    def __init__(self, nodes: list[_FakeNode]) -> None:
        self.nodes = nodes

    def locator(self, _selector: str) -> _FakeLocator:
        return _FakeLocator(self.nodes)


async def test_assistant_text_skips_user_bubbles_even_on_generic_selector():
    """即使只剩通用类名兜底，也不能把用户气泡（"你：…"）当回答返回。"""
    from muse_bridge.driver import MuseDriver

    driver = MuseDriver.__new__(MuseDriver)
    driver._page = _FakePage(
        [
            _FakeNode("嘿！我是你的私人助手", is_user=False),
            _FakeNode("你：\n\n1+1 等于几？", is_user=True),
        ]
    )
    assert await driver._assistant_text() == "嘿！我是你的私人助手"


async def test_assistant_text_prefers_last_assistant_when_user_is_last():
    """最后一条是用户气泡时，必须回退到前一条助手气泡。"""
    from muse_bridge.driver import MuseDriver

    driver = MuseDriver.__new__(MuseDriver)
    driver._page = _FakePage(
        [
            _FakeNode("第一轮回答", is_user=False),
            _FakeNode("第二轮回答", is_user=False),
            _FakeNode("你：\n\n第三问", is_user=True),
        ]
    )
    assert await driver._assistant_text() == "第二轮回答"
