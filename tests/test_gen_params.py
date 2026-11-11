"""`muse_bridge.gen_params` 的离线测试：参数取值域、拒绝策略、与既有实现的同源约束。

全部离线：不连 muse.ai、不起浏览器、不读网络。外部素材只作为证据字符串出现在
`FieldSpec.evidence` 里（本测试只断言非空与数量，不打开那些文件——它们不在仓库内，
CI 环境不一定有）。

为什么要钉「hypothesis 不得进生产路径」：并行改动已经把 `app.py:1001-1027` 接到
本模块（fal 入口经 `normalize_params` + `render_prompt_clauses`，`seed` 会被拒成 400），
但 `protocol_generation.py:190,235-236` 的 `GenerationRequest` 仍留着 `seed` 字段。
一处已降级、一处未降级，说明「拒绝策略」是全仓唯一闸门：一旦有人放宽它，
未实证字段就会从那条尚未降级的路径流进真实请求。测试把拒绝策略钉死，
改宽必须先改测试、也就必须先过审。
"""

from __future__ import annotations

import dataclasses

import pytest

from muse_bridge import gen_params, protocol_generation

# ---- 规格自身的不变量 ---------------------------------------------------


def test_levels_are_fixed_and_production_limited_to_two():
    assert gen_params.LEVELS == ("observed", "corroborated", "hypothesis", "unverified")
    assert frozenset({"observed", "corroborated"}) == gen_params.PRODUCTION_LEVELS
    # hypothesis / unverified 永远不在生产集合里，这是本模块的核心纪律。
    assert "hypothesis" not in gen_params.PRODUCTION_LEVELS
    assert "unverified" not in gen_params.PRODUCTION_LEVELS


def test_param_spec_only_contains_production_levels_and_no_hypothesis_keys():
    for name, spec in gen_params.PARAM_SPEC.items():
        assert spec.name == name
        assert spec.level in gen_params.PRODUCTION_LEVELS, f"{name} 等级不合格"
    overlap = set(gen_params.PARAM_SPEC) & set(gen_params.HYPOTHESIS_FIELDS)
    assert overlap == set(), f"同一字段同时出现在白名单与黑名单：{overlap}"


def test_param_spec_carriers_evidence_and_rationale_are_present():
    assert gen_params.PARAM_SPEC, "生产白名单不能是空的"
    for name, spec in gen_params.PARAM_SPEC.items():
        assert spec.carrier in gen_params.CARRIERS
        assert spec.kind in ("int", "str")
        assert isinstance(spec.values, tuple)
        assert spec.why.strip(), f"{name} 缺少中文理由"
        for item in spec.evidence:
            assert item.strip(), f"{name} 有空白证据项"
            # 证据必须是「文件:行号」或「chunk@偏移」形态，防止写成一句空话。
            assert (":" in item) or ("@" in item), f"{name} 证据形态不对：{item!r}"


def test_field_spec_is_frozen():
    spec = gen_params.PARAM_SPEC["duration"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.values = (5,)  # type: ignore[misc]


@pytest.mark.parametrize("level", ["hypothesis", "unverified"])
def test_field_spec_refuses_unproven_levels(level):
    with pytest.raises(ValueError) as excinfo:
        gen_params.FieldSpec(
            name="seed",
            kind="int",
            values=(1,),
            carrier="prompt_text",
            level=level,
            evidence=("a.js:1", "b.js:2"),
            why="测试用",
        )
    assert "不允许进生产白名单" in str(excinfo.value)


def test_field_spec_refuses_unknown_level():
    with pytest.raises(ValueError) as excinfo:
        gen_params.FieldSpec(
            name="x",
            kind="int",
            values=(1,),
            carrier="prompt_text",
            level="probably",
            evidence=("a.js:1",),
            why="测试用",
        )
    assert "未知验证等级" in str(excinfo.value)


def test_field_spec_requires_two_independent_evidence_items_for_corroborated():
    with pytest.raises(ValueError) as excinfo:
        gen_params.FieldSpec(
            name="duration",
            kind="int",
            values=(5,),
            carrier="prompt_text",
            level="corroborated",
            evidence=("only/one.js:1",),
            why="单份来源不该声称 corroborated",
        )
    assert "corroborated" in str(excinfo.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "float"},
        {"carrier": "telepathy"},
        {"values": ()},
        {"values": (5, 5)},
        {"evidence": ()},
    ],
)
def test_field_spec_rejects_malformed_specs(kwargs):
    base = {
        "name": "probe",
        "kind": "int",
        "values": (5,),
        "carrier": "prompt_text",
        "level": "observed",
        "evidence": ("a.js:1",),
        "why": "测试用",
    }
    base.update(kwargs)
    with pytest.raises(ValueError):
        gen_params.FieldSpec(**base)


