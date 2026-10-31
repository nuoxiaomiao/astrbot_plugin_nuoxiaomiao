# 许可证

本插件以 GNU Affero General Public License v3.0 发布，全文见 [LICENSE](LICENSE)。抽签算法与拟人回复拆条思路参考自 IRMIA（AGPL-3.0-only），与上游同协议发布。

# IRMIA 功能参考

本次整合参考 [I.R.M.I.A](https://github.com/irmia2026/I.R.M.I.A) 的 `52d3e57` 版本，本地参考目录为 `/tmp/irmia`。上游许可证为 [GNU AGPL v3](https://github.com/irmia2026/I.R.M.I.A/blob/52d3e57/LICENSE)（AGPL-3.0-only）。

本插件的 `resident/` 和管理页面围绕 AstrBot 生命周期、工具集、事件钩子及原有 SQLite 记录库独立实现，没有复制 IRMIA 的源代码或第三方技能包。上游作者对上游代码的权利和许可不受影响；今后若引入其源代码，须保留来源、版权声明并遵守相应 AGPL 义务，不能以本说明代替许可证。

没有移植 IRMIA 的独立 Node 后端、Flutter 桌面端、独立登录、模型客户端、平台连接或自更新系统。LivingMemory、夜璃关系本、AstrHe、AstrNa、OutputPro 的业务能力通过宿主与公开接口复用，依赖插件的代码与数据库不打包进本插件。

# astrmia 行为参考（独立重实现）

2.3.0 的「共享记忆与信箱」分组（全局主脑共享记忆、未读信箱分流、点名通知卡、话题概括、群聊识图）按**行为参考**方式借鉴 [astrmia](https://github.com/Kalospacer/astrbot_plugin_astrmia) 插件的功能设计。本地参考目录为 `/tmp/astrmia`（服务器侧克隆，仅用于查阅公开行为）。

该仓库没有 LICENSE 文件，GitHub API 报告 `license: None`，按默认法律推定保留所有权利。因此：

- 本插件**没有复制、改写或翻译 astrmia 的任何源码**，包括其事件处理、会话管理、提示词、SQL 与前端代码；
- 仅依据其公开文档、README 和可观察行为重新设计实现：例如「主脑会话重绑 + 串行持锁」「未读信箱分流」「话题概括」「识图留底与 read_image 工具」等能力的**思路与用户可见行为**，而非其代码表达；
- 本插件的新实现以 AGPL-3.0 发布，与本插件其余部分同协议。

若 astrmia 日后提供明确的开源许可，本插件也只按上述独立实现继续发布；引入其任何源代码都须另行评估并保留来源、版权声明与相应许可证义务，不能以本说明代替。
