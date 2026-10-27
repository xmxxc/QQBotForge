# #74 菜单、分页和文字选择：作者本地预实施记录

前置 #72 待通过，本地预实施，未独立批准。固定基点 `bcf2657cb2a1afe925eabfd232a52a7fb8656ce1`，基点 tree `9e55adb3e5c7b60dfb7ae81f8d7c522706d113cf`；personal `1e2e66e0136b9166e614ab48f961b187520cbd82`。独立工作树 `/opt/qqbot/ninja3-74`，分支 `feat/ninja3-74-local`。开始 fresh fetch，#74 无领取评论／实施 PR／对应远程分支。最终 head、tree、提交清单与报告绑定在工作树外 `/opt/qqbot/ninja3-74-evidence/DELIVERY.md`，避免提交自引用。

## 范围与结果

新增菜单／按钮／文字选择，沿用 #72 群公开查询的明确个人授权契约，不需要完整名册或私聊绑定；不改旧账户插件、self_qa、后台页面或 #72 F1。列表逐项同时检查插件开放范围和 ACL 资源查看决定，修复与本票直接重叠的 #85 F5 名称过滤；未扩大到其他 P3。

菜单只保留当前实例内存中的 Bot、群、动作引用和 300 秒期限，执行只使用实际调用者身份。`Ninja3.current()` 作为目录、取密和降级的最终同步检查入口；#75 暂停和 #78 持久化紧急状态未实现，后续须在这里接入，并应用于目录过滤。管理按钮只提供真实尚未交付提示。

## 验证

均使用假身份、假账密、临时 SQLite、可控时间和假平台传输。测试走真实 Application、PluginManager、ModuleManager、消息解析／分发、原生按钮事件与鉴权 HTTP；生命周期沿用真实 WSClient 假网关。另有真实 MessageSender，仅替换平台发送边界，检查实际 keyboard 权限、平台载荷、返回字典和纯文字降级类型。

固定资产 `04d901726cc6d94bae1b2b5524521f997a326e10` 以 `git archive <SHA> tests | tar -x -C <运行目录>` 正确平铺为 `tests/`。未制造 `tests/tests`。资产未纳入业务提交。

| 检查 | 结果 |
| --- | --- |
| 首批三项用例在基点运行 | 3 failed：菜单、分页／文字选择、ACL 停用名称 |
| 新菜单专项 | 49 passed |
| #72 原有 + #74 专项，日志分类修正后 | 70 passed |
| 平铺资产全量 `python -m pytest -q tests` | 913 passed / 0 skipped，72.53 秒 |
| 页面 `node --test tests/bot_modules/test_account_page.cjs tests/bot_modules/test_group_state_page.cjs` | 15 passed / 0 skipped |
| ninja3 页面内脚本 `node --check -` | 通过；本票页面无改动 |
| Ruff check / format --check，插件及两份 ninja3 测试 | 通过 |
| mypy 显式 main.py / service.py / storage.py，`--follow-imports=silent` | 3 files，通过 |
| mypy service.py / storage.py，追加 `--check-untyped-defs` | 2 files，通过 |
| main.py 同时开启函数体检查 | 两处基线已有 `context.ctx` 空值告警（33、38 行），本票未增加 |

全量运行绑定初次作者审查快照 `author-review-files.sha256`；之后仅修正引用查询的日志／通知分类，并添加既有用例中的日志断言，相关 70 项重跑通过。完整输出保存在工作树外的证据目录。

专项覆盖：三个菜单／目录入口、真实非 @ 接收开关、self_qa 的菜单与普通聊天回归、旧账户命令及数据回归、双重准入和资源级授权过滤、六项与双向翻页、实际点击者／发送编号者不同、生成者撤权后其他已授权者使用、当前名称和密码重读、精确 300 秒、插件重载、两个 Bot／群和跨域、停用／删除／改范围、在途改内容／撤权／拒绝／离群周期／黑名单／资源停用／授权到期／菜单到期／重载／Bot 绑定变化、回调场景／应用／chat_type／频道／黑名单校验。

发送覆盖：明确拒绝才使用同引用的纯文字编号；未知（-1、502、None、非契约布尔、异常）不重发菜单／凭据；拒绝等待期间账号、ACL、期限变化不发旧目录；Markdown 关闭直接文字选择。真实 MessageSender 用例确认群共享按钮的 permission 为所有人，但服务端继续按实际点击者鉴权。

隔离副本中的变异验证（测试退出码均为 1，原工作树未改）：

