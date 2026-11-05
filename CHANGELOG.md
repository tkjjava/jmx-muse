# 更新日志

## [0.1.0] - 2026-11-05

首个版本。

### 新增

- **账号级互斥锁**：同一账号（同一 Chrome 窗口）同一时刻只允许一个请求。
  两个请求同时往同一个输入框打字会让 DOM 互相污染、回复分不清归属。
  锁覆盖整个请求（含流式输出），通过 `DispatchResult.release()` 释放。
  - 有其它空闲账号时自动切换，不直接报错
  - 全部账号忙时返回 429 并说明「muse 只能串行」
  - 失败路径保证释放锁，不漏锁
  - `/healthz` 新增 `accounts_busy`
- **自检工具** `python -m muse_bridge.doctor`
  - 加密密钥、数据库（实测加密落盘）
  - playwright 依赖检测
  - Chrome CDP 端口连通性
  - muse.ai 登录态（前四项都通过才检查）
- **OpenAI 兼容接口**
  - `POST /v1/chat/completions`（非流式 + SSE 流式）
  - `GET /v1/models`（别名全部映射到 `muse-spark-1.3`）
- **CDP 驱动**
  - `connect_over_cdp()` 复用已登录 Chrome，不启动新实例
  - 只断开连接，不关闭用户浏览器
  - 延迟导入 playwright，未安装时给可操作报错
- **DOM 读取**
  - 选择器集中表 + 候选列表（`selectors.py`）
  - 文本单调扩展判定增量
  - 静默窗口 + 流式标记双重结束判定
  - 非单调文本整体重发（宁可重复不丢内容）
- **会话管理**
  - Fernet 加密 SQLite，密钥缺失抛错
  - `touch_session()` 走页面内 fetch 续期 `hatch_vml`（48h）
  - `wake_vm()` 唤醒休眠 VM
  - 9 状态状态机，探针 15 分钟新鲜度闸门
- **账号管理**
  - `POST /admin/attach` 连接 CDP 并入库
  - `POST /admin/accounts/{id}/refresh` 续期
  - `POST /admin/accounts/{id}/wake` 唤醒
  - `GET /admin/accounts` 脱敏快照

### 测试

### 修复

- **取消时锁泄漏**：客户端中途断开时任务处于 cancelled 状态，
  直接 `await release()` 会被立即取消，账号锁永久不释放。
  改用 `asyncio.shield` 保护收尾路径。
- **stream_reply 无超时兜底**：driver 自己有 deadline，但若其实现有 bug
  或前端一直显示"生成中"，`pool` 层会无限产出，把连接和锁永远占住。
  新增 pool 层超时保护与生成器显式关闭。
- 流式生成器被提前丢弃时未 `aclose()`，内部循环不会退出。

### 新增配置

- `.env.example`：所有环境变量与默认值的参考清单

### 测试

46 项，不启动真实浏览器。覆盖：

- 选择器表完整性
- 状态机（终态不派单、探针过期、VM 休眠可派单）
- 会话刷新窗口计算
- 心跳间隔必须短于刷新窗口
- 模型别名映射
- 账号轮转
- 派单失败落状态（browser_unavailable / session_expired）
- profile 加密落盘
- 单调扩展差分逻辑
- 非单调重绘处理

### 明确不含

- 注册 / 登录自动化
- Noise 加密复刻 / notary 伪造
- 指纹伪装
- 账号共享

---

作者：**君莫笑**
**永恒之兰**
