"""
jev_advisory — an ADDITIONAL, ADVISORY restricted-category read for the
`check_compliance` PREVIEW tool only.

WHY THIS EXISTS (36-agent audit, 2026-09-21): jev caught 10/10 adversarial
restricted-category positives (Arabic gambling/lending/cannabis, Cyrillic
homoglyphs, letters spaced apart, and it resisted two prompt-injection
attempts) against 4/10 for `compliance.content_classifier`'s regex. That is
the largest measured recall delta found anywhere in the audit, so it is
adopted here — and ONLY here.

UNION MODE — the whole safety design:
  * jev can only ADD a block, never remove one. The regex classifier stays
    the deterministic offline floor; nothing in this module can turn a
    regex "blocked" into "clean".
  * If jev fails in ANY way — non-zero/unexpected exit, timeout, empty
    stdout, unparseable JSON — this returns `available=False` and the
    caller MUST keep the deterministic verdict unchanged. An outage
    returns the system to exactly today's behaviour, which is the only
    reason union mode is safe.
  * jev's verdict is NOT stable near its decision threshold (measured:
    3 BLOCK / 21 ALLOW on 24 byte-identical runs of a borderline sample).
    That is disqualifying for an authorization point but tolerable for an
    advisory add-only layer in a preview tool: the cost of the instability
    is an occasional extra preview caution, never a random refusal of a
    real send.

HARD BOUNDARY — where this must NOT be called from:
  * `compliance.pre_check` (the authorization gate `send_message` and
    `call_business` run before dispatching) — an unreproducible flip there
    would refuse a paying customer at random with no way to explain why.
  * `core.send_message` or any other dispatch path.
  * The founder-comms path, any scheduled task, or the general pytest
    suite (jev is a live subprocess that makes a real billed network call
    on every invocation — see `tests/compliance_tests/test_jev_union_advisory.py`
    for how the live proof tests are gated off by default).
  * `core.check_compliance` is the ONLY caller. If that ever changes, re-read
    this docstring first.

BINARY RESOLUTION (board row 282, 2026-09-22 — read before touching this):
  * The first version of this module hardcoded a laptop-only absolute path
    (`C:/Users/basil/.claude/skills/jev/scripts/jev`) as the ONLY way to
    invoke jev. That path exists on this one machine and nowhere else —
    not the VPS, not the container — so jev was permanently inert in
    production from the moment this shipped, and because every failure
    (including "file does not exist") is caught and folded into the same
    fail-safe `available=False`, nothing anywhere said so. See
    `resolve_jev_binary()` for the fix: `JEV_BIN` env var, then PATH via
    `shutil.which("jev")`, then this laptop's original absolute path as a
    last-resort fallback — never the primary again.
  * `PYTHONIOENCODING=utf-8` is REQUIRED on Windows or jev crashes on its own
    Chinese-language diagnostic output. When resolution falls through to the
    dev fallback script it is invoked by absolute path via `python <script>`
    and this env var is set explicitly for that call; a `jev`/`jev.cmd`
    resolved off PATH is expected to set it itself (this laptop's
    `jev.cmd` wrapper does).
  * The model is PINNED to `jev-1.13` (the CLI normalizes this to the
    three-segment `jev-1.13.0` the native API requires). The default is a
    moving alias (`jev-latest`) that would silently change every verdict on
    an alias bump.
  * `--timeout 5 --retries 0`. Defaults are 60s x 3 retries (~3 minutes per
    call) — enough to wedge anything that calls this synchronously.
  * jev's own stderr is decoded with `errors="replace"`: it emits Chinese
    text on failure and a strict cp1252/ascii reader crashes on it.
  * Exit code 2, a timeout, empty stdout, or unparseable JSON are all
    treated as NO VERDICT (`available=False`) — never coerced to "false"
    (i.e. never treated as "not restricted").
  * The question wording is copied VERBATIM from the audit. jev reads
    conditions literally and does not infer intent — do not paraphrase it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Invocation constants — see module docstring; do not change without re-
# reading the audit that measured them.
# ---------------------------------------------------------------------------

# Explicit operator override, checked FIRST by resolve_jev_binary(). Point
# this at wherever jev actually lives on a given host (VPS, container) when
# it is not on PATH.
JEV_BIN_ENV_VAR = "JEV_BIN"

# The ORIGINAL hardcoded path (board row 282). Kept ONLY as a last-resort
# fallback for this one laptop's dev layout — see resolve_jev_binary(). Never
# treat this as primary again: it is the exact thing that made jev
# permanently, silently inert everywhere else.
_DEV_FALLBACK_SCRIPT = r"C:/Users/basil/.claude/skills/jev/scripts/jev"
JEV_MODEL = "jev-1.13"
JEV_THRESHOLD = 0.5
JEV_TIMEOUT_S = 5          # passed to jev's own --timeout
JEV_RETRIES = 0
# Our own ceiling, strictly above jev's --timeout, so a process that ignores
# its own timeout flag (or hangs in Python startup on a loaded machine)
# cannot wedge the caller either. This is a second, independent bound on top
# of jev's own, not a replacement for it.
SUBPROCESS_TIMEOUT_S = 12

# Verbatim from the audit (C:\Users\basil\AppData\Local\Temp\claude\
# C--TechMate\68f62d84-9d33-4f83-8472-aebc6f96fabd\tasks\wx1m39lqm.output).
# jev reads conditions literally — do not reword any of these three strings.
QUESTION = (
    "Does this outbound message promote a restricted category: gambling or "
    "betting, payday or high-interest lending, cannabis or controlled "
    "substances, adult or sexual services, weapons, or prescription drugs "
    "sold without a prescription?"
)
TRUE_CRITERIA = (
    "it advertises, solicits for, or drives signups to any of those "
    "categories, in any language or script, including when letters are "
    "spaced apart or replaced with lookalike characters"
)
FALSE_CRITERIA = (
    "ordinary commercial or transactional content (appointments, reminders, "
    "receipts, deliveries, support replies, marketing for unrestricted "
    "goods), a passing mention with no promotion, or text quoted from "
    "someone else"
)

# Measured, not advertised (audit, 2026-09-21): jev's own --verbose reports
# the advertised $0.000012/call estimate, which the audit found to be 6.8x
# too low against real token counts. Budget the measured figure everywhere
# a cost projection is made — see scripts/system_health.py:check_jev_budget.
MEASURED_COST_PER_CALL_USD = 0.000084

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# Deliberately OUTSIDE the agentbroker git repo (agentbroker/.git is pushed to
# github.com/basilalshukaili/agentbroker) — this is local operational
# telemetry, not product source, matching where AGENTBROKER_DEPLOY_STATE_FILE
# already lives in scripts/system_health.py.
USAGE_STATE_FILE = os.path.join(_ROOT, "state", "jev_usage.json")


def _under_test() -> bool:
    """Same gate as src/call_mode.py's `_under_test` (HatchLoop's existing
    "tests must not touch production" convention) — reused here rather than
    reinvented, and for the identical reason: jev is a live subprocess that
    makes a real billed network call on every invocation. Without this, the
    EXISTING `tests/unit/test_check_compliance.py` suite would silently
    start making real jev calls on every routine `pytest` run the moment
    core/check_compliance.py started calling this module, which is exactly
    the hard rule this adoption must not violate ("no jev near any test
    assertion" / tests-must-not-touch-production).

    `HATCHLOOP_ALLOW_LIVE=1` opts back in — used only by the gated live
    proof tests in tests/compliance_tests/test_jev_union_advisory.py.
    """
    if os.environ.get("HATCHLOOP_ALLOW_LIVE") == "1":
        return False
    return ("pytest" in sys.modules or "unittest" in sys.modules
            or bool(os.environ.get("PYTEST_CURRENT_TEST")))


def resolve_jev_binary() -> Tuple[Optional[List[str]], Optional[str]]:
    """Resolve the argv PREFIX used to invoke jev, and why, in priority order.

    Board row 282: the original code hardcoded step 3 below as the ONLY
    step, which made jev permanently inert on any machine but this laptop,
    with no visible sign of it anywhere (see the module docstring's "BINARY
    RESOLUTION" section). This function is the fix's foundation, so it is
    deliberately:
      * PUBLIC (no leading underscore) — scripts/system_health.py imports
        it directly for a standalone availability check, and must not have
        to duplicate this logic to do so (that duplication is exactly how
        the ORIGINAL hardcoded path also ended up copy-pasted into
        scripts/system_health.py's own `jev auth check` probe).
      * PURE PATH/ENV RESOLUTION — no subprocess, no network, never raises.
        Safe to call from a health check, or under plain pytest with no
        HATCHLOOP_ALLOW_LIVE, with zero cost and zero side effects.

    Order:
      1. `JEV_BIN` env var — an explicit operator override. Checked against
         the filesystem (and, for convenience, PATH) so a typo'd env var
         fails LOUD with a specific reason rather than silently falling
         through to a machine-specific default.
      2. `shutil.which("jev")` — ordinary PATH resolution. Zero-config on
         any host with a `jev` launcher on PATH — e.g. this laptop's
         `jev.cmd` wrapper (`C:\\Users\\basil\\AppData\\Local\\Python\\bin\\
         jev.cmd`), which already sets PYTHONIOENCODING itself and shells
         to the skill script — resolved binaries are invoked directly, no
         `python` prefix added here.
      3. The original hardcoded dev script path, run via `python <script>`
         the way it always was — kept ONLY as a last-resort fallback for
         this one machine's layout when neither of the above is
         configured. Never treated as primary again.

    Returns `(argv_prefix, None)` on success, or `(None, reason)` if
    nothing resolves. `reason` always contains the substring
    "binary not found" (see core/check_compliance.py's
    `_JEV_UNAVAILABLE_REASONS`, which classifies on it) so this failure
    mode is reported distinctly from a transient call failure — that
    distinction, previously impossible to make from the outside, is the
    point of this fix.
    """
    env_bin = os.environ.get(JEV_BIN_ENV_VAR)
    if env_bin:
        if os.path.isfile(env_bin):
            return [env_bin], None
        which_env_bin = shutil.which(env_bin)
        if which_env_bin:
            return [which_env_bin], None
        return None, (
            f"jev binary not found: {JEV_BIN_ENV_VAR}={env_bin!r} does not "
            "exist and is not resolvable on PATH"
        )

    which_bin = shutil.which("jev")
    if which_bin:
        return [which_bin], None

    if os.path.isfile(_DEV_FALLBACK_SCRIPT):
        return ["python", _DEV_FALLBACK_SCRIPT], None

    return None, (
        f"jev binary not found: set {JEV_BIN_ENV_VAR}, put `jev` on PATH, "
        "or run on a host with the dev fallback script present"
    )


@dataclass(frozen=True)
class JevAdvisory:
    """available=False means NO VERDICT: the caller MUST ignore `blocked`
    (it is None) and keep the deterministic regex verdict unchanged.

    `error` is DIAGNOSTIC ONLY, for logs. On several failure paths it is
    built from jev's raw stdout/stderr (an exit-code branch, an empty-stdout
    branch, an unparseable-JSON branch each fold in up to a few hundred
    characters of it), and jev's own error formatter builds that text from
    an upstream HTTP response body a validation API can construct by echoing
    back the request content. Treat `error` as untrusted, third-party text:
    never interpolate it into a field of a tool result that isn't fenced by
    core/untrusted.py (check_compliance, the only caller, is listed in
    core.untrusted.NO_THIRD_PARTY_TEXT and so fences nothing - see
    core/check_compliance.py's `_jev_unavailable_note` for the closed
    vocabulary that field is built from instead, and
    tests/unit/test_check_compliance_jev_note_no_leak.py for the pin)."""
    available: bool
    blocked: Optional[bool]
    probability: Optional[float]
    error: Optional[str]


def _record_usage(succeeded: bool) -> None:
    """Best-effort local call-count ledger against the founder's funded
    budget (see check_jev_budget in scripts/system_health.py).

    jev's TypeSafe provider has no balance endpoint — `jev auth check` only
    confirms key validity and lists usable model names (verified 2026-09-21;
    balance/spend-limit only exists on the OpenRouter provider table). So
    this is the substitute signal: track OUR OWN spend estimate, using the
    MEASURED per-call cost, and let system_health warn before a prepaid key
    could run dry the way the DeepSeek key did on 2026-09-12 with nothing
    saying so.

    Never raises. A lost increment from a races write is an acceptable loss
    for a monitoring aid; a raised exception reaching a preview tool over a
    counter file is not.
    """
    try:
        os.makedirs(os.path.dirname(USAGE_STATE_FILE), exist_ok=True)
        usage = {}
        if os.path.exists(USAGE_STATE_FILE):
            try:
                with open(USAGE_STATE_FILE, encoding="utf-8") as fh:
                    usage = json.load(fh)
            except Exception:  # noqa: BLE001 - corrupt ledger, start fresh
                usage = {}
        lifetime_calls = int(usage.get("lifetime_calls", 0)) + 1
        lifetime_cost = float(usage.get("lifetime_cost_usd_estimate", 0.0))
        if succeeded:
            lifetime_cost += MEASURED_COST_PER_CALL_USD
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        by_day = usage.get("by_day", {})
        by_day[day] = int(by_day.get(day, 0)) + 1
        usage.update({
            "lifetime_calls": lifetime_calls,
            "lifetime_cost_usd_estimate": round(lifetime_cost, 6),
            "by_day": by_day,
            "updated": datetime.now(timezone.utc).isoformat(),
        })
        tmp = USAGE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(usage, fh, indent=2)
        os.replace(tmp, USAGE_STATE_FILE)
    except Exception:  # noqa: BLE001 - a monitoring aid must never break the caller
        pass


def get_restricted_category_advisory(
    content: str,
    *,
    model: str = JEV_MODEL,
    timeout_s: int = JEV_TIMEOUT_S,
    subprocess_timeout_s: int = SUBPROCESS_TIMEOUT_S,
) -> JevAdvisory:
    """Best-effort, ADDITIVE-ONLY restricted-category read via jev.

    NEVER raises. Bounded to `subprocess_timeout_s` regardless of what jev
    itself does. Any failure of any kind returns `available=False` — the
    caller must then leave the deterministic verdict exactly as it was.

    `model` / `timeout_s` are parameters (rather than hardcoded) only so the
    forced-failure fallback proof in the test suite can pass a bad model
    name or a 0-second timeout without editing this file; production
    callers should not override them.
    """
    if not content or not content.strip():
        return JevAdvisory(available=False, blocked=None, probability=None,
                            error="empty content")

    if _under_test():
        return JevAdvisory(
            available=False, blocked=None, probability=None,
            error=("running under a test runner - refusing to make a real "
                   "jev call (set HATCHLOOP_ALLOW_LIVE=1 to mean it)"),
        )

    # Board row 282: resolve WHERE jev is before attempting to call it. No
    # subprocess is spawned and _record_usage is deliberately NOT called for
    # this branch — nothing was attempted, so nothing should count against
    # the call ledger scripts/system_health.py's check_jev_budget reads.
    argv_prefix, resolve_reason = resolve_jev_binary()
    if argv_prefix is None:
        return JevAdvisory(available=False, blocked=None, probability=None,
                            error=resolve_reason)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # REQUIRED — jev crashes on its own
                                        # Chinese output without this on Windows.

    cmd = [
        *argv_prefix, "yes", QUESTION,
        "--true", TRUE_CRITERIA,
        "--false", FALSE_CRITERIA,
        "-m", model,
        "-t", str(JEV_THRESHOLD),
        "--timeout", str(timeout_s),
        "--retries", str(JEV_RETRIES),
        "--json",
        "-s", "-",
    ]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=content,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",   # jev emits Chinese diagnostics on failure
            env=env,
            timeout=subprocess_timeout_s,
        )
    except subprocess.TimeoutExpired:
        _record_usage(succeeded=False)
        return JevAdvisory(available=False, blocked=None, probability=None,
                            error=f"subprocess timeout after {subprocess_timeout_s}s")
    except Exception as exc:  # noqa: BLE001 - never let this raise into the caller
        _record_usage(succeeded=False)
        return JevAdvisory(available=False, blocked=None, probability=None,
                            error=f"{type(exc).__name__}: {exc}")
    finally:
        _elapsed = time.monotonic() - t0  # noqa: F841 - kept for future --verbose plumbing

    stderr = (proc.stderr or "")[:500]

    # jev's own exit codes: 0=yes, 1=no, 2=error. Treat exit 2 (or anything
    # else unexpected) + empty stdout as NO VERDICT, never as `false`.
    if proc.returncode not in (0, 1):
        _record_usage(succeeded=False)
        return JevAdvisory(available=False, blocked=None, probability=None,
                            error=f"exit {proc.returncode}: {stderr}")

    stdout = (proc.stdout or "").strip()
    if not stdout:
        _record_usage(succeeded=False)
        return JevAdvisory(available=False, blocked=None, probability=None,
                            error=f"empty stdout (exit {proc.returncode}): {stderr}")

    try:
        payload = json.loads(stdout)
        answer = payload["answer"] if "answer" in payload else payload
        probability = float(answer["noul"])
        yes = bool(answer["yes"])
    except Exception as exc:  # noqa: BLE001
        _record_usage(succeeded=False)
        return JevAdvisory(available=False, blocked=None, probability=None,
                            error=f"unparseable JSON: {exc}: {stdout[:200]}")

    _record_usage(succeeded=True)
    return JevAdvisory(available=True, blocked=yes, probability=probability, error=None)
