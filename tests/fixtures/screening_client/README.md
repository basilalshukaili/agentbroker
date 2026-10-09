# Offline screening client fixtures

These six files are synthetic `tools/call` results generated on 2026-10-07
through the real `handle_screen_sanctions` handler and `_dispatch_and_label`
seam in the repaired source based on `d757aca` (itself based on `b3c7906`).
Local fake list adapters live in `tests/unit/test_screening_client_example.py`.
They contain no production calls, customer records or issuer signing key.
Their receipts are explicitly unsigned. All six responses, including
candidates and hits, have `hash_ok: true` and `response_match: true`: third-party
result text is fenced before the receipt is attached. This is source-level
fixture evidence, not a claim that a repaired release has been deployed.
The timestamps and operation identifiers belong to fixture generation only.

| File | Evidence | Consumer branch |
| --- | --- | --- |
| complete.json | No matches, complete supported coverage | record_supported_list_screen |
| partial.json | No matches, UK unavailable | hold_for_review |
| candidate_partial.json | Unverified candidate and UK unavailable | hold_for_review |
| stale_unknown.json | Complete coverage, stale OFAC and unknown EU freshness | hold_for_review |
| arabic.json | No hits, lossy Arabic transliteration | hold_for_review |
| hit.json | Confirmed name match, identity still unverified | hold_for_review |

The fixtures mirror the standard MCP free path: an OutcomeReceipt serialized
in `content[0].text` with `isError: false`. Tests also exercise SDK objects,
JSON-RPC envelopes, structured answers, conflicting answers and the ChatGPT
door's deliberate receipt omission. Fresh synthetic handler outputs are
compared with the saved review guidance to detect contract drift.

Focused verification:

```bash
python -m pytest tests/unit/test_screening_client_example.py -k dispatch_fencing -q
```

The changed result fields are
`possible_matches_unverified[0].name` (candidate) and `matches[0].name` (hit);
both gain `[UNTRUSTED]` fences. Their screening statuses remain `candidates`
and `hit`; candidate coverage remains `partial`. Both still require review,
even though their receipts bind the delivered result. Unsigned receipts do not
prove origin, and the consumer never normalizes saved evidence.

Historical `b3c7906` fenced these names after issuing the receipt, so the
delivered candidate/hit response had `hash_ok: true` and `response_match: false`.
The disabled-presigning-label control in
`tests/unit/test_screening_receipt_postlabel.py` reproduces that defect offline;
the current fixtures record the repaired behavior.

Run `python examples/screening_client.py tests/fixtures/screening_client/arabic.json`
from the repository root. Retain the input file as evidence; never interpret
the example's branch as identity verification or permission to transact.
