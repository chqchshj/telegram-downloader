"""
Configuration module for telegram-downloader.
"""
from .loader import ConfigError, load_config
from .schema import Config, FilterConfig, GlobalFilters, ProxyConfig, RetryConfig, SourceConfig
from .url_parser import parse_telegram_url

__all__ = [
    "Config",
    "ConfigError",
    "FilterConfig",
    "GlobalFilters",
    "ProxyConfig",
    "RetryConfig",
    "SourceConfig",
    "load_config",
    "parse_sources",
    "parse_telegram_url",
    "validate_source_access",
]


def __getattr__(name):
    """Lazily import Telegram-dependent helpers."""
    if name in {"parse_sources", "validate_source_access"}:
        from .source_parser import parse_sources, validate_source_access

        return {
            "parse_sources": parse_sources,
            "validate_source_access": validate_source_access,
        }[name]
    raise AttributeError(name)
