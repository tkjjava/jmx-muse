# 0.4.2 发布候选评估（C3）

> 生成时间：2026-11-11 21:1x（时间写法含义见 `CHANGELOG.md` 顶部"时间约定"）
> 候选范围：`34254d2..5fd099c`（35 个文件，+8812 / −88）
> 对照的路线图条目：`docs/2026-11-11-roadmap.md` §5 C3（5 条）与"发布门槛"

## 结论（先给答案）

- **代码侧可以进入发布候选**：测试基线可信（`1620 passed` / `ruff` 全绿），
  启停恢复有实测记录，媒体交付验收通过，P0 生命周期不变量有专项用例。
- **有两处缺口，不冒充通过**：
  1. relay 侧"媒体经下游透传"本轮**未触发**（上游对纯文本提示没回附件）；
  2. Chrome/CDP 之外的客户端（桌面 GUI、移动端播放器）**未测**。
- **发布动作本身没有执行**：打 tag / 发 release 属于路线图规定的"需用户指令"
  事项，本文只提供评估与回退依据。

---

## 1. 候选范围（清点）

| 分组 | 文件数 | 增 | 删 |
| --- | --- | --- | --- |
| 仓库根文件（CHANGELOG / README / requirements / .env.example） | 4 | 1591 | 48 |
| 工具 `tools/` | 9 | 1684 | 0 |
| 文档 `docs/` | 12 | 4839 | 26 |
| 测试 `tests/` | 6 | 466 | 0 |
| 源码 `muse_bridge/` | 4 | 232 | 14 |
| **合计** | **35** | **8812** | **88** |

核心源码逐文件：

| 文件 | 增 | 删 | 改的是什么 |
| --- | --- | --- | --- |
| `muse_bridge/app.py` | 78 | 7 | 媒体端点 HEAD/Range/416、错误分类、可观测性展示 |
| `muse_bridge/driver.py` | 69 | 4 | 助手正文提取（按角色排除用户气泡）、**消息身份判据**（见 §5.2） |
| `muse_bridge/media.py` | 53 | 1 | 原子写、MIME 嗅探、超龄清扫、Range 语义 |
| `muse_bridge/pool.py` | 32 | 2 | 驱动二选一（协议优先/浏览器兜底）、失败分类 |

新增工具共 11 支（`tools/`，另有 `README.md` 索引）：`acceptance.py`（活网验收 21 项）、
`accept_relay_media.py`（relay 媒体透传验收）、`accept_video.py`、`bridge_ops.py`
（状态/观测/日志）、`refresh_account.py`、`diff_check.py`、
`verify_tests_load_bearing.py`、`restart_bridge.py`、`lifecycle_check.py`
（启停恢复校验）、`doc_assertions.py`（文档断言审计）、`peek_muse.py`（只读页面取证）。

> 说明：`tools/` 是从 `work/` 里的一次性探针**清洗**而来——原先硬编码本机路径
> 与密钥、别人拿到跑不起来，且探针可能与产品走不同契约（2026-11-11 那次
> "三条链路一起失败"就是探针把 `proxy` 传成 requests 形式、被产品静默降级成直连）。
> 清洗后的版本与产品共用同一份 env、打印实际观测值、上游没给东西时说"未触发"。

## 2. 文档断言审计

审计结果由 **`python tools/doc_assertions.py`** 产出（可随时复查，`--strict` 接 CI）。

| 审计项 | 结果 | 处理 |
| --- | --- | --- |
| 面向本仓库的 8 份文档里的 `file.py:line` 引用 | **10 条全部可解析到非空行，0 条失效** | 无需处理 |
| 指向仓库外上游产物的行号 | 377 处（12 份逆向记录） | 约定为"只统计不判定"，因为这些源文件本仓库不持有 |
| 含糊时刻（`16:1x` 一类） | **88 处** | 不编造精确值；在 `CHANGELOG.md` 顶部写明「时间约定」 |
| 已过时的断言 | 1 处：原"本机无 relay 环境，B4 保留未验收" | 改为如实三段式（relay 1.13.1 已跑通、媒体项未触发） |
| 未证明的断言 | 见下表 | 逐条降级或标注边界 |

