"""Unified LLM / VLM provider lookup and response text extraction."""

from __future__ import annotations

from typing import Any, Iterable

try:
    from astrbot.core.provider.entities import ProviderType
except Exception:  # pragma: no cover
    ProviderType = None  # type: ignore[assignment]


class ProviderResolver:
    """Resolve providers by ID, core default, or session default."""

    def __init__(self, context: Any) -> None:
        self.context = context

    @staticmethod
    def extract_text(response: Any) -> str:
        if response is None:
            return ""
        try:
            text = str(getattr(response, "completion_text", "") or "").strip()
            if text:
                return text
            chain = getattr(response, "result_chain", None)
            if chain is not None:
                get_plain = getattr(chain, "get_plain_text", None)
                if callable(get_plain):
                    return str(get_plain() or "").strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def provider_id_of(provider: Any) -> str:
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            return str(config.get("id") or "")
        return str(getattr(provider, "id", "") or "")

    def core_provider_settings(self, umo: str = "") -> dict[str, Any]:
        get_cfg = getattr(self.context, "get_config", None)
        if not callable(get_cfg):
            return {}
        try:
            cfg = get_cfg(umo=umo) if umo else get_cfg()
        except TypeError:
            cfg = get_cfg()
        except Exception:
            return {}
        if hasattr(cfg, "get"):
            settings = cfg.get("provider_settings", {})
            if isinstance(settings, dict):
                return settings
        return {}

    async def by_id(self, provider_id: str) -> Any | None:
        pid = str(provider_id or "").strip()
        if not pid:
            return None
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            return None
        getter = getattr(manager, "get_provider_by_id", None)
        if not callable(getter):
            return None
        try:
            provider = await getter(pid)
        except Exception:
            return None
        if provider is not None and callable(getattr(provider, "text_chat", None)):
            return provider
        return None

    async def session_default(self, umo: str = "", provider_type: Any = None) -> Any | None:
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            return None
        ptype = provider_type if provider_type is not None else (
            getattr(ProviderType, "CHAT_COMPLETION", None) if ProviderType is not None else None
        )
        kwargs = {"umo": umo} if umo else {}
        get_async = getattr(manager, "get_using_provider_async", None)
        if callable(get_async):
            try:
                provider = await get_async(ptype, **kwargs)
            except TypeError:
                try:
                    provider = await get_async(ptype)
                except Exception:
                    provider = None
            except Exception:
                provider = None
            if provider is not None and callable(getattr(provider, "text_chat", None)):
                return provider
            return None
        get_sync = getattr(manager, "get_using_provider", None)
        if callable(get_sync):
            try:
                provider = get_sync(ptype, **kwargs)
            except TypeError:
                try:
                    provider = get_sync(ptype)
                except Exception:
                    provider = None
            except Exception:
                provider = None
            if provider is not None and callable(getattr(provider, "text_chat", None)):
                return provider
        return None

    async def resolve_ordered(
        self,
        provider_ids: Iterable[str],
        *,
        umo: str = "",
        core_setting_key: str = "",
        fallback_session_default: bool = True,
    ) -> Any | None:
        for pid in provider_ids:
            provider = await self.by_id(str(pid or ""))
            if provider is not None:
                return provider
        if core_setting_key:
            core_default = str(self.core_provider_settings(umo).get(core_setting_key, "") or "").strip()
            if core_default:
                provider = await self.by_id(core_default)
                if provider is not None:
                    return provider
        if fallback_session_default:
            return await self.session_default(umo=umo)
        return None
