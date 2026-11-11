"""DOM 选择器表。

muse 走加密 Noise WebSocket，纯协议拿不到正文，只能从页面 DOM 读。
前端一改版这里就会失效——所有选择器集中在此，方便单点修复。

每条选择器都是一个候选列表，按顺序尝试，第一个命中的胜出。
这样即使前端换了类名，只要有一个旧选择器还在就能继续工作。

实测来源（本轮补充）：新增/前置的选择器全部取自上游 muse2api 的活网实现
（engine.py:41,717-721,838-840,997-998,1575,1577,1689-1694,1706-1731）、
verify_engine_selectors.py:90-94 与 MUSE_MCP README.md:285-294。
旧候选一律保留、只在前面插入确认项（唯一例外：SEND_BUTTONS 里那条没有任何
实测来源的 button[data-testid="send-button"]，按本轮规格删除）。
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
# 前两项实测：MUSE_MCP README.md:287-288（composer 根下的 textarea，兜底 Lexical 编辑器）
COMPOSER_INPUTS = (
    '[data-hatch-composer-root] textarea',
    '[data-lexical-editor="true"]',
    "textarea[placeholder]",
    '[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    "textarea",
)

# 发送按钮候选
# 实测：engine.py:1075-1077 按 aria-label / data-testid 匹配 发送|send，
# 中文界面优先（上游正则同时覆盖，但 muse 的 zh 包更常见）。
SEND_BUTTONS = (
    'button[aria-label*="发送"]',
    'button[aria-label*="Send"]',
    'button[type="submit"]',
)

# 助手消息容器候选。必须是能取到"完整回答文本"的元素。
# 旧候选来源：engine.py:717,1231-1232,1404-1408,1435-1436
# （hatch-chat-groupable-bubble + hatch-agent-bubble-bg 判定助手气泡）、
# MUSE_MCP README.md:293（data-message-item / data-message-role）。
#
# 2026-11-11 真机 muse.ai DOM（唯一权威证据，本轮按它重排）：
#   * 用户消息项：DIV[data-message-item="true"][data-message-role="user"]
#     [data-message-side="user"]，inner_text 是 "你：\n\n<提问>"——"你："是渲染
#     出来的前缀，不属于提问内容。
#   * 助手消息项：同级的 DIV[data-message-item="true"][data-message-role="assistant"]。
#   * 助手正文：DIV[data-hatch-assistant-message-body="true"]，挂在
#     hatch-chat-groupable-bubble … agent-bubble 气泡里，正文本身是
#     DIV.prose … [data-hatch-plain-text="true"] > P.whitespace-pre-line。
#     它是最精确的判据：直接取正文容器，不会把气泡外壳里的头像/工具条/复制
#     按钮的文本一起读进回答。
#   * 当前构建里 [data-role="assistant"] / [data-role="user"] 一个都不存在
#     （早先某次渲染里出现过，不能当主判据），所以 data-role 只能垫底兜底。
#   * div[class*="hatch-chat-groupable-bubble"] 两边气泡都命中：放在首位会把
#     用户提问当成助手回答，回答里出现 "你：…" 前缀就是这么来的。
# 顺序因此是"最精确的属性 → 消息项 → 两边都命中的通用类名 → 猜出来的旧类名"。
ASSISTANT_MESSAGES = (
    '[data-hatch-assistant-message-body="true"]',
    '[data-message-item][data-message-role="assistant"]',
    '[data-message-role="assistant"]',
    '[data-role="assistant"]',
    '[data-testid="assistant-message"]',
    '[data-message-item]',
    'div[class*="hatch-chat-groupable-bubble"][class*="agent-bubble"]',
    'div[class*="hatch-chat-groupable-bubble"]',
    'div[class*="assistant"]',
    'div[class*="message"][class*="ai"]',
)

# 生成中的迹象（出现即表示还在写）
# 实测：engine.py:719-721,1234-1236——stop 按钮优先，其次 aria-label 匹配 Stop/停止。
STREAMING_MARKERS = (
    '[data-testid="hatch-composer-stop-button"]',
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


# 思考区域候选（MUSE_REASONING=1 时才查询）。这些是**未活网确认的候选形态**，
# 只是常见 thinking/reasoning 结构的猜测，不是对 muse 真实 DOM 的确认；
# 开关关闭时此表完全不参与查询。命中节点必须与正文容器相互独立，
# 否则思考文字会混进正文。MUSE_REASONING_SELECTORS（逗号分隔）可在不改代码的情况下
# 覆盖这里的候选。
REASONING_MESSAGES = (
    '[data-testid="reasoning-content"]',
    '[data-testid="reasoning"] [class*="content"]',
    '[data-reasoning] p',
    '[class*="reasoning"] [class*="content"]',
    '[class*="thinking"] [class*="content"]',
)

# ------------------------------------------------------------------
# 以下为生成能力（图/视频/参考素材/额度）新增表，逐条注明实测来源。
# ------------------------------------------------------------------

# 生成结果的附件容器。前缀匹配：后缀是 image / video 等类型片段。
# 实测：engine.py:41（ATT_SEL）、engine.py:718,997,1575。
ATTACHMENTS = (
    '[data-testid^="hatch-chat-attachment-presentation-"]',
)

# 助手气泡里的媒体元素兜底（附件容器没有 data-testid 时才靠它）。
# 实测：engine.py:998（agent-bubble-bg 下的 img / video，剔除 avatar 与 emoji）。
RESULT_MEDIA = (
    'div[class*="hatch-agent-bubble-bg"] img',
    'div[class*="hatch-agent-bubble-bg"] video',
)

# 用户上传/会话气泡的排除范围：落在这里的媒体是参考素材，不是成品。
# 实测：engine.py:977（closest('form, [class*=chat-user-bubble], [class*="group/msg"]')）。
MEDIA_EXCLUDE = (
    'form',
    '[class*="chat-user-bubble"]',
    '[class*="group/msg"]',
)

# hydration 完成锚点。页面 DOM 存在但 React 没接管时，输入框点了也没反应。
# 实测：engine.py:689-690。
HYDRATION = (
    '[data-hatch-shell-hydration-state="hydrated"]',
)

# composer 根容器（附件按钮、file input 都挂在它下面）。
# 实测：MUSE_MCP README.md:287；engine.py:1704-1717 的兜底是 textarea 逐级向上。
COMPOSER_ROOT = (
    '[data-hatch-composer-root]',
)

# 助手回答里的错误提示（额度/风控/生成失败的可见证据）。
# 实测：MUSE_MCP README.md:294。
ERROR_NOTICES = (
    '[data-testid="assistant-response-error-notice"]',
)

# 输入框里"移除某个附件"的按钮。中英繁三形都是实测项。
# 实测：engine.py:1691（remove attachment|移除附件|删除附件|移除|删除）、
# verify_engine_selectors.py:90-91（EN 与 ZH 两条判据同时存在）。
REMOVE_ATTACHMENT = (
    'button[aria-label*="Remove attachment"]',
    'button[aria-label*="移除附件"]',
    'button[aria-label*="删除附件"]',
)

# 上传控件。Playwright 的 set_input_files 需要它。
# 实测：engine.py:1694,1720-1727（_FILE_INPUT_JS 在 composer 根内找它）。
FILE_INPUTS = (
    'input[type="file"]',
)

# 会话列表行（读取历史聊天/定位 thread 用）。
# 实测：MUSE_MCP README.md:291。
THREAD_ROWS = (
    '[data-testid="hatch-thread-row"]',
)

# 额度面板入口：右下角 more 菜单，兜底按 Settings 标签找按钮。
# 实测：engine.py:838-840（labels = Settings／設定／设置）。
QUOTA_SETTINGS_ENTRY = (
    '[data-testid="hatch-dock-more"]',
    'button[aria-label*="Settings"]',
    'button[aria-label*="設定"]',
    'button[aria-label*="设置"]',
)

# 菜单项。实测：engine.py:852。
QUOTA_MENU_ITEM = (
    '[role="menuitem"]',
)

# 设置面板本体（额度文本就在这里的 innerText 里）。实测：engine.py:862。
QUOTA_DIALOG = (
    '[role="dialog"]',
    '[aria-modal="true"]',
)

# 附件卡片的下载容器：下载按钮只在这个范围里找，绝不点全局按钮。
# 实测：engine.py:1577 + 1570-1571 的注释（点全局下载按钮会把上传件当成成品下载，
# 等于开一个代理，属于 open-proxy 风险，必须 fail closed）。
UPLOAD_HINT = (
    '[class*="group/widget-presentation"]',
)


def first_match(candidates: tuple[str, ...]) -> str:
    """把候选列表拼成 Playwright 的 :is() 选择器，一次查询全部试一遍。"""
    if not candidates:
        raise ValueError("选择器列表为空")
    return ":is(" + ", ".join(candidates) + ")"
