#!/usr/bin/env python3
"""Measure screen_sanctions on Arabic and Arabic-romanised names, on the real lists.

WHY THIS EXISTS. Matching names by sound has no ground truth you can assert in a
unit test: the right threshold is a number you measure. This script produces the
numbers the thresholds in core/arabic_names.py were calibrated against, from the
publishers' own files, and it is the thing to re-run when a threshold is touched.
Results from the 2026-10-03 snapshot are in docs/ARABIC_SANCTIONS_EVAL.md.

THE GOLD SET IS THE LISTS THEMSELVES. The EU and UK publish many entries with
BOTH a Latin-script name and the same party's name in Arabic or Persian script
(1,387 entries on the 2026-10-03 copies: 710 EU, 677 UK). That is a ready-made, independent
answer key: take the Arabic-script name, screen it with the Arabic-script
spellings removed from the index, and ask whether the party's Latin entry comes
back. Nobody tuned the matcher to these pairs one by one; the matcher saw the
pairs only through the aggregate numbers below.

FOUR MEASUREMENTS
  A  Arabic query, Arabic alias in the index      -> is it a FINDING?
  B  Arabic query, Arabic aliases removed         -> is the party SURFACED (found
                                                     in matches or candidates)?
  C  Latin query, that exact spelling removed     -> is the party surfaced through
                                                     its OTHER romanisations?
  D  Names that are not on the lists              -> any finding at all (must be
                                                     zero), and how many candidates
                                                     does an ordinary name draw?

  N  245 ordinary names that are on no list (tests/fixtures/sanctions_negative_names.json): 125 English, 60 Gulf
     names in Latin script, the same 60 in Arabic script. Findings (must be zero), how many are read as Arabic
     at all, how many draw a sound candidate and at which grade. This is the false-positive measurement the
     review of 2026-10-03 asked for; D is the older, smaller one.

  E  A generic fuzzy baseline (RapidFuzz) on the C and D names, for context.
     Not OpenSanctions' logic-v2 matcher; see run_e.

  B and C are also run with the new layer switched off, which reproduces the
  previous matcher exactly (an Arabic name reduced to no tokens; a Latin name
  matched only on identical spelling), so the improvement is a measured
  difference and not a claim.

  Each measurement draws its own sample from its own seeded generator, so the
  same --seed gives the same names whichever measurements are run.

  B, C and D also report PRECISION BY GRADE: of the sound-based candidates the
  screen listed, how many point at the party the query was made from (own_*) and
  how many at another listed party (other_*), per grade. That is what the
  high/medium thresholds in core/arabic_names.py are calibrated against.

THE FULL PIPELINE RUNS: handle_screen_sanctions, with the OFAC list read from
the real SDN.CSV/ALT.CSV text and the EU/UK index replaced by an in-memory table
built through the same row-building code the refresh job uses. No network, no
database.

Usage:
    python scripts/eval_arabic_sanctions.py --lists-dir DIR [--sample 150]
                                            [--modes ABCD] [--json out.json]

DIR holds SDN.CSV, ALT.CSV, EU.csv and UK.csv as the publishers serve them.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import csv
import dataclasses
import io
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from core import arabic_names as an            # noqa: E402
from core import screen_sanctions as ss        # noqa: E402
import storage.supabase_client as sb           # noqa: E402
from tests.sanctions_index_fake import FakeSanctionsTable   # noqa: E402


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def load_lists(lists_dir: str) -> dict:
    def rd(name, **kw):
        with open(os.path.join(lists_dir, name), encoding=kw.get("enc", "utf-8"),
                  errors="replace", newline="") as fh:
            return fh.read()
    eu = ss._eu_parse(rd("EU.csv", enc="utf-8-sig"))
    uk = ss._uk_parse(rd("UK.csv", enc="utf-8-sig"))
    return {"records": {"EU": eu, "UK": uk}, "sdn": rd("SDN.CSV"), "alt": rd("ALT.CSV")}


def gold_entities(records: dict) -> list[dict]:
    """Entries that carry both a Latin-script and an Arabic-script name."""
    ents: dict[tuple, dict] = collections.OrderedDict()
    for code, recs in records.items():
        for r in recs:
            e = ents.setdefault((code, r["entity_id"]), {
                "list": code, "id": r["entity_id"], "etype": r["etype"],
                "latin": [], "arabic": []})
            nm = r["name"]
            bucket = "arabic" if an.has_arabic_script(nm) else "latin"
            if nm not in e[bucket]:
                e[bucket].append(nm)
    return [e for e in ents.values() if e["latin"] and e["arabic"]]


# --------------------------------------------------------------------------
# Running the real pipeline
# --------------------------------------------------------------------------

class Harness:
    def __init__(self, data: dict) -> None:
        self.data = data
        self._real_analyse = an.analyse
        self._real_fetch = (ss._fetch_ofac_sdn_csv, ss._fetch_ofac_alt_csv)
        self._real_db = (sb.select_rows_strict, sb.select_rows)

    def use_table(self, table: FakeSanctionsTable) -> None:
        sb.select_rows_strict = table.select_rows_strict
        sb.select_rows = table.select_rows
        ss._age_cache.clear()
        self.table = table

    async def _sdn(self):
        return self.data["sdn"]

    async def _alt(self):
        return self.data["alt"]

    # The sound indexes are built ONCE from the table installed first and kept
    # (as in production). A measurement that removes a spelling from the lists
    # (C) therefore cannot remove it from the table: it names the spelling here
    # and the index search drops hits on it.
    exclude: str | None = None

    def __enter__(self):
        ss._fetch_ofac_sdn_csv = self._sdn
        ss._fetch_ofac_alt_csv = self._alt
        real_search = self._real_search = an.SkeletonIndex.search
        h = self

        def search(idx, q, limit=8, min_confidence="medium"):
            if not h.exclude:
                return real_search(idx, q, limit, min_confidence)
            hits = real_search(idx, q, 400, min_confidence)
            return [x for x in hits if fold_name(idx.names[x[1]]) != h.exclude][:limit]

        an.SkeletonIndex.search = search
        ss._ofac_phonetic_state.update(key=None, index=None, info=None)
        for slot in ss._list_phonetic_state.values():
            slot.update(key=None, index=None, info=None)
        return self

    def warm(self) -> None:
        """Build the three sound indexes now, so no measured query is answered
        before they exist (the first would otherwise use the narrower fallback)."""
        self.layer_on()
        self.screen("أيمن الظواهري")
        for slot in [ss._ofac_phonetic_state, *ss._list_phonetic_state.values()]:
            if slot["future"] is not None:
                slot["future"].result(timeout=600)
        r = self.screen("أيمن الظواهري")
        assert not any("still being built" in s or "could not be built" in s
                       for s in r.get("sources_unavailable") or []), r.get("sources_unavailable")

    def __exit__(self, *exc):
        ss._fetch_ofac_sdn_csv, ss._fetch_ofac_alt_csv = self._real_fetch
        sb.select_rows_strict, sb.select_rows = self._real_db
        an.SkeletonIndex.search = self._real_search
        self.layer_on()

    def layer_off(self) -> None:
        """Reproduce the previous matcher: the Arabic layer never engages and an
        Arabic-script name is just a string with no Latin tokens in it."""
        real = self._real_analyse

        def off(name):
            a = real(name)
            return dataclasses.replace(a, script="latin", units=(), engaged=False,
                                       weak_reason=None, arabic_key=(),
                                       arabic_all_key=())
        an.analyse = off
        ss._ar.analyse = off

    def layer_on(self) -> None:
        an.analyse = self._real_analyse
        ss._ar.analyse = self._real_analyse

    def screen(self, name: str) -> dict:
        r = asyncio.run(ss.handle_screen_sanctions(name=name))
        return r.result or {}


def fold_name(s: str) -> str:
    return " ".join(sorted(set(ss._normalize_name(s).split()))) or an.normalise_arabic(s)


def is_own(m: dict, own_names: set[str], skip: str | None = None) -> bool:
    """Is this listed name the party the query was made from?

    The gold entity is a party of the EU or UK list, with every name that list
    prints for it. The same person is usually ALSO on another list under a
    different spelling ("VAHEDI, Hamid" on OFAC for "Hamid VAHEDI" on the UK
    list), and finding that one is finding the party. So a listed name is the
    party's when it folds to one of the party's names, OR when every word of it
    is a word the party's names use (two or more words: a single shared word is
    a coincidence). A relative that adds a word ("Ali Barzan ...") is not.

    `skip` is a spelling that must not count (measurement C removed it on
    purpose; another list printing the very same spelling is not a find)."""
    own_tokens = {t for n in own_names for t in n.split()}
    for key in ("name", "listed_primary_name"):
        nm = m.get(key) or ""
        if not nm or (skip and fold_name(nm) == skip):
            continue
        if fold_name(nm) in own_names:
            return True
        toks = set(ss._normalize_name(nm).split())
        if len(toks) >= 2 and toks <= own_tokens:
            return True
    return False


def surfaced(result: dict, own_names: set[str], skip: str | None = None) -> str | None:
    """'finding' / 'candidate:<confidence>' / None, for the entity whose listed
    names are `own_names` (folded)."""
    for m in result.get("matches") or []:
        if is_own(m, own_names, skip):
            return "finding"
    for m in result.get("possible_matches_unverified") or []:
        if is_own(m, own_names, skip):
            return "candidate:" + str(m.get("match_confidence", "exact-token"))
    return None


def own_name_set(e: dict) -> set[str]:
    def fold(s):
        return " ".join(sorted(set(ss._normalize_name(s).split()))) or an.normalise_arabic(s)
    return {fold(x) for x in e["latin"] + e["arabic"]}


def grade_counts(result: dict, own_names: set[str], into: collections.Counter,
                 skip: str | None = None) -> None:
    """For every SOUND-BASED candidate the screen listed, count it under its
    grade as `own_<grade>` (it points at the party the query was made from) or
    `other_<grade>` (it points at another listed party). This is the precision
    measure the grades are calibrated on: a grade is only worth printing if the
    candidates carrying it are mostly the party that was asked about."""
    for m in (result.get("matches") or []) + (result.get("possible_matches_unverified") or []):
        if m.get("_matcher") not in ("name_sound_match", "arabic_script_exact"):
            continue
        grade = str(m.get("match_confidence"))
        into[("own_" if is_own(m, own_names, skip) else "other_") + grade] += 1


# --------------------------------------------------------------------------
# The four measurements
# --------------------------------------------------------------------------

def pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:.1f}%" if b else "n/a"


def run_a(h: Harness, gold: list[dict], n: int, rng: random.Random) -> dict:
    h.layer_on()
    h.use_table(FakeSanctionsTable.from_records(h.data["records"], include_arabic=True))
    sample = [e for e in gold if e["etype"] == "INDIVIDUAL"]
    sample = rng.sample(sample, min(n, len(sample)))
    res = collections.Counter()
    for e in sample:
        q = e["arabic"][0]
        r = h.screen(q)
        res["n"] += 1
        got = surfaced(r, own_name_set(e))
        res["finding" if got == "finding" else "candidate" if got else "miss"] += 1
    return dict(res)


def run_b(h: Harness, gold: list[dict], n: int, rng: random.Random) -> dict:
    out = {}
    sample = rng.sample(gold, min(n, len(gold)))
    for label in ("new", "previous"):
        h.layer_on() if label == "new" else h.layer_off()
        h.use_table(FakeSanctionsTable.from_records(h.data["records"], include_arabic=False))
        res = collections.defaultdict(collections.Counter)
        cands = collections.defaultdict(list)
        t0 = time.time()
        for e in sample:
            q = e["arabic"][0]
            r = h.screen(q)
            got = surfaced(r, own_name_set(e))
            c = res[e["etype"]]
            grade_counts(r, own_name_set(e), c)
            c["n"] += 1
            c["surfaced" if got else "miss"] += 1
            if got and got.endswith(("high", "medium")):
                c["surfaced_high_or_medium"] += 1
            if got and got.endswith("high"):
                c["surfaced_high"] += 1
            cands[e["etype"]].append(len(r.get("possible_matches_unverified") or []))
            res[e["etype"]]["status_" + str(r.get("screening_status"))] += 1
        out[label] = {k: dict(v) for k, v in res.items()}
        out[label + "_candidates_per_query"] = {
            k: {"mean": round(sum(v) / len(v), 2), "max": max(v)} for k, v in cands.items()}
        out[label + "_seconds_per_query"] = round((time.time() - t0) / max(1, len(sample)), 2)
    h.layer_on()
    return out


def run_c(h: Harness, gold: list[dict], n: int, rng: random.Random) -> dict:
    """A Latin spelling of a listed party that the lists do not themselves carry:
    remove the queried spelling (and any spelling with the same tokens) from the
    index, and see whether the party still comes back through its others."""
    pool = [e for e in gold if e["etype"] == "INDIVIDUAL" and len(e["latin"]) >= 3]
    sample = rng.sample(pool, min(n, len(pool)))
    out = {}
    for label in ("new", "previous"):
        h.layer_on() if label == "new" else h.layer_off()
        res = collections.Counter()
        base = FakeSanctionsTable.from_records(h.data["records"], include_arabic=False)
        for e in sample:
            q = max(e["latin"], key=lambda s: len(s.split()))
            key = " ".join(sorted(set(ss._normalize_name(q).split())))
            # The same table, minus the one row that IS the queried spelling.
            h.use_table(FakeSanctionsTable([
                r for r in base.rows
                if not (r["list_code"] == e["list"] and r["entity_id"] == e["id"]
                        and r["name_key"] == key)]))
            h.exclude = key
            r = h.screen(q)
            h.exclude = None
            others = own_name_set({"latin": [x for x in e["latin"] if x != q], "arabic": []})
            got = surfaced(r, others, key)
            grade_counts(r, others, res, key)
            res["n"] += 1
            res["finding" if got == "finding" else "candidate" if got else "miss"] += 1
        out[label] = dict(res)
    h.layer_on()
    return out


NEGATIVE_ARABIC = [
    "سالم بن سعيد البلوشي", "خالد بن محمد الحارثي", "فاطمة بنت علي الهنائية",
    "ناصر بن حمد الكندي", "محمد بن عبدالله الريامي", "أحمد بن سالم المعمري",
    "سعيد بن راشد الشحي", "عبدالله بن خميس العبري", "يوسف بن علي اللواتي",
    "مريم بنت سالم الرواحية", "هلال بن ناصر السيابي", "حمد بن سيف الغافري",
    "شركة النور للتجارة", "مؤسسة الواحة للمقاولات", "شركة مسقط للخدمات الهندسية",
    "مجموعة الأفق القابضة", "عبدالرحمن بن صالح الفارسي", "إبراهيم بن يعقوب الحبسي",
    "ثريا بنت حمود المنذرية", "بدر بن عبدالله الجابري",
]
NEGATIVE_LATIN = [
    "Salim bin Said Al Balushi", "Khalid bin Mohammed Al Harthy",
    "Fatima bint Ali Al Hinai", "Nasser bin Hamad Al Kindi",
    "Mohammed bin Abdullah Al Riyami", "Ahmed bin Salim Al Maamari",
    "Said bin Rashid Al Shehhi", "Abdullah bin Khamis Al Abri",
    "Yousuf bin Ali Al Lawati", "Maryam bint Salim Al Rawahi",
    "Al Noor Trading LLC", "Al Waha Contracting Co", "Muscat Engineering Services",
    "Al Ufuq Holding", "Abdulrahman bin Saleh Al Farsi",
    "Maria Garcia", "Star Trading LLC", "John Smith Consulting", "Acme Trading LLC",
    "Joe's Pizza LLC", "Muscat Coffee House", "Al Noor Enterprises",
    "Bright Star Trading Company", "Gulf General Trading LLC", "Sam's Barbershop",
]


def run_d(h: Harness, n: int) -> dict:
    out = {}
    names = NEGATIVE_ARABIC + NEGATIVE_LATIN
    for label in ("new", "previous"):
        h.layer_on() if label == "new" else h.layer_off()
        h.use_table(FakeSanctionsTable.from_records(h.data["records"], include_arabic=True))
        res = collections.Counter()
        rows = []
        for q in names:
            r = h.screen(q)
            nm = len(r.get("matches") or [])
            nc = len(r.get("possible_matches_unverified") or [])
            res["queries"] += 1
            res["findings"] += 1 if nm else 0
            res["with_candidates"] += 1 if nc else 0
            res["candidates_total"] += nc
            for m in r.get("possible_matches_unverified") or []:
                if m.get("_matcher") == "name_sound_match":
                    res["sound_" + str(m.get("match_confidence"))] += 1
            res["status_" + str(r.get("screening_status"))] += 1
            if nm or nc:
                rows.append((q, nm, nc))
        out[label] = {"summary": dict(res), "queries_with_hits": rows[:40]}
    h.layer_on()
    return out


def negative_groups() -> dict:
    path = os.path.join(REPO, "tests", "fixtures", "sanctions_negative_names.json")
    with open(path, encoding="utf-8") as fh:
        fx = json.load(fh)
    return {"english": [f"{a} {b}" for a, b in fx["english_pairs"]],
            "gulf_latin": [b for a, b in fx["gulf"]],
            "gulf_arabic": [a for a, b in fx["gulf"]]}


def run_n(h: Harness) -> dict:
    """Ordinary names, new matcher against the previous one: findings, engagement and candidates by grade."""
    out = {}
    groups = negative_groups()
    for label in ("new", "previous"):
        h.layer_on() if label == "new" else h.layer_off()
        h.use_table(FakeSanctionsTable.from_records(h.data["records"], include_arabic=True))
        out[label] = {}
        for g, names in groups.items():
            res = collections.Counter()
            for q in names:
                r = h.screen(q)
                cands = [c for c in r.get("possible_matches_unverified") or [] if c.get("_matcher") == "name_sound_match"]
                res["queries"] += 1
                res["findings"] += 1 if r.get("matches") else 0
                res["read_as_arabic"] += 1 if (r.get("arabic_matching") or {}) else 0
                res["with_sound_candidate"] += 1 if cands else 0
                for c in cands:
                    res["candidates_" + str(c.get("match_confidence"))] += 1
                res["status_" + str(r.get("screening_status"))] += 1
            out[label][g] = dict(res)
    h.layer_on()
    return out


def run_e(h: Harness, gold: list[dict], n: int, rng: random.Random) -> dict:
    """A GENERIC FUZZY BASELINE, for context. RapidFuzz token_sort_ratio (name
    order does not matter) over every Latin name on the three lists, on the same
    sample and the same queries as measurement C, and on the same names as
    measurement D. This is NOT OpenSanctions' logic-v2 matcher (which needs the
    nomenklatura and rigour packages and was not installed for this work); it is
    what a plain off-the-shelf string matcher does with Arabic-romanised names.
    It has no answer for measurement B at all: an Arabic-script query has nothing
    for a string matcher to compare with."""
    from rapidfuzz import fuzz, process

    seen: set[str] = set()
    choices: list[str] = []
    for recs in h.data["records"].values():
        for r in recs:
            if not an.has_arabic_script(r["name"]) and r["name"].lower() not in seen:
                seen.add(r["name"].lower())
                choices.append(r["name"])
    for row in csv.reader(io.StringIO(h.data["sdn"])):
        if len(row) >= 4 and row[1] and row[1].lower() not in seen:
            seen.add(row[1].lower())
            choices.append(row[1])
    for row in csv.reader(io.StringIO(h.data["alt"])):
        if len(row) >= 4 and row[3] and row[3].lower() not in seen and row[3] != "-0-":
            seen.add(row[3].lower())
            choices.append(row[3])

    def fold(s):
        return " ".join(sorted(set(ss._normalize_name(s).split())))

    pool = [e for e in gold if e["etype"] == "INDIVIDUAL" and len(e["latin"]) >= 3]
    sample = rng.sample(pool, min(n, len(pool)))
    out = {"choices": len(choices), "C_latin_variant_query": {}, "D_names_not_on_the_lists": {}}
    for cutoff in (80, 85, 90):
        c = collections.Counter()
        for e in sample:
            q = max(e["latin"], key=lambda s: len(s.split()))
            others = own_name_set({"latin": [x for x in e["latin"] if x != q], "arabic": []})
            qk = fold(q)
            res = process.extract(q, choices, scorer=fuzz.token_sort_ratio,
                                  score_cutoff=cutoff, limit=200)
            # the spelling that IS the query is not an answer (it was removed from
            # the index in measurement C)
            res = [r for r in res if fold(r[0]) != qk]
            c["n"] += 1
            c["surfaced" if any(fold(r[0]) in others for r in res) else "miss"] += 1
            c["results_total"] += len(res)
        out["C_latin_variant_query"][f"cutoff_{cutoff}"] = dict(c)
        d = collections.Counter()
        for q in NEGATIVE_LATIN:
            res = process.extract(q, choices, scorer=fuzz.token_sort_ratio,
                                  score_cutoff=cutoff, limit=200)
            d["queries"] += 1
            d["with_results"] += 1 if res else 0
            d["results_total"] += len(res)
        out["D_names_not_on_the_lists"][f"cutoff_{cutoff}"] = dict(d)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lists-dir", required=True)
    ap.add_argument("--sample", type=int, default=150)
    ap.add_argument("--modes", default="ABCD")
    ap.add_argument("--seed", type=int, default=20261003)
    ap.add_argument("--json")
    a = ap.parse_args(argv)

    data = load_lists(a.lists_dir)
    gold = gold_entities(data["records"])
    print(f"gold entries (Latin + Arabic-script name on one entry): {len(gold)} "
          f"({sum(1 for e in gold if e['etype'] == 'INDIVIDUAL')} individuals)", flush=True)
    # One generator PER MEASUREMENT, seeded by the measurement's letter: the
    # sample a measurement draws must not depend on which others were run
    # before it, or the same --seed would give different samples in an ABCD run
    # and a CD run, and C and E (which compare on the same names) would differ.
    def rng_for(mode: str) -> random.Random:
        return random.Random(f"{a.seed}-{'C' if mode == 'E' else mode}")

    report: dict = {"gold_entries": len(gold), "sample": a.sample, "seed": a.seed}
    with Harness(data) as h:
        t0 = time.time()
        h.use_table(FakeSanctionsTable.from_records(data["records"], include_arabic=True))
        h.warm()
        print(f"sound indexes built in {time.time() - t0:.1f}s (OFAC {len(ss._ofac_phonetic_state['index'])} names; "
              + ", ".join(f"{k} {len(v['index'])}" for k, v in ss._list_phonetic_state.items()) + ")",
              flush=True)
        if "A" in a.modes:
            report["A_arabic_alias_in_index"] = run_a(h, gold, a.sample, rng_for("A"))
            print("A", report["A_arabic_alias_in_index"], flush=True)
        if "B" in a.modes:
            report["B_arabic_query_sound_only"] = run_b(h, gold, a.sample, rng_for("B"))
            print("B", json.dumps(report["B_arabic_query_sound_only"], indent=1), flush=True)
        if "C" in a.modes:
            report["C_latin_variant_query"] = run_c(h, gold, a.sample, rng_for("C"))
            print("C", report["C_latin_variant_query"], flush=True)
        if "D" in a.modes:
            report["D_names_not_on_the_lists"] = run_d(h, a.sample)
            print("D", json.dumps(report["D_names_not_on_the_lists"], indent=1, ensure_ascii=False), flush=True)
        if "N" in a.modes:
            report["N_ordinary_names"] = run_n(h)
            print("N", json.dumps(report["N_ordinary_names"], indent=1), flush=True)
        if "E" in a.modes:
            report["E_generic_fuzzy_baseline"] = run_e(h, gold, a.sample, rng_for("E"))
            print("E", json.dumps(report["E_generic_fuzzy_baseline"], indent=1), flush=True)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
