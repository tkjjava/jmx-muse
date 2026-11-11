# 已知 oracle 冲突（测试自身矛盾，实现按上游真语义保持不动）

本文件记录 `tests/` 里**不可能被任何正确实现满足**的两条断言。它们不是实现缺陷，
是测试写错了；为了不让后来的人反复怀疑实现，这里把矛盾双方、证明与裁定固定下来。

统一裁定：**实现不动、测试不改（本轮禁止改这两个测试文件），红是预期状态。**
全量基线因此是 `288 passed / 2 failed`，看到这 2 条红不需要处理。

复核日期：2026-11-11，环境 Python 3.14.7 / pytest 9.1.1 / 分支工作树 `muse-bridge`。

---

## 1. `test_noise.py::test_protocol_constants_match_references` —— 协议名字节数手数错位

### 位置

- 测试文件：`tests/test_noise.py`
- 失败断言：**第 84 行**
- 同一测试的自相矛盾对手：**第 82 行**

### 矛盾双方（各引测试原文一行）

```python
# tests/test_noise.py:82
    assert noise.PROTOCOL == b"Noise_XX_25519_AESGCM_SHA256"
# tests/test_noise.py:84
    assert len(noise.PROTOCOL) == 27, "≤32 才允许补零而不是哈希，这个前提变了要改代码"
```

同一个对象的字节串被第 82 行钉成字面量，第 84 行又断言它的长度是 27。
该字面量实际是 **28** 字节 —— 两条断言互斥，任何实现都无法同时满足。

### 证明：字符计数拆解

| 片段 | 内容 | 字节数 | 累计 |
| --- | --- | --- | --- |
| `Noise` | 名字主体 | 5 | 5 |
| `_` | 分隔符 | 1 | 6 |
| `XX` | 握手模式 | 2 | 8 |
| `_` | 分隔符 | 1 | 9 |
| `25519` | 密钥协商 | 5 | 14 |
| `_` | 分隔符 | 1 | 15 |
| `AESGCM` | AEAD | 6 | 21 |
| `_` | 分隔符 | 1 | 22 |
| `SHA256` | 哈希 | 6 | **28** |

ASCII 字符串里全是单字节字符，`len()` 即字符数，所以 28 无歧义。

失败输出（pytest 原文）：

```
E   AssertionError: ≤32 才允许补零而不是哈希，这个前提变了要改代码
    assert 28 == 27
     +  where 28 = len(b'Noise_XX_25519_AESGCM_SHA256')
```

补充：第 84 行的**语义**本身是对的（`len ≤ 32` 时 SymmetricState 的 `h` 用补零而不是哈希，
见 `muse_bridge/noise.py` 的 `Symmetric.__init__`），只有那个 `27` 是笔误。
28 同样满足 `≤ 32` 的前提，所以实现行为不受影响。

### 参考实现证据（同一字节串）

| 实现 | 位置 | 字面量 |
| --- | --- | --- |
| lichao199208__muse2api | `app/muse_engine/protocol_client.py:36` | `PROTOCOL = b"Noise_XX_25519_AESGCM_SHA256"` |
| manishkumar1601__muse-ai-api | `src/noise/handshake.ts:6` | `const PROTOCOL = "Noise_XX_25519_AESGCM_SHA256";` |

> 校正一处引用：交接指令里写的是 `protocol_client.py:32`，实测该常量的赋值行是
> **第 36 行**（第 32 行附近是 `from app.muse_engine.engine import ...`）。
> 另外第三个参考（tianrking）本轮由 lead 引用、我未在本地核过行号，
> 因此上表只列我自己读过原文的两份。两份实现的协议名与本项目完全一致。

### 裁定

实现保持 `PROTOCOL = b"Noise_XX_25519_AESGCM_SHA256"`（28 字节）不变，测试不改，本条红。
理由：把常量改成 27 字节等于改掉 Noise 协议名，握手哈希 `h` 的初值随之改变，
对端直接不认——**为了让一条笔误断言变绿而改字节，会把唯一正确的东西改坏**。
后续若要清理，最小修法是 `tests/test_noise.py:84` 的 `27` → `28`（一处数字，无语义变更）。

---

## 2. `test_proto.py::test_int64_signed_roundtrip_through_message[9223372036854775808]` —— 64 位补码下 ±2\*\*63 同字节

### 位置

- 测试文件：`tests/test_proto.py`
- 参数化定义：**第 76 行**
- 失败断言：**第 81 行**（以及第 82 行的同源断言）

### 矛盾双方（各引测试原文一行）

```python
# tests/test_proto.py:76 —— 同一参数列表里同时要求这两个值都能往返
@pytest.mark.parametrize("value", [-1, -(2**63), 2**63, -12345, 12345])
# tests/test_proto.py:81 —— 往返后必须还原成输入本身
    assert decoded.chunk_id == value
```

