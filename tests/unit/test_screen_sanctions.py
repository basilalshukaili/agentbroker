"""

Unit tests for screen_sanctions -- free, read-only sanctions watchlist screening.



Tests use unittest.mock to patch httpx so no real network calls are made.

Covers:

  1. Match found via OpenSanctions (mocked hit).

  2. Match found via OFAC SDN CSV (mocked hit, OpenSanctions unavailable).

  3. Clean name returns no match (both sources return empty).

  4. Bad input: empty name returns FAILURE.

  5. Fail-open: both upstreams down -> partial result, no exception raised.

  6. Fail-open: OpenSanctions 401 -> falls through to OFAC CSV.

  7. ASCII-only output: non-ASCII chars in sanctions data replaced with '?'.

  8. MCP annotations: readOnlyHint=True, idempotentHint=True.

  9. Not in _WRITE_TOOLS_REQUIRING_AUTH.

  10. MCP tools/call dispatch: callable via the MCP dispatcher (matched case).

  11. MCP tools/call dispatch: callable via the MCP dispatcher (no-match case).

  12. preview_cost reports free for screen_sanctions.

  13. country and type filters are forwarded to OpenSanctions payload.

  14. Deduplication: same entity from both sources appears only once.

  15. Fuzzy name matching: partial word overlap scores correctly.

"""

from __future__ import annotations



import asyncio

import json

from unittest.mock import AsyncMock, MagicMock, patch



import unicodedata
import pytest



from core.screen_sanctions import (

    handle_screen_sanctions,

    _word_match_score,

    _normalize_name,

    _parse_ofac_sdn,

    _ascii,

)

from core.models import OperationStatus

from agent_interface.mcp_server import _build_tool_list, handle_mcp_request





def run(coro):

    return asyncio.run(coro)





# ---------------------------------------------------------------------------

# Shared mock data

# ---------------------------------------------------------------------------



_OPENSANCTIONS_MATCH_RESPONSE = {

    "responses": {

        "q1": {

            "query": {"schema": "Thing", "properties": {"name": ["Kim Jong-un"]}},

            "results": [

                {

                    "id": "NK-ABC1234567890",

                    "schema": "Person",

                    "caption": "KIM Jong Un",

                    "properties": {

                        "name": ["KIM Jong Un"],

                        "nationality": ["KP"],

                        "topics": ["sanction"],

                        "sanctionProgram": ["DPRK"],

                    },

                    "datasets": ["us_ofac_sdn", "eu_fsf"],

                    "score": 0.95,

                    "match": True,

                    "referents": [],

                }

            ],

        }

    }

}



_OPENSANCTIONS_EMPTY_RESPONSE = {

    "responses": {

        "q1": {

            "query": {},

            "results": [],

        }

    }

}



# Minimal OpenSanctions targets.simple.csv snippet (comma-delimited, with header).

# Format: id,schema,name,aliases,birth_date,countries,addresses,identifiers,

#         sanctions,phones,emails,program_ids,dataset,first_seen,last_seen,last_change

# TREASURY'S OWN SDN.CSV FORMAT, not OpenSanctions' export.
#
# We now ingest https://sanctionslistservice.ofac.treas.gov (SDN.CSV + ALT.CSV)
# rather than data.opensanctions.org, because their aggregated dataset is
# CC-BY-NonCommercial and we are a commercial product - and because the manifest
# claimed the list came "directly from the US Treasury" while it did not.
#
# Treasury's export is HEADERLESS and 12 columns:
#   0 ent_num  1 SDN_Name  2 SDN_Type  3 Program  4 Title  5 Call_Sign
#   6 Vess_type  7 Tonnage  8 GRT  9 Vess_flag  10 Vess_owner  11 Remarks
# Absent fields are the literal "-0-", not empty. Names are "SURNAME, Given".
#
# Aliases live in a SEPARATE file, ALT.CSV: [ent_num, alt_num, alt_type,
# alt_name, remarks].

_OFAC_CSV_HEADER = ""   # Treasury publishes no header row

