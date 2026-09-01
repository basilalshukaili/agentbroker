"""A stubbed compliance gate must actually be stubbed.

WHAT HAPPENED (2026-08-31). Two tests in tests/unit/test_number_pool.py failed
once in a full-suite run and passed on every re-run. They believed they had
switched the compliance gate off for the duration:

    monkeypatch.setattr("compliance.pre_check.pre_check", lambda **k: None)

They had not. Every channel adapter does `from compliance.pre_check import
pre_check` at import time, so the adapter holds its OWN reference to the real
function; rebinding the definition site afterwards changes a name the adapter
never looks at. The stub was dead code and the real gate ran on every send.

That is not a cosmetic mistake. The gate's last act is
`get_audit_log().record()`, which fire-and-forgets a Supabase write onto
whatever event loop is running - so a test that was written to do no I/O at all
was issuing a live HTTP POST from inside its own assertion window, wherever
SUPABASE_URL happened to be configured. It passed when the network was quiet
and failed when it was not.

Same family as the dead `except` blocks around the Supabase helpers: code that
reads like a safeguard, is never exercised, and is therefore never known to be
broken. So the rule gets a test rather than a note.

THE RULE: stub the gate where the CALLER reads it
(`channels.whatsapp.cloud_api.pre_check`), never where it is defined.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]

# Built from parts so this file does not match its own scan.
_DEFINITION_SITE = "compliance.pre_check" + ".pre_check"
_BAD_STUB = re.compile(
    r"""(?:monkeypatch\.setattr|mock\.patch|patch)\(\s*["']"""
    + re.escape(_DEFINITION_SITE)
    + r"""["']"""
)


def _adapter_modules() -> list[pathlib.Path]:
    """Every channel adapter that imports the gate symbol directly."""
    return sorted(
        path for path in (_REPO / "channels").rglob("*.py")
        if "from compliance.pre_check import pre_check"
        in path.read_text(encoding="utf-8", errors="replace")
    )


def test_there_are_adapters_to_protect():
    """If this ever returns nothing, the two tests below are vacuously green -
    the producer-with-no-caller failure, applied to a guard."""
    assert _adapter_modules(), "no adapter imports the compliance gate - has the wiring moved?"


@pytest.mark.parametrize(
    "module_path",
    _adapter_modules(),
    ids=lambda p: p.stem,
)
def test_patching_the_definition_site_does_not_reach_the_adapter(module_path, monkeypatch):
    """Executable proof of the trap, so nobody has to rediscover it."""
    import importlib

    import compliance.pre_check as definition_site

    dotted = ".".join(module_path.relative_to(_REPO).with_suffix("").parts)
    adapter = importlib.import_module(dotted)
    real_gate = adapter.pre_check

    sentinel = lambda **k: None  # noqa: E731
    monkeypatch.setattr(definition_site, "pre_check", sentinel)

    assert adapter.pre_check is real_gate, (
        f"{dotted} unexpectedly follows the definition site - if this ever "
        f"becomes true the guidance below should be revisited")
    assert adapter.pre_check is not sentinel, (
        f"Stubbing '{_DEFINITION_SITE}' does NOT stub {dotted}. "
        f"Patch '{dotted}.pre_check' instead.")


def test_no_test_stubs_the_gate_at_its_definition_site():
    """The scan. A stub that cannot fire is worse than no stub: the test reads
    as isolated, runs against the live gate, and drags the gate's Supabase
    audit-mirror write onto its own event loop."""
    offenders = []
    for path in sorted((_REPO / "tests").rglob("*.py")):
        if path.resolve() == pathlib.Path(__file__).resolve():
            continue
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _BAD_STUB.search(line):
                offenders.append(f"{path.relative_to(_REPO)}:{lineno}")

    assert not offenders, (
        "These tests stub the compliance gate where it is DEFINED, so the stub "
        "never fires and the real gate runs (issuing a Supabase audit write "
        "mid-test). Patch the caller's binding instead, e.g. "
        "'channels.whatsapp.cloud_api.pre_check':\n  " + "\n  ".join(offenders))
