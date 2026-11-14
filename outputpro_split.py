"""让 astrbot_plugin_outputpro 的分段步骤支持 QQ 官方平台。"""

from __future__ import annotations

import importlib
import sys

from astrbot.api import logger

if __package__:
    from .qq_official import QQ_OFFICIAL_PLATFORM_NAMES
else:
    from qq_official import QQ_OFFICIAL_PLATFORM_NAMES

ADDED_PLATFORMS = QQ_OFFICIAL_PLATFORM_NAMES
DISPLAY_NAME = "糯小喵"

MODULE_CANDIDATES = (
    "data.plugins.astrbot_plugin_outputpro.core.step.split",
    "astrbot_plugin_outputpro.core.step.split",
)

_PATCHED_ADDED: dict[int, set[str]] = {}


def _resolve_split_module():
    for name in MODULE_CANDIDATES:
        module = sys.modules.get(name)
        if module is not None:
            return module
    for name in MODULE_CANDIDATES:
        try:
            return importlib.import_module(name)
        except Exception:  # noqa: BLE001
            continue
    return None


def enable_qq_official() -> bool:
    """幂等地把 QQ 官方平台并入 outputpro SplitStep 的允许平台。"""
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
    current_set = set(current)
    missing = set(ADDED_PLATFORMS) - current_set
    if not missing:
        return True
    try:
        step_cls.support_platforms = current_set | set(ADDED_PLATFORMS)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] 打开 QQ 官方分段支持失败: %s", DISPLAY_NAME, exc)
        return False
    _PATCHED_ADDED.setdefault(id(step_cls), set()).update(missing)
    logger.info("[%s] outputpro 分段：已为 QQ 官方启用平台支持", DISPLAY_NAME)
    return True


def disable_qq_official() -> bool:
    """插件卸载或关闭 QQ 官方适配时，撤销本插件额外添加的平台集合。"""
    module = _resolve_split_module()
    if module is None:
        return False
    step_cls = getattr(module, "SplitStep", None)
    if step_cls is None:
        return False
    added = _PATCHED_ADDED.pop(id(step_cls), None)
    if not added:
        return False
    current = getattr(step_cls, "support_platforms", None)
    if not isinstance(current, (set, frozenset)):
        return False
    try:
        step_cls.support_platforms = set(current) - added
    except Exception as exc:  # noqa: BLE001
        logger.debug("[%s] 恢复 outputpro 分段平台列表失败: %s", DISPLAY_NAME, exc)
        return False
    return True