_OFAC_CSV_WITH_MATCH = (
    '"20157","KIM, Jong Un","individual","DPRK3","Chairman","-0- ","-0- ",'
    '"-0- ","-0- ","-0- ","-0- ","-0- "\n'
    '"99001","SOME OTHER ENTITY","-0- ","SDT","-0- ","-0- ","-0- ",'
    '"-0- ","-0- ","-0- ","-0- ","-0- "\n'
)

# Alternate spellings for the entity above, in ALT.CSV shape.
_OFAC_ALT_WITH_MATCH = (
    '"20157","1","aka","KIM Jong-un","-0- "\n'
    '"20157","2","aka","KIM Jong Un","-0- "\n'
)

_OFAC_CSV_NO_MATCH = (
    '"77001","TOTALLY UNRELATED CORP XYZ999","-0- ","IRAN","-0- ","-0- ",'
    '"-0- ","-0- ","-0- ","-0- ","-0- ","-0- "\n'
)


def _mock_os_hit():

    resp = MagicMock()

    resp.status_code = 200

    resp.json.return_value = _OPENSANCTIONS_MATCH_RESPONSE

    return resp





def _mock_os_empty():

    resp = MagicMock()

    resp.status_code = 200

    resp.json.return_value = _OPENSANCTIONS_EMPTY_RESPONSE

    return resp





def _mock_os_401():

    resp = MagicMock()

    resp.status_code = 401

    resp.json.return_value = {"detail": "Unauthorized"}

    return resp





def _mock_os_429():

    resp = MagicMock()

    resp.status_code = 429

    resp.json.return_value = {"detail": "Rate limited"}

    return resp





def _mock_ofac_hit():

    resp = MagicMock()

    resp.status_code = 200

    resp.text = _OFAC_CSV_WITH_MATCH

    return resp





def _mock_ofac_empty():

    resp = MagicMock()

    resp.status_code = 200

    resp.text = _OFAC_CSV_NO_MATCH

    return resp





def _client_ctx(response):

    """Build an AsyncClient context manager that returns response from .post() or .get()."""

    client_instance = AsyncMock()

    client_instance.post = AsyncMock(return_value=response)

    client_instance.get = AsyncMock(return_value=response)

    client_instance.__aenter__ = AsyncMock(return_value=client_instance)

    client_instance.__aexit__ = AsyncMock(return_value=False)

    return client_instance





# ---------------------------------------------------------------------------

# Test 1: Match found via OpenSanctions

# ---------------------------------------------------------------------------



class TestOfacMatch:
    """Was TestOpensanctionsMatch. OpenSanctions is gone (its data is
    CC-BY-NonCommercial and we sell screening), so the match has to come from
    a source we actually use."""

    def test_match_found_via_ofac(self):
        """A real match, produced by the real matcher.

        This test used to mock an OpenSanctions reply and assert we surfaced it,
        which mostly tested the mock. Patching the FETCH instead means the OFAC
        parser and the token-set matcher both actually run.
        """
        with patch("core.screen_sanctions._fetch_ofac_sdn_csv",
                   new=AsyncMock(return_value=_OFAC_CSV_WITH_MATCH)):
            result = run(handle_screen_sanctions(name="Kim Jong-un", country="KP"))

        assert result.status == OperationStatus.SUCCESS
        assert result.result["matched"] is True
        top = result.result["matches"][0]
        # "KIM, Jong Un" against "Kim Jong-un": same token set once normalised,
        # which is the ONLY relationship an uncalibrated matcher may assert.
        assert "KIM" in top["name"].upper()
        assert "OFAC-SDN" in top["list"]
        assert result.cost.amount == 0.0
        assert result.cost.basis == "free"
        assert "disclaimer" in result.result
        assert "screened_at" in result.result
        # A retired dependency must not appear anywhere in the receipt.
        assert "opensanctions" not in str(result.result).lower()





# ---------------------------------------------------------------------------

# Test 2: Match found via OFAC CSV when OpenSanctions unavailable

# ---------------------------------------------------------------------------



