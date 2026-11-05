"""DOM 选择器表。

muse 走加密 Noise WebSocket，纯协议拿不到正文，只能从页面 DOM 读。
前端一改版这里就会失效——所有选择器集中在此，方便单点修复。

每条选择器都是一个候选列表，按顺序尝试，第一个命中的胜出。
这样即使前端换了类名，只要有一个旧选择器还在就能继续工作。
"""

from __future__ import annotations

# 登录态判定的候选。命中任一即认为已登录。
LOGGED_IN_MARKERS = (
    '[data-testid="composer"]',
    "textarea[placeholder]",
    '[contenteditable="true"]',
    'button[aria-label*="Send"]',
    'button[aria-label*="发送"]',
)

# 未登录的迹象
LOGGED_OUT_MARKERS = (
    'a[href*="login"]',
    'button:has-text("Log in")',
    'button:has-text("登录")',
    'button:has-text("Sign in")',
)

# 输入框候选，按优先级排列
COMPOSER_INPUTS = (
    "textarea[placeholder]",
    '[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    "textarea",
)

# 发送按钮候选
SEND_BUTTONS = (
    'button[aria-label*="Send"]',
    'button[aria-label*="发送"]',
    'button[data-testid="send-button"]',
    'button[type="submit"]',
)

# 助手消息容器候选。必须是能取到"完整回答文本"的元素。
ASSISTANT_MESSAGES = (
    '[data-testid="assistant-message"]',
    '[data-role="assistant"]',
    'div[class*="assistant"]',
    'div[class*="message"][class*="ai"]',
)

# 生成中的迹象（出现即表示还在写）
STREAMING_MARKERS = (
    '[data-testid="streaming"]',
    'button[aria-label*="Stop"]',
    'button[aria-label*="停止"]',
    '[class*="cursor"][class*="blink"]',
)

# 新对话按钮候选（每次请求前重置上下文时用）
NEW_CHAT_BUTTONS = (
    'button[aria-label*="New chat"]',
    'button[aria-label*="新对话"]',
    'a[href="/"]',
)


def first_match(candidates: tuple[str, ...]) -> str:
    """把候选列表拼成 Playwright 的 :is() 选择器，一次查询全部试一遍。"""
    if not candidates:
        raise ValueError("选择器列表为空")
    return ":is(" + ", ".join(candidates) + ")"
