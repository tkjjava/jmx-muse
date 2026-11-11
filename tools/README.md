# tools/ — 运维与验收工具

这里的脚本解决两类问题：**把运行态说清楚**，和**证明它在真实链路上确实成立**。

## 为什么单独建这个目录

2026-11-11 那次排障留下的教训值得写下来：诊断脚本和产品走了**不同的契约**。
探针把 `proxy` 传成 requests 风格（`{"http":…, "https":…}`），而产品只读
`proxy["server"]`，于是代理被静默丢弃、连了直连；三条链路"一起失败"看起来像
上游全挂，实际是探针自己的 bug。后来改成 `{"server": …}` 并回读 opener 收到的
kwargs，才证明代理真的用上了。

从那以后，这里的每个脚本都遵守三条：

1. **与产品共用同一份 env**，不另造一套配置；
2. 每项检查打印**实际观测值**，不只打印通过/失败——出问题时不用重跑一遍；
3. 上游没给东西时说"未触发"，不冒充通过。

凭据一律从环境变量读。源码里不留密钥、不留本机绝对路径。

## 脚本

| 脚本 | 作用 | 会不会消耗上游额度 |
| --- | --- | --- |
| `bridge_ops.py` | 自检、健康、观测快照、读日志尾部 | 否 |
| `restart_bridge.py` | 杀掉占端口的旧进程、用当前环境起新的、等就绪 | 否 |
| `refresh_account.py` | 刷账号探针，让 `accounts_dispatchable` 回到 1 | 否 |
| `acceptance.py` | 成品链路、流式 SSE、清扫计数、（可选）协议生成 | `generation` 会 |
| `accept_video.py` | 视频端到端：任务轮询、`ftyp` 魔数、Range 分段、时长 | 会 |
| `accept_relay_media.py` | 经 relay 验媒体字段透传 + **按客户端方式取件** | 会 |
| `diff_check.py` | 差分检查：某个测试在修复前必须失败、修复后必须通过 | 否 |
| `verify_tests_load_bearing.py` | 抽查测试是否真的钉住行为，而不是恒真 | 否 |
| `lifecycle_check.py` | 启停恢复校验：快照 → 重启 → 逐项对比（`--chat` 多验一次聊天） | 否 |
| `doc_assertions.py` | 文档断言审计：行号引用是否失效、含糊时刻计数 | 否 |
| `peek_muse.py` | 只读取证 muse 页面 DOM（排障用，**不点击不输入**） | 否 |

### 三个"把结论变成可复查命令"的工具

发布候选评估里最容易被口头糊弄的三件事，各配了一支工具：

- `lifecycle_check.py`：**"停得掉、起得来、数据不丢"**。它先快照（端口/pid/
  `/healthz`/账号总数/成品文件数与字节/账号库大小），再委托 `restart_bridge.py`
  重启，最后逐项对比；账号变少、成品变少、账号库明显变小都会报 ✗ 并以非零码
  退出。`snapshot` 子命令只打快照不重启，`--chat` 额外验证"重启后真的能聊天"。
  ```bash
  python tools/lifecycle_check.py            # 快照 -> 重启 -> 对比
  python tools/lifecycle_check.py snapshot   # 只看当前状态
  python tools/lifecycle_check.py --chat     # 多验一次聊天
  ```
- `doc_assertions.py`：**"文档里的行号引用还能不能解析"**。只判定面向本仓库的
  文档（CHANGELOG/README/tools/README/USAGE/LIVE_RUNBOOK/roadmap/候选报告）；
  逆向记录里指向上游产物的引用只统计。解析不到、越界、指向空行，三种都算失效。
  ```bash
  python tools/doc_assertions.py
  python tools/doc_assertions.py --json    # 便于接 CI
  ```
- `peek_muse.py`：**"页面此刻到底有什么"**。只读：不点击、不输入、不刷新。
  打印标签页、输入框数量与残留内容、最后一条助手正文与 `data-message-id`。
  bridge 报"没有任何反应"时用它区分三种情况：输入框有残留（没发出去）、
  最后 id 没变（真没反应）、id 变了但 bridge 说没反应（判据问题）。
  ```bash
  python tools/peek_muse.py               # 驱动会选中的那个 muse 标签
  python tools/peek_muse.py --all-tabs    # 全部标签
  python tools/peek_muse.py --screenshot shot.png
  ```

### 两个"必须有真实上游产出"的验收

`accept_video.py` 和 `accept_relay_media.py` 都要等上游真出东西，会消耗额度，
不能当成 CI 用例。它们解决的是别的测试覆盖不到的那一段：