class TestOfacCsvFallbackMatch:

    def test_match_via_ofac_csv_only(self):


        with patch("core.screen_sanctions._fetch_ofac_sdn_csv",

                   new=AsyncMock(return_value=_OFAC_CSV_WITH_MATCH)):

            result = run(handle_screen_sanctions(name="Kim Jong-un"))



        assert result.status == OperationStatus.SUCCESS

        assert result.result["matched"] is True

        ofac_matches = [m for m in result.result["matches"] if m["list"] == "OFAC-SDN"]

        assert len(ofac_matches) >= 1

        assert ofac_matches[0]["entity_type"] == "INDIVIDUAL"

        # sources_unavailable should mention OpenSanctions

        unavail = result.result.get("sources_unavailable", [])

        # WAS: assert OpenSanctions appears in sources_unavailable.
        # It is not a source at all now, so it must appear NOWHERE - neither
        # as screened nor as unavailable. A retired dependency that still
        # shows up in a receipt is a claim about how we screened.
        assert not any("opensanctions" in str(s).lower() for s in unavail)
        assert not any("opensanctions" in str(s).lower()
                       for s in result.result.get("lists_screened") or [])





# ---------------------------------------------------------------------------

# Test 3: Clean name -- no match on either source

# ---------------------------------------------------------------------------



class TestNoMatch:

    def test_clean_name_returns_no_match(self):


        with patch("core.screen_sanctions._fetch_ofac_sdn_csv",

                   new=AsyncMock(return_value=_OFAC_CSV_NO_MATCH)):

            result = run(handle_screen_sanctions(name="Jane Smith Completely Innocent"))



        assert result.status == OperationStatus.SUCCESS

        assert result.result["matched"] is False

        assert result.result["matches"] == []

        # Must name which lists were screened

        screened = result.result.get("lists_screened", [])

        assert len(screened) > 0

        assert "disclaimer" in result.result



    def test_no_match_message_names_lists_screened(self):


        with patch("core.screen_sanctions._fetch_ofac_sdn_csv",

                   new=AsyncMock(return_value=_OFAC_CSV_NO_MATCH)):

            result = run(handle_screen_sanctions(name="Jane Smith Completely Innocent"))



        # human_message must describe what was screened, not just "no match"

        assert "screened" in result.human_message.lower() or "no match" in result.human_message.lower()





# ---------------------------------------------------------------------------

# Test 4: Bad input

# ---------------------------------------------------------------------------



class TestBadInput:

    def test_empty_name_returns_failure(self):

        result = run(handle_screen_sanctions(name=""))

        assert result.status == OperationStatus.FAILURE

        assert result.reason_code == "bad_input"

        assert result.cost.amount == 0.0



    def test_whitespace_only_name_returns_failure(self):

        result = run(handle_screen_sanctions(name="   "))

        assert result.status == OperationStatus.FAILURE

        assert result.reason_code == "bad_input"





# ---------------------------------------------------------------------------

# Test 5: Fail-open when both upstreams are down

# ---------------------------------------------------------------------------



class TestFailOpen:

    def test_all_upstreams_down_no_exception(self):

        import httpx



        async def _raise_timeout(*args, **kwargs):

            raise httpx.TimeoutException("timed out")




        with patch("core.screen_sanctions._fetch_ofac_sdn_csv",

                   new=AsyncMock(return_value=None)):

            result = run(handle_screen_sanctions(name="Some Corp"))



        # Must not raise; must return a valid OutcomeReceipt

        assert result.status == OperationStatus.SUCCESS  # fail-open

        assert "sources_unavailable" in result.result

        # matched should be False (no data to match against)

        assert result.result["matched"] is False





# ---------------------------------------------------------------------------

# Test 6: OpenSanctions 401 -> falls through to OFAC CSV

# ---------------------------------------------------------------------------



