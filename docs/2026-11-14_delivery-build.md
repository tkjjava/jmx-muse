# 交付包重建记录（2026-11-14）

## 产物

| 文件 | 大小 | SHA-256 |
|---|---|---|
| `D:\deep seek\Muse-Stack-V1.13.4.zip` | 1,914,717 B / 302 项 | `5f7f9ed9fe6f243f08c4999eac73dae0028dd60b53b4feacb4ab5902785eb03c` |
| `D:\deep seek\relay\Muse-Relay-V1.13.4.zip` | 783,212 B / 119 项 | `14758d58a45939308d1dd5864ecaf8c165046c276735b0d358305830ab430b3a` |

旧包（2026-11-12 16:0x）备份在 `D:\deep seek\tmp\packages-prev-20261112\`：

- 旧 Stack：`6e5a861f0a563fd2…`（268 项，1,799,677 B）
- 旧 Relay：`14758d58…`（与本次相同——relay 源码本轮未改）

## 包内容

- `muse-bridge/`：HEAD `0b59953`、版本 0.4.2，**181 个文件**（旧包 147）。
  新增内容包括：断线恢复（`chat.history` 严格前缀校验、默认开启且可关闭）、
  恢复计数、`tools/protocol_cancel_timing.py`、`tools/protocol_resume_probe.py`、
  `tools/bridge_disconnect_probe.py`，以及本期报告
  （`docs/2026-11-14_history-recovery-probe.md`、`…_resume-continuity-report.md`、
  `…_stack-acceptance-report.md` 等）。
- `Muse-Relay-V1.13.4/`：119 个文件，与旧包逐字节一致（relay 源码未变，
  `VERSION=1.13.4`，单路由 `muse-bridge / muse-1`）。
- 根目录：`部署说明.md`（首次部署六步、断线恢复运维要点、取消语义、验收记录）、
  `版本信息.txt`（HEAD、测试基线、实连验收数字、账务口径）。

## 构建方式

沿用 `tmp/rebuild_packages.py`（不入库）：

1. 源目录 = `relay/Muse-Relay-V1.12.8`（内含 1.13.4 代码）+ `muse-bridge`；
2. 过滤 `.git/.data/data/__pycache__/*.pyc/*.bak/*.log` 等；
3. 关闭 zip 后 `testzip()` 自检 + 条目过滤断言；
4. 解压到临时目录复核：relay 包内 22 条对齐/流程测试、`VERSION`/路由、桥的
   `tools/protocol_flow.py --help`。

## 本次复核

- 打包时 bridge 工作区干净（`git status` 空），包内代码 = HEAD `0b59953`；
- 打包脚本：完整性 302 项通过；relay 包内测试 **22 passed**；VERSION 1.13.4 /
  单路由正确；桥 CLI 帮助可用；
- **解压后的总包内** bridge 全量测试：**1930 passed**（39.45s）；
- relay 全量测试在源目录重跑：**371 passed / 7 skipped**（2026-11-14）。

## 未包含 / 不声称

- 不含账号库、cookie、媒体成品、任何密钥（均由部署侧环境变量提供）；
- 未打 tag / 发 release（需明确指令）；
- 包内实例的"独立端口起服务并聊天"本轮未重做（2026-11-12 做过一次）；本轮用
  隔离 bridge 实测的断线恢复证据是独立工具，不是本包实例。