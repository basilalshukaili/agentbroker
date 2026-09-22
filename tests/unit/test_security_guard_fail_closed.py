"""
Board row 250 — three secret-strength security guards
(billing/receipt_signer.py, agent_interface/identity.py,
agent_interface/unsubscribe.py) all keyed on
`os.getenv("ENVIRONMENT") == "production"`, which reads False when
ENVIRONMENT is unset — the exact state the real production container ran
in, so none of the three guards ever fired despite every one of them
running on a dev-default secret.

The fix is core/env_guard.is_production_for_security_guards(): unset,
empty, or an unrecognised ENVIRONMENT value now counts as production;
only an explicit ENVIRONMENT=development opts out.

Each guard runs at MODULE IMPORT TIME, so proving it means reloading the
module under a controlled environment rather than calling a function --
same pattern already used in tests/unit/test_machine_mint_key.py for
MACHINE_MINT_SECRET.

For every guard we prove three things:
  1. ENVIRONMENT unset + a dev-default secret -> the guard fires.
  2. ENVIRONMENT=development (the explicit escape hatch) + a dev-default
     secret -> the guard does NOT fire.
  3. ENVIRONMENT=production + a REAL (non-default) secret -> the guard
     does NOT fire. A guard that fires on everything proves nothing --
     see memory a-gate-that-inspects-nothing.
"""
from __future__ import annotations

import importlib
import os
import sys
import pathlib
import unittest
from unittest.mock import patch

# Make sure the agentbroker root is importable when tests run from the
# tests dir (same fix-up as tests/unit/test_machine_mint_key.py).
ROOT = str(pathlib.Path(__file__).resolve().parents[2])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Every env var any of the three guards, or their secret fallback chains,
# read. Snapshotting exactly these (and only these) keeps this test from
# stepping on unrelated env state.
_GUARD_ENV_VARS = (
    "ENVIRONMENT",
    "BILLING_SIGNING_KEY",
    "JWT_SIGNING_SECRET",
    "UNSUBSCRIBE_SECRET",
    "KEY_VERIFY_SECRET",
)

_GUARD_MODULE_NAMES = (
    "billing.receipt_signer",
    "agent_interface.identity",
    "agent_interface.unsubscribe",
)


class _GuardReloadTestCase(unittest.TestCase):
    """Shared setUp/tearDown for tests that reload one of the three guard
    modules under a controlled environment.

    Each test's environment is fully isolated (every relevant var is
    cleared before being explicitly set), and tearDown both restores the
    original env AND reloads all three guard modules so a later test file
    that imports e.g. agent_interface.identity never sees the "poisoned"
    dev-default secret / unset ENVIRONMENT state a previous test in this
    file constructed.
    """

    def setUp(self):
        self._orig = {k: os.environ.get(k) for k in _GUARD_ENV_VARS}

    def tearDown(self):
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._reload_all_guard_modules()

    @staticmethod
    def _reload_all_guard_modules():
        for name in _GUARD_MODULE_NAMES:
            mod = sys.modules.get(name)
            if mod is None:
                continue
            try:
                importlib.reload(mod)
            except RuntimeError:
                # billing/receipt_signer.py RAISES by design when the
                # ambient (real, outside-this-test) environment itself has
                # no BILLING_SIGNING_KEY set -- which is genuinely true in
                # a bare local/CI shell. That is not test contamination to
                # fix; the module has zero callers in the whole codebase
                # (verified: `grep -rn "^\s*from billing\.receipt_signer"`
                # across the repo returns nothing), so there is no shared
                # state here for a later test to inherit. Do not let this
                # module's own guard firing during cleanup masquerade as a
                # test failure.
                pass

    @staticmethod
    def _clear_and_set(env: dict) -> None:
        for k in _GUARD_ENV_VARS:
            os.environ.pop(k, None)
        os.environ.update(env)


class TestReceiptSignerFailsClosed(_GuardReloadTestCase):
    """billing/receipt_signer.py -- RAISE."""

    def test_unset_environment_with_dev_default_key_fires(self):
        self._clear_and_set({
            "BILLING_SIGNING_KEY": "dev-signing-key-replace-in-production",
        })
        import billing.receipt_signer as m
        with self.assertRaises(RuntimeError) as ctx:
            importlib.reload(m)
        self.assertIn("BILLING_SIGNING_KEY", str(ctx.exception))

    def test_explicit_development_with_dev_default_key_does_not_fire(self):
        self._clear_and_set({
            "ENVIRONMENT": "development",
            "BILLING_SIGNING_KEY": "dev-signing-key-replace-in-production",
        })
        import billing.receipt_signer as m
        importlib.reload(m)  # must not raise

    def test_production_with_real_key_does_not_fire(self):
        self._clear_and_set({
            "ENVIRONMENT": "production",
            "BILLING_SIGNING_KEY": "a-real-random-signing-key-not-the-dev-default",
        })
        import billing.receipt_signer as m
        importlib.reload(m)  # must not raise


