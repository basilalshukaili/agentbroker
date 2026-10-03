"""Only billing/switches.py may read CREDITS_ENABLED and DATA_METERING_ENABLED.

WHY THIS IS A TEST. The point of billing/switches.py is that "the gate runs" and "we say it runs"
are the SAME expression. That is only true while nothing else reads the variable. Before this
file there were seven independent readers (four in agent_interface/mcp_server.py, one each in
billing/polar_webhook.py, core/preview_cost.py and main.py) plus two dead constants in config.py
with a different default path, and the discovery descriptor read none of them - it asserted
"active" and rails=["credits"] as literals.

A new reader would not break anything today. It would quietly become the next place that can
disagree with the descriptor, so it fails here instead.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The trees that execute inside the container (the same cut scripts/check_deploy_env.py makes).
_SKIP_DIRS = {"tests", "scripts", "migrations", "deploy", "docs", "edge", "node_modules", ".git",
              "__pycache__", "state", "reports", "obsidian-vault"}
_OWNER = os.path.join("billing", "switches.py")
_NAMES = {"CREDITS_ENABLED", "DATA_METERING_ENABLED"}


def _runtime_sources():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def test_nothing_but_the_switches_module_names_the_two_variables():
    offenders = []
    scanned = 0
    for path in _runtime_sources():
        rel = os.path.relpath(path, ROOT)
        if rel == _OWNER:
            continue
        scanned += 1
        with open(path, encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read(), filename=rel)
        for node in ast.walk(tree):
            # An EXACT string equal to the variable name is a read (os.getenv("X"), environ["X"],
            # _env_bool("X")). Docstrings and comments contain the name inside longer text.
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in _NAMES:
                offenders.append(f"{rel}:{node.lineno} reads {node.value} directly")
    assert scanned > 40, "the scan found almost nothing; it would pass vacuously"
    assert offenders == [], (
        "read the switch through billing.switches (credits_enabled / data_metering_enabled), "
        "so the gate and the advertisement cannot disagree:\n  " + "\n  ".join(offenders))


def test_the_switches_module_exports_the_names_the_deploy_checker_stages():
    """scripts/check_deploy_env.py (parent repo) stages these three; this is the list it must match."""
    from billing import switches
    assert set(switches.SWITCH_VARS) == {"X402_ENABLED", "CREDITS_ENABLED", "DATA_METERING_ENABLED"}
    assert switches.CREDITS_VAR in _NAMES and switches.DATA_METERING_VAR in _NAMES