- 最终门直接返回 True：12 个在途选择参数全部失败。
- 去掉列表资源 ACL 过滤：停用名称过滤用例失败。
- 恢复旧日志分类：共享点击取密的日志断言失败；修正后通过。

## 作者两轴自查

同一固定快照并行执行 Standards／Spec，只是作者自查，不是独立审计。

- Standards：文档标准硬违规 0；确认一项 P3：按资源引用取密被记作“账号列表”。修正查询日志与未授权通知的动作分类，补日志断言；隔离旧逻辑变异失败，新代码相关测试通过。ACL 内部状态耦合、菜单／范围读取重复为判断项，保留现有模块接口，不扩展框架契约。
- Spec：0 项发现；未发现必修缺漏或范围蔓延。
- 修正后重新捕获最终快照并请两轴核对增量，结果见交付文件。

## 剩余前置、冲突与限制

- #72 F1 表单仍待原作者修复和独立复核，本票未触碰 `page.html`。正式基线就绪后由总控安排同步、推送与受影响组合回归；本票未推送、未创建 PR、未合并、未标 ready、未部署。
- 可能的集成冲突集中在 `plugins/ninja3/main.py`、`service.py`、`tests/bot_modules/test_ninja3.py` 和 `docs/ninja3.md`；F1 的页面文件本票无改动。
- 没有浏览器视觉核查（本票无页面变更），没有真实 QQ／游戏平台／Bot 订阅许可核查，没有运行 #70 `--check`，没有读取生产配置、数据或凭据。
- 网络发送与撤权不是原子事务；插件保证最终同步门后立即进入发送调用，底层网络既有同载荷重试行为未改变。
- 继承 fixture 假日志服务缺少方法产生的后台 AttributeError 仍可出现，不影响确切消息断言；没有以此声称修复框架日志。

## 审查 F1/F2 修复（2026-10-24，作者自查，待原 #74 审查者复核）

来源：独立审查 https://github.com/xmxxc/QQBotForge/issues/74#issuecomment-6368689971 （F1 P1 真实发送准备之后缺少最终复查；F2 P2 目录解码完整凭据）。审查原探针 `tests/bot_modules/test_independent74.py` 未改动，只在运行副本中加入执行。

- **分层**：基础候选 `fix/72-send-boundary-base`（父 #72 `129df14`）闭环旧文字查询；本分支 merge 该候选（同时带入 #72 已独立复核的页面 F1 修复），再把菜单、目录分页、共享选择、明确拒绝后的纯文字降级与管理提示接入同一门。
- **F1**：`offer()` 的按钮发送与降级发送、选择后的凭据发送、管理动作提示都经 `reply(..., gate=...)`；`current()`／`menu_current()` 在插件最后一次 await 后检查一次，并作为本实例最后一个 `before_send` 回调在框架准备之后、首次 `post_json` 之前再检查一次。变化时拦截并改发“账号或授权刚刚变化”；只有明确拒绝才降级，降级同样两次检查；未知结果不重发。
- **F2**：目录（含分页）只读 SQLite JSON 投影 `id/name/enabled/open_groups`；按引用或名称选中后，查看决定通过才读取该账号完整记录。
- **原测试适配**：`test_list_final_gate_after_titles_loaded` 原来通过包装 `store.all` 注入变化，目录现改读 `store.project`，只把包装点移到 `project`，断言不变。
- **红绿**（真实 MessageSender／HookManager，仅替换平台 `post_json`；另一个 `before_send` 准备等待期间注入变化）：
  - 审查原探针：`0ae3b29` 4 failed／3 passed（3 项时效 + 目录读取红）；本分支 7 passed。
  - 新增 `test_ninja3_send_gate.py` + `test_ninja3_menus_send_gate.py` 放到 `0ae3b29`：55 failed／6 passed，通过的 6 项恰为无变化对照；本分支全部通过。覆盖文字查询、目录、按钮选择、文字编号选择、翻页、明确拒绝后降级、主人管理提示；变化为改密、停用、关群、授权管理收窄、撤权、拒绝、离群、授权模块重载、插件重载（旧实例）、菜单到期。
  - 变异：门回调直接放行 → 56 failed；“门须为最后一项”检查恒真 → 门顺序用例失败。
- **框架缺口（未修复）**：`_request` 在管道之后、首个请求之前仍可能 await 令牌刷新；反例与最小接口建议见 `docs/verification/ninja3-72.md` 同名小节与交付证据。#75 暂停、#78 紧急状态接入 `current()` 时必须保持同步。
