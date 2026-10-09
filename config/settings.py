"""Environment configuration loader and validator.

All configuration comes from environment variables (optionally via a ``.env``
file in the project root). ``load_settings()`` validates everything up front and
reports *all* problems at once, so you never have to fix them one restart at a
time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Regions (affinities) accepted by the HenrikDev v3 matches endpoint.
VALID_REGIONS = {"na", "eu", "ap", "kr", "latam", "br"}
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

MIN_POLL_INTERVAL = 30  # seconds; protects your API key's rate limit
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Raised when one or more settings are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Validated, immutable application settings."""

    # Secrets are excluded from repr() so they can never leak into logs.
    discord_token: str = field(repr=False)
    henrik_api_key: str = field(repr=False)
    channel_id: int
    region: str
    riot_name: str
    riot_tag: str
    poll_interval: int
    match_mode: str
    history_size: int
    log_level: str

    @property
    def riot_id(self) -> str:
        return f"{self.riot_name}#{self.riot_tag}"


def _get(name: str, default: str = "") -> str:
    """Read an env var, stripping whitespace and optional surrounding quotes."""
    value = os.getenv(name, default).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value


def _get_int(name: str, default: int, errors: list[str], minimum: int, maximum: int | None = None) -> int:
    raw = _get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be a whole number (got {raw!r}).")
        return default
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        errors.append(f"{name} must be {bound} (got {value}).")
        return default
    return value


def load_settings() -> Settings:
    """Load and validate settings. Raises ``ConfigError`` listing every problem."""
    # Load .env from the project root regardless of the current working directory.
    # Real environment variables always win over the file.
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    errors: list[str] = []

    discord_token = _get("DISCORD_BOT_TOKEN")
    if not discord_token:
        errors.append("DISCORD_BOT_TOKEN is required.")

    henrik_key = _get("HENRIK_API_KEY")
    if not henrik_key:
        errors.append("HENRIK_API_KEY is required.")

    channel_raw = _get("DISCORD_CHANNEL_ID")
    channel_id = 0
    if not channel_raw:
        errors.append("DISCORD_CHANNEL_ID is required.")
    elif not channel_raw.isdigit():
        errors.append(f"DISCORD_CHANNEL_ID must be a numeric channel ID (got {channel_raw!r}).")
    else:
        channel_id = int(channel_raw)

    region = _get("VALORANT_REGION", "ap").lower()
    if region not in VALID_REGIONS:
        errors.append(f"VALORANT_REGION must be one of {sorted(VALID_REGIONS)} (got {region!r}).")

    riot_name = _get("VALORANT_NAME")
    if not riot_name:
        errors.append("VALORANT_NAME is required (the part of your Riot ID before the '#').")

    riot_tag = _get("VALORANT_TAG").lstrip("#")
    if not riot_tag:
        errors.append("VALORANT_TAG is required (the part of your Riot ID after the '#').")

    poll_interval = _get_int("POLL_INTERVAL_SECONDS", 180, errors, minimum=MIN_POLL_INTERVAL)
    history_size = _get_int("HISTORY_SIZE", 10, errors, minimum=3, maximum=30)

    match_mode = _get("MATCH_MODE", "competitive")
    if not match_mode:
        errors.append("MATCH_MODE must not be empty.")

    log_level = _get("LOG_LEVEL", "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        errors.append(f"LOG_LEVEL must be one of {sorted(VALID_LOG_LEVELS)} (got {log_level!r}).")

    if errors:
        bullet_list = "\n".join(f"  - {e}" for e in errors)
        raise ConfigError(
            f"Invalid configuration:\n{bullet_list}\n\n"
            f"Copy .env.example to .env in {PROJECT_ROOT} and fill it in."
        )

    return Settings(
        discord_token=discord_token,
        henrik_api_key=henrik_key,
        channel_id=channel_id,
        region=region,
        riot_name=riot_name,
        riot_tag=riot_tag,
        poll_interval=poll_interval,
        match_mode=match_mode,
        history_size=history_size,
        log_level=log_level,
    )