未证明断言的处置：

| 断言 | 原状态 | 现在怎么写 |
| --- | --- | --- |
| "relay 媒体透传已验收" | 一度凭一次 16/16 的口头结论 | 改为"3/3 通过 + 2 项未触发"，附件形态标"待复现" |
| "思考透传首批选择器可用" | 文档里写成对 DOM 的确认 | 保留"猜测的常见形态，不是确认"，并给出 devtools 自证办法 |
| "UTF-8 无头/有头指纹差异" | 写成结论 | 降级为经验值：`MUSE_HEADLESS` 默认有头是保守选择，不是证明 |
| "非 Chrome 客户端可用" | 未涉及 | 明确列入已知限制（§3.5） |

## 3. 统一说明

### 3.1 默认开关（代码默认值，非启动工具默认值）

| 开关 | 代码默认 | 作用 | 打开意味着什么 |
| --- | --- | --- | --- |
| `MUSE_PROTOCOL_DRIVER` | `0`（关） | 聊天/生成走纯 Noise 协议，失败回退浏览器一次 | 需要账号已导入 cookie 且能连 `*.metaaivm.com` |
| `MUSE_WARM_POOL` | `0`（关） | 跨派单复用同一条 Noise 会话 | 省约 3.3 s 握手；关掉时行为与没有该功能逐字节一致 |
| `MUSE_WARM_POOL_TTL` | `300` | 暖池空闲回收秒数 | 只在暖池打开时生效 |
| `MUSE_STREAM_IDLE_TIMEOUT` | `0` | 订阅流"多少秒没新字节"就结束本回合 | `0` = 关闭保护；打开可防僵死订阅空转 |
| `MUSE_GENERATION_ENABLED` | `1` | 生成能力总开关 | 关掉时 `/v1/images*`、`/v1/videos*` 直接 503，聊天不受影响 |
| `MUSE_REASONING` | `0` | 思考内容以 `delta.reasoning_content` 透传 | 关闭时思考容器**根本不被查询** |
| `MUSE_HEADLESS` | `false` | 无头浏览器 | 默认有头：无头指纹不同，更容易被上游拒绝 |
| `MUSE_REF_LOCAL_FILES` | `0` | 允许素材字段读服务器本地文件 | **危险**：等于把任意文件读挂到 API 上 |
| `MUSE_REF_PRIVATE_FETCH` | `0` | 允许抓内网地址 | **危险**：等于给 API 一个 SSRF 原语 |
| `MUSE_JOB_QUEUE_MAX` | `8` | 生成任务排队上限 | 队列满直接 429，避免无限内存与无限等待 |
| `MUSE_MEDIA_TTL` | `172800` | 成品保留 2 天 | 与上游直链过期节奏一致 |
| `MUSE_MAX_MEDIA_BYTES` | `268435456` | 单个成品上限 256 MiB | 超限直接拒写，不留半截文件 |

**必须强调的一处差异**：`tools/restart_bridge.py` 用 `setdefault` 把
`MUSE_PROTOCOL_DRIVER=1`、`MUSE_WARM_POOL=1`（各配 `_TTL=300`）以及协议代理一并
打开。所以**本机用这个工具启动时，`/healthz` 会显示
`"chat_driver":"protocol"` 与 `"warm_pool":{"enabled":true}`**——这与上表的代码
默认值不同，不是 bug，但发布说明里必须写清"用工具启动 ≠ 出厂默认"。实测：
2026-11-11 20:3x 重启后的就绪行正是 `driver=protocol 可派单=1/1 协议代理=已配置`。

### 3.2 必要配置

