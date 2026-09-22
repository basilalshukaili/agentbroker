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

------------------------------------------------------------------------------
WIDENED 2026-09-21. This file's original scan below only ever matched ONE
hardcoded dotted path, `compliance.pre_check.pre_check`. It was written to
prove a single documented incident, not to generalise the rule it states in
its own docstring - so when the SAME bug class recurred somewhere else
entirely (`agent_interface/key_requests.py` does
`from agent_interface.key_request_logic import send_verification_email`;
`tests/unit/test_usage_telemetry.py::test_request_with_valid_email_returns_200`
patched `agent_interface.key_request_logic.send_verification_email`, the
definition site), this guard had nothing to say about it: the offending
string never contains the substring "compliance.pre_check.pre_check", so the
regex never fires. The mechanism was scope, not syntax - the old guard did
not fail to recognise `monkeypatch.setattr`/`patch` calls in general, it
simply never looked for any target other than the one it was built for.

The scan below generalises the rule: for EVERY module in the codebase, find
every OTHER module that does `from that_module import that_name` at import
time (eagerly - not inside a function, where the import re-runs on every call
and therefore always sees a live value). That produces the set of "fragile"
(module, name) pairs - the ones where a consumer has already taken its own
snapshot. Then it scans every patch-style call in tests/ (`patch(...)`,
`mock.patch(...)`, `patch.object(...)`, `monkeypatch.setattr(...)` in both its
string and object forms - the two styles actually used in this repo, by a
471-call-site survey, are `patch(...)` and `monkeypatch.setattr(...)`; the
others are supported for the same reason seatbelts exist in the back seat)
and flags any that target a fragile pair.

THIS OVER-FLAGS ON PURPOSE. A (module, name) pair can be fragile for ONE
consumer (an eager importer) while being perfectly safe to patch at the
definition site for ANOTHER (one that re-imports the name inside a function,
or accesses it through `import module` + `module.name`, both of which read
the live attribute every time). Knowing which consumer a given test actually
exercises is a call-graph question this static scan cannot answer, so instead
of guessing it flags every collision and requires a human-verified,
per-call-site ALLOWLIST entry with a stated reason for anything that turns
out to be the safe kind - see `_KNOWN_SAFE_COLLISIONS` below. Silently
passing an unrecognised collision was rejected on purpose: a gate that
resolves its own ambiguity in the direction of "pass" is the same failure
mode this file exists to catch.
"""
from __future__ import annotations

import ast
import pathlib
import re
from collections import defaultdict

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_THIS_FILE = pathlib.Path(__file__).resolve()

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
    audit-mirror write onto its own event loop.

    Kept alongside the general scan below as a narrow, specific regression
    test for the exact 2026-08-31 incident - the general scan already covers
    this pair too (compliance.pre_check has five eager importers), so this is
    belt-and-braces, not the only line of defence any more.
    """
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


# ==============================================================================
# GENERAL SCAN: any dead-stub-at-the-definition-site, anywhere in tests/.
# ==============================================================================
#
# The three pieces:
#   1. build_fragile_map(sources)   -- which (module, name) pairs are fragile
#   2. scan_patch_calls(sources)    -- every patch-style call site and its target
#   3. find_violations(...)         -- the intersection
#
# All three take {relpath: source_text} dicts rather than reading straight off
# disk, so the "prove it fails on the known-bad sample" test below can hand
# them a tiny two-file reconstruction instead of needing the bug to still be
# live in the real tree (it was fixed earlier today).

_EXCLUDED_DIR_NAMES = {
    "__pycache__", ".git", "node_modules",
    # Hard rule for this task: never touch, and for hygiene, never even
    # walk into, trees that belong to production deploys elsewhere in the
    # workspace. Neither exists under this repo, but excluding by name is
    # free insurance if this scan is ever pointed at a wider root.
    "web_hatchloop_v2", "tm_app_template",
}


def _module_dotted(relparts: tuple[str, ...]) -> str:
    parts = list(relparts)
    assert parts[-1].endswith(".py")
    parts[-1] = parts[-1][: -len(".py")]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _all_module_names(relpaths) -> set[str]:
    """Every dotted module AND package name derivable from a set of paths -
    used to recognise `from pkg import submodule` (a live module reference,
    immune to this bug) and exclude it from the fragile set."""
    names: set[str] = set()
    for rp in relpaths:
        dotted = _module_dotted(tuple(rp.split("/")))
        names.add(dotted)
        parts = dotted.split(".")
        for i in range(1, len(parts)):
            names.add(".".join(parts[:i]))
    return names


