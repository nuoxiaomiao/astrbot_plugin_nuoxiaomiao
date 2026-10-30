# IRMIA 功能参考

本次整合参考 [I.R.M.I.A](https://github.com/irmia2026/I.R.M.I.A) 的 `52d3e57` 版本，本地参考目录为 `/tmp/irmia`。上游许可证为 [GNU AGPL v3](https://github.com/irmia2026/I.R.M.I.A/blob/52d3e57/LICENSE)（AGPL-3.0-only）。

本插件的 `resident/` 和管理页面围绕 AstrBot 生命周期、工具集、事件钩子及原有 SQLite 记录库独立实现，没有复制 IRMIA 的源代码或第三方技能包。上游作者对上游代码的权利和许可不受影响；今后若引入其源代码，须保留来源、版权声明并遵守相应 AGPL 义务，不能以本说明代替许可证。

没有移植 IRMIA 的独立 Node 后端、Flutter 桌面端、独立登录、模型客户端、平台连接或自更新系统。LivingMemory、夜璃关系本、AstrHe、MiaoFlow、AstrNa、OutputPro 的业务能力通过宿主与公开接口复用，依赖插件的代码与数据库不打包进本插件。