| 级别 | 变量 | 缺了会怎样 |
| --- | --- | --- |
| **必填** | `MUSE_BRIDGE_KEY` | 启动直接退出：账号库加密密钥缺失 |
| 强烈建议 | `MUSE_BRIDGE_ADMIN_KEYS` | 不设则退回认 `MUSE_BRIDGE_API_KEYS`，`/healthz` 的 `admin_key_separated` 标 `false`（权限面未分离） |
| 强烈建议 | `MUSE_PROTOCOL_PROXY` | 留空 = 直连；真机实测直连被墙（会话面与 WS 面都要代理） |
| 部署相关 | `MUSE_PUBLIC_BASE` | 留空则媒体 url 是相对路径 `/v1/media/<token>`；反代后要填绝对地址 |
| 可选 | 其余 43 项 | 都有代码默认值；完整清单见 `.env.example`（47 项，有测试钉住不漂移） |

### 3.3 媒体字段契约（下游要按这个接）

| 项 | 值 / 行为 | 证据 |
| --- | --- | --- |
| 字段名 | 非流式在 `choices[].message.media[]`；流式在 `choices[].delta.media[]` | `tools/accept_relay_media.py` 断言 |
| 每项主键 | `token`（`MediaRef.to_dict()` 产出） | 同上 |
| `media.url` | **相对路径** `/v1/media/<token>`；设了 `MUSE_PUBLIC_BASE` 才变绝对 | 同上 |
| 下载 | `GET` 200，`content-type: application/octet-stream`、`nosniff`、`attachment` | bridge 验收 media 6/6 |
| `HEAD` | 200，正文为空，带同一组头 | 同上 |
| `Range` | 206 + 正确的 `Content-Range`；起点越界返回 **416**（不被 clamp 成 206） | 同上 |
| 鉴权 | 错 key / 无 key → 401；未知 token → 404 | relay 1.13.1 探针矩阵 |
| `accept-ranges` | 单值 `bytes`（不是重复头） | 同上 |

### 3.4 已知限制（发布说明里必须带）

1. **非 Chrome 客户端未测**：桌面 GUI、移动端内置播放器这类"不是浏览器但会
   `Range` 请求媒体"的客户端，本轮只验了端点行为（206/416/HEAD/401/404）
   与 Chrome 渲染，没有真机客户端证据。
2. **relay 侧媒体透传本轮未触发**：上游对纯文本提示没有回附件，`tools/accept_relay_media.py`
   的结果是"3/3 通过、2 项未触发"。"未触发"不等于通过。
3. **参考素材有两个上限**：`MUSE_REFERENCE_MAX`（app 层，超出**静默截断**）与
   `MUSE_MAX_REFERENCE_FILES`（refs 层，超出**报 400**）。默认都是 10，所以
   "传 12 个"会先被截断成 10、400 那条分支基本走不到。要统一得先定契约（§8）。
4. **单账号吞吐**：一个账号同一时刻只允许一个请求（同一浏览器窗口的 DOM 会
   互相污染），所以并发度实际等于账号数。
5. **上游 DOM 改版会让浏览器驱动失效**：所有选择器集中在 `muse_bridge/selectors.py`，
   前端一改版就失效——这是浏览器兜底路径的固有脆弱性，协议路径不受影响。
6. **协议路径本机未启用**：`restart_bridge.py` 会打开它，但当前账号没有导入
   cookie（`store.cookies()` 为空时 `_build_driver` 直接选浏览器，不会尝试协议）。
   因此本轮活网验收覆盖的是**浏览器路径**，协议路径的证据来自更早的
   P1-B3~B5 轮记录。

## 4. 目标环境验证（依赖 → 启动 → 停止 → 恢复）

### 4.1 依赖

- `requirements.txt` 6 项全部满足（实测版本）：`httpx 0.28.1`、`fastapi 0.143.3`、
  `uvicorn 0.54.0`、`cryptography 50.0.1`、`pydantic 2.14.1`、`playwright 1.64.0`。
- Noise 协议路径**不硬依赖** `websockets`：`protocol_client._default_ws_opener()`
  惰性导入它（本机装了 17.2），没装则自动回退到本包内置的 `ws_fallback`
  （自带完整 WebSocket 实现，导出 `WebSocket`/`FrameHead` 等）。所以它没写进
  `requirements.txt` 是**有意为之**，不是漏项——`requirements.txt` 里已加注释说明。
  （C3 第一版稿子把这条记成"待办欠账"，核对源码后更正。）
