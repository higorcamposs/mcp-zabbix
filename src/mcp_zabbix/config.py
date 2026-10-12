"""Configuração do servidor, lida de variáveis de ambiente.

Para uso local há também um arquivo `.env` opcional (veja `.env.example`). Ele
só preenche o que o ambiente ainda não definiu: uma variável exportada no shell
ou no `env` do cliente MCP sempre vence o arquivo.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("mcp_zabbix.config")

DEFAULT_URL = "http://localhost:8080"
DEFAULT_TIMEOUT_SECONDS = 15.0
API_PATH = "api_jsonrpc.php"

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TRUE = {"1", "true", "yes", "on", "sim"}
_FALSE = {"0", "false", "no", "off", "nao", "não"}


def parse_bool(raw: str | None, default: bool) -> bool:
    """Interpreta 'true/false/1/0/yes/no/on/off'. Valor desconhecido usa o padrão."""
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    logger.warning("valor booleano inválido %r; usando %s", raw, default)
    return default


def _env_float(name: str, default: float) -> float:
    """Lê segundos positivos e finitos. NaN, inf, zero e negativos caem no padrão."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r não é numérico; usando %s", name, raw, default)
        return default
    if not math.isfinite(value) or value <= 0:
        logger.warning("%s=%r precisa ser um número positivo; usando %s", name, raw, default)
        return default
    return value


def _parse_env_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.removeprefix("export ").strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return (key, value) if key else None


def load_env_file(path: Path | None = None) -> Path | None:
    """Carrega um `.env` sem sobrescrever variáveis já definidas.

    Ordem de busca: `MCP_ZABBIX_ENV_FILE`, `.env` na raiz do projeto e `.env` no
    diretório atual. Devolve o arquivo usado, ou `None` se nenhum existe.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    else:
        explicit = os.getenv("MCP_ZABBIX_ENV_FILE")
        if explicit:
            candidates.append(Path(explicit).expanduser())
        candidates += [PROJECT_ROOT / ".env", Path.cwd() / ".env"]

    for candidate in candidates:
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_line(line)
            if parsed is not None:
                os.environ.setdefault(*parsed)
        return candidate
    return None


@dataclass(frozen=True)
class Settings:
    """Parâmetros de conexão com o Zabbix."""

    url: str = DEFAULT_URL
    api_token: str | None = None
    user: str | None = None
    password: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    verify_tls: bool = True
    read_only: bool = False

    @property
    def endpoint(self) -> str:
        """URL do endpoint JSON-RPC. Aceita a URL base ou a URL completa."""
        url = self.url.strip().rstrip("/")
        return url if url.endswith(".php") else f"{url}/{API_PATH}"

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_token or (self.user and self.password))

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            url=os.getenv("ZABBIX_URL", "").strip() or DEFAULT_URL,
            api_token=os.getenv("ZABBIX_API_TOKEN", "").strip() or None,
            user=os.getenv("ZABBIX_USER", "").strip() or None,
            password=os.getenv("ZABBIX_PASSWORD") or None,
            timeout_seconds=_env_float("ZABBIX_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            verify_tls=parse_bool(os.getenv("ZABBIX_VERIFY_TLS"), True),
            read_only=parse_bool(os.getenv("ZABBIX_READ_ONLY"), False),
        )