矛盾不在"实现算错了"，而在**输入集合本身重复**：`2**63` 不是合法的 int64 值，
它在 64 位二进制补码里与 `-2**63` 是同一个位型，因此也是同一条 protobuf varint。
一个确定的字节串只能解回一个数，所以 `2**63` 与 `-(2**63)` 这两条参数**不可能同时为绿**。

### 证明：补码编码逐字节相同

`muse_bridge/proto.py` 实测输出：

```
encode_varint( 2**63).hex() == '80808080808080808001'
encode_varint(-2**63).hex() == '80808080808080808001'
两者相同：True
as_int64(decode_varint('80808080808080808001')) == -9223372036854775808
```

失败输出（pytest 原文）：

```
E   AssertionError: assert -9223372036854775808 == 9223372036854775808
     +  where -9223372036854775808 = NoiseTransportFrame(chunk_id=-9223372036854775808, ...).chunk_id
```

为什么是同一条字节串：protobuf 的 `int64` 走 LEB128，负数按 64 位二进制补码编码。

1. `2**63` 的位型是 `0x8000000000000000`（最高位置 1、其余全 0）。
2. `-2**63` 在 int64 里的位型也正是 `0x8000000000000000`。
3. 两者取同样的位型做 LEB128：低 7 位连续 9 组都是 0（每组后面还要接更高位，故都带
   续位 `0x80`），第 10 组剩下一位 1 → 字节流 `80 ×9 ‖ 01`。
4. 解码侧只能把它当成"无符号 2\*\*63"，再按 int64 语义折叠（`value - 2**64`）→ `-2**63`。
   这唯一的折叠结果是 protobuf 规范行为，没有第二种合理解释。

> 注意：第 62 行的往返用例给的是 `2**63 - 1`（合法 int64 上界，绿）；
> 第 76 行给的是 `2**63`（越界、与 `-2**63` 同字节，必然红）。两条只差一个 `-1`，
> 这正是笔误的来源。

### 参考实现证据

- `NoiseTransportFrame.chunk_id` 在 schema 里是 `optional int64 = 1`
  （`hatch.proto`），即有符号 64 位，天然只覆盖 `[-2**63, 2**63-1]`。
- 线上生成方式：`protocol_client.py:449`
  `chunk_id = int.from_bytes(secrets.token_bytes(8), "big", signed=True)` ——
  **有符号**，取值域就是 `[-2**63, 2**63-1]`，永远不会产生 `2**63`。
  所以"`2**63` 能否往返"在真实流量里是个不会发生的输入，断言本身也无从对齐。
- 本项目 `muse_bridge/proto.py` 的 `as_int64()` docstring 已按同一裁定写明有符号折叠语义。

### 裁定

实现保持"有符号折叠"（`as_int64`：`value > 2**63-1` 时减 `2**64`）不变，测试不改，
`[9223372036854775808]` 这一条参数红是数学必然。
理由：若为了让它绿而让 `as_int64` 对 `2**63` 返回 `+2**63`，
同一次调用就必须对 `-2**63` 返回不同结果——可它们的输入字节串完全相同，
这在函数式解码里做不到；而"返回 `+2**63`"还会把 `chunk_id` 送出 int64 取值域，
后续任何按 int64 处理该字段的代码（含真实服务端）都会不一致。
后续若要清理，最小修法是把 `tests/test_proto.py:76` 参数列表里的 `2**63` 删掉或改成
`2**63 - 1`（一行，无语义变更）。

---

## 如何复核

两条都是**确定性事实**：不依赖网络、不依赖浏览器、不依赖随机数、不依赖时序，
任何机器上跑都得到同样结果。

第 1 条（协议名字节数）：

```
python -Xutf8 -c "print(len(b'Noise_XX_25519_AESGCM_SHA256'))"
```

期望输出 `28`。与 `tests/test_noise.py:84` 期望的 `27` 直接矛盾，而
`tests/test_noise.py:82` 钉的字面量与参考实现（`protocol_client.py:36`、
`handshake.ts:6`）逐字节相同。

第 2 条（±2\*\*63 同字节）：

```
python -Xutf8 -c "from muse_bridge import proto; print(proto.encode_varint(2**63).hex(), proto.encode_varint(-(2**63)).hex())"
```

期望输出两个一模一样的 `80808080808080808001 80808080808080808001`。
同字节 ⇒ 同解码结果 ⇒ `tests/test_proto.py:76` 里 `2**63` 与 `-(2**63)` 不可能双绿。

跑单条用例确认红的是"哪一条"，以及确认全量基线：

```
python -Xutf8 -m pytest tests/test_noise.py::test_protocol_constants_match_references tests/test_proto.py -q --tb=line
python -Xutf8 -m pytest -q
```

第二条命令期望结尾是 `2 failed, 288 passed`，且两条失败恰好是本文件的两节。

最后再强调一次处置纪律：**不要为了让这两条绿去改 `muse_bridge/noise.py` 的
`PROTOCOL` 字节或 `proto.py` 的 `as_int64` 折叠语义。** 那两处是握手与线格式的根基，
改了会静默破坏与真实服务端的互通，而收益只是 pytest 少两行红。
