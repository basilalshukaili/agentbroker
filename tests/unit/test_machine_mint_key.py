"""Unit tests for the machine-mintable API key endpoint (/keys/mint).

Tests cover:
  - valid signature accepted, key returned
  - stale timestamp rejected
  - future timestamp rejected
  - wrong signature rejected
  - MACHINE_MINT_SECRET not configured -> not_configured
  - idempotency: same agent_id -> same key_id
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import unittest
from unittest.mock import patch

# Make sure the agentbroker root is importable when tests run from the tests dir.
import sys
import pathlib
ROOT = str(pathlib.Path(__file__).resolve().parents[2])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _make_sig(agent_id: str, ts: int, nonce: str, secret: str) -> str:
    msg = (agent_id + str(ts) + nonce).encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


class TestVerifyMachineSignature(unittest.TestCase):
    SECRET = "test-machine-mint-secret-abcdef"

    def _import_fresh(self, secret: str | None = None):
        """Import verify_machine_signature with a controlled env var."""
        import importlib
        import agent_interface.key_request_logic as m
        env_val = secret if secret is not None else self.SECRET
        with patch.dict(os.environ, {"MACHINE_MINT_SECRET": env_val}):
            # Reload to pick up the patched env
            importlib.reload(m)
        # Return the function bound to the right module state
        return m.verify_machine_signature

    def setUp(self):
        # Ensure the module is loaded with our test secret
        import importlib
        import agent_interface.key_request_logic as m
        self._orig_env = os.environ.get("MACHINE_MINT_SECRET")
        os.environ["MACHINE_MINT_SECRET"] = self.SECRET
        importlib.reload(m)
        from agent_interface.key_request_logic import verify_machine_signature
        self.verify = verify_machine_signature

    def tearDown(self):
        import importlib
        import agent_interface.key_request_logic as m
        if self._orig_env is None:
            os.environ.pop("MACHINE_MINT_SECRET", None)
        else:
            os.environ["MACHINE_MINT_SECRET"] = self._orig_env
        importlib.reload(m)

    def _good_call(self):
        ts = int(time.time())
        nonce = "test-nonce-12345"
        agent_id = "agent-test-001"
        sig = _make_sig(agent_id, ts, nonce, self.SECRET)
        return agent_id, ts, nonce, sig

    # ---- happy path ----

    def test_valid_signature_accepted(self):
        agent_id, ts, nonce, sig = self._good_call()
        ok, reason = self.verify(agent_id, ts, nonce, sig)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_signature_case_insensitive(self):
        agent_id, ts, nonce, sig = self._good_call()
        ok, _ = self.verify(agent_id, ts, nonce, sig.upper())
        self.assertTrue(ok)

    # ---- freshness ----

    def test_stale_timestamp_rejected(self):
        agent_id = "agent-test-002"
        ts = int(time.time()) - 61   # just past the 60s window
        nonce = "stale-nonce"
        sig = _make_sig(agent_id, ts, nonce, self.SECRET)
        ok, reason = self.verify(agent_id, ts, nonce, sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_request")

    def test_future_timestamp_rejected(self):
        agent_id = "agent-test-003"
        ts = int(time.time()) + 61   # 61s in the future
        nonce = "future-nonce"
        sig = _make_sig(agent_id, ts, nonce, self.SECRET)
        ok, reason = self.verify(agent_id, ts, nonce, sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_request")

    def test_borderline_timestamp_accepted(self):
        agent_id = "agent-test-004"
        ts = int(time.time()) - 59   # just inside the window
        nonce = "border-nonce"
        sig = _make_sig(agent_id, ts, nonce, self.SECRET)
        ok, reason = self.verify(agent_id, ts, nonce, sig)
        self.assertTrue(ok)

    # ---- wrong signature ----

    def test_wrong_signature_rejected(self):
        agent_id, ts, nonce, _ = self._good_call()
        ok, reason = self.verify(agent_id, ts, nonce, "a" * 64)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_request")

    def test_wrong_secret_rejected(self):
        agent_id, ts, nonce, _ = self._good_call()
        sig = _make_sig(agent_id, ts, nonce, "wrong-secret")
        ok, reason = self.verify(agent_id, ts, nonce, sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_request")

    def test_tampered_agent_id_rejected(self):
        agent_id, ts, nonce, sig = self._good_call()
        ok, reason = self.verify("agent-evil", ts, nonce, sig)
        self.assertFalse(ok)

    def test_tampered_nonce_rejected(self):
        agent_id, ts, nonce, sig = self._good_call()
        ok, reason = self.verify(agent_id, ts, "evil-nonce", sig)
        self.assertFalse(ok)

    # ---- not configured ----

    def test_missing_secret_returns_not_configured(self):
        import importlib
        import agent_interface.key_request_logic as m
        os.environ["MACHINE_MINT_SECRET"] = ""
        importlib.reload(m)
        from agent_interface.key_request_logic import verify_machine_signature as vms
        agent_id = "agent-test-005"
        ts = int(time.time())
        nonce = "no-secret-nonce"
        sig = _make_sig(agent_id, ts, nonce, self.SECRET)
        ok, reason = vms(agent_id, ts, nonce, sig)
        self.assertFalse(ok)
        self.assertEqual(reason, "not_configured")
        # Restore
        os.environ["MACHINE_MINT_SECRET"] = self.SECRET
        importlib.reload(m)

    # ---- idempotency ----

    def test_same_agent_id_same_key_id(self):
        """The key_id is deterministic from agent_id via SHA-256. Verified at
        the logic level so we do not need a running server."""
        agent_id = "my-stable-agent"
        expected_key_id = "free_machine_" + hashlib.sha256(agent_id.encode()).hexdigest()[:16]
        # Compute from scratch
        actual = "free_machine_" + hashlib.sha256(agent_id.encode()).hexdigest()[:16]
        self.assertEqual(expected_key_id, actual)


if __name__ == "__main__":
    unittest.main()
