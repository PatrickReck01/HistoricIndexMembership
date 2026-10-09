"""Where the SEC user agent comes from, and the command line."""

from __future__ import annotations

from pathlib import Path

import pytest

from historic_index_membership.cli import main
from historic_index_membership.config import (
    CACHE_DIR_VARIABLE,
    USER_AGENT_VARIABLE,
    load_settings,
)


@pytest.fixture
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run in an empty directory with neither variable set.

    Setting and then deleting each variable makes monkeypatch remove whatever a test's
    ``.env`` file puts into the environment.

    Args:
        monkeypatch: Pytest's monkeypatch.
        tmp_path: An empty directory.

    Returns:
        The directory.
    """
    for variable in (USER_AGENT_VARIABLE, CACHE_DIR_VARIABLE):
        monkeypatch.setenv(variable, "placeholder")
        monkeypatch.delenv(variable)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_argument_wins_over_the_environment(
    clean_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(USER_AGENT_VARIABLE, "Env User env@example.com")
    assert load_settings().user_agent == "Env User env@example.com"
    assert load_settings("Arg User arg@example.com").user_agent == "Arg User arg@example.com"


def test_the_user_agent_and_cache_dir_are_read_from_a_dotenv_file(clean_environment: Path) -> None:
    (clean_environment / ".env").write_text(
        f'{USER_AGENT_VARIABLE}="Dotenv User dotenv@example.com"\n{CACHE_DIR_VARIABLE}=my_cache\n',
        encoding="utf-8",
    )
    settings = load_settings()
    assert settings.user_agent == "Dotenv User dotenv@example.com"
    assert settings.cache_dir == Path("my_cache")


def test_a_missing_or_emailless_user_agent_is_an_error(clean_environment: Path) -> None:
    with pytest.raises(RuntimeError, match="SEC requires a User-Agent"):
        load_settings()
    with pytest.raises(RuntimeError, match="SEC requires a User-Agent"):
        load_settings("No Email")


def test_cli_lists_the_indices(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    output = capsys.readouterr().out
    assert "sp500" in output and "S&P 500" in output and "IVV" in output


def test_cli_build_without_a_user_agent_explains_how_to_set_one(
    clean_environment: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["build", "sp500", "--start", "2010-01-01", "--end", "2010-12-31"]) == 2
    assert "--user-agent" in capsys.readouterr().err
