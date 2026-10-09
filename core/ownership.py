"""
Who may READ a stored record — one rule, three tools.

get_conversation, get_status and get_outcome each have to answer the same
question before they return anything: is the caller the agent that created
this row? Until now each answered it differently. get_conversation had a
guard, and it read

    if agent_id and row.get("agent_id") and row["agent_id"] != agent_id:

so a row with NO owner walked straight through it — while the MCP dispatcher
never bound a caller identity onto send_message, which meant every
conversation opened through the MCP surface was exactly such a row. Put the
two halves together and the guard admitted anyone holding a conversation id,
or guessing a four-digit reference. get_status and get_outcome asked nothing
at all. A rule that lives in three places is three rules, so it lives here.

THE THREE CALLER STATES, AND WHY `None` IS NOT "anonymous":

  None            No external surface is involved — an in-process call, such
                  as the async-booking smoke path in
                  core/schedule_appointment.py. There is no request to
                  authorise, so there is nothing to deny. Every external
                  surface passes a string ALWAYS (the MCP dispatcher and the
                  /ops/* routes both derive it from the bearer token), so
                  omitting the argument can never quietly come to mean
                  "trusted"; tests/unit/test_conversation_ownership.py drives
                  both real surfaces to prove they still pass one.

  "anonymous"     An external caller that presented no identity, or one whose
                  token did not validate — _agent_id_from_token collapses both
                  to this string. It is NOT an identity: two anonymous callers
                  are indistinguishable, so it can never match an owner, and
                  `owner_for_storage` keeps it from ever BECOMING one. Storing
                  the sentinel as a row's owner would create a single shared
                  account that every unidentified caller on the internet is a
                  member of, which is the hole again wearing a name.

  anything else   A validated agent_id taken from the bearer token.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# What _agent_id_from_token (agent_interface/identity.py) returns for a caller
# it could not identify. Imported from here by the write paths so the sentinel
# is defined once.
ANONYMOUS = "anonymous"

# Tools that cost nothing and still need a key, because what they return
# belongs to ONE caller.
#
# DEFINED IN core/tool_auth.py, which owns the whole tool-to-auth-class
# mapping and every published count derived from it. It is re-exported here
# under its original name because this is where the rule is ENFORCED and
# because the ownership test imports it from here; the set itself must exist
# once, or the six surfaces that describe it drift again the next time the
# rule moves - which is exactly what happened when this set was added.
from core.tool_auth import IDENTITY_REQUIRED_READ_TOOLS  # noqa: E402,F401


def owner_for_storage(agent_id: Optional[str]) -> Optional[str]:
    """The value to STORE as a row's owner, or None when there is no owner.

    Blank and the anonymous sentinel both become None: a row whose owner is
    NULL is honestly unowned, and the read rule below can then treat it as
    such instead of matching every anonymous caller against it.
    """
    value = (agent_id or "").strip()
    if not value or value == ANONYMOUS:
        return None
    return value


@dataclass(frozen=True)
class Denial:
    """Why a read was refused. Callers turn this into their own error shape."""
    reason_code: str
    human_message: str
    # What the caller can DO about it, in the receipt's own `next_actions` field. Empty unless the refusal has a
    # remedy the caller can carry out (a keyless caller can get a key).
    next_actions: tuple = field(default_factory=tuple)


def free_key_next_actions() -> tuple:
    """The step a caller with no key can take. The write tools' `auth_required` already names this address
    (agent_interface/key_state.free_key_url: PUBLIC_BASE_URL + /keys/request, the one definition both refusals use);
    the read tool that needs a key said only "send your key" - in the 14 days to 2026-10-09, 98 get_conversation calls
    met that wording (`identity_required`: 59 from outside callers, 39 from our own tooling) and had nowhere to go. (3
    further get_conversation calls got `conversation_not_found`, a different answer that carries no key pointer.)
    Deliberately the free key only: credits and x402 are switched rails that the write tools' message derives at call
    time, and a read refusal makes no claim about either."""
    from agent_interface.key_state import free_key_url  # the one definition of the address; lazy: core must not import it at load
    return (f"Get a free key (email-verified, no payment) at {free_key_url()}, then send it as the "
            f"X-Agent-Identity header on this call.",)


def read_denial(
    *,
    caller_agent_id: Optional[str],
    owner_agent_id: Optional[str],
    subject: str,
    unowned_is_readable: bool,
) -> Optional[Denial]:
    """Return a Denial if this caller must not read this row, else None.

    `unowned_is_readable` has no default ON PURPOSE. It decides what happens
    to a row we cannot attribute, which is the whole of this bug, so every
    call site has to state its answer out loud where a reviewer sees it. The
    two live answers and their reasons are at the call sites in
    core/get_conversation.py and core/status_outcome.py.
    """
    if caller_agent_id is None:
        return None                     # in-process call, not a request

    caller = owner_for_storage(caller_agent_id)
    owner = owner_for_storage(owner_agent_id)

    if owner is None:
        if unowned_is_readable:
            return None
        return Denial(
            reason_code=f"{subject}_owner_unknown",
            human_message=(
                f"This {subject} has no recorded owner, so we cannot tell "
                f"whether it is yours, and we do not release records we "
                f"cannot attribute. Records created before caller identity "
                f"was bound, and records created by a caller that presented "
                f"no identity, are readable only by the call that created "
                f"them. Send X-Agent-Identity when you create the record and "
                f"you will be able to read it back."),
        )

    if caller is None:
        return Denial(
            reason_code="identity_required",
            human_message=(
                f"This {subject} belongs to an agent identity, and no "
                f"identity was presented. Send the X-Agent-Identity key that "
                f"created it."),
        )

    if caller != owner:
        return Denial(
            reason_code=f"not_your_{subject}",
            human_message=f"This {subject} belongs to a different agent identity.",
        )

    return None