# ---- duration ----------------------------------------------------------


@pytest.mark.parametrize("value", [5, 6, 8, 10])
def test_duration_accepts_whitelist(value):
    out = gen_params.normalize_params({"duration": value})
    assert out == {
        "model": None,
        "duration": value,
        "aspect_ratio": None,
        "expect": "video",
    }


@pytest.mark.parametrize("value", [0, 4, 7, 9, 12, 100])
def test_duration_rejects_values_outside_whitelist(value):
    with pytest.raises(ValueError) as excinfo:
        gen_params.normalize_params({"duration": value})
    assert "不在允许取值" in str(excinfo.value)


@pytest.mark.parametrize("value", [5.0, "5", True, [5], {"d": 5}])
def test_duration_rejects_non_integer_types(value):
    with pytest.raises(ValueError) as excinfo:
        gen_params.normalize_params({"duration": value})
    assert "必须是整数" in str(excinfo.value)


# ---- aspect_ratio ------------------------------------------------------


@pytest.mark.parametrize("value", ["16:9", "9:16", "1:1", "4:3", "3:4"])
def test_aspect_ratio_accepts_whitelist(value):
    out = gen_params.normalize_params({"aspect_ratio": value})
    assert out["aspect_ratio"] == value
    assert out["expect"] is None, "只有画幅不该推断出图/视频"


@pytest.mark.parametrize("value", ["21:9", "16/9", "16：9", "1:1 ", "", "SQUARE"])
def test_aspect_ratio_rejects_non_whitelist_spellings(value):
    with pytest.raises(ValueError) as excinfo:
        gen_params.normalize_params({"aspect_ratio": value})
    assert "不在允许取值" in str(excinfo.value)


@pytest.mark.parametrize("value", [169, ["16:9"], ("16:9",)])
def test_aspect_ratio_rejects_non_string_types(value):
    with pytest.raises(ValueError) as excinfo:
        gen_params.normalize_params({"aspect_ratio": value})
    assert "必须是字符串" in str(excinfo.value)


# ---- model / expect ----------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expect"), [("muse-image", "image"), ("muse-video", "video")]
)
def test_model_accepts_bridge_aliases_and_derives_expect(model, expect):
    out = gen_params.normalize_params({"model": model})
    assert out["model"] == model
    assert out["expect"] == expect


@pytest.mark.parametrize("value", ["muse-spark", "gpt-4o", "sora", ""])
def test_model_rejects_unknown_ids(value):
    with pytest.raises(ValueError) as excinfo:
        gen_params.normalize_params({"model": value})
    assert "不在允许取值" in str(excinfo.value)


@pytest.mark.parametrize("value", [1, True, ["muse-video"]])
def test_model_rejects_non_string_types(value):
    with pytest.raises(ValueError) as excinfo:
        gen_params.normalize_params({"model": value})
    assert "必须是字符串" in str(excinfo.value)


def test_model_values_and_expect_map_are_consistent():
    # 白名单里的每个模型都必须有 expect 映射，映射里也不许出现白名单外的模型。
    assert set(gen_params._EXPECT_BY_MODEL) == set(
        gen_params.PARAM_SPEC["model"].values
    )
    assert set(gen_params._EXPECT_BY_MODEL.values()) <= {"image", "video"}


def test_duration_with_image_model_is_rejected():
    with pytest.raises(ValueError) as excinfo:
        gen_params.normalize_params({"model": "muse-image", "duration": 8})
    assert "图片模型" in str(excinfo.value)


def test_full_video_combo_normalizes():
    out = gen_params.normalize_params(
        {"model": "muse-video", "duration": 8, "aspect_ratio": "9:16"}
    )
    assert out == {
        "model": "muse-video",
        "duration": 8,
        "aspect_ratio": "9:16",
        "expect": "video",
    }


# ---- 未知字段 / hypothesis 拒绝策略 -----------------------------------


def test_empty_inputs_are_allowed():
    for raw in (None, {}):
        out = gen_params.normalize_params(raw)
        assert out == {
            "model": None,
            "duration": None,
            "aspect_ratio": None,
            "expect": None,
        }


def test_null_for_whitelisted_field_means_absent():
    out = gen_params.normalize_params({"model": None, "duration": None})
    assert out["model"] is None
    assert out["duration"] is None
    assert out["expect"] is None


@pytest.mark.parametrize("raw", [[], "duration=8", 42, True])
def test_non_mapping_input_is_rejected(raw):
    with pytest.raises(ValueError) as excinfo:
        gen_params.normalize_params(raw)  # type: ignore[arg-type]
    assert "必须是 mapping" in str(excinfo.value)


