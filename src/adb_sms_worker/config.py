from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Load a small, dependency-free subset of .env syntax.

    Existing environment variables always win. Quotes around complete values are
    removed; shell expansion is deliberately not supported.
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_username: str
    db_password: str
    db_database: str
    adb_path: str
    device_serials: tuple[str, ...]
    poll_interval_seconds: float
    device_refresh_seconds: float
    adb_command_timeout_seconds: float
    ui_wait_seconds: float
    stale_claim_seconds: int
    sim_slot: int | None
    wake_and_dismiss_keyguard: bool
    log_level: str

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        load_dotenv(Path(env_file))
        serials = tuple(
            item.strip()
            for item in os.getenv("ADB_DEVICE_SERIALS", "").split(",")
            if item.strip()
        )
        sim_raw = os.getenv("SIM_SLOT", "").strip()
        sim_slot = int(sim_raw) if sim_raw else None
        if sim_slot not in (None, 1, 2):
            raise ValueError("SIM_SLOT 只能为空、1 或 2")
        return cls(
            db_host=os.getenv("DB_HOST", "127.0.0.1"),
            db_port=_int("DB_PORT", 3306),
            db_username=os.getenv("DB_USERNAME", ""),
            db_password=os.getenv("DB_PASSWORD", ""),
            db_database=os.getenv("DB_DATABASE", "db_bangni"),
            adb_path=os.getenv("ADB_PATH", "adb"),
            device_serials=serials,
            poll_interval_seconds=_float("POLL_INTERVAL_SECONDS", 2.0),
            device_refresh_seconds=_float("DEVICE_REFRESH_SECONDS", 5.0),
            adb_command_timeout_seconds=_float("ADB_COMMAND_TIMEOUT_SECONDS", 20.0),
            ui_wait_seconds=_float("UI_WAIT_SECONDS", 1.2),
            stale_claim_seconds=_int("STALE_CLAIM_SECONDS", 300),
            sim_slot=sim_slot,
            wake_and_dismiss_keyguard=_bool("WAKE_AND_DISMISS_KEYGUARD", True),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def validate_database(self) -> None:
        missing = []
        if not self.db_username:
            missing.append("DB_USERNAME")
        if not self.db_database:
            missing.append("DB_DATABASE")
        if missing:
            raise ValueError(f"缺少数据库配置: {', '.join(missing)}")

