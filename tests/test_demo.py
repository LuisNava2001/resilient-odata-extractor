import re

from odata_extractor.demo import render, run_all

from .helpers import REPO_ROOT


def test_demo_runs_are_deterministic_and_committed_data_is_current() -> None:
    first = render(run_all())
    assert first == render(run_all())
    committed = (REPO_ROOT / "demo" / "data.js").read_text(encoding="utf-8")
    assert committed == first, "run `python -m odata_extractor demo --out demo/data.js`"
    assert "127.0.0.1:" not in first  # no ports or hosts in recorded traces


def test_demo_covers_the_required_stories() -> None:
    statuses = {t["name"]: t["status"] for t in run_all()}
    assert statuses["healthy"] == "COMPLETE"
    assert statuses["rate_limit"] == "COMPLETE"
    assert statuses["nextlink_cycle"] == "INCOMPLETE"
    assert statuses["duplicate_page"] == "INCOMPLETE"
    assert statuses["empty_page"] == "INCOMPLETE"
    assert statuses["no_count"] == "INDETERMINATE"


def test_demo_page_is_static_and_offline() -> None:
    demo = REPO_ROOT / "demo"
    script = (demo / "app.js").read_text(encoding="utf-8")
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "fetch(", "XMLHttpRequest"):
        assert forbidden not in script
    for name in ("index.html", "app.js", "styles.css"):
        assert re.search(r"https?://", (demo / name).read_text(encoding="utf-8")) is None, name
