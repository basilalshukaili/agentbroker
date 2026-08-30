"""preflight must not silently stop covering a CI step.

Its whole value is the sentence it prints last - "safe to push". That is only
worth anything if the set of things it ran matches the set of things CI runs,
so every way it can quietly cover less than it claims is a defect in the
guard itself.

Two were live:

  * a `run:` BLOCK was split on newlines and each line treated as its own
    command, so `for i in 1 2 3; do` / `echo ...` / `done` became three
    fragments, none runnable and none of them what CI executes;
  * a step whose `run` was not a plain string was dropped with no record -
    the output looked identical whether it was covered or not.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREFLIGHT = os.path.join(ROOT, "scripts", "preflight.py")


def _load():
    spec = importlib.util.spec_from_file_location("_preflight", PREFLIGHT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pf():
    return _load()


def test_a_multi_line_run_block_stays_one_command(pf):
    job = {"steps": [{
        "name": "Retry loop",
        "run": 'for i in 1 2 3; do\n  echo "try $i"\ndone\n',
    }]}
    steps, unparsed = pf._steps(job)
    assert not unparsed
    assert len(steps) == 1, (
        f"a shell block was split into {len(steps)} 'commands'; CI runs it as "
        f"one, and the fragments are not valid on their own")
    assert steps[0]["cmd"].startswith("for i in 1 2 3; do")
    assert steps[0]["cmd"].rstrip().endswith("done")


def test_a_line_continuation_is_not_cut_in_half(pf):
    job = {"steps": [{"name": "Long", "run": "python x.py \\\n  --flag\n"}]}
    steps, _ = pf._steps(job)
    assert len(steps) == 1
    assert "--flag" in steps[0]["cmd"]


def test_a_run_this_parser_cannot_read_is_reported_not_dropped(pf):
    """The silent `continue` is the bug. Coverage that disappears without a
    line of output is indistinguishable from coverage that is there."""
    job = {"steps": [
        {"name": "Weird step", "run": ["python a.py", "python b.py"]},
        {"name": "Normal", "run": "python ok.py"},
    ]}
    steps, unparsed = pf._steps(job)
    assert len(steps) == 1
    assert unparsed, "an unreadable step vanished with no record"
    assert "Weird step" in unparsed[0]
    assert "NOT covering" in unparsed[0]


def test_uses_steps_are_not_commands(pf):
    job = {"steps": [{"uses": "actions/checkout@v4"}]}
    steps, unparsed = pf._steps(job)
    assert not steps and not unparsed


def test_comments_and_blank_blocks_produce_nothing(pf):
    job = {"steps": [{"name": "c", "run": "# just a comment\n\n"}]}
    steps, unparsed = pf._steps(job)
    assert not steps and not unparsed


def test_every_run_step_in_the_real_workflow_is_accounted_for(pf):
    """The end-to-end version: nothing in ci.yml may be invisible to
    preflight. Either it runs, or it appears in the skipped list with a
    reason."""
    yaml = pytest.importorskip("yaml")
    with open(pf.WORKFLOW, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)

    in_workflow = sum(
        1
        for job in (wf.get("jobs") or {}).values() if isinstance(job, dict)
        for st in (job.get("steps") or [])
        if isinstance(st, dict) and isinstance(st.get("run"), str)
        and st["run"].strip()
        and not all(l.strip().startswith("#") or not l.strip()
                    for l in st["run"].splitlines())
    )
    runnable, skipped = pf.collect()
    assert in_workflow > 0
    assert len(runnable) + len(skipped) == in_workflow, (
        f"ci.yml has {in_workflow} run steps; preflight accounts for "
        f"{len(runnable)} run + {len(skipped)} skipped. The difference is "
        f"coverage nobody is told about.")
