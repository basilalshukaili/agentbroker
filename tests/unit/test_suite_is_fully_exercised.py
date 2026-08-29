"""
A skipped test is not a passing test, and this suite let one hide for weeks.

WHAT HAPPENED (2026-08-29). A second machine cloned this repo at the exact
commit the founder's laptop was sitting on, installed requirements.txt in full,
and ran the suite. It reported 815 passed / 1 failed where the laptop reported
816 passed / 0 failed on identical code.

The difference was not the code. `celery==5.3.6` is a DECLARED REQUIREMENT that
the laptop had never installed, so
`test_enqueue_booking_voice_fallback_returns_failure_not_success` hit its
`pytest.skip("Celery not importable in this environment")` guard on every run
and was counted as a skip nobody looked at.

When it finally ran, it failed - twice over. It patched
`reliability.async_runner.get_directory`, a name that is imported inside the
function and is therefore not a module attribute; and underneath that, it
called a 6-argument function with 7 arguments. Two independent breakages
stacked in one test is the signature of code that has never executed once.

WHY IT MATTERED. That test guards a real honesty property: when voice booking
is not provisioned, the runner must return FAILURE and never a fabricated
confirmation. Production runs Celery. So the path was live, the guard was
broken, and the suite was green.

THE GENERAL SHAPE, which this codebase keeps meeting: green does not mean
verified. A test skipped for a missing dependency reports the same colour as a
test that ran and passed, and the summary line adds it to a total that gets
quoted as if everything was checked.

So: if a declared dependency is missing, this suite says so LOUDLY instead of
quietly shrinking.
"""
from __future__ import annotations

import importlib
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Packages whose absence SILENTLY DISABLES tests rather than breaking them.
# Map: import name -> the requirements.txt name, since they differ often enough
# that guessing is how this kind of check rots.
GATES_A_TEST = {
    "celery": "celery",
    "redis": "redis",
}


@pytest.mark.parametrize("module,requirement", sorted(GATES_A_TEST.items()))
def test_a_dependency_that_gates_tests_is_actually_installed(module, requirement):
    """Fail loudly rather than skip quietly.

    This is deliberately not a skip. A skip here would reproduce the exact
    failure it exists to prevent - an environment that cannot run part of the
    suite, reporting a number that looks complete.

    If this fails: `python -m pip install -r requirements.txt`.
    """
    try:
        importlib.import_module(module)
    except ImportError:  # noqa: PERF203
        pytest.fail(
            f"{module!r} is not installed, but it is declared in "
            f"requirements.txt as {requirement!r} AND at least one test skips "
            f"itself when it is missing. That test is not running here, and "
            f"the suite total is quietly smaller than it looks. "
            f"Run: python -m pip install -r requirements.txt")


def test_every_gated_package_is_a_real_requirement():
    """Keeps the list above honest in the other direction.

    If a package is listed here but is NOT in requirements.txt, this check
    would demand something the project does not actually require - and the
    obvious way to make it pass would be to delete the entry, taking the
    protection with it.
    """
    path = os.path.join(ROOT, "requirements.txt")
    reqs = open(path, encoding="utf-8", errors="replace").read().lower()
    for _module, requirement in GATES_A_TEST.items():
        assert requirement.lower() in reqs, (
            f"{requirement} is guarded here but is not in requirements.txt - "
            f"either add it there or stop gating tests on it")


def test_the_celery_gated_test_can_no_longer_be_silently_absent():
    """The specific test that hid, named so a future skip is traceable.

    Asserting the guard's CONDITION rather than the test's result: if Celery is
    importable, that test runs, and its own assertions do the rest.
    """
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from reliability import async_runner as ar
    assert ar.CELERY_AVAILABLE, (
        "Celery is not available, so the voice-fallback honesty test is "
        "skipping. That test guards against returning a fabricated booking "
        "confirmation, and production HAS Celery - so it is guarding a live "
        "path from an environment that never exercises it")