- `python -m pip check` → `No broken requirements found.`
- `python -m pip install -r requirements.txt --dry-run` → 全部
  `Requirement already satisfied`（无任何待装项）。
- 导入冒烟：`pkgutil.walk_packages` 遍历 `muse_bridge` 共 **31 个模块，0 失败**。

### 4.2 启动

```
cd 'muse-bridge'
$env:MUSE_BRIDGE_KEY = <44 字符 Fernet key>
$env:MUSE_PROTOCOL_PROXY = 'http://127.0.0.1:7890'
python tools\restart_bridge.py --log 'D:\deep seek\work\bridge.log'
```

实测：`已启动 pid=44372 端口=8788`；就绪行
`version=0.4.2 driver=protocol 可派单=1/1 协议代理=已配置 暖池=[] 权限面分离=False`；
`/healthz` 200，`accounts_total=1`。

### 4.3 停止

| 观测项 | 停止前 | 停止后 |
| --- | --- | --- |
| pid | 12012 | — |
| 端口 8788 | LISTEN | **关闭**（无监听） |
| `/healthz` | 200 | **连接被拒**（`127.0.0.1:8788` ECONNREFUSED） |

### 4.4 恢复（重启后是否真的可用）

| 观测项 | 停止前 | 重启后 |
| --- | --- | --- |
| pid | 12012 | 44372（新进程） |
| `/healthz` | 200 | 200 |
| 账号数（`accounts_total`） | 1 | **1**（未重建库） |
| `POST /admin/accounts/muse1/refresh` | — | 200 `{"ok":true,"refreshed":true,"via":"browser"}` |
| 非流式聊天 | — | **200**，正文 `OK` |
| `data/media` 成品 | 24 个 / 25404473 B | **24 个 / 25404473 B** |
| `data/muse.db` | 1822720 B | **1822720 B** |

结论：**重启不重建账号库、不清理成品目录**。这条同时是回退方案的前提——回退
代码不会弄丢账号与媒体（`.env`、`data/`、`*.db` 都在 `.gitignore` 里，未被跟踪）。

这一节的步骤已固化成可重复命令：`python tools/lifecycle_check.py`
（快照 → 重启 → 逐项对比，任一退化项报 ✗ 并以非零码退出；加 `--chat`
再多验一次"重启后真的能聊天"）。换机器、换人复核时不必再手抄一遍命令。

## 5. 验收记录（2026-11-11 本次实跑，不含历史口头结论）

| 验收项 | 命令 | 结果 |
| --- | --- | --- |
| bridge 全量测试 | `python -m pytest -q` | **1620 passed**（23.29 s） |
| bridge 静态检查 | `python -m ruff check muse_bridge tests tools` | All checks passed |
| bridge 成品链路 | `python tools\acceptance.py media` | **6/6**（407142 B，octet-stream，206，416，404） |
| bridge 流式 | `python tools\acceptance.py stream` | **10/10**（含"非流式同样成功 200"） |
| bridge 清扫 | `python tools\acceptance.py sweep` | **5/5** |
| 启停恢复（工具化） | `python tools\lifecycle_check.py verify --chat` | **恢复通过**：账号 1→1、媒体/库不变、聊天 200 |
| 文档断言审计 | `python tools\doc_assertions.py` | **失效 0 条**；10 条仓库内引用可解析、377 条上游引用只统计 |
| relay 媒体透传 | `python tools\accept_relay_media.py --relay … --model muse-1` | **3/3 通过，2 项未触发** |
| relay 健康 | `GET http://127.0.0.1:8765/healthz` | 200 `{"status":"ok","version":"1.13.1"}` |
| relay 全量 | 解压后的 `Muse-Relay-V1.13.1.zip` 内跑 `pytest` | **346 passed / 7 skipped** |

### 5.2 本轮修复的一个真缺陷（会误伤正常流量）