- `accept_video.py`：生成一个视频任务，轮询到 `completed`，再用**魔数**（不是
  `Content-Type`，那个固定是 `octet-stream`）确认拿到的是 MP4，并检查 Range 分段
  返回的字节能接上。
- `accept_relay_media.py`：把聊天请求发到 **relay**，比对非流式 `message.media` 与
  流式 `delta.media` 的字段集，再**按客户端的方式**（以 relay 的 origin 解析相对
  `url`）去下载、核对 `sha256`。最后这条是整个脚本的重点：字段存在不等于能用，
  2026-11-11 就出现过"字段齐全、链接 404"（relay 缺 `/v1/media` 路由）。

```bash
# 视频端到端（会消耗额度）
python tools/accept_video.py

# 经 relay 的媒体验收（先起 relay，并把客户端 key 放进环境）
MUSE_TOOLS_API_KEY=<relay 客户端 key> \
python tools/accept_relay_media.py --relay http://127.0.0.1:8765 --model muse-1
```

`accept_relay_media.py` 需要注意一点：**不要断言两个请求的 sha256 相同**。非流式
和流式是两次独立生成，出来的图本来就不一样；要比的是同一次请求里"声明值 vs 实测值"。


## 常用命令

```bash
# 跑验收前先自检：把"环境没配好"和"功能有 bug"分开
python tools/bridge_ops.py preflight

# 完整重启（需要 MUSE_BRIDGE_KEY 在环境里）
python tools/restart_bridge.py --log data/bridge.log

# 只等就绪（进程由别的东西托管时用）
python tools/restart_bridge.py --wait-only

# 账号探针过期、accounts_dispatchable 掉到 0 时刷一下
python tools/refresh_account.py muse1

# 跑不消耗额度的三项验收
python tools/acceptance.py

# 单项
python tools/acceptance.py media
python tools/acceptance.py stream
python tools/acceptance.py sweep

# 协议生成真机验收（会真的要图，消耗额度）
python tools/acceptance.py generation

# 看观测快照与日志尾部
python tools/bridge_ops.py observability
python tools/bridge_ops.py log 60
```

## 环境变量

工具用 `MUSE_TOOLS_` 前缀，与产品的 `MUSE_*` 分开，避免互相误覆盖。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MUSE_BRIDGE_KEY` | 无（必填） | 账号 cookie 的 Fernet 密钥。缺了就报错，不给默认值 |
| `MUSE_TOOLS_PORT` | `8788` | bridge 端口 |
| `MUSE_TOOLS_BASE` | `http://127.0.0.1:<port>` | 覆盖基址（反代/远端） |
| `MUSE_TOOLS_API_KEY` | 空 | 客户端 Bearer key；空则匿名 |
| `MUSE_TOOLS_DB` | `<repo>/data/muse.db` | 账号库 |
| `MUSE_TOOLS_MEDIA_DIR` | `<repo>/data/media` | 成品目录 |
| `MUSE_TOOLS_LOG` | `<repo>/data/bridge.log` | 运行日志 |
| `MUSE_TOOLS_CDP_URL` | `http://127.0.0.1:9222` | Chrome 调试端口 |
| `MUSE_PROTOCOL_PROXY` | 空 | 协议链路代理（`restart_bridge.py` 也读它） |

产品侧配置见 `.env.example`。**本目录的脚本不读 `.env` 文件**，与产品保持一致：
由调用方先把变量放进环境，或写进你自己的启动脚本。

### 关于代理

`MUSE_PROTOCOL_PROXY` 决定协议链路能不能出去。填**本机当前真正可用的那个**，
不要照抄端口号。填一个不通的代理，症状是 `/api/session` 超时或
`Noise WebSocket 连接失败`，而这两个错误看起来都像"上游挂了"——
先用 `python tools/acceptance.py stream` 单独验一次聊天，再判断哪一层坏了。

## 一个容易踩的坑

`MediaStore` 的默认根是 `./data/media`，**按当前工作目录解析**。从仓库外运行
脚本会让它指向别处，甚至在错误位置 `mkdir` 出一个空目录，表现是"成品凭空消失"。
所以这里的脚本都用 `MUSE_TOOLS_MEDIA_DIR` 或仓库根推导出绝对路径。

## 验收记录

每次活网验收的结论写进 `docs/2026-11-11-roadmap.md` 对应条目，不写在这里——
这个文件只讲工具怎么用，不讲某次跑出来什么。
