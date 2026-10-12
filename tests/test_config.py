"""Leitura de configuração: variáveis de ambiente e arquivo .env."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_zabbix.config import Settings, load_env_file, parse_bool

ENV_NAMES = (
    "ZABBIX_URL", "ZABBIX_API_TOKEN", "ZABBIX_USER", "ZABBIX_PASSWORD",
    "ZABBIX_TIMEOUT_SECONDS", "ZABBIX_VERIFY_TLS", "ZABBIX_READ_ONLY", "MCP_ZABBIX_ENV_FILE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:8080", "http://localhost:8080/api_jsonrpc.php"),
        ("http://localhost:8080/", "http://localhost:8080/api_jsonrpc.php"),
        ("https://monitor.exemplo.com/zabbix", "https://monitor.exemplo.com/zabbix/api_jsonrpc.php"),
        ("https://monitor.exemplo.com/zabbix/api_jsonrpc.php", "https://monitor.exemplo.com/zabbix/api_jsonrpc.php"),
    ],
)
def test_endpoint_accepts_base_or_full_url(url: str, expected: str) -> None:
    assert Settings(url=url).endpoint == expected


def test_defaults_when_nothing_is_set() -> None:
    settings = Settings.from_env()

    assert settings.url == "http://localhost:8080"
    assert settings.has_credentials is False
    assert settings.read_only is False
    assert settings.verify_tls is True
    assert settings.timeout_seconds == 15.0


def test_reads_everything_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZABBIX_URL", "https://zbx.test")
    monkeypatch.setenv("ZABBIX_API_TOKEN", "tok")
    monkeypatch.setenv("ZABBIX_READ_ONLY", "true")
    monkeypatch.setenv("ZABBIX_VERIFY_TLS", "false")
    monkeypatch.setenv("ZABBIX_TIMEOUT_SECONDS", "3.5")

    settings = Settings.from_env()

    assert (settings.url, settings.api_token) == ("https://zbx.test", "tok")
    assert settings.read_only is True
    assert settings.verify_tls is False
    assert settings.timeout_seconds == 3.5
    assert settings.has_credentials is True


def test_user_without_password_is_not_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZABBIX_USER", "Admin")

    assert Settings.from_env().has_credentials is False


@pytest.mark.parametrize("raw", ["abc", "nan", "inf", "-1", "0", ""])
def test_invalid_timeout_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("ZABBIX_TIMEOUT_SECONDS", raw)

    assert Settings.from_env().timeout_seconds == 15.0


@pytest.mark.parametrize(
    ("raw", "default", "expected"),
    [("true", False, True), ("YES", False, True), ("0", True, False), ("off", True, False),
     ("talvez", True, True), (None, False, False), ("", True, True)],
)
def test_parse_bool(raw: str | None, default: bool, expected: bool) -> None:
    assert parse_bool(raw, default) is expected


def test_env_file_fills_gaps_but_never_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comentário\n"
        "ZABBIX_URL=http://do-arquivo\n"
        "export ZABBIX_USER=\"Admin\"\n"
        "ZABBIX_PASSWORD='com espaço'  \n"
        "ZABBIX_READ_ONLY=true # inline\n"
        "linha sem igual\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ZABBIX_URL", "http://do-ambiente")

    used = load_env_file(env_file)
    settings = Settings.from_env()

    assert used == env_file
    assert settings.url == "http://do-ambiente"  # o ambiente vence o arquivo
    assert settings.user == "Admin"
    assert settings.password == "com espaço"
    assert settings.read_only is True

    for name in ("ZABBIX_USER", "ZABBIX_PASSWORD", "ZABBIX_READ_ONLY"):
        monkeypatch.delenv(name, raising=False)


def test_missing_env_file_is_fine(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nao-existe.env") is None
