"""Environment into an object, validated at boot, secrets masked."""

from __future__ import annotations

from typing import Annotated

import pytest

from inceptools.core.config import ConfigError, Secret, Settings, load, load_dotenv, mask


class ShopSettings(Settings):
    model_config = Settings.configure(env_prefix="SHOP_")

    port: int = 8080
    database_url: Annotated[str, Secret]
    api_token: str = "unset"
    origins: list[str] = ["*"]


def test_every_problem_is_reported_at_once(monkeypatch):
    monkeypatch.delenv("SHOP_DATABASE_URL", raising=False)
    monkeypatch.setenv("SHOP_PORT", "not-a-number")
    with pytest.raises(ConfigError) as caught:
        load(ShopSettings)
    assert len(caught.value.problems) == 2
    assert any("SHOP_DATABASE_URL is required" in p for p in caught.value.problems)


def test_an_env_var_is_read(monkeypatch):
    monkeypatch.setenv("SHOP_DATABASE_URL", "postgres://u:p@host/db")
    monkeypatch.setenv("SHOP_PORT", "9000")
    assert load(ShopSettings).port == 9000


def test_describe_masks_secrets(monkeypatch):
    monkeypatch.setenv("SHOP_DATABASE_URL", "postgres://user:hunter2@host/db")
    monkeypatch.setenv("SHOP_API_TOKEN", "sk-live-abcdefgh")
    lines = load(ShopSettings).describe()
    joined = "\n".join(lines)
    assert "hunter2" not in joined
    assert "sk-live-abcdefgh" not in joined
    assert "SHOP_PORT=8080" in lines


def test_a_name_that_looks_secret_is_masked_without_the_marker(monkeypatch):
    """api_token is not annotated Secret, but it is obviously one."""
    monkeypatch.setenv("SHOP_DATABASE_URL", "x://y")
    monkeypatch.setenv("SHOP_API_TOKEN", "supersecretvalue")
    assert "supersecretvalue" not in "\n".join(load(ShopSettings).describe())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", ""), ("abc", "****"), ("abcd", "****"), ("postgres://db", "po********db")],
)
def test_masking(raw, expected):
    assert mask(raw) == expected


def test_dotenv_does_not_override_a_real_env_var(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('export SHOP_PORT=1111\nSHOP_API_TOKEN="from-file"\n# comment\n\n')
    monkeypatch.setenv("SHOP_PORT", "2222")
    load_dotenv(env)
    import os

    assert os.environ["SHOP_PORT"] == "2222"
    assert os.environ["SHOP_API_TOKEN"] == "from-file"


def test_a_missing_dotenv_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == 0