def _resolve_relative(node: ast.ImportFrom, consumer_module: str, is_package: bool) -> str | None:
    if node.level == 0:
        return node.module
    parts = consumer_module.split(".")
    base_parts = parts if is_package else parts[:-1]
    if node.level > 1:
        cut = len(base_parts) - (node.level - 1)
        base_parts = base_parts[: max(cut, 0)]
    base = ".".join(base_parts)
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base or None


class _EagerImportVisitor(ast.NodeVisitor):
    """Records `from X import Y` statements that execute at import time (i.e.
    are NOT inside a function/coroutine body, where they would instead
    re-resolve Y fresh on every call and therefore never go stale)."""

    def __init__(self, consumer_module, relpath, is_package, submodule_names, fragile):
        self.consumer_module = consumer_module
        self.relpath = relpath
        self.is_package = is_package
        self.submodule_names = submodule_names
        self.fragile = fragile
        self._func_depth = 0

    def visit_FunctionDef(self, node):
        self._func_depth += 1
        self.generic_visit(node)
        self._func_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ImportFrom(self, node):
        if self._func_depth == 0:
            module = _resolve_relative(node, self.consumer_module, self.is_package)
            if module:
                for alias in node.names:
                    name = alias.name
                    if name == "*":
                        continue
                    if f"{module}.{name}" in self.submodule_names:
                        continue  # binds a submodule reference, not a value snapshot
                    self.fragile[(module, name)].append(
                        (self.consumer_module, self.relpath, node.lineno, alias.asname or name)
                    )
        self.generic_visit(node)


def build_fragile_map(sources: dict[str, str]) -> dict[tuple[str, str], list[tuple]]:
    """sources: {relpath (forward slashes, from repo root): file text}.
    Returns {(defining_module, symbol): [(consumer_module, relpath, lineno, bound_as), ...]}.
    """
    submodule_names = _all_module_names(sources.keys())
    fragile: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for relpath, text in sources.items():
        try:
            tree = ast.parse(text, filename=relpath)
        except SyntaxError:
            continue
        mod = _module_dotted(tuple(relpath.split("/")))
        is_package = relpath.endswith("/__init__.py") or relpath == "__init__.py"
        _EagerImportVisitor(mod, relpath, is_package, submodule_names, fragile).visit(tree)
    return dict(fragile)


def _resolve_expr(node, alias_map):
    if isinstance(node, ast.Name):
        return alias_map.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve_expr(node.value, alias_map)
        return f"{base}.{node.attr}" if base else None
    return None