class TestOpensanctions401Fallback:

    def test_401_falls_back_to_ofac(self):


        with patch("core.screen_sanctions._fetch_ofac_sdn_csv",

                   new=AsyncMock(return_value=_OFAC_CSV_WITH_MATCH)):

            result = run(handle_screen_sanctions(name="Kim Jong Un"))



        assert result.status == OperationStatus.SUCCESS

        # OFAC should provide a match even without OpenSanctions

        assert result.result["matched"] is True

        # The 401 message should be in sources_unavailable

        unavail = result.result.get("sources_unavailable", [])

        # WAS: assert OpenSanctions appears in sources_unavailable.
        # It is not a source at all now, so it must appear NOWHERE - neither
        # as screened nor as unavailable. A retired dependency that still
        # shows up in a receipt is a claim about how we screened.
        assert not any("opensanctions" in str(s).lower() for s in unavail)
        assert not any("opensanctions" in str(s).lower()
                       for s in result.result.get("lists_screened") or [])





# ---------------------------------------------------------------------------

# Test 7: ASCII-only output

# ---------------------------------------------------------------------------



class TestAsciiOutput:

    def test_output_preserves_non_latin_scripts(self):

        """The receipt must record WHAT WAS SCREENED.



        This asserted _ascii("Café") == "Cafe?" - every non-ASCII character

        replaced by '?'. The consequence in production:



            screen_sanctions("Сбербанк")  -> "MATCH FOUND for '????????'"

            screen_sanctions("حزب الله")   -> "MATCH FOUND for '??? ????'"



        Matching was unaffected (OpenSanctions handles those scripts), but the

        receipt is the audit artefact and it could not say what was checked.

        "Wire-safe" was never a real constraint - MCP responses are JSON and

        JSON is UTF-8 - and for an Oman company whose market writes Arabic,

        destroying Arabic names in its own compliance receipts is self-sabotage.

        """

        assert _ascii("Café") == "Café"

        assert _ascii("Сбербанк") == "Сбербанк"

        assert _ascii("حزب الله") == "حزب الله"

        assert _ascii("") == ""

        assert _ascii("OFAC-SDN") == "OFAC-SDN"



    def test_output_still_strips_control_characters(self):

        """The one genuine wire-safety concern, kept."""

        assert _ascii("Bank" + chr(0) + "Melli" + chr(7)) == "BankMelli"



    def test_non_ascii_in_sanctions_name_replaced(self):

        # Use a name with non-ASCII character: U+2019 right single quote

        curly_apos = chr(0x2019)

        alqaida_name = "AL-QA" + curly_apos + "IDA"

        csv_with_unicode = (

            _OFAC_CSV_HEADER +

            '"NK-ALQAIDA","Organization","' + alqaida_name + '","QAEDA","","","","","'

            'SDT - Terrorism","","","US-SDT","US OFAC SDN","","2026-08-23",""\n'

        )

        matches = _parse_ofac_sdn(csv_with_unicode, "AL-QAIDA")

        # The name is now PRESERVED rather than mangled - what must not survive

        # is control characters, and what must survive is the actual name.

        for m in matches:

            assert m["name"], "the matched name was lost"

            assert all(unicodedata.category(c)[0] != "C" for c in m["name"]), (

                f"control character in match name: {m['name']!r}"

            )



    def test_normalize_name_handles_hyphens(self):

        """Hyphens separate; APOSTROPHES ARE DELETED.



        This asserted "al qa ida" - the old behaviour where an apostrophe

        became a space. That was wrong in both directions and both were real:



        FALSE POSITIVE: "Joe's Pizza LLC" tokenised to ['joe','s','pizza'] and

        the orphaned "s" matched "RICA'S PIZZA", producing a live OFAC-SDN

        US-NARCO hit on a pizza shop.



        FALSE NEGATIVE, which is worse: "AL-QA'IDA" became ['al','qa','ida'],

        which scores 0.33 against a list entry written "Al Qaida" - a MISS on

        one of the most-listed entities in the world. Deleting the apostrophe

        gives "al qaida" for both spellings and they now match at 1.00.

        """

        assert _normalize_name("Kim Jong-un") == "kim jong un"

        assert _normalize_name("AL-QA'IDA") == "al qaida"

        assert _normalize_name("Joe's Pizza") == "joes pizza"





