# 三模块上游复用核对

核对日期：2026-10-20。源码仅下载到临时目录阅读，没有执行上游插件或读取运行数据。

| 来源及固定提交 | 已核对内容 | 适用行为／接口 | 本轮选择 |
| --- | --- | --- | --- |
| [插件市场](https://github.com/ElainaCore/Elaina-plugins/tree/3944ef7b6d80e0a859b0e65c2105a102104244d8) | `plugins.json`、README | 目录安装、模块／插件入口及版本声明；关键词回复 4.0.1、群管 2.0.9 的实际来源 | 用于定位源码与分发约定，市场描述不作为验收证据 |
| [关键词回复](https://github.com/miaolik/plugin-gjc/blob/704f00b535b7149ff58b0e491b8998bef3ab5f2c/关键词回复.py) | 元数据、JSON 持久化、规则校验、匹配、拦截器、管理与回复入口 | 全局／分群开关、精确／包含／正则、组合回复、`register_route`／`register_page` 和发送接口 | 参考产品行为，复用本仓库已有框架接口；不直接依赖整插件 |
| [群管](https://github.com/lengxi-root/elaina-plugins-lengxi/tree/3fd62064406eb204dd4c6424a7b19b9da3ebcd08/groupguard) | 入口、`storage/core.py`、`handlers/events.py`、`services/permissions.py` | SQLite WAL、管理路由、Bot 在群状态、角色读取和错误处理 | 参考能力查询与页面接线；不把活跃成员缓存视为完整名册 |

这两个插件面向 ElainaBot v2，使用本仓库已有的 `core.plugin.*`、`core.message.*` 接口；它们是插件而非本规划所需的独立群状态、授权、问答模块。关键词插件存在全局超级管理员／全局数据、同步正则与额外推送／群管行为；群管的存储和业务动作耦合，角色缓存没有完整名册及连续性证明。直接安装不能满足按 Bot／消费方隔离、公开问答独立安装和先鉴权后匹配的要求。

两个源码入口均声明 `license: MIT`，所固定仓库未发现独立 LICENSE 文件；本轮不复制其源码。以后如提取代码，须保留可核验的作者、版权及许可文本；元数据声明不替代完整的许可条件核对。市场仅引用链接和元数据。

B01 复用本仓库的 `ExtensionStore` 和标准库 SQLite。独立分发的每个目录自带同一小型实现，使用相对导入；不引入新的必装业务模块或框架接口。现有三份数据库的路径、`extension_records` 主键、JSON 记录与 `user_version=1` 均保持兼容。后续群状态可采用相同分发方式。
