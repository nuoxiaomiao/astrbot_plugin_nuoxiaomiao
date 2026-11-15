"""Configuration lifecycle, catalogs, diagnostics, user-default snapshots, and Web APIs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from astrbot.api import logger
from quart import jsonify, request

if __package__:
    from .config_support import (
        BLANK_BUILTIN_KEYS,
        PluginConfig,
        atomic_write_bytes,
        atomic_write_json,
        build_plugin_config,
        config_diagnostics,
        fold_config_aliases,
        json_safe_config,
        load_schema,
        normalize_config,
        schema_defaults,
        strip_removed_config_keys,
    )
    from .infra.constants import DISPLAY_NAME, LEGACY_CUSTOM_TOOL_CALL_PROMPT, PLUGIN_NAME
    from .infra.runtime import plugin_name_of_tool, timezone_label
else:
    from config_support import (
        BLANK_BUILTIN_KEYS,
        PluginConfig,
        atomic_write_bytes,
        atomic_write_json,
        build_plugin_config,
        config_diagnostics,
        fold_config_aliases,
        json_safe_config,
        load_schema,
        normalize_config,
        schema_defaults,
        strip_removed_config_keys,
    )
    from infra.constants import DISPLAY_NAME, LEGACY_CUSTOM_TOOL_CALL_PROMPT, PLUGIN_NAME
    from infra.runtime import plugin_name_of_tool, timezone_label


class ConfigService:
    """Owns typed runtime config, WebUI endpoints, diagnostics, and user defaults."""

    def __init__(
        self,
        context: Any,
        raw_config: Any,
        data_dir_resolver: Callable[[], Path],
        *,
        plugin_dir: Path | None = None,
        migration_error: str = "",
        resident_catalog_fn: Callable[[], list[dict[str, Any]]] | None = None,
        on_applied: Callable[[PluginConfig], None] | None = None,
        user_default_override_fn: Callable[[], str] | None = None,
    ) -> None:
        self.context = context
        self._raw_config_src = raw_config
        self._data_dir_resolver = data_dir_resolver
        self._plugin_dir = plugin_dir or Path(__file__).resolve().parent
        self._migration_error = migration_error
        self._resident_catalog_fn = resident_catalog_fn
        self._on_applied = on_applied
        self._user_default_override_fn = user_default_override_fn
        self.schema = load_schema(self._plugin_dir)
        self.current: PluginConfig = self.apply()

    @property
    def raw_config(self) -> Any:
        return self._raw_config_src() if callable(self._raw_config_src) else self._raw_config_src

    @raw_config.setter
    def raw_config(self, value: Any) -> None:
        self._raw_config_src = value

    def apply(self) -> PluginConfig:
        if not self.schema:
            self.schema = load_schema(self._plugin_dir)
        cfg = build_plugin_config(dict(self.raw_config), self.schema, self._data_dir_resolver)
        self.current = cfg
        if self._on_applied is not None:
            self._on_applied(cfg)
        return cfg

    def register_web_apis(self) -> None:
        base = f"/{PLUGIN_NAME}"
        self.context.register_web_api(f"{base}/config", self.webapi_get_config, ["GET"], "获取糯小喵配置")
        self.context.register_web_api(f"{base}/config", self.webapi_save_config, ["POST"], "保存糯小喵配置")
        self.context.register_web_api(f"{base}/config/default", self.webapi_save_default, ["POST"], "保存配置并设为默认")

    def user_default_path(self) -> Path:
        if self._user_default_override_fn is not None:
            override = str(self._user_default_override_fn() or "").strip()
            if override:
                return Path(override)
        return self._data_dir_resolver() / "user_default_config.json"

    def load_user_default(self) -> dict[str, Any] | None:
        path = self.user_default_path()
        try:
            if not path.is_file():
                return None
            with path.open(encoding="utf-8-sig") as f:
                data = json.load(f)
            return strip_removed_config_keys(fold_config_aliases(data)) if isinstance(data, dict) and data else None
        except Exception as exc:
            logger.warning("[%s] 读取自定义默认配置失败: %s", DISPLAY_NAME, exc)
            return None

    def write_user_default(self, config: dict[str, Any]) -> dict[str, Any]:
        snapshot = strip_removed_config_keys(fold_config_aliases(config or {}))
        atomic_write_json(self.user_default_path(), snapshot)
        return snapshot

    def clear_user_default(self) -> bool:
        path = self.user_default_path()
        if path.is_file():
            path.unlink()
            return True
        return False

    def tool_plugin_catalog(self) -> list[dict[str, Any]]:
        try:
            from astrbot.core.star.star import star_map as _star_map
        except Exception:
            _star_map = {}
        try:
            tools = self.context.get_llm_tool_manager().get_full_tool_set().tools
        except Exception:
            return []
        grouped: dict[str, list[str]] = {}
        for tool in tools:
            if not getattr(tool, "active", True):
                continue
            name = str(getattr(tool, "name", "") or "")
            if not name:
                continue
            plugin_name = plugin_name_of_tool(tool, _star_map)
            key = plugin_name or "（内置工具）"
            grouped.setdefault(key, []).append(name)
        return [
            {
                "name": plugin_name,
                "builtin": plugin_name == "（内置工具）",
                "tools": sorted(grouped[plugin_name]),
                "count": len(grouped[plugin_name]),
            }
            for plugin_name in sorted(grouped, key=lambda x: (x == "（内置工具）", x))
        ]

    def provider_catalog(self) -> list[dict[str, str]]:
        try:
            providers = self.context.get_all_providers()
        except Exception:
            return []
        catalog: dict[str, dict[str, str]] = {}
        for provider in providers:
            try:
                meta = provider.meta()
                pid = str(getattr(meta, "id", "") or "").strip()
                if not pid:
                    continue
                model = str(getattr(meta, "model", "") or "").strip()
                catalog[pid] = {
                    "id": pid,
                    "label": f"{pid} · {model}" if model and model != pid else pid,
                }
            except Exception:
                continue
        return [catalog[key] for key in sorted(catalog)]

    def effective_config(self, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        src = schema if schema is not None else self.schema
        effective, _ = normalize_config(dict(self.raw_config), src, runtime=True)
        cfg = self.current
        for key, spec in src.items():
            if not isinstance(spec, dict):
                continue
            if key in BLANK_BUILTIN_KEYS:
                stored = effective.get(key)
                if not stored or (key == "tool_call_prompt" and stored == LEGACY_CUSTOM_TOOL_CALL_PROMPT):
                    continue
            value = getattr(cfg, key, None)
            if spec.get("type") in {"bool", "int", "float", "string", "text"} and isinstance(value, (bool, int, float, str)):
                effective[key] = value
        effective["proactive_cooldown_minutes"] = cfg.proactive_cooldown_seconds // 60 if cfg.proactive_cooldown_seconds % 60 == 0 else cfg.proactive_cooldown_seconds / 60
        return effective

    def diagnostics(self, schema: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        src = schema if schema is not None else self.schema
        effective = self.effective_config(src)
        diags = config_diagnostics(dict(self.raw_config), src, effective)
        if self._migration_error:
            diags.append({"code": "config_migration_failed", "severity": "warning", "message": self._migration_error, "fields": []})
        if not effective.get("enabled", True):
            diags.append({"code": "plugin_disabled", "severity": "info", "message": "插件总开关已关闭，功能设置保留，当前不会执行。", "fields": ["enabled"]})
        if effective.get("proactive_silent_wake_enabled") and not effective.get("proactive_enabled"):
            diags.append({"code": "self_wake_requires_proactive", "severity": "info", "message": "主动消息关闭时自醒不会运行；自醒设置已保留。", "fields": ["proactive_enabled", "proactive_silent_wake_enabled"]})
        pid = effective.get("proactive_judge_provider_id")
        if pid and pid not in {item["id"] for item in self.provider_catalog()}:
            diags.append({"code": "self_wake_provider_unavailable", "severity": "warning", "message": "指定的自醒模型当前不可用；请重新选择模型。", "fields": ["proactive_judge_provider_id"]})
        recording_on = bool(effective.get("activity_buffer_enabled"))
        if effective.get("qq_official_enabled") and effective.get("qq_official_quote_trigger") and (not recording_on or not effective.get("activity_record_bot_replies")):
            diags.append({
                "code": "qq_quote_requires_bot_history",
                "severity": "info",
                "message": "QQ 官方引用唤醒需要开启「记录聊天」与「记录机器人回复」，否则无法比对被引用消息是否出自机器人。",
                "fields": ["qq_official_quote_trigger", "activity_buffer_enabled", "activity_record_bot_replies"],
            })
        return diags

    def validate_web_config(self, incoming: dict[str, Any], schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        merged = strip_removed_config_keys(fold_config_aliases(dict(self.raw_config)))
        updates = fold_config_aliases(incoming)
        merged.update({key: value for key, value in updates.items() if key in schema})
        return normalize_config(merged, schema, include_defaults=True)

    @staticmethod
    def error_response(field_errors: dict[str, str], schema: dict[str, Any], return_validation_errors: bool = False):
        errors = [f"{schema.get(key, {}).get('description', key)}：{message}" for key, message in field_errors.items()]
        details = {"message": "配置校验失败：\n" + "\n".join(errors), "errors": errors, "field_errors": field_errors}
        if return_validation_errors:
            return jsonify({"status": "ok", "data": {"validation_error": True, **details}}), 200
        return jsonify({"status": "error", **details}), 400

    def commit_web_config(self, config: dict[str, Any]) -> None:
        original = dict(self.raw_config)
        save_config = getattr(self.raw_config, "save_config", None)
        persisted = False
        try:
            self.raw_config.clear()
            self.raw_config.update(config)
            if callable(save_config):
                save_config(config)
                persisted = True
            self.apply()
        except Exception:
            self.raw_config.clear()
            self.raw_config.update(original)
            if persisted:
                try:
                    save_config(original)
                    self.apply()
                except Exception as rollback_exc:
                    raise RuntimeError("应用新配置失败，恢复原配置也失败；请重新读取确认状态") from rollback_exc
            raise

    def saved_response(self, schema: dict[str, Any]) -> dict[str, Any]:
        return json_safe_config({
            "config": strip_removed_config_keys(dict(self.raw_config)),
            "effective_config": self.effective_config(schema),
            "diagnostics": self.diagnostics(schema),
        })

    @staticmethod
    def webapi_username() -> str:
        try:
            from quart import g as _g

            username = getattr(_g, "username", None)
            if isinstance(username, str) and username.strip():
                return username.strip()
        except Exception:  # noqa: BLE001
            pass
        try:
            from astrbot.api.web import request as _web_request

            username = getattr(_web_request, "username", None)
            if isinstance(username, str) and username.strip():
                return username.strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    @staticmethod
    def auth_denied():
        return jsonify({"status": "error", "message": "未登录或登录已过期"}), 401

    async def webapi_get_config(self):
        if not self.webapi_username():
            return self.auth_denied()
        schema = load_schema(self._plugin_dir)
        self.schema = schema
        resident_tools = self._resident_catalog_fn() if callable(self._resident_catalog_fn) else []
        return jsonify(
            {
                "status": "ok",
                "data": json_safe_config({
                    "config": strip_removed_config_keys(dict(self.raw_config)),
                    "schema": schema,
                    "defaults": schema_defaults(schema),
                    "user_default": self.load_user_default(),
                    "tool_plugins": self.tool_plugin_catalog(),
                    "resident_tools": resident_tools,
                    "providers": self.provider_catalog(),
                    "timezone": timezone_label(self.context),
                    "effective_config": self.effective_config(schema),
                    "diagnostics": self.diagnostics(schema),
                }),
            }
        )

    async def webapi_save_config(self):
        if not self.webapi_username():
            return self.auth_denied()
        raw_text = await request.get_data(as_text=True)
        body = await request.get_json(silent=True)
        if body is None and raw_text.strip():
            return jsonify({"status": "error", "message": "请求体必须是合法 JSON"}), 400
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return jsonify({"status": "error", "message": "配置数据必须是对象"}), 400
        incoming = body.get("config", body)
        if not isinstance(incoming, dict):
            return jsonify({"status": "error", "message": "config 必须是对象"}), 400
        schema = load_schema(self._plugin_dir)
        if not schema:
            return jsonify({"status": "error", "message": "配置说明文件不可用，无法保存"}), 500
        merged, field_errors = self.validate_web_config(incoming, schema)
        if field_errors:
            return self.error_response(field_errors, schema, body.get("return_validation_errors") is True)
        try:
            self.commit_web_config(merged)
        except Exception as exc:
            logger.warning("[%s] 保存 WebUI 配置失败: %s", DISPLAY_NAME, exc)
            return jsonify({"status": "error", "message": f"保存配置失败: {exc}"}), 500
        return jsonify({
            "status": "ok",
            "message": "配置已保存并即时生效",
            "data": self.saved_response(schema),
        })

    async def webapi_save_default(self):
        if not self.webapi_username():
            return self.auth_denied()
        raw_text = await request.get_data(as_text=True)
        body = await request.get_json(silent=True)
        if body is None and raw_text.strip():
            return jsonify({"status": "error", "message": "请求体必须是合法 JSON"}), 400
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return jsonify({"status": "error", "message": "配置数据必须是对象"}), 400
        if body.get("clear"):
            try:
                removed = self.clear_user_default()
            except Exception as exc:
                logger.warning("[%s] 清除自定义默认失败: %s", DISPLAY_NAME, exc)
                return jsonify({"status": "error", "message": "清除个人默认失败，原快照保留"}), 500
            return jsonify({
                "status": "ok",
                "message": "已清除自定义默认，恢复出厂默认" if removed else "本就没有自定义默认",
                "data": {"user_default": None},
            })
        incoming = body.get("config", body)
        if not isinstance(incoming, dict):
            return jsonify({"status": "error", "message": "config 必须是对象"}), 400
        schema = load_schema(self._plugin_dir)
        if not schema:
            return jsonify({"status": "error", "message": "配置说明文件不可用，无法保存"}), 500
        merged, field_errors = self.validate_web_config(incoming, schema)
        if field_errors:
            return self.error_response(field_errors, schema, body.get("return_validation_errors") is True)
        snapshot = {key: merged[key] for key, spec in schema.items() if isinstance(spec, dict) and key in merged}
        snapshot_path = self.user_default_path()
        original_snapshot = None
        snapshot_written = False
        try:
            if snapshot_path.exists():
                original_snapshot = snapshot_path.read_bytes()
            self.write_user_default(snapshot)
            snapshot_written = True
            self.commit_web_config(merged)
        except Exception as exc:
            if snapshot_written:
                try:
                    if original_snapshot is None:
                        snapshot_path.unlink(missing_ok=True)
                    else:
                        atomic_write_bytes(snapshot_path, original_snapshot)
                except Exception as rollback_exc:
                    logger.error("[%s] 回滚个人默认失败: %s", DISPLAY_NAME, rollback_exc)
                    return jsonify({"status": "error", "message": "保存失败，个人默认回滚也失败；请重新读取配置确认状态"}), 500
            logger.warning("[%s] 保存个人默认失败: %s", DISPLAY_NAME, exc)
            return jsonify({"status": "error", "message": f"保存个人默认失败：{exc}"}), 500
        return jsonify({
            "status": "ok",
            "message": "已保存并设为「我的默认」",
            "data": {**self.saved_response(schema), "user_default": snapshot},
        })
