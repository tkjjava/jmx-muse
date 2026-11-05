# 贡献指南

> 作者：**君莫笑** · **永恒之兰**

## 欢迎的贡献

- **DOM 选择器适配**（前端改版后更新 `selectors.py`）——这是最有价值的贡献
- 流式判定的可靠性改进
- 文档修正
- 测试覆盖补充

## 不接受的方向

1. **任何形式的自动注册** —— muse 需要 Meta OAuth，本项目不介入
2. **复刻 Noise 加密 / 伪造 notary token** —— 这是绕过服务端信任根
3. **指纹伪装 / stealth 注入** —— 修改 UA、注入反检测脚本
4. **账号对外共享**

理由：这些会让项目从"协议适配"变成"绕过服务方控制"。

## 开发环境

```bash
python -m pip install -r requirements.txt
python -m pip install pytest pytest-asyncio
```

## 跑测试

```bash
python -m pytest tests -q
```

测试**不启动真实浏览器**，全部用假 driver。新增测试也应如此。

需要真实浏览器的验证请手动做，不要写进自动化测试。

## 代码约定

- 类型标注齐全，`from __future__ import annotations`
- **所有 DOM 选择器必须在 `selectors.py`**，不许散落在 driver 里
- 浏览器异常转成 `BrowserUnavailable` / `NotLoggedIn`，不让原始异常穿透
- 只断开 CDP，不关闭用户的 Chrome
- 中文注释，解释"为什么"

## 修改选择器的正确做法

```python
# ✅ 加到候选列表最前面，保留旧的作为兜底
COMPOSER_INPUTS = (
    'div[data-new-version="true"] textarea',   # 新增
    'textarea[placeholder]',                    # 保留
    '[contenteditable="true"][role="textbox"]',
)

# ❌ 不要直接替换掉旧的
COMPOSER_INPUTS = ('div[data-new-version="true"] textarea',)
```

## 提交信息格式

```
<类型>: <简短描述>
```

类型：`feat` / `fix` / `docs` / `test` / `refactor` / `chore`

---

作者：**君莫笑**
**永恒之兰**
