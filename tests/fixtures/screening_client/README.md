# Offline screening client fixtures

These six files are synthetic `tools/call` results generated on 2026-10-07
from the real `handle_screen_sanctions` handler and `_dispatch_and_label` seam
at release `b3c7906`, with local fake list adapters in
`tests/unit/test_screening_client_example.py`.
They contain no production calls, customer records or issuer signing key.
Their receipts are explicitly unsigned. Candidate/hit responses preserve the
current dispatch limitation: names are fenced after receipt issuance, leaving
`hash_ok: true` but `response_match: false`.
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

Focused reproduction:

```bash
python -m pytest tests/unit/test_screening_client_example.py -k dispatch_fencing -q
```

The changed result fields are
`possible_matches_unverified[0].name` (candidate) and `matches[0].name` (hit);
both gain `[UNTRUSTED]` fences. Their screening statuses remain `candidates`
and `hit`; candidate coverage remains `partial`. This example holds and
preserves independent review hints as unverified. It does not repair server
hashing or normalize saved evidence.

Run `python examples/screening_client.py tests/fixtures/screening_client/arabic.json`
from the repository root. Retain the input file as evidence; never interpret
the example's branch as identity verification or permission to transact.