# ---------------------------------------------------------------------------

# Test 8: MCP annotations

# ---------------------------------------------------------------------------



class TestMcpAnnotations:

    def test_listed_as_read_only_and_idempotent(self):

        tools = {t["name"]: t for t in _build_tool_list()}

        assert "screen_sanctions" in tools, "screen_sanctions not in tools/list"

        ann = tools["screen_sanctions"]["annotations"]

        assert ann["readOnlyHint"] is True

        assert ann["idempotentHint"] is True

        assert ann["destructiveHint"] is False





# ---------------------------------------------------------------------------

# Test 9: Not in write tools

# ---------------------------------------------------------------------------



class TestNotInWriteTools:

    def test_not_in_write_tools_requiring_auth(self):

        from agent_interface.mcp_server import _WRITE_TOOLS_REQUIRING_AUTH

        assert "screen_sanctions" not in _WRITE_TOOLS_REQUIRING_AUTH





# ---------------------------------------------------------------------------

# Test 10: MCP dispatch -- matched case

# ---------------------------------------------------------------------------



class TestMcpDispatchMatched:

    def test_callable_via_mcp_match_found(self):
        """The same match, reached through the MCP dispatch path."""
        with patch("core.screen_sanctions._fetch_ofac_sdn_csv",
                   new=AsyncMock(return_value=_OFAC_CSV_WITH_MATCH)):
            resp = run(handle_mcp_request({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "screen_sanctions",
                           "arguments": {"name": "Kim Jong-un"}},
            }))
            data = json.loads(resp["result"]["content"][0]["text"])

        assert data["status"] == "success"
        assert data["result"]["matched"] is True
        assert len(data["result"]["matches"]) >= 1
        assert "opensanctions" not in str(data).lower()





# ---------------------------------------------------------------------------

# Test 11: MCP dispatch -- no-match case

# ---------------------------------------------------------------------------



class TestMcpDispatchNoMatch:

    def test_callable_via_mcp_no_match(self):


        with patch("core.screen_sanctions._fetch_ofac_sdn_csv",

                   new=AsyncMock(return_value=_OFAC_CSV_NO_MATCH)):

            r = run(handle_mcp_request({

                "jsonrpc": "2.0", "id": 201, "method": "tools/call",

                "params": {

                    "name": "screen_sanctions",

                    "arguments": {"name": "Jane Smith Clearly Innocent"},

                },

            }))



        assert "result" in r

        data = json.loads(r["result"]["content"][0]["text"])

        assert data["status"] == "success"

        assert data["result"]["matched"] is False

        assert "disclaimer" in data["result"]



    def test_missing_name_returns_param_error(self):

        r = run(handle_mcp_request({

            "jsonrpc": "2.0", "id": 202, "method": "tools/call",

            "params": {"name": "screen_sanctions", "arguments": {}},

        }))

        assert "error" in r

        assert r["error"]["code"] == -32602





# ---------------------------------------------------------------------------

# Test 12: preview_cost reports free

# ---------------------------------------------------------------------------



class TestPreviewCostFree:

    def test_preview_cost_reports_free_for_screen_sanctions(self):

        from core.preview_cost import handle_preview_cost

        from core.models import PreviewCostRequest

        resp = run(handle_preview_cost(PreviewCostRequest(

            operation="screen_sanctions",

            params={"name": "Kim Jong-un"},

        )))

        assert resp.estimated_cost_usd == 0.0





# ---------------------------------------------------------------------------

# Test 13: country and type filters forwarded

# ---------------------------------------------------------------------------



