"""让 astrbot_plugin_outputpro 的分段步骤支持 QQ 官方平台。

`SplitStep.support_platforms = {"aiocqhttp", "telegram", "lark"}`，QQ 官方的回复
因此在 `handle()` 里被直接跳过，整轮回复只会发一条。这里只做一件事：把这个
类属性就地并入 `{"qq_official", "qq_official_webhook"}` —— 切分规则、分段数、
打字延迟、尾段回填等全部沿用 outputpro 原实现，不做任何改写。

为什么不直接改 outputpro：它是第三方仓库（目录内还有自指符号链接），升级/覆盖
会把改动冲掉。运行时改类属性则在每次重新加载后都能重新生效：AstrBot 热重载会
删除旧模块并重建类对象，本模块用「类对象上的集合是否已包含两个平台名」做幂等
标记，因此重载后再次调用会自动补上。

所有失败路径都只返回 False/记 debug 日志，绝不影响 QQ 官方回复本身。
"""

from __future__ import annotations

import importlib
import sys

from astrbot.api import logger

#: SplitStep 目前只放行这三个平台；QQ 官方被漏掉是「整轮只发一条」的根因。
ADDED_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})

DISPLAY_NAME = "糯小喵"

#: AstrBot 运行时模块名（star_manager 以 "data.plugins." 前缀加载插件）；
#: 测试里则按父目录 import，两种命名都试。
MODULE_CANDIDATES = (
    "data.plugins.astrbot_plugin_outputpro.core.step.split",
    "astrbot_plugin_outputpro.core.step.split",
)


def _resolve_split_module():
    """取已加载的 outputpro split 模块；未加载时尝试按候选名 import。"""
    for name in MODULE_CANDIDATES:
        module = sys.modules.get(name)
        if module is not None:
            return module
    for name in MODULE_CANDIDATES:
        try:
            return importlib.import_module(name)
        except Exception:  # noqa: BLE001 - 未安装/包名变化都按「没有 outputpro」处理
            continue
    return None


def enable_qq_official() -> bool:
    """幂等地把 QQ 官方平台并入 outputpro SplitStep 的允许平台。

    返回 True 表示该集合当前已允许 QQ 官方（本次或此前已生效）；outputpro
    未安装、结构变化、类属性不可写时返回 False，绝不抛异常。
    """
    module = _resolve_split_module()
    if module is None:
        return False
    step_cls = getattr(module, "SplitStep", None)
    if step_cls is None:
        logger.debug("[%s] outputpro SplitStep 缺失，跳过 QQ 官方分段支持", DISPLAY_NAME)
        return False
    current = getattr(step_cls, "support_platforms", None)
    if not isinstance(current, (set, frozenset)):
        logger.debug(
            "[%s] outputpro SplitStep.support_platforms 类型异常(%s)，跳过 QQ 官方分段支持",
            DISPLAY_NAME,
            type(current).__name__,
        )
        return False
    if ADDED_PLATFORMS <= set(current):
        return True
    try:
        step_cls.support_platforms = set(current) | set(ADDED_PLATFORMS)
    except Exception as exc:  # noqa: BLE001 - 只读属性/元类限制都不能影响回复
        logger.debug("[%s] 打开 QQ 官方分段支持失败: %s", DISPLAY_NAME, exc)
        return False
    logger.info("[%s] outputpro 分段：已为 QQ 官方启用平台支持", DISPLAY_NAME)
    return True
