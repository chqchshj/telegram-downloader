"""Tests for Web configuration persistence helpers."""
from pathlib import Path

import yaml

from src.config.web_config import load_config_document, redacted_config, save_config_document


def test_redacted_config_hides_api_hash_and_proxy_password():
    data = {
        "api_id": 12345,
        "api_hash": "0123456789abcdef0123456789abcdef",
        "phone_number": "+1234567890",
        "proxy": {
            "enabled": True,
            "scheme": "socks5",
            "hostname": "127.0.0.1",
            "port": 40000,
            "password": "secret",
        },
    }

    result = redacted_config(data)

    assert result["api_hash"] == "0123…cdef"
    assert result["api_hash_set"] is True
    assert result["proxy"]["password"] == "********"
    assert result["proxy"]["password_set"] is True


def test_save_config_document_preserves_existing_secrets_when_blank(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "api_id": 12345,
                "api_hash": "existing_hash",
                "proxy": {"enabled": True, "password": "existing_proxy_password"},
            }
        ),
        encoding="utf-8",
    )

    saved = save_config_document(
        config_path,
        {
            "api_id": 67890,
            "api_hash": "",
            "phone_number": "+1987654321",
            "sources": [{"url": "https://t.me/example"}],
            "global_filters": {"extensions": [".pdf"]},
            "download_dir": "/downloads",
            "daemon": {"enabled": True, "check_interval": 600},
            "proxy": {"enabled": True, "scheme": "socks5", "hostname": "127.0.0.1", "port": 40000, "password": ""},
            "flat_structure": True,
        },
    )

    assert saved["api_id"] == 67890
    assert saved["api_hash"] == "existing_hash"
    assert saved["proxy"]["password"] == "existing_proxy_password"
    assert saved["daemon"]["check_interval"] == 600
    assert saved["flat_structure"] is True

    reloaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert reloaded["api_hash"] == "existing_hash"
    assert reloaded["proxy"]["password"] == "existing_proxy_password"


def test_load_config_document_returns_empty_dict_for_missing_file(tmp_path):
    assert load_config_document(tmp_path / "missing.yaml") == {}
