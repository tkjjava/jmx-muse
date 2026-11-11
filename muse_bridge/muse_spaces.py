"""muse Spaces / idea-cards 的**只读**协议适配层。

为什么单独一个模块（而不是让 canvas_api 直接摸 `protocol_client`）：

  * `canvas_api` 是 FastAPI 路由层，它的职责是"把请求翻成 JSON/字节 + 把异常
    翻成状态码"。协议侧的字段归一化、二进制响应、上游错误分类都不该写进路由；
  * 这里把 `HatchProtocolClient` 包成窄接口（`list_spaces` / `get_space` /
    `export_html` / `list_idea_cards` / `get_idea_card` / `idea_card_media`），
    构造时只注入 cookies/proxy/超时，**测试可以完全不建 WS 连接**
    （`protocol_client=` 注入假客户端即可）；
  * 只读：不调用 `spaces.save` / `spaces.share` / `idea-cards.execute` 等任何写
    方法。`docs/CANVAS_DESIGN.md` §10 P3 修订明确"不承诺文档级双向同步"，写路径
    （创建/保存）发生在 muse frame app 内部，本模块不做。

证据（`docs/CANVAS_PROTOCOL.md`，括号内是前端 chunk 锚点）：

  * §2.1 归一化字段表（`2lzbmcnnh3_e0.js@15101`–`@16698`）：slug 缺失回退
    `space_id`；`construction_status` 优先取 `construction_status`，再回退
    `build_status`；`is_shared` 归一成 `is_saved_foreign_share`；`preview_image`
    的 `version` / `thumbnail_url` / `preview_card_url` 任一缺失即整块置 null。
  * §2.2 `construction_status` 大小写双写（`constructing` 与 `BUILDING` 都出现
    过，客户端必须大小写不敏感），未知值一律 null。
  * §3.1/§3.2 `spaces.list`：`GET /spaces/v2`，唯一参数 `include_constructing`；
    没有分页/排序参数；响应顶层必须 `{spaces: [...]}`，**任一条目不可解析即
    整个响应失败**（`2lzbmcnnh3_e0.js@14846`–`@14883`）。
  * §3.3 排序在客户端做（`frequency_score * exp(-Δt/7d)`），服务端不排；本模块
    如实按上游顺序返回，排序交给渲染层。
  * §10.1/§12.1#10 `spaces.export_html`：`GET /spaces/{slug}/export.html`，参数只
    有 slug，`responseType=binary, channel=media`，返回整包 HTML（Blob）。
  * §12.3 `api.idea-cards.list`：`GET /api/idea-cards`，只读；响应 `{result:{...}}`。

真机只读探针（2026-11-11，Lead 执行；落盘在
`work/canvas_re/protocol-route-dump/`，全部是纯协议 GET、无写、无生成）：

  * `GET /spaces/v2` 与 `GET /spaces/v2?include_constructing=true` → 200
    `{"spaces":[]}`（13 B；该账号当前没有 Space）。这轮没有观察到 403 ACL 拒绝。
  * `GET /spaces/v2/{slug}` → 404 `{"ok":false,"error":{"code":"space_unavailable",
    "message":"space source directory was not found"}}`（98 B）⇒ **单 Space GET
    路由存在**，只是资源不存在；成功响应形状仍未证实（本模块按"`{space:{...}}`
    或扁平 raw space"两种形态容错）。
  * `GET /spaces/{slug}/export.html` → 404 `{"ok":false,"error":{"code":"not_found",
    "message":"web artifact was not found"}}`（80 B）；同 slug 的
    `GET /spaces/{slug}/export` → 404 `"route not found"`（69 B）⇒ **导出只有
    `.html` 这一种形态**。
  * `GET /api/idea-cards` → 200，67,759 B，`result.sections` 7 个
    （Ideas / Productivity / Financial Management / Relationships /
    Health & Fitness / Shopping / Travel）；卡片字段 `id` / `ideaCardId` /
    `title` / `summary` / `primaryLabel` / `actionLabel` / `buildStatus` /
    `prerequisiteNotes` / `content{title,summary,buildSummary}` /
    `presentation{icon.variants[], labels, badges, item_count}`。
  * `GET /api/idea-cards/{ideaCardId}` → 200，1,877 B
    `{"ok":true,"result":{"card":{...}}}`，含 `items` 与 `sharing.kind`。
  * `GET /api/idea-cards/media/icon-57e8…aa6` → 200 `image/png` 1,045 B（PNG 签名
    `\\x89PNG` 已核对）⇒ 走纯协议就能取到卡片图标，不需要浏览器、不烧额度。
  * 404 有两种 message：`route not found`（路由不存在，说明**我们拼错了路径**）
    与 `... was not found`（资源不存在）。`_status_error` 按这条区分：前者记成
    协议错误（502，本进程的 bug），后者才是业务 404。

两条硬边界：

  1. **slug/标识符校验后才拼路径**。它们会进上游 URL 路径（export、单 space、
     idea-card 详情/媒体）：允许 `[A-Za-z0-9._-]`、禁止 `..`、再 percent-encode。
     上游域名是我们凭 cookie 能打到的服务，不能让一个 `../` / `?` / `#` 把请求
     改成别的端点。
  2. **异常消息不带 cookie、不带堆栈**。cookie 只交给 `HatchProtocolClient`，
     本模块的异常只写账号名（可选）、路径、HTTP 状态与响应片段（服务端错误
     文案里没有本地凭据）。

作者：**君莫笑** · **永恒之兰**
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

from .protocol_client import (
    HatchProtocolClient,
    ProtocolAuthError,
    ProtocolEgressError,
    ProtocolError,
    ProtocolRateLimited,
)
from .refs import normalize_mime, sniff_mime

__all__ = [
    "MuseAuthError",
    "MuseBinary",
    "MuseEgressError",
    "MuseInvalidResponse",
    "MuseInvalidSlug",
    "MuseNotFound",
    "MuseRateLimited",
    "MuseSpacesClient",
    "MuseSpacesError",
    "MuseUnavailable",
    "normalize_idea_card",
    "normalize_idea_cards_response",
    "normalize_space",
    "normalize_spaces_response",
    "validate_identifier",
    "validate_slug",
]

DEFAULT_SITE = "https://muse.ai"
DEFAULT_MODEL = "hatch-web"
DEFAULT_TIMEOUT = 30.0

#: slug / idea-card id / 媒体 handle 的合法形态。真实值形如
#: `mb-canvas-probe-20261111`、`idea_58a45ab5…`、`icon-57e8d9bd…`；
#: 这里只在**拼路径**时收紧（列表返回的 slug 不因这条被丢弃）。
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# ---------------------------------------------------------------- 异常


class MuseSpacesError(RuntimeError):
    """muse 只读适配层的失败基类。

    `status_code` / `code` 是给 HTTP 层**直接映射**用的（与 `media.MediaError`
    同一取舍：异常自带映射信息，路由层不写 isinstance 链）。`note` 就是回给
    客户端的那句话，不含堆栈、不含 cookie、不含本地路径。
    """

    status_code = 502
    code = "muse_protocol_error"

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


class MuseUnavailable(MuseSpacesError):
    """没有可用账号 / 账号库不可用。503 是"现在没有能力"，不是"请求写错了"。"""

    status_code = 503
    code = "muse_unavailable"


class MuseAuthError(MuseSpacesError):
    """上游 401 一类：muse cookie 失效。

    映射 502 而不是 401：401 在本进程里专指"调用方没带正确的 bridge key"，
    混用会让前端以为自己的 key 错了，而去重输 key（真正要修的是 muse 登录）。
    """

    status_code = 502
    code = "muse_session_expired"


class MuseEgressError(MuseSpacesError):
    """上游 403 一类：出口受限。**不得**据此判定 cookie 失效。"""

    status_code = 502
    code = "muse_egress_blocked"


class MuseRateLimited(MuseSpacesError):
    """上游 429：退避重试即可，账号本身没坏。透传 429 让调用方能区分。"""

    status_code = 429
    code = "muse_rate_limited"


class MuseNotFound(MuseSpacesError):
    """上游 404 且 message 是"资源不存在"：space / 导出 / 媒体不存在。"""

    status_code = 404
    code = "not_found"


class MuseInvalidResponse(MuseSpacesError):
    """上游 200 但响应形状不符合契约（例如 `spaces` 不是数组）。"""

    status_code = 502
    code = "muse_invalid_response"


class MuseInvalidSlug(MuseSpacesError):
    """slug / idea-card 标识符不合法（调用方输入错误，400）。"""

    status_code = 400
    code = "invalid_slug"


# ---------------------------------------------------------------- 字段归一化
# 规则逐条照 §2.1 的前端归一化函数；`missing -> null` 是本轮的显式要求。
# 三个小工具：`_text` 把"非字符串 / 空串 / 纯空白"统一成 None，`_number` 抹掉
# 非有限数字（前端是 `(0,n.f)(x)`，NaN/Infinity 一律变成缺失），`_flag` 只认
# 真布尔（`"false"` 字符串不当 false，避免把上游拼错的类型静默转成有效值）。

_CONSTRUCTION_STATUS = {
    "constructing": "constructing",
    "building": "constructing",
    "succeeded": "succeeded",
    "ready": "succeeded",
    "failed": "failed",
    "interrupted": "interrupted",
    "updating": "updating",
}


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def normalize_construction_status(value: object) -> str | None:
    """§2.2 的枚举：大小写不敏感，未知值 null（不编造一个状态）。"""
    text = _text(value)
    if text is None:
        return None
    return _CONSTRUCTION_STATUS.get(text.lower())


def normalize_preview_image(value: object) -> dict[str, object] | None:
    """§2.1 的 `preview_image`：`version`/`thumbnail_url`/`preview_card_url` 缺一即 null。"""
    if not isinstance(value, dict):
        return None
    version = _text(value.get("version"))
    thumbnail = _text(value.get("thumbnail_url"))
    preview_card = _text(value.get("preview_card_url"))
    if version is None or thumbnail is None or preview_card is None:
        return None
    return {
        "version": version,
        "icon_url": _text(value.get("icon_url")),
        "thumbnail_url": thumbnail,
        "thumbnail_width_px": _number(value.get("thumbnail_width_px")),
        "thumbnail_height_px": _number(value.get("thumbnail_height_px")),
        "preview_card_url": preview_card,
        "preview_card_width_px": _number(value.get("preview_card_width_px")),
        "preview_card_height_px": _number(value.get("preview_card_height_px")),
    }


def normalize_space(value: object) -> dict[str, object] | None:
    """一条 raw space -> §2.1 `SpaceListItem`；不可解析返回 None（调用方据此整响应失败）。

    刻意**不**复刻 §2.4 的图标 CDN 重写：那条规则要把 `shortcode`/`share_type`
    拼进 `*.metaaiusercontent.com` 域名，属于渲染层（浏览器）的取图策略；服务端
    只如实交付上游给的 `image_url`/`preview_image`，让画布前端自己决定显示哪张。
    """
    if not isinstance(value, dict):
        return None
    slug = _text(value.get("slug")) or _text(value.get("space_id"))
    name = _text(value.get("name"))
    if slug is None or name is None:
        # §2.1：slug(含 space_id 回退) 或 name 缺一 => 整条 space 被丢弃。
        return None
    space_id = _text(value.get("space_id"))
    raw_status = value.get("construction_status")
    if raw_status is None:  # 前端是 `e.construction_status ?? e.build_status`
        raw_status = value.get("build_status")
    normalized: dict[str, object] = {
        "construction_status": normalize_construction_status(raw_status),
        "slug": slug,
        "image_url": _text(value.get("image_url")),
        "name": name,
        "updated_at_ms": _number(value.get("updated_at_ms")),
        "accessed_at_ms": _number(value.get("accessed_at_ms")),
        "frequency_score": _number(value.get("frequency_score")),
        "favorite_order": _number(value.get("favorite_order")),
        "preview_image": normalize_preview_image(value.get("preview_image")),
        "is_saved_foreign_share": value.get("is_shared") is True,
        "runtime": _text(value.get("runtime")),
        "is_current_build_published": _flag(value.get("is_current_build_published")),
        "has_server_actions": _flag(value.get("has_server_actions")),
        "theme_accent": _text(value.get("theme_accent")),
        "shortcode": _text(value.get("shortcode")),
        "share_type": _text(value.get("share_type")),
        "source_url": _text(value.get("source_url")),
        "saved_target_shortcode": _text(value.get("saved_target_shortcode")),
        "view_url": _text(value.get("view_url")),
    }
    if space_id is not None and space_id != slug:
        # 前端只在 `space_id` 与 `slug` 不同才保留它（同值等于重复标识）。
        normalized["space_id"] = space_id
    return normalized


def normalize_spaces_response(payload: object) -> list[dict[str, object]]:
    """§3.2 响应归一化：非数组 / 任一条坏 => `MuseInvalidResponse`（照抄前端严格性）。"""
    raw_spaces = payload.get("spaces") if isinstance(payload, dict) else None
    if not isinstance(raw_spaces, list):
        raise MuseInvalidResponse("muse spaces 响应不合法：spaces 必须是数组")
    normalized = [normalize_space(item) for item in raw_spaces]
    if any(item is None for item in normalized):
        raise MuseInvalidResponse("muse spaces 响应不合法：存在无法解析的条目")
    return [item for item in normalized if item is not None]


# ---------------------------------------------------------------- idea-cards 归一化
# 真机样本（见模块头）：list 的卡片就是 detail 卡片去掉 `items` 的形态；字段名用
# 前端 camelCase 原样保留（`buildStatus` / `ideaCardId` / `primaryLabel` …），
# 只额外补三个便于渲染的派生键：`iconUrl` / `iconMime` / `iconVariants`。
# 未知键一律进 `extra`（"unknown 透传"）：上游加字段不需要改本模块，
# 前端也能按旧字段正常工作。

_IDEA_CARD_KNOWN = frozenset(
    {
        "id",
        "ideaCardId",
        "title",
        "summary",
        "primaryLabel",
        "actionLabel",
        "buildStatus",
        "prerequisiteNotes",
        "content",
        "presentation",
        "items",
        "sharing",
    }
)


def _normalize_icon_variant(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    url = _text(value.get("url"))
    if url is None:
        return None
    return {
        "url": url,
        "role": _text(value.get("role")),
        "width": _number(value.get("width")),
        "height": _number(value.get("height")),
        "mimeType": _text(value.get("mimeType")),
        "byteSize": _number(value.get("byteSize")),
        "version": _text(value.get("version")),
    }


def _normalize_label(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    text = _text(value.get("text"))
    if text is None:
        return None
    return {"text": text, "role": _text(value.get("role"))}


def normalize_idea_card(value: object, *, detail: bool = False) -> dict[str, object] | None:
    """一张 idea-card -> 画布可直接消费的卡片对象；缺 `id`/`title` 返回 None。

    与 spaces 的严格性**有意不同**：`spaces.list` 是前端钉死的"任一条坏即整响应
    失败"（§2.1），而 idea-cards 列表有 7 个 section / 几十张卡，单张坏卡不应
    连累整页（语料没有相反证据），所以这里容错：坏卡跳过、坏 section 跳过。
    """
    if not isinstance(value, dict):
        return None
    card_id = _text(value.get("id")) or _text(value.get("ideaCardId"))
    title = _text(value.get("title"))
    if card_id is None or title is None:
        return None
    presentation = value.get("presentation")
    presentation = presentation if isinstance(presentation, dict) else {}
    icon = presentation.get("icon")
    icon = icon if isinstance(icon, dict) else {}
    raw_variants = icon.get("variants")
    variants = [
        variant
        for item in (raw_variants if isinstance(raw_variants, list) else [])
        if (variant := _normalize_icon_variant(item)) is not None
    ]
    first: dict[str, object] = variants[0] if variants else {}
    content = value.get("content")
    content = content if isinstance(content, dict) else {}
    labels = presentation.get("labels")
    badges = presentation.get("badges")
    display_badge = presentation.get("display_badge")
    sharing = value.get("sharing")
    normalized: dict[str, object] = {
        "id": card_id,
        "ideaCardId": _text(value.get("ideaCardId")) or card_id,
        "title": title,
        "summary": _text(value.get("summary")),
        "primaryLabel": _text(value.get("primaryLabel")),
        "actionLabel": _text(value.get("actionLabel"))
        or _text(presentation.get("actionLabel")),
        "buildStatus": _text(value.get("buildStatus")),
        "prerequisiteNotes": _text(value.get("prerequisiteNotes")),
        "iconUrl": first.get("url"),
        "iconMime": first.get("mimeType"),
        "iconWidth": first.get("width"),
        "iconHeight": first.get("height"),
        "iconVariants": variants,
        "content": {
            "title": _text(content.get("title")),
            "summary": _text(content.get("summary")),
            "buildSummary": _text(content.get("buildSummary")),
        },
        "labels": [
            label
            for item in (labels if isinstance(labels, list) else [])
            if (label := _normalize_label(item)) is not None
        ],
        "badges": [
            label
            for item in (badges if isinstance(badges, list) else [])
            if (label := _normalize_label(item)) is not None
        ],
        "displayBadge": _normalize_label(display_badge),
        "itemCount": _number(presentation.get("item_count")),
        "items": value.get("items") if detail and isinstance(value.get("items"), list) else None,
        "sharing": sharing if isinstance(sharing, dict) else None,
        "extra": {key: item for key, item in value.items() if key not in _IDEA_CARD_KNOWN},
    }
    return normalized


def normalize_idea_cards_response(payload: object) -> dict[str, object]:
    """§12.3 list 响应 -> `{sections, viewerState, pagination}`（坏 section/卡跳过）。"""
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise MuseInvalidResponse("idea-cards 响应缺少 result 对象")
    raw_sections = result.get("sections")
    if not isinstance(raw_sections, list):
        raise MuseInvalidResponse("idea-cards 响应不合法：sections 必须是数组")
    sections: list[dict[str, object]] = []
    for item in raw_sections:
        if not isinstance(item, dict):
            continue
        section_id = _text(item.get("id")) or _text(item.get("sectionId"))
        if section_id is None:
            continue
        cards = [
            card
            for raw_card in (item.get("cards") if isinstance(item.get("cards"), list) else [])
            if (card := normalize_idea_card(raw_card)) is not None
        ]
        presentation = item.get("presentation")
        sections.append(
            {
                "id": section_id,
                "sectionId": _text(item.get("sectionId")) or section_id,
                "title": _text(item.get("title")),
                "layout": _text(item.get("layout")),
                "presentation": (
                    {"layout": _text(presentation.get("layout"))}
                    if isinstance(presentation, dict)
                    else None
                ),
                "cards": cards,
            }
        )
    viewer = result.get("viewerState")
    pagination = result.get("pagination")
    return {
        "sections": sections,
        "viewerState": viewer if isinstance(viewer, dict) else None,
        "pagination": pagination if isinstance(pagination, dict) else None,
    }


def normalize_idea_card_detail(payload: object) -> dict[str, object]:
    """§12.3 detail 响应 -> `{card}`；形状坏抛出 `MuseInvalidResponse`。"""
    result = payload.get("result") if isinstance(payload, dict) else None
    card = result.get("card") if isinstance(result, dict) else None
    normalized = normalize_idea_card(card, detail=True)
    if normalized is None:
        raise MuseInvalidResponse("idea-card 详情响应不合法：缺少 card/id/title")
    return normalized


# ---------------------------------------------------------------- 校验


def validate_identifier(value: object, *, what: str) -> str:
    """校验并收紧 slug / idea-card id / 媒体 handle（它们会进上游 URL 路径）。

    见模块头第 1 条：不允许 `..`、`/`、`?`、`#`、空白，只留 URL 路径里安全的
    字符集，随后调用方还要再 percent-encode 一层。
    """
    if not isinstance(value, str):
        raise MuseInvalidSlug(f"{what} 必须是字符串")
    cleaned = value.strip()
    if not cleaned:
        raise MuseInvalidSlug(f"{what} 不能为空")
    if ".." in cleaned or not SLUG_RE.fullmatch(cleaned):
        raise MuseInvalidSlug(f"{what} 不合法：{cleaned[:64]!r}")
    return cleaned


def validate_slug(slug: object) -> str:
    """space slug 的校验（export / 单 space GET 用）。"""
    return validate_identifier(slug, what="slug")


# ---------------------------------------------------------------- 响应工具


def _note(account: str, message: str) -> str:
    """异常文案前缀。账号名不是机密（排障要用），cookie 绝不进这里。"""
    return f"账号 {account}：{message}" if account else message


def _body_snippet(response: object, limit: int = 160) -> str:
    """把上游错误 body 压成一行。上游错误文案里没有本地凭据，可以拼进异常。"""
    raw = bytes(getattr(response, "body", b"") or b"")
    text = " ".join(raw.decode("utf-8", "replace").split())
    if not text:
        return "（空 body）"
    return text[:limit] + ("…" if len(text) > limit else "")


def _response_header(response: object, name: str) -> str:
    target = name.lower()
    for item in getattr(response, "headers", None) or []:
        try:
            key, value = item
        except (TypeError, ValueError):
            continue
        if str(key).lower() == target:
            return str(value)
    return ""


def _status_error(path: str, response: object) -> MuseSpacesError:
    """按上游状态码分类。分类必须分开：401/403/429 的处置动作完全不同。

    404 再按真机结论细分（模块头第 3 条）：`route not found` 意味着**路径拼错**，
    是本进程的 bug，不能伪装成"资源不存在"的 404 交给前端。
    """
    status = int(getattr(response, "status", 0) or 0)
    snippet = _body_snippet(response)
    if status == 404:
        if "route not found" in snippet.lower():
            return MuseSpacesError(f"{path} 路由不存在（上游 404 route not found）：{snippet}")
        return MuseNotFound(f"{path} 不存在（HTTP 404）：{snippet}")
    if status == 401:
        return MuseAuthError(f"{path} 认证失败（HTTP 401）：muse cookie 可能已失效")
    if status == 403:
        return MuseEgressError(f"{path} 被拒绝（HTTP 403）：出口受限，不代表 cookie 失效")
    if status == 429:
        return MuseRateLimited(f"{path} 被限流（HTTP 429）：稍后重试")
    return MuseSpacesError(f"{path} 未成功（HTTP {status}）：{snippet}")


@dataclass(frozen=True)
class MuseBinary:
    """一次二进制读取的结果（export.html / idea-card 媒体共用）。

    `mime` 对导出是上游声明的 Content-Type；对 idea-card 媒体是**文件头嗅探**
    结果（嗅探不出才回退声明）。调用方**不能**直接拿 `mime` 决定内联渲染——
    inline 的安全性由 `canvas_api` 的白名单决定（见那里的注释）。
    """

    data: bytes
    mime: str
    filename: str


# ---------------------------------------------------------------- 客户端


class MuseSpacesClient:
    """一个账号一条只读协议连接。连接是**显式**步骤（`connect` / 上下文管理器）。

    注入点：
      * `cookies`：来自 `AccountStore.cookies(account)`；只交给协议客户端；
      * `proxy`：`MUSE_PROTOCOL_PROXY` 的字符串形态（与 pool 协议路径同一个
        环境变量；真机结论：WS 必须经本机代理到 hatch.metaaivm.com）；
      * `timeout`：单次请求的等待上限（秒）；
      * `protocol_client`：测试注入的假客户端（鸭子契约：`connect/close/request`）。
        不注入时才真的建 `HatchProtocolClient`——**离线测试因此不可能触网**。
    """

    def __init__(
        self,
        cookies: Mapping[str, str],
        *,
        account: str = "",
        proxy: str | None = None,
        site: str = DEFAULT_SITE,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        protocol_client: object | None = None,
    ) -> None:
        self.account = account
        self.timeout = float(timeout)
        self._connected = False
        if protocol_client is not None:
            self._client: object = protocol_client
        else:
            self._client = HatchProtocolClient(
                dict(cookies),
                proxy={"server": proxy} if proxy else None,
                site=site,
                model=model,
            )

    # ------------------------------------------------------------ 连接
    async def connect(self) -> None:
        """建立 Noise 连接；已连则直接返回（幂等）。

        `connect()` 内部的 401/403/429 分类在 `protocol_client` 已做对，这里只做
        到本层异常类型的翻译——两套异常都往外抛会让路由层写两遍 isinstance 链。
        """
        if self._connected:
            return
        try:
            await self._client.connect()  # type: ignore[attr-defined]
        except ProtocolAuthError as exc:
            raise MuseAuthError(_note(self.account, f"muse 会话失效：{exc}")) from None
        except ProtocolEgressError as exc:
            raise MuseEgressError(_note(self.account, f"muse 出口受限：{exc}")) from None
        except ProtocolRateLimited as exc:
            raise MuseRateLimited(_note(self.account, f"muse 被限流：{exc}")) from None
        except ProtocolError as exc:
            raise MuseSpacesError(_note(self.account, f"muse 协议连接失败：{exc}")) from None
        except Exception as exc:  # 未预期异常只报类型名（消息可能含代理凭据）
            raise MuseSpacesError(
                _note(self.account, f"muse 协议连接异常（{type(exc).__name__}）")
            ) from None
        self._connected = True

    async def close(self) -> None:
        """幂等、不抛：收尾失败不能把请求路径一起卡死。"""
        self._connected = False
        with contextlib.suppress(Exception):
            await self._client.close()  # type: ignore[attr-defined]

    async def __aenter__(self) -> MuseSpacesClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------ 发送
    async def _request(self, verb: str, path: str, body: dict | None = None) -> object:
        """一次 HTTP-over-Noise 请求；把所有协议异常归一到 `MuseSpacesError`。"""
        try:
            return await self._client.request(  # type: ignore[attr-defined]
                verb, path, body, timeout=self.timeout
            )
        except ProtocolAuthError as exc:
            raise MuseAuthError(_note(self.account, f"{path} 认证失败：{exc}")) from None
        except ProtocolEgressError as exc:
            raise MuseEgressError(_note(self.account, f"{path} 出口受限：{exc}")) from None
        except ProtocolRateLimited as exc:
            raise MuseRateLimited(_note(self.account, f"{path} 被限流：{exc}")) from None
        except MuseSpacesError:
            raise
        except ProtocolError as exc:
            raise MuseSpacesError(_note(self.account, f"{path} 协议失败：{exc}")) from None
        except Exception as exc:
            raise MuseSpacesError(
                _note(self.account, f"{path} 协议调用异常（{type(exc).__name__}）")
            ) from None

    async def _json(self, verb: str, path: str, body: dict | None = None) -> dict:
        response = await self._request(verb, path, body)
        status = int(getattr(response, "status", 0) or 0)
        if status != 200:
            raise _status_error(path, response)
        try:
            payload = json.loads(bytes(getattr(response, "body", b"") or b"") or b"null")
        except ValueError:
            raise MuseInvalidResponse(f"{path} 返回的不是 JSON") from None
        if not isinstance(payload, dict):
            raise MuseInvalidResponse(f"{path} 返回的 JSON 不是对象")
        return payload

    async def _binary(self, path: str) -> MuseBinary:
        """GET 一个二进制资源；空 body 视为失败（前端把非 Blob 当导出失败）。"""
        response = await self._request("GET", path, None)
        status = int(getattr(response, "status", 0) or 0)
        if status != 200:
            raise _status_error(path, response)
        data = bytes(getattr(response, "body", b"") or b"")
        if not data:
            raise MuseInvalidResponse(f"{path} 返回了空文件")
        declared = _response_header(response, "content-type")
        mime = sniff_mime(data) or normalize_mime(declared) or "application/octet-stream"
        return MuseBinary(data=data, mime=mime, filename="")

    # ------------------------------------------------------------ 只读 RPC
    async def list_spaces(
        self, *, include_constructing: bool = False
    ) -> list[dict[str, object]]:
        """§3/§12.1#1：`GET /spaces/v2`，返回 §2.1 归一化列表（按上游顺序）。

        排序**不在这里做**：§3.3 明确服务端不排、前端按
        `frequency_score * exp(-(now-accessed_at_ms)/7d)` 降序；把这条公式放进
        API 层会让"客户端与服务端各排一次"这种分歧无从排查，所以如实交付。
        真机：`GET /spaces/v2` 与 `?include_constructing=true` 都是 200
        `{"spaces":[]}`（13 B）。
        """
        await self.connect()
        body: dict = {"include_constructing": True} if include_constructing else {}
        payload = await self._json("GET", "/spaces/v2", body)
        return normalize_spaces_response(payload)

    async def get_space(self, slug: str) -> dict[str, object]:
        """单 Space 读取：`GET /spaces/v2/{slug}`（真机证实**路由存在**）。

        真机只有 404 证据（`space_unavailable`），成功响应形状未证实；因此这里
        同时接受 `{"space": {...}}` 与扁平 raw space 两种形态，解析不出仍按
        `MuseInvalidResponse` 失败（不靠猜字段交付半份数据）。
        """
        clean = validate_slug(slug)
        await self.connect()
        path = f"/spaces/v2/{quote(clean, safe='')}"
        payload = await self._json("GET", path, None)
        nested = payload.get("space")
        normalized = normalize_space(nested if isinstance(nested, dict) else payload)
        if normalized is None:
            raise MuseInvalidResponse(f"{path} 响应无法归一化成 space")
        return normalized

    async def export_html(self, slug: str) -> MuseBinary:
        """§10.1/§12.1#10：`GET /spaces/{slug}/export.html`，返回整包 HTML 字节。

        为什么要求非空：前端在拿到非 Blob 时直接抛错（`1nf703hwxciza.js@64962`），
        空文件同样是"导出失败"，交给上层当下载成功会让人以为导出了一份空空间。
        真机：路由存在、资源不存在时 404
        `{"code":"not_found","message":"web artifact was not found"}`；`.html`
        是**唯一**形态（`/export` 是 `route not found`）。
        """
        clean = validate_slug(slug)
        await self.connect()
        path = f"/spaces/{quote(clean, safe='')}/export.html"
        result = await self._binary(path)
        return MuseBinary(
            data=result.data,
            mime=result.mime,
            filename=f"{clean}.html",
        )

    async def list_idea_cards(self) -> dict[str, object]:
        """§12.3：`GET /api/idea-cards`，返回归一化的 `{sections, viewerState, pagination}`。

        真机 200 / 67,759 B / 7 个 section；卡片图标在
        `presentation.icon.variants[].url`（相对路径 `/api/idea-cards/media/...`）。
        本轮只读列表与详情，**不**接 `idea-cards.execute`：那是"AI 搭 Web 应用"
        的异步生成路径（`docs/CANVAS_DESIGN.md` §10），与本轮"只读预览卡"不是
        同一个产品面。
        """
        await self.connect()
        payload = await self._json("GET", "/api/idea-cards", None)
        return normalize_idea_cards_response(payload)

    async def get_idea_card(self, card_id: str) -> dict[str, object]:
        """§12.3：`GET /api/idea-cards/{ideaCardId}`，返回归一化卡片（含 `items`）。

        真机 200 / 1,877 B `{"ok":true,"result":{"card":{...}}}`。
        """
        clean = validate_identifier(card_id, what="idea card id")
        await self.connect()
        path = f"/api/idea-cards/{quote(clean, safe='')}"
        payload = await self._json("GET", path, None)
        return normalize_idea_card_detail(payload)

    async def idea_card_media(self, handle: str) -> MuseBinary:
        """§12.3：`GET /api/idea-cards/media/{handle}`，返回图标/媒体字节。

        真机：`icon-57e8…aa6` → 200 `image/png` 1,045 B（PNG 签名核对过），纯协议
        可取，不需要浏览器。mime 以**文件头嗅探**为准（声明可能是错的，repo 内
        `media`/`refs` 同一条判据），嗅探不出才回退声明；是否 inline 由路由层
        白名单决定。
        """
        clean = validate_identifier(handle, what="idea card media handle")
        await self.connect()
        path = f"/api/idea-cards/media/{quote(clean, safe='')}"
        result = await self._binary(path)
        return MuseBinary(data=result.data, mime=result.mime, filename=clean)
