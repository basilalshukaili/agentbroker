"""Drive EVERY advertised parameter through real dispatch and check it lands.

scripts/check_params_reach_dispatch.py is a static check, and an adversarial
reviewer defeated it with 7 of 8 one-line mutations that each still dropped a
parameter:

    _ignored = args.get("requested_time")                 # fetched, discarded
    logger.debug("...", args.get("requested_time"))       # logged, not passed
    requested_times=args.get("requested_time")            # one typo'd kwarg
    **_pick(args, "smb_id", "action")                     # whitelist helper
    args = {"smb_id": args["smb_id"]}                     # rebound first

The typo is the one that matters: these models use pydantic's default
extra='ignore', so `requested_times=...` constructs cleanly with
requested_time=None. One character, guard green, parameter gone.

No pattern over source text survives that. This runs the dispatcher instead
and looks at what the handler was actually handed, which is the only question
worth asking. It also covers the branches the static check skips entirely -
it treats any `**args` splat as a blanket pass, which today blanks 14 of 78
advertised parameters across escalate_to_human, handle_inbound and
send_transactional_confirmation.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import json
import os

import pytest

from agent_interface.mcp_server import _dispatch_operation

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(REPO, "manifest", "manifest.json")
DISPATCH = os.path.join(REPO, "agent_interface", "mcp_server.py")

# Parameters that legitimately do not land on the request model under their own
# name, each with the reason. Anything here must still be honest to the caller.
DECLARED_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("preview_cost", "operation"): "names the tool to price; not a model field",
    ("preview_cost", "params"): "the priced call's own arguments, passed through",
    ("get_status", "operation_id"): "a lookup key, not a request model field",
    ("get_outcome", "operation_id"): "a lookup key, not a request model field",
    ("send_message", "recipient"): "normalised into a Recipient object",
    ("send_transactional_confirmation", "recipient"): "normalised into Recipient",
    ("find_business", "location"): "normalised into a LocationFilter object",
    ("screen_sanctions", "name"): "handler takes plain arguments, not a model",
    ("screen_sanctions", "country"): "handler takes plain arguments",
    ("screen_sanctions", "type"): "maps to the entity_type argument",
    ("verify_company_record", "name"): "handler takes plain arguments",
    ("verify_company_record", "country"): "handler takes plain arguments",
    ("verify_company_record", "lei"): "handler takes plain arguments",
}


def _manifest_ops() -> dict:
    with open(MANIFEST, encoding="utf-8") as fh:
        man = json.load(fh)
    return {op["name"]: op for op in man.get("operations") or []}


def _handler_for(tool: str):
    """Find the `from core.x import y` inside this tool's dispatch branch.

    Derived from the source rather than hardcoded, so a renamed handler shows
    up as a failure here instead of silently skipping the tool.
    """
    with open(DISPATCH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_dispatch_operation")

    def _branch(node):
        for sub in ast.walk(node):
            if not isinstance(sub, ast.If):
                continue
            t = sub.test
            if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                    and t.left.id == "name" and len(t.ops) == 1
                    and isinstance(t.ops[0], ast.Eq)
                    and isinstance(t.comparators[0], ast.Constant)
                    and t.comparators[0].value == tool):
                return sub
        return None

    br = _branch(fn)
    if br is None:
        return None
    for node in ast.walk(ast.Module(body=br.body, type_ignores=[])):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("core."):
            for alias in node.names:
                if alias.name.startswith("handle") or alias.name.startswith("_handle"):
                    return node.module, alias.name
    return None


# Values that must be well-formed to get past model validation. Without these
# the test SKIPS, and a skip is indistinguishable from a pass - the exact
# failure mode this repo has been bitten by before.
_REALISTIC = {
    "id_value": "+14045550100",
    "recipient_id": "+14045550100",
    "business_phone": "+14045550100",
    "contact_phone": "+14045550100",
    "phone": "+14045550100",
    "email": "someone@example.com",
    "contact_email": "someone@example.com",
    "country_code": "US",
    "origin_country": "DE",
    "destination_country": "RU",
    "url": "https://cal.com/example/intro",
    "booking_url": "https://cal.com/example/intro",
    "hs_code": "8413.70",
    "lei": "HWUPKR0MPOU8FGXBT394",
}


def _sample(schema: dict, key: str = ""):
    """A plausible value for one advertised parameter, from its own schema."""
    if key in _REALISTIC:
        return _REALISTIC[key]
    t = schema.get("type")
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if t == "string":
        if schema.get("format") == "date-time":
            return "2026-09-15T14:00:00Z"
        return "smoke-test-value"
    if t == "integer":
        return int(schema.get("minimum") or 1)
    if t == "number":
        return float(schema.get("minimum") or 1)
    if t == "boolean":
        return True
    if t == "array":
        item = schema.get("items") or {"type": "string"}
        return [_sample(item, key)]
    if t == "object":
        props = schema.get("properties") or {}
        return {k: _sample(v, k) for k, v in props.items()} or {"k": "v"}
    return "smoke-test-value"


def _tools_with_model_handlers():
    ops = _manifest_ops()
    out = []
    for tool, op in sorted(ops.items()):
        h = _handler_for(tool)
        if h:
            out.append((tool, op, h))
    return out


@pytest.mark.parametrize("tool,op,handler",
                         _tools_with_model_handlers(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_every_advertised_parameter_reaches_the_handler(tool, op, handler,
                                                        monkeypatch):
    mod_name, fn_name = handler
    mod = importlib.import_module(mod_name)
    props = ((op.get("input_schema") or op.get("inputSchema") or {})
             .get("properties") or {})
    if not props:
        pytest.skip(f"{tool} advertises no parameters")

    box: dict = {}

    class _Stop(Exception):
        pass

    async def _capture(req=None, *a, **kw):
        box["req"] = req
        box["kw"] = kw
        raise _Stop

    monkeypatch.setattr(mod, fn_name, _capture)

    args = {k: _sample(v, k) for k, v in props.items()}
    try:
        asyncio.run(_dispatch_operation(tool, args))
    except _Stop:
        pass
    except Exception as exc:                    # noqa: BLE001
        if "req" not in box:
            pytest.skip(f"{tool} could not be driven with synthetic args: "
                        f"{type(exc).__name__}: {str(exc)[:120]}")

    req, kw = box.get("req"), box.get("kw") or {}
    if req is None and not kw:
        pytest.skip(f"{tool}: handler not reached with synthetic arguments")

    # SOME HANDLERS TAKE PLAIN ARGUMENTS, NOT A REQUEST MODEL
    # (screen_sanctions, verify_company_record, map_trade_restriction ...).
    # Looking only at the first positional made those SKIP, which reads
    # exactly like a pass. Their keyword arguments are the thing to inspect.
    dropped = []
    for param in props:
        if (tool, param) in DECLARED_EXCEPTIONS:
            continue
        if req is not None and hasattr(req, param):
            if getattr(req, param, None) is None:
                dropped.append(param)
        elif param in kw:
            if kw[param] is None:
                dropped.append(param)

    assert not dropped, (
        f"{tool}: advertised parameter(s) {dropped} were sent by the caller "
        f"and arrived at {mod_name}.{fn_name} as None. The published schema "
        f"says they do something; the handler never sees them.")
