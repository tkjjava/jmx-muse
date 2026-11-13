# 取消 ACK 活网核验与默认 WebSocket 适配

## 本轮结果

2026-11-13 Asia/Shanghai，先完成一次仅连接、不发消息的 Noise 核验，再对自行发起
的一个测试回合发送显式取消。结果：收到正文后 POST /chat/cancel 返回布尔
cancelled=true，随后 read_stream 正常结束且 _turn_complete=true，总耗时 8.914 秒。
没有观察到 delta.message_done，因此不能宣称收到 interrupted，亦不能仅凭 ACK
保证所有后台工作已停止。线程取消与跨客户端作用范围未验证。

本轮仅修复默认 websockets 适配器的关键字映射和代理默认值；新增两个定向回归。
共享工作树的完整套件为 1779 passed，ruff 全绿。其中还包括同时进行的握手预算
修改，该部分不归属于本文的适配器修复。

## 活网证据

使用既有凭据（只读打开数据库，密钥取用户环境）、显式代理、无浏览器。日志仅
记录布尔值、错误类别和耗时，不输出 token URL、账号、正文或标识符。

仅连接核验结果：

```json
{"stage":"upgrade","ok":true,"endpoint_has_frontend_env_id":true,"endpoint_id_equals_session_vm_id":true,"vm_name_present":true,"vm_name_equals_endpoint_id":true,"explicit_proxy":true}
```

本地前端 3w55hkryr8wwj.js 的目标构造从 endpoint 域名提取环境 ID，并退回 vmName；
实际当前三者一致。生产仍使用已验收的 LB 目标，没有据此改路由、重置 VM 或换号。
这一成功连接不能定位此前 Upgrade 502 的生成层，只能证明故障不持续存在。

自己的测试回合结果：

```json
{"ok":true,"via":"direct_noise_protocol","cancel_sent_after_own_text":true,"cancelled_ack":true,"observed_done_statuses":[],"terminal_event":false,"recognized_terminal":true,"elapsed_s":8.914}
```

recognized_terminal 是既有客户端规则识别的终态，不代表观察到 message_done。
本轮没有保留状态事件明细，因此不把它进一步写成特定 idle/completed 事件。

## 默认适配器修复

本机实际依赖为 websockets 17.2；检查其 asyncio/client.py 构造签名及属性赋值可见：

- user_agent_header 才是 HTTP UA 参数；其他关键字被保存为底层连接关键字。
- proxy 默认 True，会发现系统/环境代理；显式 None 才关闭自动发现。

原先产品默认 opener 直接返回 websockets.connect，但上层注入契约用 user_agent。
设置自定义 UA 会把未知参数传到事件循环连接函数。未显式配置代理时，WS 还可能
偷偷采用系统代理，与 HTTP trust_env=False 的行为不一致。这不是本次显式代理下
502 的已证实根因，不能把两者混为一谈。

现在默认 opener 使用薄包装：把 user_agent 映射到 user_agent_header，缺省 proxy
设为 None；显式代理原样保留。自定义注入 opener 和内置 ws_fallback 的参数契约
保持原样。没有重新发送聊天动作或新增重试。

## 验证

仓库根目录：

```powershell
python -m pytest tests/test_protocol_ws_adapter.py -q -p no:cacheprovider
python -m pytest -q -p no:cacheprovider
python -m ruff check muse_bridge tests
```

两个定向测试分别检查 UA 映射及禁止隐式代理、显式代理和其他 Upgrade 参数保留。
采用严格关键字形参的依赖替身，不打开网络。完整共享工作树验证为 1779 passed。

生产服务未重启，配置和凭据未修改。取消 API 仍为显式调用，未绑定 close/aclose。
