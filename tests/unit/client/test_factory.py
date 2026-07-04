"""Tests for Pyrogram client factory configuration."""
from pathlib import Path
from unittest.mock import Mock

from src.client.factory import create_client
from src.config.schema import Config


def test_create_client_passes_pyrogram_proxy(monkeypatch, tmp_path):
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return Mock()

    monkeypatch.setattr("src.client.factory.Client", fake_client)
    config = Config.model_validate({
        "api_id": 12345,
        "api_hash": "abc123def456789012345678901234ab",
        "session_dir": str(tmp_path / "sessions"),
        "proxy": {
            "enabled": True,
            "scheme": "socks5",
            "host": "192.168.2.20",
            "port": 40000,
        },
    })

    create_client(config)

    assert captured["proxy"] == {
        "scheme": "socks5",
        "hostname": "192.168.2.20",
        "port": 40000,
    }
    assert Path(captured["workdir"]).exists()