def test_unknown_key_is_rejected_and_lists_allowed_keys():
    with pytest.raises(ValueError) as excinfo:
        gen_params.normalize_params({"strength": 0.5})
    message = str(excinfo.value)
    assert "未知生成参数" in message
    assert "strength" in message
    for allowed in gen_params.PARAM_SPEC:
        assert allowed in message


@pytest.mark.parametrize("key", sorted(gen_params.HYPOTHESIS_FIELDS))
def test_every_hypothesis_field_is_explicitly_rejected(key):
    # 值本身给什么都无所谓：hypothesis 检查在类型校验之前，先拒绝再谈值。
    with pytest.raises(ValueError) as excinfo:
        gen_params.normalize_params({key: 1})
    message = str(excinfo.value)
    assert "hypothesis" in message
    assert key in message
    assert gen_params.HYPOTHESIS_FIELDS[key][:12] in message, "报错里必须带原始理由"


def test_hypothesis_field_is_rejected_even_with_null_value():
    # 传 null 不能当作「没给」——否则调用方可以用 seed=null 试探白名单松紧。
    with pytest.raises(ValueError) as excinfo:
        gen_params.normalize_params({"seed": None})
    assert "seed" in str(excinfo.value)


def test_hypothesis_rejection_reason_mentions_second_hand_source():
    for reason in gen_params.HYPOTHESIS_FIELDS.values():
        assert reason.strip()
        assert any(word in reason for word in ("唯一出处", "单份二手", "只有")), reason


# ---- 与既有实现的同源约束（不修改既有文件，只钉一致性） ----------------


def test_whitelist_parity_with_protocol_generation_constants():
    # protocol_generation.py:116-117 是第二套白名单；两套必须一致，否则同一个
    # duration=8 会在一条路径合法、在另一条路径非法。
    assert (
        gen_params.PARAM_SPEC["duration"].values == protocol_generation.VIDEO_DURATIONS
    )
    assert (
        gen_params.PARAM_SPEC["aspect_ratio"].values
        == protocol_generation.ASPECT_RATIOS
    )


def test_protocol_generation_accepts_only_the_same_duration_set():
    for value in gen_params.PARAM_SPEC["duration"].values:
        protocol_generation.GenerationRequest(prompt="x", duration=value)
    with pytest.raises(protocol_generation.GenerationError):
        protocol_generation.GenerationRequest(prompt="x", duration=7)


def test_protocol_generation_accepts_only_the_same_aspect_set():
    for value in gen_params.PARAM_SPEC["aspect_ratio"].values:
        protocol_generation.GenerationRequest(prompt="x", aspect_ratio=value)
    with pytest.raises(protocol_generation.GenerationError):
        protocol_generation.GenerationRequest(prompt="x", aspect_ratio="21:9")


def test_protocol_generation_has_no_seed_entry():
    # 早期版本给 `GenerationRequest` 留过 seed（单份二手来源）。它现在被**删掉**：
    # 允许构造就等于告诉调用方"传了有用"。这条钉住"没有入口"，防止有人再加回来
    # 却没同步 gen_params 的 hypothesis 等级。
    fields = {
        item.name for item in dataclasses.fields(protocol_generation.GenerationRequest)
    }
    assert "seed" not in fields
    assert "seed" in gen_params.HYPOTHESIS_FIELDS


# ---- 提示词子句渲染 ----------------------------------------------------


def test_render_prompt_clauses_matches_protocol_generation_wording():
    normalized = gen_params.normalize_params({"duration": 8, "aspect_ratio": "9:16"})
    clauses = gen_params.render_prompt_clauses(normalized)
    assert clauses == "画面比例严格 9:16，时长严格为 8 秒"
    rendered = protocol_generation.GenerationRequest(
        prompt="海边走路", expect="video", duration=8, aspect_ratio="9:16"
    ).render_prompt()
    assert clauses in rendered, "两套渲染措辞必须同源，不能各写一份"


def test_render_prompt_clauses_for_empty_and_single_field():
    assert gen_params.render_prompt_clauses(gen_params.normalize_params(None)) == ""
    only_aspect = gen_params.render_prompt_clauses(
        gen_params.normalize_params({"aspect_ratio": "16:9"})
    )
    assert only_aspect == "画面比例严格 16:9"
    only_duration = gen_params.render_prompt_clauses(
        gen_params.normalize_params({"duration": 5})
    )
    assert only_duration == "时长严格为 5 秒"


def test_normalize_returns_fresh_dict_each_call():
    first = gen_params.normalize_params({"duration": 6})
    second = gen_params.normalize_params({"duration": 6})
    assert first == second
    first["duration"] = 5
    assert second["duration"] == 6, "返回的是新 dict，调用方改它不能影响下一次"
