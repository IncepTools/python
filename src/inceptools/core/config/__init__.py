"""Environment into an object, validated at boot, secrets masked in dumps.

Two guarantees:

* **Every problem at once.** A misconfiguration costs one restart to fix, not
  one restart per missing variable. Pydantic collects them; this module renders
  them as a sorted list naming the environment variable, not the Python field.
* **A secret cannot be printed by accident.** Mark a field with
  :data:`Secret` and :meth:`Settings.describe` masks it. The point is that the
  safe thing is the easy thing: ``log.info(settings.describe())`` is correct,
  while ``log.info(settings)`` is the mistake nobody should have to remember
  not to make.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TypeVar

from pydantic import Field, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, SettingsConfigDict

from inceptools.core import errs

__all__ = [
    "ConfigError",
    "Secret",
    "Settings",
    "load",
    "load_dotenv",
    "mask",
]

T = TypeVar("T", bound="Settings")

#: Mark a settings field as secret: ``dsn: Annotated[str, Secret]``.
Secret = Field(json_schema_extra={"secret": True})

_SECRET_HINTS = (
    "password",
    "secret",
    "token",
    "key",
    "dsn",
    "credential",
    "private",
    "passwd",
    "auth",
)


class ConfigError(errs.AppError):
    """One or more configuration problems, reported together."""

    def __init__(self, problems: list[str]) -> None:
        listed = "\n".join(f"  - {p}" for p in problems)
        super().__init__(
            kind=errs.ErrorKind.INTERNAL,
            message=f"config: {len(problems)} problem(s):\n{listed}",
            code="CONFIG_INVALID",
            redact=False,
        )
        self.problems = problems


def mask(value: Any) -> str:
    """``"postgres://..."`` -> ``"po********rt"``.

    Short values are masked entirely: revealing two of four characters is
    revealing half of it.
    """
    text = "" if value is None else str(value)
    if not text:
        return ""
    if len(text) <= 4:
        return "****"
    return text[:2] + "*" * min(len(text) - 4, 8) + text[-2:]


def _is_secret(name: str, info: FieldInfo) -> bool:
    extra = info.json_schema_extra
    if isinstance(extra, dict) and extra.get("secret"):
        return True
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


class Settings(BaseSettings):
    """The base for a service's configuration.

    Subclass it, declare fields, and load once at boot::

        class AppSettings(inceptools.Settings):
            model_config = inceptools.Settings.configure(env_prefix="SHOP_")

            port: int = 8080
            database_url: Annotated[str, inceptools.Secret]
            request_timeout: float = 30.0
    """

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        env_file=None,
        validate_default=True,
    )

    @staticmethod
    def configure(**overrides: Any) -> SettingsConfigDict:
        """``model_config`` with the framework defaults plus *overrides*."""
        base: dict[str, Any] = {
            "case_sensitive": False,
            "extra": "ignore",
            "env_file": None,
            "validate_default": True,
        }
        base.update(overrides)
        return SettingsConfigDict(**base)  # type: ignore[typeddict-item]

    # -- presentation ----------------------------------------------------

    def describe(self) -> list[str]:
        """``["PORT=8080", "DATABASE_URL=po********rt"]``, sorted and masked.

        Safe to log. This is the only rendering of configuration that is.
        """
        prefix = str(self.model_config.get("env_prefix", "") or "")
        lines: list[str] = []
        for name, info in type(self).model_fields.items():
            value = getattr(self, name, None)
            env_name = f"{prefix}{name}".upper()
            rendered = mask(value) if _is_secret(name, info) else _render(value)
            lines.append(f"{env_name}={rendered}")
        return sorted(lines)

    def __str__(self) -> str:
        return "\n".join(self.describe())


def _render(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return ",".join(str(v) for v in value)
    return str(value)


def _problems_from(error: ValidationError, prefix: str) -> list[str]:
    problems: list[str] = []
    for item in error.errors():
        loc = ".".join(str(p) for p in item.get("loc", ()))
        env_name = f"{prefix}{loc}".upper()
        kind = str(item.get("type", ""))
        if kind == "missing":
            problems.append(f"{env_name} is required but not set")
        else:
            problems.append(f"{env_name}: {item.get('msg', 'is invalid')}")
    return sorted(problems)


def load(settings_cls: type[T], **overrides: Any) -> T:
    """Load *settings_cls*, reporting every problem at once.

    Call it once at boot and put the result in the container. A config object
    resolved lazily on first use is a config object whose absence is discovered
    by a request rather than by the deploy.
    """
    try:
        return settings_cls(**overrides)
    except ValidationError as exc:
        prefix = str(settings_cls.model_config.get("env_prefix", "") or "")
        raise ConfigError(_problems_from(exc, prefix)) from exc


def load_dotenv(path: str | os.PathLike[str] = ".env", *, override: bool = False) -> int:
    """Read a ``.env`` file into the environment. Returns how many were set.

    A real environment variable beats a committed file unless *override* says
    otherwise, and a missing file is not an error — it is the normal case in
    production.
    """
    target = Path(path)
    if not target.is_file():
        return 0

    applied = 0
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            applied += 1
    return applied
