# #70 原始协议素材

来源：[xmxxc/QQBotForge #70 正文第 11、12 节](https://github.com/xmxxc/QQBotForge/issues/70)，2026-10-23 读取。两个 Python 文件按代码块原样还原，UTF-8、LF、末尾一个换行；不得运行格式化工具改写原件。CRLF 转换会改变哈希。

| 文件 | SHA256 |
| --- | --- |
| change_password.py | `f356b4205e7f4e31753050d63caffda3c3a6ebf9c3c3a6ba63648260f3d78001` |
| test_change_password.py | `9753e10b95d74d7f61eccdbc2d1cd1dbc2e92b7c160cce904496c4dd2fb9b2a8` |

原开发目录没有 Git 提交。#70 提及的仓库 main SHA 只是上下文，不能代表脚本来源或包含关系。原测试中的密码、密钥均为人工假数据。用户已确认脚本真实改密成功；账号渠道和旧游戏会话下线策略仍未知。

这里仅保存来源与离线对照，不供应用导入。原 CLI 和 `--check` 都会真实登录，不能当作离线验证命令。应用适配实现见 `plugins/ninja3/password_protocol.py`，复用项目已有 httpx 和 cryptography；原件测试另外需要 requests（仅测试工具，不新增生产依赖）。

离线执行命令（从仓库根目录）：

```bash
python -m pytest -q tests/password_protocol
```

测试 harness 原样加载并执行原十项 unittest，同时核对文件哈希。自动 fixture 禁止 socket connect/connect_ex、create_connection 和 DNS getaddrinfo，且以主动连接尝试确认阻断生效。不要直接执行这里的 CLI 来验证部署。
