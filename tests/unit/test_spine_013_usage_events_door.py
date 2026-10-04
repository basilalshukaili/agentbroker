"""migrations/spine/013_usage_events_door_columns.sql, read as text: the properties that must hold WITHOUT a database.

The database-backed twin (test_spine_013_usage_events_door_pg.py, opt-in) applies the file to a real PostgreSQL.
These tests are the ones that run everywhere, including CI with no docker, and they pin the promises the header
of the file makes: additive, idempotent, one new function, the old writers untouched, least privilege, a refusal
to be applied by the wrong role, and numbered where the operator's apply order expects it.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPINE = ROOT / "migrations" / "spine"
FILE = SPINE / "013_usage_events_door_columns.sql"
SQL = FILE.read_text(encoding="utf-8")


def _code(sql: str) -> str:
    """The SQL with line comments and string literals removed, lower-cased: what would actually execute."""
    no_comments = re.sub(r"--[^\n]*", "", sql)
    no_strings = re.sub(r"'(?:[^']|'')*'", "''", no_comments)
    return no_strings.lower()


CODE = _code(SQL)


def test_it_is_numbered_after_012_and_nothing_else_wears_the_number():
    assert FILE.name.startswith("013_")
    assert [p.name for p in SPINE.glob("013_*")] == [FILE.name]


def test_it_adds_exactly_three_nullable_columns_and_no_default():
    adds = re.findall(r"add column if not exists (\w+)\s+(\w+)(?!\s+(?:default|not null))", CODE)
    assert adds == [("door", "text"), ("protocol_version", "text"), ("result_count", "integer")]
    assert not re.search(r"add column[^;]*(default|not null)", CODE), "a default would rewrite a hot table"


def test_it_is_additive_nothing_is_dropped_renamed_truncated_or_deleted():
    for forbidden in (r"\bdrop\b", r"\brename\b", r"\btruncate\b", r"\bdelete\b", r"alter column", r"drop column"):
        assert not re.search(forbidden, CODE), forbidden


def test_it_creates_one_function_and_does_not_touch_the_live_writers():
    created = re.findall(r"create (?:or replace )?function\s+([\w.]+)", CODE)
    assert created == ["public.usage_events_insert_v3"]
    # v1 and v2 are named in the comments (why) but never created, replaced, granted, revoked or commented on
    for old in ("usage_events_insert(", "usage_events_insert_v2("):
        assert old not in CODE, old


def test_every_statement_is_safe_to_run_twice():
    assert not re.search(r"\bcreate (?!or replace function)(?!index if not exists)", CODE)
    assert "add column if not exists" in CODE
    # the only UPDATEs fill empty columns and nothing else
    updates = re.findall(r"update public\.usage_events\s+set (\w+) = .*?where (\w+) is null", CODE, re.S)
    assert updates == [("door", "door"), ("protocol_version", "protocol_version")]


def test_the_function_is_security_definer_with_a_pinned_search_path():
    m = re.search(r"create or replace function public\.usage_events_insert_v3\(.*?\$\$;", CODE, re.S)
    body = m.group(0)
    assert "security definer" in body
    assert "set search_path = public" in body


def test_privileges_start_from_nothing_and_grant_only_anon_and_service_role():
    sig = r"public\.usage_events_insert_v3\(\s*text, text, text, text, text, text, text, text, text, integer, integer, text, text, text, text\[\], text, text,\s*text, text, integer\s*\)"
    assert re.search(r"revoke all on function " + sig + r"\s*from public, anon, authenticated;", CODE)
    assert re.search(r"grant execute on function " + sig + r"\s*to anon, service_role;", CODE)
    assert "grant" not in re.sub(r"grant execute on function.*?;", "", CODE, flags=re.S), "no other grant"


def test_the_new_parameters_are_last_and_defaulted_so_an_old_style_call_still_works():
    m = re.search(r"create or replace function public\.usage_events_insert_v3\((.*?)\)\s*returns", SQL, re.S)
    params = [p.strip() for p in re.sub(r"--[^\n]*", "", m.group(1)).split(",") if p.strip()]
    names = [p.split()[0] for p in params]
    assert names[-3:] == ["p_door", "p_protocol_version", "p_result_count"]
    assert all("default null" in p for p in params[7:]), "everything after the seven original fields is optional"


def test_it_keeps_every_check_of_v2_and_refuses_the_three_new_values_without_echoing_them():
    for check in ("invalid session_kind", "invalid outcome", "invalid key_state"):
        assert f"usage_events_insert_v3: {check}" in SQL, check
    for field in ("door", "protocol_version", "result_count"):
        m = re.search(rf"raise exception 'usage_events_insert_v3: invalid {field}'(?!\s*,)", SQL)
        assert m, f"{field}: refused with errcode 22023 and without echoing the value"
    assert SQL.count("using errcode = '22023'") >= 6


def test_the_outcome_list_includes_notification_which_010_added():
    """v3 is built from v2 AFTER 010: a copy of 009's shorter list would drop every notification row."""
    assert "'notification'" in SQL


def test_it_refuses_to_run_as_any_role_but_spine_owner():
    assert "spine_owner" in SQL
    assert re.search(r"current_user <> 'spine_owner'", SQL)
    assert "42501" in SQL


def test_it_bounds_how_long_it_may_block_the_hot_insert_path():
    assert re.search(r"set local lock_timeout = '\d+s';", SQL)


def test_the_header_states_what_the_columns_mean_and_what_is_not_stored():
    head = SQL[: SQL.index("set local lock_timeout")]
    for phrase in ("ADDITIVE AND IDEMPOTENT", "WHAT THE THREE COLUMNS ARE", "WHAT IS DELIBERATELY NOT STORED",
                   "HISTORY", "LEAST PRIVILEGE", "OWNERSHIP", "retired:<slug>", "agent-broker"):
        assert phrase in head, phrase


def test_the_history_copy_is_exactly_as_strict_as_the_shape_the_server_wrote():
    assert "'(?:^| )door=([a-z0-9][a-z0-9._-]{0,62})(?: |$)'" in SQL
    assert "'(?:^| )pv=([0-9]{4}-[0-9]{2}-[0-9]{2})(?: |$)'" in SQL


def test_the_verification_script_names_the_same_columns_and_function():
    script = (ROOT / "scripts" / "verify_spine_013.py").read_text(encoding="utf-8")
    assert 'COLUMNS = ["door", "protocol_version", "result_count"]' in script
    assert "usage_events_insert_v3" in script
