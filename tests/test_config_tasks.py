from pathlib import Path

from bench.config import _quantity_to_mib
from bench.tasks import generate_tasks

SRC = Path("src_tasks")


def test_thirteen_tasks_in_expected_projects(tasks):
    assert len(tasks) == 13
    ids = {t.id for t in tasks}
    assert ids == {"BUG-01", "FEAT-01", "REF-01", "TOOL-01", "AGENT-01", "FEAT-02", "FEAT-03", "FEAT-04",
                   "FEAT-05", "FEAT-06", "WEB-BUG-01", "WEB-FEAT-01", "WEB-REF-01"}
    by_fixture = {}
    for t in tasks:
        by_fixture.setdefault(t.fixture, []).append(t.id)
    assert {k: len(v) for k, v in by_fixture.items()} == {"mini-calc-buggy": 3, "mini-calc": 2,
                                                          "dark-mode-site": 5, "mini-web": 3}


def test_verifiers_have_no_npm_install_and_prompts_are_verbatim(tasks):
    for t in tasks:
        assert "npm install" not in t.verify_cmd
        assert t.prompt.strip()
    feat01 = next(t for t in tasks if t.id == "FEAT-01")
    assert "verbose: add 2 3 -> 5" in feat01.verify_cmd          # the corrected grader, not the old text check


def test_generated_tasks_match_the_committed_file(tasks):
    generated = generate_tasks(SRC)
    assert [g["id"] for g in generated] == [t.id for t in tasks]
    assert [g["verify_cmd"] for g in generated] == [t.verify_cmd for t in tasks]


def test_profiles(profiles):
    tiny = profiles["tiny"]
    assert (tiny.cpu_limit, tiny.memory, tiny.memory_mib) == ("1", "512Mi", 512.0)
    assert profiles["starve_256"].cpu_limit == "500m"
    sbc = profiles["sbc_arm"]
    assert sbc.cpu_limit == "1600m" and sbc.emulated and sbc.arch == "arm64"
    assert not tiny.emulated


def test_memory_quantities():
    assert _quantity_to_mib("512Mi") == 512 and _quantity_to_mib("8Gi") == 8192 and _quantity_to_mib("1048576") == 1
