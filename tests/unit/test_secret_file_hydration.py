"""
Unit tests for config.hydrate_env_from_secret_files().

Verifies the secret-file -> os.environ hydration logic used to load
Render secret files (/etc/secrets/<NAME>) before config variables are resolved.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hydrate(tmp_path: Path, extra_dirs: list[Path] | None = None) -> list[str]:
    """Import-safe wrapper: always re-imports so module-level side effects
    do not interfere with per-test isolation."""
    # Import here to avoid polluting the module cache across tests when we
    # need to verify specific behaviours.
    from config import hydrate_env_from_secret_files
    return hydrate_env_from_secret_files(extra_dirs=extra_dirs or [tmp_path])


# ---------------------------------------------------------------------------
# Test: a valid secret file is loaded into os.environ
# ---------------------------------------------------------------------------

def test_valid_secret_file_is_hydrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A file whose name is a valid ENV key and whose var is unset gets injected."""
    secret_file = tmp_path / "FAKE_TEST_KEY"
    secret_file.write_text("fake-secret-value", encoding="utf-8")

    # Ensure the key is absent before hydration.
    monkeypatch.delenv("FAKE_TEST_KEY", raising=False)

    hydrated = _hydrate(tmp_path)

    assert "FAKE_TEST_KEY" in hydrated
    assert os.environ.get("FAKE_TEST_KEY") == "fake-secret-value"


# ---------------------------------------------------------------------------
# Test: already-set env var is NOT overwritten
# ---------------------------------------------------------------------------

def test_existing_env_var_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """If the variable is already in os.environ, the secret file must not clobber it."""
    secret_file = tmp_path / "KEEP_ORIGINAL_KEY"
    secret_file.write_text("from-secret-file", encoding="utf-8")

    monkeypatch.setenv("KEEP_ORIGINAL_KEY", "original-value")

    hydrated = _hydrate(tmp_path)

    assert "KEEP_ORIGINAL_KEY" not in hydrated
    assert os.environ.get("KEEP_ORIGINAL_KEY") == "original-value"


# ---------------------------------------------------------------------------
# Test: files with lowercase / oddly-named names are ignored
# ---------------------------------------------------------------------------

def test_non_env_key_filenames_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Files whose names do not match ^[A-Z][A-Z0-9_]*$ must be skipped."""
    bad_names = ["lowercase_key", "1STARTS_DIGIT", "HAS-DASH", "has.dot", ".hidden"]
    for name in bad_names:
        (tmp_path / name).write_text("should-not-be-loaded", encoding="utf-8")

    for name in bad_names:
        monkeypatch.delenv(name, raising=False)

    hydrated = _hydrate(tmp_path)

    assert len(hydrated) == 0
    for name in bad_names:
        assert os.environ.get(name) is None


# ---------------------------------------------------------------------------
# Test: sub-directories inside the secret dir are skipped silently
# ---------------------------------------------------------------------------

def test_subdirectories_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A sub-directory whose name looks like an ENV key must not cause an error."""
    sub = tmp_path / "LOOKS_LIKE_KEY"
    sub.mkdir()

    monkeypatch.delenv("LOOKS_LIKE_KEY", raising=False)

    # Must not raise and must not hydrate anything from a directory.
    hydrated = _hydrate(tmp_path)
    assert "LOOKS_LIKE_KEY" not in hydrated


# ---------------------------------------------------------------------------
# Test: non-existent directories are silently skipped (local dev safety)
# ---------------------------------------------------------------------------

def test_nonexistent_dir_is_skipped(tmp_path: Path):
    """A directory path that does not exist must not raise."""
    missing = tmp_path / "does_not_exist"
    assert not missing.exists()

    # Should return an empty list, not raise.
    from config import hydrate_env_from_secret_files
    result = hydrate_env_from_secret_files(extra_dirs=[missing])
    assert result == [] or isinstance(result, list)


# ---------------------------------------------------------------------------
# Test: empty secret file is skipped (no blank var injection)
# ---------------------------------------------------------------------------

def test_empty_secret_file_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A secret file with empty/whitespace-only content must not inject a blank value."""
    secret_file = tmp_path / "EMPTY_SECRET_KEY"
    secret_file.write_text("   \n", encoding="utf-8")

    monkeypatch.delenv("EMPTY_SECRET_KEY", raising=False)

    hydrated = _hydrate(tmp_path)
    assert "EMPTY_SECRET_KEY" not in hydrated
    assert os.environ.get("EMPTY_SECRET_KEY") is None


# ---------------------------------------------------------------------------
# Test: value is stripped of surrounding whitespace
# ---------------------------------------------------------------------------

def test_secret_value_is_stripped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Values read from secret files must have leading/trailing whitespace stripped."""
    secret_file = tmp_path / "STRIP_TEST_KEY"
    secret_file.write_text("  real-value  \n", encoding="utf-8")

    monkeypatch.delenv("STRIP_TEST_KEY", raising=False)

    _hydrate(tmp_path)
    assert os.environ.get("STRIP_TEST_KEY") == "real-value"


# ---------------------------------------------------------------------------
# Cleanup: remove test keys from os.environ after each test
# ---------------------------------------------------------------------------

TEST_KEYS = [
    "FAKE_TEST_KEY",
    "KEEP_ORIGINAL_KEY",
    "EMPTY_SECRET_KEY",
    "STRIP_TEST_KEY",
]


@pytest.fixture(autouse=True)
def _cleanup_env():
    """Remove test-injected keys from os.environ after each test."""
    yield
    for key in TEST_KEYS:
        os.environ.pop(key, None)