class TestIdentityFailsClosed(_GuardReloadTestCase):
    """agent_interface/identity.py -- LOG."""

    def test_unset_environment_with_missing_secret_logs_error(self):
        self._clear_and_set({})  # JWT_SIGNING_SECRET unset -> falls back to dev default
        import agent_interface.identity as m
        with self.assertLogs("smb_broker.identity", level="ERROR") as cap:
            importlib.reload(m)
        self.assertTrue(any("JWT_SIGNING_SECRET" in line for line in cap.output))

    def test_explicit_development_with_dev_default_secret_does_not_log(self):
        self._clear_and_set({
            "ENVIRONMENT": "development",
            "JWT_SIGNING_SECRET": "dev-secret-replace-in-production",
        })
        import agent_interface.identity as m
        with self.assertNoLogs("smb_broker.identity", level="ERROR"):
            importlib.reload(m)

    def test_production_with_real_secret_does_not_log(self):
        self._clear_and_set({
            "ENVIRONMENT": "production",
            "JWT_SIGNING_SECRET": "a-real-random-jwt-secret-not-the-dev-default",
        })
        import agent_interface.identity as m
        with self.assertNoLogs("smb_broker.identity", level="ERROR"):
            importlib.reload(m)


class TestUnsubscribeFailsClosed(_GuardReloadTestCase):
    """agent_interface/unsubscribe.py -- LOG."""

    def test_unset_environment_with_dev_default_secret_logs_error(self):
        self._clear_and_set({})  # all three fallback vars unset -> _SECRET == _DEV_SECRET
        import agent_interface.unsubscribe as m
        with self.assertLogs("smb_broker.unsubscribe", level="ERROR") as cap:
            importlib.reload(m)
        self.assertTrue(any("UNSUBSCRIBE_SECRET" in line for line in cap.output))

    def test_explicit_development_with_dev_default_secret_does_not_log(self):
        self._clear_and_set({"ENVIRONMENT": "development"})
        import agent_interface.unsubscribe as m
        with self.assertNoLogs("smb_broker.unsubscribe", level="ERROR"):
            importlib.reload(m)

    def test_production_with_real_secret_does_not_log(self):
        self._clear_and_set({
            "ENVIRONMENT": "production",
            "UNSUBSCRIBE_SECRET": "a-real-random-unsub-secret-not-the-dev-default",
        })
        import agent_interface.unsubscribe as m
        with self.assertNoLogs("smb_broker.unsubscribe", level="ERROR"):
            importlib.reload(m)


class TestSharedHelperDirectly(unittest.TestCase):
    """core/env_guard.py's decision, tested directly (no reload needed --
    it reads the environment at call time, not at import time)."""

    def setUp(self):
        self._orig_environment = os.environ.get("ENVIRONMENT")

    def tearDown(self):
        if self._orig_environment is None:
            os.environ.pop("ENVIRONMENT", None)
        else:
            os.environ["ENVIRONMENT"] = self._orig_environment

    def test_unset_is_treated_as_production(self):
        os.environ.pop("ENVIRONMENT", None)
        from core.env_guard import is_production_for_security_guards
        self.assertTrue(is_production_for_security_guards())

    def test_unrecognised_value_is_treated_as_production(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "staging"}):
            from core.env_guard import is_production_for_security_guards
            self.assertTrue(is_production_for_security_guards())

    def test_development_is_the_only_escape_hatch(self):
        from core.env_guard import is_production_for_security_guards
        for value in ("development", "Development", "  development  ", "DEVELOPMENT"):
            with patch.dict(os.environ, {"ENVIRONMENT": value}):
                self.assertFalse(
                    is_production_for_security_guards(),
                    f"ENVIRONMENT={value!r} should have been treated as non-production",
                )

    def test_explicit_production_is_treated_as_production(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "production"}):
            from core.env_guard import is_production_for_security_guards
            self.assertTrue(is_production_for_security_guards())


if __name__ == "__main__":
    unittest.main()