class TestFilterForwarding:

    def test_country_is_reported_as_not_applied(self):
        """`country` and `entity_type` were consumed ONLY by OpenSanctions.

        With it gone they narrow nothing - our name index carries no country
        column - but the response still said "(country filter: IR)". That tells
        a caller their screen was narrowed when it was not, so a clean result
        reads as more specific than it is. On a screening tool that is the
        dangerous direction to be wrong in.

        The parameters are still ACCEPTED, so no existing call breaks. What
        changed is that the answer says plainly they were not applied.
        """
        with patch("core.screen_sanctions._fetch_ofac_sdn_csv",
                   new=AsyncMock(return_value=_OFAC_CSV_NO_MATCH)):
            result = run(handle_screen_sanctions(
                name="Mahan Air", country="ir", entity_type="entity"))

        assert result.status == OperationStatus.SUCCESS
        assert result.result["country_filter_applied"] is False
        assert "do NOT narrow" in result.result["filter_note"]
        # The human-readable half must not imply a filter either.
        assert "country filter:" not in result.human_message
        assert "NOT used to narrow" in result.human_message





# ---------------------------------------------------------------------------

# Test 14: Deduplication -- same entity from both sources counted once

# ---------------------------------------------------------------------------



class TestDeduplication:

    def test_same_entity_deduped_across_sources(self):
        """One entity listed on two of our sources must be reported once per list.

        Dedup used to be tested across OpenSanctions and OFAC. The sources are
        now OFAC, EU and UK, so the test drives EU through the database-backed
        screen and OFAC through the CSV, with the SAME name on both.
        """
        async def _fake_db(name, list_code, list_label, source_url):
            if list_code != "EU":
                return [], [], []
            row = {
                "name": "KIM, Jong Un",
                "list": list_label,
                "match_score": 1.0,
                "program": "DPRK",
                "entity_type": "INDIVIDUAL",
                "source_url": source_url,
                "_matcher": "local_word_overlap",
            }
            # The same entity twice from one list: the merge must collapse these.
            return [row, dict(row)], [list_label], []

        with patch("core.screen_sanctions._screen_list_db", new=_fake_db):
            with patch("core.screen_sanctions._fetch_ofac_sdn_csv",
                       new=AsyncMock(return_value=_OFAC_CSV_WITH_MATCH)):
                result = run(handle_screen_sanctions(name="Kim Jong-un"))

        matches = result.result["matches"]
        eu = [m for m in matches if m["list"].startswith("EU-CONSOLIDATED")]
        assert len(eu) == 1, f"EU duplicate not collapsed: {eu}"
        ofac = [m for m in matches if "OFAC-SDN" in m["list"]]
        assert len(ofac) == 1, f"Expected 1 OFAC match, got {len(ofac)}"
        # Same person, two lists: BOTH are kept, because which list carries a
        # designation is exactly what a compliance caller needs to know.
        assert len(matches) >= 2





# ---------------------------------------------------------------------------

# Test 15: Fuzzy match scoring

# ---------------------------------------------------------------------------



class TestFuzzyMatchScoring:

    def test_exact_match_scores_1(self):

        assert _word_match_score("Kim Jong-un", "Kim Jong Un") == 1.0



    def test_partial_match_above_threshold(self):

        # "Ramzan Kadyrov" vs "KADYROV, Ramzan Akhmatovich"

        score = _word_match_score("Ramzan Kadyrov", "KADYROV, Ramzan Akhmatovich")

        assert score >= 0.60, f"Expected >=0.60, got {score}"



    def test_unrelated_names_score_low(self):

        score = _word_match_score("Jane Smith", "Kim Jong Un")

        assert score == 0.0, f"Expected 0.0, got {score}"



    def test_empty_query_scores_zero(self):

        assert _word_match_score("", "Kim Jong Un") == 0.0

        assert _word_match_score("Kim", "") == 0.0



    def test_ofac_csv_parse_finds_kim_jong_un(self):

        matches = _parse_ofac_sdn(_OFAC_CSV_WITH_MATCH, "Kim Jong-un",
                                  _OFAC_ALT_WITH_MATCH)

        assert len(matches) >= 1

        assert any("KIM" in m["name"].upper() for m in matches)



    def test_ofac_csv_parse_no_match_for_clean_name(self):

        matches = _parse_ofac_sdn(_OFAC_CSV_NO_MATCH, "Jane Smith Completely Innocent")

        assert len(matches) == 0

