#!/usr/bin/env python3
"""Cut the real-list fixture used by tests/unit/test_screen_sanctions_arabic.py.

The tests must run offline and must not depend on a 50 MB download, so they use
an EXCERPT of the publishers' own files: entries that carry an Arabic- or
Persian-script name next to their Latin one, plus the OFAC rows for the same
parties. Nothing in the fixture is invented; every string is a published name.

SELECTION. A hand-picked set (parties the tests name explicitly, chosen for the
variety of the problem: glued words, Persian letters, Maghrebi French spellings,
kunyas, organisations) plus a seeded random sample of the rest, so the fixture is
not only the cases the author thought of. The seed is recorded in the file.

LICENCES. EU: the Commission's reuse policy for its open data (commercial reuse
permitted, attribution). UK: Open Government Licence v3.0. OFAC: US Government
work, public domain. The same terms screen_sanctions.py states for the full
lists.

Usage:
    python scripts/build_arabic_sanctions_fixture.py --lists-dir DIR \
        --uk-report-date 02-Oct-2026 --fetched 2026-10-03
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for p in (REPO, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from core import screen_sanctions as ss          # noqa: E402
import eval_arabic_sanctions as ev               # noqa: E402

OUT = os.path.join(REPO, "tests", "fixtures", "sanctions_arabic_real_entries.json")

HAND_PICKED = [
    ("EU", "551"), ("EU", "534"), ("EU", "923"), ("EU", "1082"), ("EU", "514"),
    ("EU", "1020"), ("EU", "1084"), ("EU", "1095"), ("EU", "642"),
    ("EU", "3780"), ("EU", "3782"), ("EU", "3808"), ("EU", "3825"),
    ("EU", "4000"), ("EU", "4140"), ("EU", "5529"), ("EU", "5759"),
    ("EU", "6017"), ("EU", "6177"), ("EU", "6215"), ("EU", "6303"),
    ("EU", "6304"), ("EU", "6307"), ("EU", "6310"), ("EU", "6400"),
    ("EU", "6478"), ("EU", "6612"),
    ("UK", "IRQ0145"), ("UK", "IRQ0097"), ("UK", "IRQ0112"), ("UK", "AQD0190"),
    ("UK", "AQD0271"), ("UK", "AQD0305"), ("UK", "IRN0168"), ("UK", "SYR0023"),
    # Names the DATABASE-REGEX lookup could not follow, and the in-process sound
    # index can: a word spelled as one in one language and two in another
    # (Gholam Reza / Gholamreza, Shams-abad / Shamsabad), a compound with its
    # article (Abdulhai / Abd al-Hayy), a family name with a long vowel the other
    # script drops. Found by measuring the pipeline against the gold set.
    ("UK", "AFG0042"), ("UK", "INU0352"), ("EU", "149403"), ("EU", "113197"),
    ("EU", "147462"), ("EU", "150654"), ("UK", "IRN0026"), ("UK", "INU0003"),
]
SAMPLE = 30
SEED = 20261003


def norm_key(name: str) -> str:
    return " ".join(sorted(set(ss._normalize_name(name).split())))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lists-dir", required=True)
    ap.add_argument("--uk-report-date", required=True)
    ap.add_argument("--fetched", required=True)
    a = ap.parse_args(argv)

    data = ev.load_lists(a.lists_dir)
    recs_by_entity: dict[tuple, list[dict]] = {}
    for code, recs in data["records"].items():
        for r in recs:
            recs_by_entity.setdefault((code, r["entity_id"]), []).append(r)
    gold = {(e["list"], e["id"]): e for e in ev.gold_entities(data["records"])}

    picked = [k for k in HAND_PICKED if k in gold]
    missing = [k for k in HAND_PICKED if k not in gold]
    if missing:
        print("not in the gold set (skipped):", missing)
    rng = random.Random(SEED)
    rest = sorted(k for k in gold if k not in picked)
    picked += rng.sample(rest, SAMPLE)

    # OFAC counterparts: same party, found by an identical Latin token set.
    sdn = list(csv.reader(io.StringIO(data["sdn"])))
    alt = list(csv.reader(io.StringIO(data["alt"])))
    by_key: dict[str, set] = {}
    for r in sdn:
        if len(r) >= 4:
            by_key.setdefault(norm_key(r[1]), set()).add(r[0])
    for r in alt:
        if len(r) >= 4:
            by_key.setdefault(norm_key(r[3]), set()).add(r[0])

    entities, ofac_ents = [], set()
    for k in picked:
        e = gold[k]
        recs = recs_by_entity[k]
        ofac = None
        for lat in e["latin"]:
            key = norm_key(lat)
            if len(key.split()) >= 2 and key in by_key:
                ofac = sorted(by_key[key])[0]
                break
        if ofac:
            ofac_ents.add(ofac)
        entities.append({
            "list": e["list"], "id": e["id"], "etype": e["etype"],
            "programme": recs[0].get("programme", ""),
            "countries": recs[0].get("countries", []),
            "latin": e["latin"][:10], "arabic": e["arabic"][:4],
            "ofac_ent": ofac,
        })

    sdn_rows = [r for r in sdn if len(r) >= 4 and r[0] in ofac_ents]
    alt_rows = [r for r in alt if len(r) >= 4 and r[0] in ofac_ents]
    out = {
        "_about": ("Excerpt of the publishers' own EU, UK and OFAC list files: "
                   "entries that carry an Arabic or Persian script name beside "
                   "the Latin one. Built by scripts/build_arabic_sanctions_fixture.py. "
                   "Every string is a published name; nothing is invented."),
        "seed": SEED,
        "hand_picked": len([k for k in picked[:len(HAND_PICKED)]]),
        "snapshot": {
            "EU": {"publisher": "European Commission, consolidated financial sanctions (FSF)",
                   "licence": "European Commission open data reuse policy (commercial reuse permitted, attribution)",
                   "fetched": a.fetched},
            "UK": {"publisher": "UK Foreign, Commonwealth & Development Office, UK Sanctions List",
                   "licence": "Open Government Licence v3.0",
                   "report_date": a.uk_report_date, "fetched": a.fetched},
            "OFAC": {"publisher": "US Department of the Treasury, OFAC (SDN.CSV, ALT.CSV)",
                     "licence": "US Government work, public domain", "fetched": a.fetched},
        },
        "entities": entities,
        "ofac_sdn_rows": sdn_rows,
        "ofac_alt_rows": alt_rows,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=0)
    print(f"{len(entities)} entities, {len(sdn_rows)} SDN rows, {len(alt_rows)} ALT rows -> {OUT} "
          f"({os.path.getsize(OUT) // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
