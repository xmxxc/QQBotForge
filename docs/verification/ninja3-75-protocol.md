# #75 协议层离线预实施证据

2026-10-23，作者自查。非完整交付，前置 #72 待通过。未推送、未开 PR、未部署，未运行真实 `--check`、登录或改密。

## 固定范围

- personal 上下文：`1e2e66e0136b9166e614ab48f961b187520cbd82`。
- fresh fetch 后从 #72 head `bcf2657cb2a1afe925eabfd232a52a7fb8656ce1` 建立独立工作树 `/opt/qqbot/ninja3-75-protocol`，分支 `feat/75-password-protocol-offline`；开工该工作树干净。
- 主工作树的既存改动保留；未修改 main.py、service.py、page.html、共享存储、群状态和授权接口，未跟随后续前置移动。
- 新文件：协议模块、tests/password_protocol 下3个测试文件、#70 原件2个及来源说明、协议接线说明、本证据页。
- #70 原件哈希和 LF 说明见 [来源记录](../vendor/issue70/README.md)；测试直接核对两个完整 SHA256，并原样执行原十项。

## 验证

隔离工具环境 `/tmp/qqbot-75-venv`，用项目 `.[dev]` 与仅供原件测试的 requests 安装，没有修改生产环境或依赖声明。Python 3.12；httpx／cryptography 复用项目依赖约定。

```bash
python -m pytest -q tests/password_protocol
# 122 passed（包含原十项和哈希检查）
python -m pytest -q tests
# 最终完整回归结果见交付记录（初轮539项通过，新增6项清理回归）
ruff check plugins/ninja3/password_protocol.py tests/password_protocol
ruff format --check plugins/ninja3/password_protocol.py tests/password_protocol
mypy --strict --follow-imports=silent plugins/ninja3/password_protocol.py
# 均通过；显式指定插件文件，避免 mypy 的默认 plugins 排除规则漏检
```

初轮完整回归539项通过。作者自查发现会话清理异常会覆盖已解析的明确成功／拒绝，修复后新增6项回归（成功／拒绝 × 清理失败／取消／超时），旧快照6项均复现错误分流，最终122项协议测试全通过。原件有意不格式化，保持来源字节。没有引入新测试框架。

离线协议测试的自动 fixture 阻断 socket connect/connect_ex、create_connection、getaddrinfo，主动连接控制验证确实会抛错。HTTP 全部经 httpx.MockTransport／原测试 Mock；未配置真实账号并非断网依据。其余现有回归使用原有临时 host 和回环假平台，不全局阻断它们所需的本地连接。

正常控制验证：env.js→登录→userinfo→改密完整调用顺序，新 Cookie jar 会传递登录 Cookie；原件函数独立计算签名和密文，逐个与实际发出请求比对；动态密钥／appId 生效，改密仅一次。反向用例覆盖：userinfo 掩码缺失、前后缀或格式错误、类型错误、verify 身份失败；各阶段301/302/307/308；明确拒绝、非200、无效JSON／结构／业务码、验证码；各阶段超时、传输失败、取消；真实 asyncio.Task.cancel 在提交前后；持久化回调失败与待交付取消；非法输入；密码、Cookie、令牌、动态密钥与原始响应在异常出口、repr、普通及DEBUG日志、stdout/stderr均不泄露（仅使用假数据）。

## 边界与后续

信任边界是任务层提供的授权目标／凭据、公开配置、登录会话及改密响应。固定源站和重定向拒绝防止凭据改投；严格身份核对防止改错目标；敏感输入不出现在 repr 或返回枚举；提交后异常保持未知。测试 transport／时间／随机函数属于可信注入端。

userinfo 契约按真实脱敏结构记录为 `data.phone = "+86 " + 原手机号前三位 + "****" + 末四位`；`data.area` 不参与检查。协议与 verify 共用同一 phone 检查并对不匹配结果 fail-closed。

没有生产密码存储或任务引擎；成功结果只指远端报告，本地保存另算。取消不能撤回远端请求，进程强杀依靠下一阶段持久意图恢复。接线与暂停规则见 [接口说明](../ninja3-password-protocol.md)。下一阶段由总控在 #72 通过后安排：受保护持久意图、账户暂停、同账户串行、普通确认、重启恢复、本地提交、主人私聊／后台回执；#76 才处理未知结果登录核验。