| 阶段 | 现象 |
| --- | --- |
| 修复前 | 连续两次问「只回复 OK」：第一次 200 带 `OK`，**第二次起必 504**「发送后 45 秒页面没有任何反应」，账号随即 `rate_limited`，之后一律 503 |
| 反证 | Playwright 直连同一 Chrome 标签、手工发同一条消息 → 5 秒内出现新气泡，正文也是 `OK`（通道与页面都正常） |
| 根因 | 判据用"最后一条助手正文是否变化"；回答与上一条逐字相同时，已发出的新回答被当成旧消息 |
| 修法 | 改用 `data-message-id` 判断"是不是新消息"；id 读不到才回退比正文 |
| 修复后 | 同一提示连发三次 **200 / 200 / 200**（18.7 s / 14.8 s / 13.1 s，正文均 `OK`）；`acceptance.py stream` 6/9 → **10/10** |
| 回归钉子 | `tests/test_driver_reaction_identity.py` 3 条，且已验证"退回旧判据"会让 (a) 失败 |

## 6. 发布门槛清单（对照路线图 §5）

| 门槛 | 状态 | 依据 |
| --- | --- | --- |
| 可信的测试基线 | ✅ | 1620 passed / ruff 全绿；relay 346 passed / 7 skipped |
| P0 生命周期不变量有专项用例 | ✅ | 暖池租用归属、并发拒绝、失败清理等用例在 `tests/` 内 |
| ≥1 条明确记录的媒体交付验收 | ✅ | bridge media 6/6（含 HEAD/206/416/404） |
| relay / 客户端覆盖说明 | ⚠ 部分 | relay 媒体项"未触发"；非 Chrome 客户端未测（§3.4） |
| 回滚不破坏账号与媒体 | ✅ | 启停实验：账号 1→1、媒体 24 个字节不变、db 大小不变 |

## 7. 回退依据（要保留的 baseline）

- 候选之前的最后一个提交：**`34254d2`**（`chore(release): 版本统一到 0.4.2`）。
- 先看清要回退什么：

```bash
git log --oneline 34254d2..HEAD
```

- 安全回退（保留历史，新建分支从 baseline 起）：

```bash
git switch -c rollback-0.4.2 34254d2
```

- 只想撤某个文件（例如驱动判据）：

```bash
git checkout 34254d2 -- muse_bridge/driver.py
```

- 回退**不会**影响运行数据：`.env`、`data/`、`*.db` 均被 `.gitignore` 排除，
  实测重启前后账号数、媒体文件数与字节、账号库大小均不变（§4.4）。
  回退后想自己复核一遍，直接跑 `python tools/lifecycle_check.py verify --chat`：
  它会在同一条命令里把"停得掉、起得来、数据不丢、还能聊天"重新验一次。
- relay 侧回退：`D:\deep seek\relay\Muse-Relay-V1.13.1.zip` 是当前发布包（113 项，
  解压后 346 passed / 7 skipped）；要退回旧版就恢复上一份解压目录（V1.12.8 目录
  里放的是 1.13.1 代码，不要再被目录名误导）。

## 8. 需要人拍板的开放问题

1. **打 tag / 发 release**：路线图规定"发布动作需用户指令"，本文不代为执行。
2. **relay 媒体透传复现**：需要一次真能产生附件的上游提示（或用上游已存在的
   附件回合），才能把"未触发"变成"通过"。
3. **参考素材两个上限统一**：`MUSE_REFERENCE_MAX`（静默截断）与
   `MUSE_MAX_REFERENCE_FILES`（报 400）语义冲突，统一成哪个是**对外契约变更**，
   需要拍板后再改。
4. **非 Chrome 客户端验收**：是否排进下一轮（需要真实 GUI/移动端环境）。
5. ~~**`websockets` 是否写进 `requirements.txt`**~~ **已结清**：核对源码后确认它是
   可选依赖（惰性导入 + 内置 `ws_fallback` 兜底），不写进 requirements 是对的；
   已在 §4.1 说明，并要求"用官方实现"的部署自行 `pip install websockets>=13`。
6. **`restart_bridge.py` 的默认是否对齐文档**：它打开协议驱动与暖池（§3.1），
   与代码默认不同。要么改工具、要么在发布说明里显著标注——本轮选择了后者。
