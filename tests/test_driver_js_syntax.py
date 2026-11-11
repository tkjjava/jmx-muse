"""JS 片段语法哨兵（离线）。

2026-11-11 真机实测抓到 `_JS_EXTRACT` 少一个右括号：Playwright 的
`page.evaluate()` 直接抛 SyntaxError，而 `_js()` 把异常吞成 None，于是
"生成成功但取不回文件"——成品提取整条链路静默失效，浏览器侧生图全部 500。
这类错误在离线单测里只能靠括号配平来抓（不起浏览器解析器），足够抓住
"少一个括号/多一个花括号"这种形态；语义错误仍然要真机跑。
"""

from __future__ import annotations

import muse_bridge.driver as driver_module


def _js_constants() -> list[tuple[str, str]]:
    return [
        (name, value)
        for name in dir(driver_module)
        if name.startswith("_JS") and isinstance(
            (value := getattr(driver_module, name)), str
        )
    ]


def test_every_js_snippet_has_balanced_delimiters():
    broken = []
    for name, text in _js_constants():
        delta = (
            text.count("(") - text.count(")"),
            text.count("{") - text.count("}"),
            text.count("[") - text.count("]"),
        )
        # 括号出现在字符串字面量时会误报——JS 片段里没有这种写法，出现就说明
        # 有人往里塞了带括号的文案，先让测试红，提醒他改成字符拼接或转义。
        if any(delta):
            broken.append(f"{name}: 括号差值 {delta}")
    assert not broken, "JS 片段括号不配平（Playwright 会直接 SyntaxError）：\n" + "\n".join(
        broken
    )


def test_js_constants_are_function_expressions_or_invoked():
    """`page.evaluate()` 只接受函数表达式或已经声明的调用，不接受裸语句。"""
    for name, text in _js_constants():
        stripped = text.strip()
        assert (
            stripped.startswith("(")
            or stripped.startswith("async")
            # `_JS_COMPOSER_ROOTS` 是拼接用的 const 前缀，不是独立表达式。
            or stripped.startswith("const ")
        ), f"{name} 既不是函数表达式也不是可拼接的 const 片段：{stripped[:40]!r}"


def test_hydration_probe_recognises_chinese_connecting_marker():
    """中文界面的「正在连接」必须算 not-ready。

    2026-11-11 真机：页面显示的是 `Muse 正在连接…`，旧片段只认英文
    `Connecting...`，于是 `wait_hydrated()` 返回 ready，发送路径照常填字，
    消息实际没发出去——要么 180 秒后报"没有产生新回答"，要么旧版本直接
    返回 200 + 空正文。两个标记都必须在，少一个就退回同一个坑。
    """
    text = driver_module._JS_HYDRATION
    assert "Connecting..." in text
    assert "正在连接" in text
    assert "'connecting'" in text