class _PatchCallVisitor(ast.NodeVisitor):
    """Finds patch()/mock.patch()/patch.object()/monkeypatch.setattr() call
    sites and, where the target is statically resolvable, the (module, attr)
    dotted pair being patched."""

    def __init__(self, relpath):
        self.relpath = relpath
        self.alias_map: dict[str, str] = {}
        self.patch_func_names: set[str] = set()
        self.findings: list[tuple[int, str, str, str, str]] = []  # lineno,style,module,attr,target

    def visit_Import(self, node):
        for alias in node.names:
            if alias.asname:
                self.alias_map[alias.asname] = alias.name
            else:
                top = alias.name.split(".")[0]
                self.alias_map[top] = top
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module in ("unittest.mock", "mock"):
            for alias in node.names:
                if alias.name == "patch":
                    self.patch_func_names.add(alias.asname or alias.name)
        if node.level == 0 and node.module:
            for alias in node.names:
                self.alias_map[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        self.generic_visit(node)

    def _first_str_const(self, args):
        if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
            return args[0].value
        return None

    def _record(self, lineno, style, target):
        module, sep, attr = target.rpartition(".")
        if sep:
            self.findings.append((lineno, style, module, attr, target))

    def visit_Call(self, node: ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in self.patch_func_names:
            s = self._first_str_const(node.args)
            if s:
                self._record(node.lineno, "patch()", s)
        elif isinstance(func, ast.Attribute):
            if func.attr == "object":
                inner = func.value
                is_patch_obj = (
                    (isinstance(inner, ast.Name) and inner.id in self.patch_func_names)
                    or (isinstance(inner, ast.Attribute) and inner.attr == "patch"
                        and _resolve_expr(inner.value, self.alias_map) in ("mock", "unittest.mock"))
                )
                if is_patch_obj and len(node.args) >= 2:
                    target_mod = _resolve_expr(node.args[0], self.alias_map)
                    attr_c = node.args[1]
                    if target_mod and isinstance(attr_c, ast.Constant) and isinstance(attr_c.value, str):
                        self.findings.append(
                            (node.lineno, "patch.object()", target_mod, attr_c.value,
                             f"{target_mod}.{attr_c.value}"))
            elif func.attr == "patch":
                if _resolve_expr(func.value, self.alias_map) in ("mock", "unittest.mock"):
                    s = self._first_str_const(node.args)
                    if s:
                        self._record(node.lineno, "mock.patch()", s)
            elif func.attr == "setattr":
                self._handle_setattr(node)
        self.generic_visit(node)

    def _handle_setattr(self, node: ast.Call):
        args = node.args
        if len(args) == 2:
            s = self._first_str_const(args)
            if s:
                self._record(node.lineno, "monkeypatch.setattr(str)", s)
        elif len(args) >= 3:
            target_expr, name_expr = args[0], args[1]
            if isinstance(name_expr, ast.Constant) and isinstance(name_expr.value, str):
                if isinstance(target_expr, ast.Constant) and isinstance(target_expr.value, str):
                    module = target_expr.value
                else:
                    module = _resolve_expr(target_expr, self.alias_map)
                if module:
                    self.findings.append(
                        (node.lineno, "monkeypatch.setattr(obj,name)", module, name_expr.value,
                         f"{module}.{name_expr.value}"))


def scan_patch_calls(sources: dict[str, str]) -> list[tuple[str, int, str, str, str, str]]:
    """Returns (relpath, lineno, style, module, attr, target) for every
    patch-style call site found."""
    out = []
    for relpath, text in sources.items():
        try:
            tree = ast.parse(text, filename=relpath)
        except SyntaxError:
            continue
        v = _PatchCallVisitor(relpath)
        v.visit(tree)
        for lineno, style, module, attr, target in v.findings:
            out.append((relpath, lineno, style, module, attr, target))
    return out


def find_violations(fragile_map, patch_findings):
    """Cross-reference: a patch call is a violation iff its (module, attr)
    target is a fragile pair (some OTHER module already snapshotted it via an
    eager `from module import attr`)."""
    violations = []
    for relpath, lineno, style, module, attr, target in patch_findings:
        key = (module, attr)
        if key in fragile_map:
            violations.append((relpath, lineno, style, target, fragile_map[key]))
    return violations


def _read_repo_sources(subdir: str, exclude_files: set[pathlib.Path] = frozenset()) -> dict[str, str]:
    root = _REPO / subdir
    out = {}
    for p in root.rglob("*.py"):
        rel_parts = p.relative_to(_REPO).parts
        if _EXCLUDED_DIR_NAMES & set(rel_parts):
            continue
        if p.resolve() in exclude_files:
            continue
        out[p.relative_to(_REPO).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
    return out


def _read_all_production_sources() -> dict[str, str]:
    """Every .py file in the repo EXCEPT tests/ - the whole surface a test
    could conceivably import from and eagerly-snapshot a name out of. A
    hand-picked directory list would silently go stale the day a new
    top-level package is added (or, as happened while building this scan,
    silently miss `web/` and `scripts/`, which do exist and do import
    things) - walking everything outside tests/ instead means the fragile
    map can only ever be too WIDE, never accidentally too narrow."""
    out = {}
    for p in _REPO.rglob("*.py"):
        rel_parts = p.relative_to(_REPO).parts
        if rel_parts[0] == "tests":
            continue
        if _EXCLUDED_DIR_NAMES & set(rel_parts):
            continue
        out[p.relative_to(_REPO).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
    return out


# ------------------------------------------------------------------------------
# PROOF: the widened scan actually fires on the known-bad shape.
#
# tests/unit/test_usage_telemetry.py::test_request_with_valid_email_returns_200
# used to do exactly this (fixed earlier today), so it can no longer be found
# live in the tree. Reconstructed here as a two-file fixture instead of
# trusted from memory - "a gate must be able to fail on demand".
# ------------------------------------------------------------------------------

def test_reconstructed_known_bad_sample_is_flagged():
    """Rebuilds the exact shape of the incident this file documents and
    proves the widened scan catches it. If this test cannot be made to fail
    the scan, the scan is not trustworthy."""
    consumer_source = (
        "from __future__ import annotations\n"
        "from agent_interface.key_request_logic import (\n"
        "    send_verification_email,\n"
        ")\n"
        "\n"
        "async def request_free_key(email):\n"
        "    sent = await send_verification_email(email, 'https://x/verify')\n"
        "    return sent\n"
    )
    definition_source = (
        "async def send_verification_email(email, verify_url):\n"
        "    return True\n"
    )
    sources = {
        "agent_interface/key_requests.py": consumer_source,
        "agent_interface/key_request_logic.py": definition_source,
    }
    known_bad_test_source = (
        "from unittest.mock import patch, AsyncMock\n"
        "\n"
        "def test_request_with_valid_email_returns_200():\n"
        "    with patch('agent_interface.key_request_logic.send_verification_email',\n"
        "               new=AsyncMock(return_value=True)):\n"
        "        pass\n"
    )
    test_sources = {"tests/unit/test_reconstructed_known_bad.py": known_bad_test_source}

    fragile = build_fragile_map(sources)
    assert ("agent_interface.key_request_logic", "send_verification_email") in fragile, (
        "the fixture itself is wrong: key_requests.py's eager import was not "
        "picked up as fragile")

    findings = scan_patch_calls(test_sources)
    assert findings, "the fixture itself is wrong: the patch() call was not found at all"

    violations = find_violations(fragile, findings)
    assert len(violations) == 1, (
        f"expected exactly one violation on the reconstructed known-bad sample, "
        f"got {violations!r} -- the widened guard does not reliably fail on the "
        f"exact incident it was built to catch")
    relpath, lineno, style, target, consumers = violations[0]
    assert target == "agent_interface.key_request_logic.send_verification_email"
    assert any(c[0] == "agent_interface.key_requests" for c in consumers)


def test_reconstructed_correct_fix_is_not_flagged():
    """The other half of the proof: patching the CALLER's binding (the actual
    fix applied to test_usage_telemetry.py today) must NOT be flagged, or the
    guard would be pushing everyone toward doing extra, needless plumbing."""
    consumer_source = (
        "from agent_interface.key_request_logic import send_verification_email\n"
        "\n"
        "async def request_free_key(email):\n"
        "    return await send_verification_email(email, 'https://x/verify')\n"
    )
    definition_source = "async def send_verification_email(email, verify_url):\n    return True\n"
    sources = {
        "agent_interface/key_requests.py": consumer_source,
        "agent_interface/key_request_logic.py": definition_source,
    }
    fixed_test_source = (
        "from unittest.mock import patch, AsyncMock\n"
        "\n"
        "def test_request_with_valid_email_returns_200():\n"
        "    with patch('agent_interface.key_requests.send_verification_email',\n"
        "               new=AsyncMock(return_value=True)):\n"
        "        pass\n"
    )
    test_sources = {"tests/unit/test_reconstructed_fixed.py": fixed_test_source}

    fragile = build_fragile_map(sources)
    findings = scan_patch_calls(test_sources)
    violations = find_violations(fragile, findings)
    assert violations == [], (
        "patching agent_interface.key_requests.send_verification_email (the "
        "caller's own binding) was incorrectly flagged as dead - false "
        "positive on the CORRECT pattern")


def test_reconstructed_import_module_style_is_not_flagged():
    """False-positive guard: a consumer that does `import module` and calls
    `module.thing()` resolves at call time, so patching the definition site
    IS correct there. Must not be flagged just because some unrelated module
    also does an eager `from X import Y` of the same name."""
    eager_consumer = "from pkg.defs import thing\n\ndef use():\n    return thing()\n"
    live_consumer = "import pkg.defs\n\ndef use2():\n    return pkg.defs.thing()\n"
    definition = "def thing():\n    return 1\n"
    sources = {
        "pkg/eager_consumer.py": eager_consumer,
        "pkg/live_consumer.py": live_consumer,
        "pkg/defs.py": definition,
    }
    # A test that only exercises live_consumer's access pattern, patching the
    # definition site by object (import pkg.defs as d; monkeypatch.setattr(d, "thing", ...)).
    test_source = (
        "import pkg.defs as d\n"
        "\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(d, 'thing', lambda: 2)\n"
    )
    fragile = build_fragile_map(sources)
    assert ("pkg.defs", "thing") in fragile  # eager_consumer makes it fragile in general
    findings = scan_patch_calls({"tests/unit/test_x.py": test_source})
    violations = find_violations(fragile, findings)
    # The scan is module-granularity, not call-graph precise: it WILL flag
    # this, because it cannot know the test never reaches eager_consumer.
    # This assertion documents that limitation rather than hiding it - see
    # _KNOWN_SAFE_COLLISIONS for how real instances of this shape are handled.
    assert len(violations) == 1


# ------------------------------------------------------------------------------
# THE REAL SCAN.
# ------------------------------------------------------------------------------
#
# _KNOWN_SAFE_COLLISIONS: (relpath, target) pairs where the general scan's
# module-granularity over-flags a patch that is, on manual trace of the
# specific test's own call path, genuinely correct. Each entry names the
# consumer actually exercised and the deferred (function-local) import that
# makes patching the definition site work for it. Keyed by exact target
# string (not just (module, attr)) and exact file, so a NEW, unrelated patch
# of the same symbol elsewhere is still caught.
#
# Every one of these was individually traced on 2026-09-21 while widening
# this guard - see the task's own report for the file:line evidence. None of
# them touch reliability/async_runner.py or core/schedule_appointment.py
# (owned by a concurrent agent this session) - only the TEST files' patch
# targets were inspected, never the production files.
_KNOWN_SAFE_COLLISIONS: dict[tuple[str, str], str] = {
    ("tests/unit/test_async_booking_defect.py", "supply.smb_directory.get_directory"):
        "targets reliability.async_runner.enqueue_booking, which does "
        "`from supply.smb_directory import get_directory` INSIDE the function body "
        "(async_runner.py:49) -- not core.schedule_appointment/find_business/"
        "capture_lead/verify_business's eager, module-level imports of the same name.",
    ("tests/unit/test_async_booking_defect.py", "channels.direct_api.calcom.CalComAdapter"):
        "targets reliability.async_runner.enqueue_booking, which does "
        "`from channels.direct_api.calcom import CalComAdapter` INSIDE the function "
        "body (async_runner.py:47) -- not core.schedule_appointment's eager import.",
    # Added for assignment #8 (booking-retry safety, async path, 2026-09-21).
    # Same fixture convention as test_async_booking_defect.py above, verbatim
    # (same deferred-import consumer, same reasoning) -- a separate file so
    # this fix's tests do not touch a file another agent may be editing.
    ("tests/unit/test_async_booking_retry_safety.py", "supply.smb_directory.get_directory"):
        "targets reliability.async_runner.enqueue_booking, which does "
        "`from supply.smb_directory import get_directory` INSIDE the function body "
        "(async_runner.py:49) -- not core.schedule_appointment/find_business/"
        "capture_lead/verify_business's eager, module-level imports of the same name.",
    ("tests/unit/test_async_booking_retry_safety.py", "channels.direct_api.calcom.CalComAdapter"):
        "targets reliability.async_runner.enqueue_booking, which does "
        "`from channels.direct_api.calcom import CalComAdapter` INSIDE the function "
        "body (async_runner.py:47) -- not core.schedule_appointment's eager import.",
    ("tests/unit/test_business_tier.py", "supply.smb_directory.get_directory"):
        "targets core.business_tier.resolve_tier, which does "
        "`from supply.smb_directory import get_directory` INSIDE the function body "
        "(business_tier.py:100).",
    ("tests/unit/test_honesty_fixes.py", "supply.smb_directory.get_directory"):
        "targets reliability.async_runner.enqueue_booking (deferred import); the "
        "test's own comment documents this exact reasoning.",
    ("tests/unit/test_schedule_appointment_ownership.py", "supply.smb_directory.get_directory"):
        "targets reliability.async_runner.enqueue_booking via .__wrapped__ (deferred import).",
    ("tests/unit/test_availability_upstream_error_honesty.py", "compliance.consent_store.get_consent_store"):
        "targets core.schedule_appointment's own booking-specific opt-out check, which "
        "does `from compliance.consent_store import get_consent_store` INSIDE the "
        "function body (schedule_appointment.py:206) -- deliberately separate from "
        "compliance.pre_check's eager import of the same name.",
    ("tests/unit/test_booking_confirmation_honesty.py", "compliance.consent_store.get_consent_store"):
        "targets core.schedule_appointment's deferred consent-store import (see above).",
    ("tests/unit/test_booking_honours_the_requested_time.py", "compliance.consent_store.get_consent_store"):
        "targets core.schedule_appointment's deferred consent-store import (see above).",
    ("tests/unit/test_booking_lands_on_the_right_calendar.py", "compliance.consent_store.get_consent_store"):
        "targets core.schedule_appointment's deferred consent-store import (see above).",
    ("tests/unit/test_booking_optout.py", "compliance.consent_store.get_consent_store"):
        "targets core.schedule_appointment's deferred consent-store import; this file's "
        "own docstring/fixture comment documents having already learned this exact "
        "lesson for get_directory and applies the same correct pattern here.",
    ("tests/unit/test_cancellation_authorization.py", "compliance.consent_store.get_consent_store"):
        "targets core.schedule_appointment's deferred consent-store import (see above).",
    ("tests/unit/test_demand_queue.py", "compliance.consent_store.get_consent_store"):
        "targets core.demand_queue, which does `from compliance.consent_store import "
        "get_consent_store` INSIDE the function body (demand_queue.py:221).",
    ("tests/unit/test_unsubscribe.py", "compliance.consent_store.get_consent_store"):
        "targets agent_interface.unsubscribe, which does `from compliance.consent_store "
        "import get_consent_store` INSIDE the function body (unsubscribe.py:123).",
    ("tests/unit/test_compliance_receipt.py", "storage.supabase_client.select_rows"):
        "targets core.screen_sanctions, which re-imports select_rows inside the "
        "function body -- not billing.credits.get_balance, the only eager consumer, "
        "which this file never calls or references.",
    ("tests/unit/test_conversation_ownership.py", "storage.supabase_client.select_rows"):
        "targets core.conversations (deferred import); no reference to get_balance or "
        "billing.credits anywhere in this file.",
    ("tests/unit/test_conversation_threading.py", "storage.supabase_client.select_rows"):
        "targets core.conversations / core.demand_shaping (both deferred imports); no "
        "reference to get_balance or billing.credits anywhere in this file.",
    ("tests/unit/test_data_metering.py", "storage.supabase_client.select_rows"):
        "targets billing.data_quota (deferred import); no reference to get_balance or "
        "billing.credits anywhere in this file.",
    ("tests/unit/test_demand_queue.py", "storage.supabase_client.select_rows"):
        "targets core.demand_queue (deferred import); no reference to get_balance or "
        "billing.credits anywhere in this file.",
    ("tests/unit/test_idempotency_dispatch.py", "storage.supabase_client.select_rows"):
        "targets agent_interface.idempotency_gate (deferred import), patched via its "
        "own `import storage.supabase_client as sb` module reference.",
    ("tests/unit/test_polar_webhook.py", "storage.supabase_client.select_rows"):
        "targets billing.polar_webhook's idempotency check (deferred import); no "
        "reference to get_balance or billing.credits anywhere in this file.",
    ("tests/unit/test_portal.py", "storage.supabase_client.select_rows"):
        "targets agent_interface.portal (deferred import); no reference to get_balance "
        "in this file (it DOES call billing.credits._maybe_low_balance_nudge, which "
        "has its own separate deferred re-import of select_rows at credits.py:222, "
        "distinct from the frozen module-level one get_balance uses at credits.py:269).",
    ("tests/unit/test_quota_hang.py", "storage.supabase_client.select_rows"):
        "targets billing.data_quota (deferred import); no reference to get_balance or "
        "billing.credits anywhere in this file.",
    ("tests/unit/test_schedule_appointment_ownership.py", "storage.supabase_client.select_rows"):
        "patches storage.supabase_client's insert_row/upsert_row/select_rows together "
        "via its own `import storage.supabase_client as real` module reference, "
        "exercising the FastAPI dispatch path's various deferred-import consumers, not "
        "billing.credits.get_balance.",
    ("tests/unit/test_screening_status_shape.py", "storage.supabase_client.select_rows"):
        "targets core.screen_sanctions (deferred import), patched via its own "
        "`import storage.supabase_client as sb` module reference.",
    ("tests/unit/test_shaping_degraded.py", "storage.supabase_client.select_rows"):
        "targets core.demand_shaping (deferred import); no reference to get_balance or "
        "billing.credits anywhere in this file.",
    ("tests/unit/test_credits_slice3_4.py", "agent_interface.identity.issue_subscription_token"):
        "targets billing.polar_webhook.handle_polar_event, which does "
        "`from agent_interface.identity import issue_subscription_token` INSIDE the "
        "function body (polar_webhook.py:370) -- not main.py's eager import.",
    ("tests/unit/test_polar_webhook.py", "agent_interface.identity.issue_subscription_token"):
        "targets billing.polar_webhook.handle_polar_event (deferred import, see above).",
    ("tests/unit/test_typed_errors.py", "agent_interface.identity.validate_token"):
        "targets agent_interface.mcp_server's tool-dispatch auth check, which "
        "re-imports validate_token INSIDE the function body at each of its call sites "
        "(mcp_server.py:667, 1175, 1226, 1400) -- not main.py's eager import.",
    ("tests/unit/test_typed_errors.py", "agent_interface.key_request_logic.consume_free_daily"):
        "targets agent_interface.mcp_server's free-quota check, which does "
        "`from agent_interface.key_request_logic import is_free_key, "
        "consume_free_daily, get_free_daily_remaining` INSIDE the function body "
        "(mcp_server.py:1225) -- not agent_interface.key_requests's eager, "
        "backward-compat re-export of the same names.",
    ("tests/unit/test_typed_errors.py", "agent_interface.key_request_logic.get_free_daily_remaining"):
        "targets agent_interface.mcp_server's free-quota check (deferred import, see above).",
    # Added for assignment #8 (booking-retry safety, 2026-09-21). Both new
    # files reuse the exact `_wired` fixture convention already established
    # (and already allowlisted above) for test_booking_confirmation_honesty.py
    # / test_cancellation_authorization.py / test_booking_optout.py etc.
    ("tests/unit/test_booking_retry_safety.py", "compliance.consent_store.get_consent_store"):
        "targets core.schedule_appointment's own booking-specific opt-out check, which "
        "does `from compliance.consent_store import get_consent_store` INSIDE the "
        "function body (schedule_appointment.py:206ish) -- deliberately separate from "
        "compliance.pre_check's eager import of the same name.",
    ("tests/unit/test_outcome_durability.py", "compliance.consent_store.get_consent_store"):
        "targets core.schedule_appointment's deferred consent-store import (see above).",
    # test_outcome_durability.py patches storage.supabase_client.upsert_row via
    # its own `import storage.supabase_client as sb` module reference,
    # exercising storage/outcome_store.py's _supabase_upsert (deferred import
    # INSIDE the function body). It used to also patch `select_rows` here for
    # the fresh-process readback tests, which needed an entry below because
    # billing.credits.py imports select_rows eagerly; the 2026-09-21
    # unavailable-vs-absent fix moved outcome_store.py's _supabase_fetch onto
    # `select_rows_strict` (never imported eagerly anywhere, so patching it is
    # not a fragile collision and needs no entry), and this file's readback
    # tests were updated to patch that instead -- see
    # tests/unit/test_outcome_store_unavailable_vs_absent.py for the new tests
    # this fix added, which patch select_rows_strict the same way.
    # Added while converting the sanctions-honesty tests that used to skip
    # for "no database config" (task: make them run against a fake instead).
    # test_country_never_removes_a_match.py patches both select_rows_strict
    # and select_rows via its own `import storage.supabase_client as sb`
    # reference (see `_seed_fake_index`), exercising core.screen_sanctions'
    # `_screen_list_db` and `_list_refreshed_at` -- BOTH of which do their own
    # deferred `from storage.supabase_client import select_rows,
    # select_rows_strict, SupabaseUnavailable` INSIDE the function body
    # (screen_sanctions.py, inside _screen_list_db and _list_refreshed_at) --
    # not billing.credits.get_balance, the only eager consumer of
    # select_rows, which this file never calls or references. Same reasoning
    # already accepted for test_outcome_durability.py above.
    ("tests/unit/test_country_never_removes_a_match.py", "storage.supabase_client.select_rows"):
        "targets core.screen_sanctions._screen_list_db's deferred select_rows import "
        "(the empty-index probe fallback), exercised only through "
        "handle_screen_sanctions/_screen_list_db; no reference to get_balance or "
        "billing.credits anywhere in this file.",
    # Added for board row 206 (2026-09-22): storage/outcome_store.py's
    # _supabase_fetch / _supabase_upsert / _supabase_fetch_by_appointment_id
    # now call `rpc()` -- routed through the operations_* SECURITY DEFINER
    # RPCs (sql/agentbroker/001_operations_security_definer_rpc.sql) instead
    # of the raw `operations` table, because this service deploys with only
    # the Supabase anon key (no service-role key on a public box). Every one
    # of outcome_store.py's three call sites does
    # `from storage.supabase_client import rpc` INSIDE the function body --
    # a deferred import, exactly like select_rows's existing entries above.
    # billing/credits.py is the only EAGER consumer of this name
    # (`from storage.supabase_client import rpc, select_rows` at
    # credits.py:35, used only by the credits billing rail's
    # credit_reserve/credit_commit/credit_release/credit_grant calls, gated
    # behind a funded credit_accounts row via run_metered_tool). None of the
    # five files below authenticate a funded credit account or otherwise
    # drive that rail -- traced 2026-09-22 while shipping the honesty fix
    # this board row required.
    ("tests/unit/test_outcome_store_unavailable_vs_absent.py", "storage.supabase_client.rpc"):
        "targets storage.outcome_store._supabase_fetch's deferred rpc import; this "
        "file never references billing.credits or run_metered_tool.",
    ("tests/unit/test_outcome_durability.py", "storage.supabase_client.rpc"):
        "targets storage.outcome_store._supabase_fetch/_supabase_upsert's deferred rpc "
        "import, driven via core.schedule_appointment.handle_schedule_appointment; no "
        "agent token in this file is funded with credits, and CREDITS_ENABLED is not "
        "set in the test environment, so run_metered_tool's credits branch never runs.",
    ("tests/unit/test_core_operations.py", "storage.supabase_client.rpc"):
        "targets storage.outcome_store._supabase_fetch's deferred rpc import via "
        "core.status_outcome.handle_get_status/handle_get_outcome directly; no "
        "reference to billing.credits or run_metered_tool in this file.",
    ("tests/unit/test_fixes_2026_08_23.py", "storage.supabase_client.rpc"):
        "targets storage.outcome_store._supabase_fetch's deferred rpc import via "
        "core.status_outcome.handle_get_status/handle_get_outcome directly; no "
        "reference to billing.credits or run_metered_tool in this file.",
    ("tests/unit/test_schedule_appointment_ownership.py", "storage.supabase_client.rpc"):
        "targets storage.outcome_store's three deferred rpc imports via the real REST "
        "routes / MCP dispatcher / Celery task body this file drives; the FakeSB.rpc "
        "method it installs only ever serves the operations_* functions those three "
        "call, and no test in this file authenticates a funded credit account.",
    ("tests/unit/test_operations_rpc_boundary.py", "storage.supabase_client.rpc"):
        "the boundary test for board row 206 item 1 itself: drives "
        "storage.outcome_store.OutcomeStore directly (set_complete_durable/"
        "get_async), never billing.credits or run_metered_tool. Its own "
        "_forbid_raw_table_access fixture additionally patches select_rows/"
        "select_rows_strict/upsert_row/insert_row to raise if called at all, which "
        "would itself surface a real regression into billing.credits' eager import "
        "the moment that path was exercised -- it is not.",
}


def test_known_safe_collisions_are_still_present():
    """Hygiene check on the allowlist itself (the 'gate that inspects
    nothing' failure mode, applied to an allowlist instead of a scan): every
    entry must still correspond to a real finding. If code changes make an
    entry stop matching, it is dead weight that could be hiding something
    else by coincidence later - it must be re-verified and updated, not left
    in place."""
    test_sources = _read_repo_sources("tests", exclude_files={_THIS_FILE})
    findings = scan_patch_calls(test_sources)
    found_pairs = {(relpath, target) for relpath, _, _, _, _, target in findings}
    missing = set(_KNOWN_SAFE_COLLISIONS) - found_pairs
    assert not missing, (
        "these _KNOWN_SAFE_COLLISIONS entries no longer match any patch call site - "
        "the code moved or changed underneath them; re-verify and update the "
        "allowlist instead of leaving stale entries in place:\n  "
        + "\n  ".join(f"{f}: {t}" for f, t in sorted(missing)))


def test_no_unallowlisted_dead_stub_patches_in_tests():
    """The general scan. Every patch-style call in tests/ that targets a
    fragile (module, symbol) pair must either be fixed or have a
    human-verified, justified entry in _KNOWN_SAFE_COLLISIONS above."""
    source_sources = _read_all_production_sources()
    fragile = build_fragile_map(source_sources)

    test_sources = _read_repo_sources("tests", exclude_files={_THIS_FILE})
    findings = scan_patch_calls(test_sources)
    violations = find_violations(fragile, findings)

    unexplained = [
        v for v in violations
        if (v[0], v[3]) not in _KNOWN_SAFE_COLLISIONS
    ]
    if unexplained:
        lines = []
        for relpath, lineno, style, target, consumers in unexplained:
            lines.append(f"{relpath}:{lineno}  [{style}]  patches '{target}'")
            for cmod, cfile, clineno, bound_as in consumers:
                lines.append(
                    f"    but {cfile}:{clineno} imports it eagerly as `{bound_as}` "
                    f"(module {cmod}) -- patching the definition site never reaches it")
        pytest.fail(
            "Found patch-style call(s) targeting a definition site that some "
            "consumer already snapshotted via an eager `from module import name` - "
            "the stub is dead for that consumer. Either patch the consumer's own "
            "binding instead, or if this is a verified-safe collision (the test "
            "actually exercises a DIFFERENT, deferred-import consumer of the same "
            "name), add a justified entry to _KNOWN_SAFE_COLLISIONS:\n\n"
            + "\n".join(lines))
