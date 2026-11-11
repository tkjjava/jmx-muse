"""生成参数白名单 —— 本仓唯一允许的图/视频生成参数取值域。

为什么需要这个文件
------------------
本轮前端逆向的硬结论：muse.ai 的 hatch-web 在线上**没有**结构化的生成参数字段。
两套互相独立的实现（lichao199208 的纯协议分支、manishkumar1601 的 Node 分支）
发往 `POST /chat/stream` 的载荷里只有文本项/会话 id/能力位；时长与画幅在浏览器
路径（上游 `build_image_prompt` / `build_video_prompt`）和纯协议路径里都是写进
自然语言提示词的。于是本仓所谓「参数白名单」只能是**桥自己的约定**：允许哪些值
被编进提示词文本。约定必须有单点出处：本轮交付前 `app.py` 曾自己拼
`--duration/--aspect/--seed`（muse 侧零解析证据），`protocol_generation.py` 又另存
一套常量；现在两处都已指向本模块（`app.py:1001-1027` 调 `normalize_params` 与
`render_prompt_clauses`，`protocol_generation.py:68,118-119` 直接取 `PARAM_SPEC`），
单一出处就此成立。来龙去脉见 `docs/FRONTEND_RE.md` §1、§5。

验证分级（全仓统一口径，`docs/FRONTEND_RE.md` 第 0 节重复定义）
--------------------------------------------------------------
* observed      —— 本工作区里直接看得到字节/字面量（真实 chunk 文件、抓包记录）。
* corroborated  —— ≥2 份**互相独立**的二手实现/观察记录给出同一结论。
* hypothesis    —— 只有 1 份二手来源。
* unverified    —— 零证据。

结构性纪律：`FieldSpec` 在构造时就把 hypothesis/unverified 挡在门外，所以未实证的
字段根本进不了 `PARAM_SPEC`；它们只能登记在 `HYPOTHESIS_FIELDS`，而
`normalize_params` 见到它们会抛带中文原因的错。这样「忘了删掉一个未验证字段」
不会变成线上事故，而是很早就炸在调用方。

证据路径口径
------------
* 外部素材路径相对 `D:\\deep seek`（例如 `work/museapi-video/platform/config.js`）。
* hatch-web 的真实 chunk 被压缩成单行 JS，行号没有意义，因此用字符偏移标注成
  `文件@偏移`（例如 `24vdgjhlw22uj.js@9994`）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: 四个验证等级。拼写固定，文档与测试都按字面量断言，不要改大小写。
LEVELS = ("observed", "corroborated", "hypothesis", "unverified")

#: 只有这两个等级允许进生产白名单（`PARAM_SPEC`）。
PRODUCTION_LEVELS = frozenset({"observed", "corroborated"})

#: 参数的承载方式取值。
CARRIERS = frozenset({"prompt_text", "bridge_route"})


@dataclass(frozen=True)
class FieldSpec:
    """一个已实证的生成参数。

    字段：
    * `kind`      —— 只支持 `"int"` / `"str"`，类型校验按它走。
    * `values`    —— 闭集取值域；闭集外的值一律拒绝，不做「宽松透传」。
    * `carrier`   —— 该参数在真实链路上靠什么承载：
                     `"prompt_text"` = 只能编进提示词文本（线上没有这个字段）；
                     `"bridge_route"` = 只在本仓内部决定走哪条生成路径。
    * `level`     —— 见模块文档的四个等级；只有 observed/corroborated 能进来。
    * `evidence`  —— 证据列表，`文件:行号` 或 `chunk@偏移`；
                     corroborated 至少两条（否则「多份独立」不成立）。
    * `why`       —— 中文理由，解释为什么是这个取值域，以及服务端语义是否证实。
    """

    name: str
    kind: str
    values: tuple[Any, ...]
    carrier: str
    level: str
    evidence: tuple[str, ...]
    why: str

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"{self.name}: 未知验证等级 {self.level!r}，只认 {LEVELS}")
        if self.level not in PRODUCTION_LEVELS:
            raise ValueError(
                f"{self.name}: 验证等级 {self.level!r} 不允许进生产白名单；"
                "未实证字段请登记到 HYPOTHESIS_FIELDS，不要塞进 PARAM_SPEC"
            )
        if self.kind not in ("int", "str"):
            raise ValueError(
                f"{self.name}: kind 只支持 'int' / 'str'，收到 {self.kind!r}"
            )
        if self.carrier not in CARRIERS:
            raise ValueError(
                f"{self.name}: carrier 只支持 {sorted(CARRIERS)}，收到 {self.carrier!r}"
            )
        if not self.values:
            raise ValueError(f"{self.name}: 取值域不能为空（空集等于拒绝一切参数值）")
        if len(set(self.values)) != len(self.values):
            raise ValueError(f"{self.name}: 取值域里有重复值 {self.values!r}")
        if not self.evidence:
            raise ValueError(f"{self.name}: 没有证据的字段不许进白名单")
        if self.level == "corroborated" and len(self.evidence) < 2:
            raise ValueError(
                f"{self.name}: corroborated 需要 ≥2 条互相独立的证据，"
                f"当前只有 {len(self.evidence)} 条（单份来源请按 hypothesis 处理）"
            )


# ---------------------------------------------------------------------------
# 生产白名单：只放 observed / corroborated 的字段。
# ---------------------------------------------------------------------------

PARAM_SPEC: dict[str, FieldSpec] = {
    "model": FieldSpec(
        name="model",
        kind="str",
        values=("muse-image", "muse-video"),
        carrier="bridge_route",
        level="corroborated",
        evidence=(
            "work/museapi-video/platform/generate.js:83",
            "work/muse-re/sources/lichao199208__muse2api/app/muse_engine/service.py:511-512",
            "work/muse-re/sources/lichao199208__muse2api/static/app.js:561-562",
        ),
        why=(
            "muse.ai 线上没有模型 id 字段：生成走 /chat/stream，图还是视频由提示词与"
            "结果资源的 kind 决定。这两个值是两个独立实现都在用的桥侧别名，唯一作用"
            "是决定 expect=image/video，不能当线上参数发给服务端。"
        ),
    ),
    "duration": FieldSpec(
        name="duration",
        kind="int",
        values=(5, 6, 8, 10),
        carrier="prompt_text",
        level="corroborated",
        evidence=(
            "work/museapi-video/platform/config.js:37",
            "work/museapi-video/recon/upstream/README.md:53",
            "work/museapi-video/recon/upstream/app.py:945-976",
        ),
        why=(
            "时长不是线上字段：上游把它写成自然语言（`时长严格为 {dur} 秒`，"
            "app.py:976），纯协议路径同样只能写文本。5/6/8/10 这套取值在平台侧与"
            "上游文档里一致；muse 服务端是否只认这几个值**未证实**（它压根不解析）。"
        ),
    ),
    "aspect_ratio": FieldSpec(
        name="aspect_ratio",
        kind="str",
        values=("16:9", "9:16", "1:1", "4:3", "3:4"),
        carrier="prompt_text",
        level="corroborated",
        evidence=(
            "work/museapi-video/platform/config.js:38",
            "work/museapi-video/recon/upstream/README.md:49",
            "work/muse-re/sources/lichao199208__muse2api/static/app.js:561,567",
        ),
        why=(
            "上游两个 prompt 构造器按这几个字面量分支（16:9/9:16/1:1/4:3/3:4），"
            "未识别的值落到通用兜底句 `{aspect} 画面比例`（app.py:734-737）。"
            "所以这是文本约定，不是服务端枚举。"
        ),
    ),
}

# ---------------------------------------------------------------------------
# 未实证字段：禁止使用。原因必须写清「为什么只有一份证据」。
# ---------------------------------------------------------------------------

HYPOTHESIS_FIELDS: dict[str, str] = {
    "seed": (
        "唯一出处是 museapi-video 自己的参数面板（ParamsPanel.jsx:35 的 `--seed`），"
        "muse 侧零字段、零解析证据；本仓 app.py:1014 已有同样写法，属于照抄二手。"
    ),
    "stylize": (
        "唯一出处是 museapi-video 参数面板的 `--st`（ParamsPanel.jsx:18，0..1000），"
        "Midjourney 风格的字符串拼接；muse 线上没有任何解析它的代码。"
    ),
    "st": "与 stylize 同源（ParamsPanel.jsx:18 的短名），同样是单份二手。",
    "chaos": (
        "唯一出处是 ParamsPanel.jsx:19（`--chaos`，0..100），"
        "没有第二份实现、也没有任何 muse 侧证据。"
    ),
    "weird": "唯一出处是 ParamsPanel.jsx:20（`--weird`，0..3000 步进 50），单份二手。",
    "iw": "唯一出处是 ParamsPanel.jsx:21（`--iw`，0..3 步进 0.1），单份二手。",
    "cw": "唯一出处是 ParamsPanel.jsx:22（`--cw`，0..100），单份二手。",
    "sw": "唯一出处是 ParamsPanel.jsx:23（`--sw`，0..100），单份二手。",
    "quality": (
        "唯一出处是 ParamsPanel.jsx:24（`--q`，0.25..2 步进 0.25），单份二手；"
        "muse 侧没有任何「质量档位」字段。"
    ),
    "q": "与 quality 同源（ParamsPanel.jsx:24 的短名），单份二手。",
    "resolution": (
        "只有 muse2api 这一支上游实现有（app.py:496 的 VideoRequest.resolution、"
        "app.py:988-989 把 `画质规格：{resolution}` 写进提示词）；"
        "平台侧 UI 不暴露它，也没有任何真机证据证明 muse 认这个说法。"
    ),
    "extra": (
        "只有 muse2api 自家 REST 定义（app.py:742/:990 把 extra 原样插进提示词）一份；"
        "语义是任意文本 = 允许绕过本白名单，因此在真机复核前必须拒绝。"
    ),
}


def _hypothesis_error(key: str, reason: str) -> str:
    """hypothesis 字段的统一报错文案：把「为什么不行」和「怎样才行」都说清。"""
    return (
        f"生成参数 {key!r} 尚未实证（hypothesis），禁止使用：{reason}"
        " 要启用它必须先做真机复核，把验证等级提到 observed / corroborated 之后"
        "再用 FieldSpec 加进 PARAM_SPEC。"
    )


def _check_value(spec: FieldSpec, value: Any) -> Any:
    """按 FieldSpec 的 kind 与闭集校验单个值；不合法就抛中文 ValueError。"""
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"{spec.name}={value!r} 必须是整数（bool 是 int 的子类，这里也拒绝）"
            )
    elif not isinstance(value, str):
        raise ValueError(
            f"{spec.name}={value!r} 必须是字符串，收到 {type(value).__name__}"
        )
    if value not in spec.values:
        raise ValueError(
            f"{spec.name}={value!r} 不在允许取值 {list(spec.values)} 内"
            f"（验证等级 {spec.level}；证据见 FieldSpec.evidence）"
        )
    return value


#: model → 成品 kind。键集与 `PARAM_SPEC["model"].values` 保持一致（测试钉住）。
_EXPECT_BY_MODEL = {"muse-image": "image", "muse-video": "video"}


def normalize_params(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """把调用方给的生成参数归一成受控 dict；越界/未实证/未知一律抛 ValueError。

    返回键固定为 `model` / `duration` / `aspect_ratio` / `expect`：
    * 没给的字段是 `None`，显式传 `None` 等同于没给（hypothesis 字段例外：
      只要键出现就拒绝，避免调用方用 null 蒙混过关）；
    * `expect` 由 `model` 推导（`muse-image`→`image`、`muse-video`→`video`）；
      只给了 `duration` 时推导成 `video`（时长只对视频有意义），否则 `None`。

    为什么返回 `expect`：本仓内部靠它决定要取回的成品 kind，属于纯路由信息；
    它不是线上字段，调用方不要把 `expect` 写回上游请求。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"生成参数必须是 mapping，收到 {type(raw).__name__}")

    normalized: dict[str, Any] = {"model": None, "duration": None, "aspect_ratio": None}
    for key, value in raw.items():
        reason = HYPOTHESIS_FIELDS.get(key)
        if reason is not None:
            raise ValueError(_hypothesis_error(key, reason))
        if value is None:
            continue
        spec = PARAM_SPEC.get(key)
        if spec is None:
            raise ValueError(
                f"未知生成参数 {key!r}；只接受 {sorted(PARAM_SPEC)}。"
                f"另外这些字段因未实证被显式拒绝：{sorted(HYPOTHESIS_FIELDS)}"
            )
        normalized[key] = _check_value(spec, value)

    model = normalized["model"]
    duration = normalized["duration"]
    expect = _EXPECT_BY_MODEL.get(model)
    if expect is None and duration is not None:
        expect = "video"
    if expect == "image" and duration is not None:
        raise ValueError(
            f"model={model} 是图片模型，但传了 duration={duration}；"
            "时长只对 muse-video 有意义，图片路径没有任何时长语义"
        )
    normalized["expect"] = expect
    return normalized


def render_prompt_clauses(params: Mapping[str, Any]) -> str:
    """把已归一的参数渲染成提示词附加句，返回空串表示无需追加。

    措辞与 `protocol_generation.GenerationRequest.render_prompt()` 完全一致
    （`画面比例严格 X`、`时长严格为 N 秒`），避免同一参数出现两套文本；那两句本身
    又是上游 `build_video_prompt` 的实际写法，不是本仓发明的。
    """
    parts: list[str] = []
    aspect = params.get("aspect_ratio")
    duration = params.get("duration")
    if aspect:
        parts.append(f"画面比例严格 {aspect}")
    if duration:
        parts.append(f"时长严格为 {duration} 秒")
    return "，".join(parts)


__all__ = [
    "CARRIERS",
    "HYPOTHESIS_FIELDS",
    "LEVELS",
    "PARAM_SPEC",
    "PRODUCTION_LEVELS",
    "FieldSpec",
    "normalize_params",
    "render_prompt_clauses",
]
