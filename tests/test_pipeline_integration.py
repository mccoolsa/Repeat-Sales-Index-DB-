"""The pipeline runner runs stages in order, logs them and stops on failure."""

import json
from pathlib import Path

import pytest

from ireland_rsi import pipeline
from ireland_rsi.storage import DuckDBTableStore

JOB = '''
from pathlib import Path
def main(argv):
    root = Path(argv[argv.index("--project-root") + 1])
    with open(root / "calls.txt", "a") as f:
        f.write("{name} " + " ".join(argv[argv.index("--profile"):]) + "\\n")
    {body}
'''


def make_jobs(root: Path, failing: str | None = None):
    (root / "jobs").mkdir()
    for stage, script, _ in pipeline.STAGES:
        body = "raise RuntimeError('boom')" if stage == failing else "return 0"
        (root / "jobs" / script).write_text(JOB.format(name=stage, body=body),
                                            encoding="utf-8")


def calls(root):
    return [line.split()[0] for line in (root / "calls.txt").read_text().splitlines()]


def test_stages_run_in_dependency_order(tmp_path):
    make_jobs(tmp_path)
    tables = DuckDBTableStore()
    out = pipeline.run_pipeline(tmp_path, "donegal", "local", tables, "run-1")
    assert out["succeeded"]
    # train_linkage.py serves both tracks; the log has one row per stage.
    assert [s["stage"] for s in out["stages"]] == [s[0] for s in pipeline.STAGES]
    assert calls(tmp_path)[-1] == "publish"
    log = tables.query("SELECT stage, status FROM silver.pipeline_runs")
    assert len(log) == len(pipeline.STAGES) and set(log.status) == {"succeeded"}


def test_a_failure_stops_the_run_before_publish(tmp_path):
    make_jobs(tmp_path, failing="validation")
    tables = DuckDBTableStore()
    out = pipeline.run_pipeline(tmp_path, "donegal", "local", tables, "run-2")
    assert not out["succeeded"]
    assert out["stages"][-1]["stage"] == "validation"
    assert "boom" in out["stages"][-1]["error"]
    assert "publish" not in calls(tmp_path)
    failed = tables.query("SELECT detail_json FROM silver.pipeline_runs WHERE status = 'failed'")
    assert "boom" in json.loads(failed.iloc[0, 0])["error"]


def test_restart_from_a_stage_skips_completed_ones(tmp_path):
    make_jobs(tmp_path)
    tables = DuckDBTableStore()
    out = pipeline.run_pipeline(tmp_path, "donegal", "local", tables, "run-3",
                                start="validation")
    assert calls(tmp_path) == ["validation", "backtest", "publish"]
    assert out["succeeded"]


def test_stage_arguments_are_passed_through(tmp_path):
    make_jobs(tmp_path)
    pipeline.run_pipeline(tmp_path, "donegal", "local", DuckDBTableStore(), "run-4",
                          start="linkage_production", stop="linkage_production")
    line = (tmp_path / "calls.txt").read_text().strip()
    assert line.endswith("--profile donegal --track production")


def test_unknown_stage_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        pipeline.run_pipeline(tmp_path, "donegal", "local", DuckDBTableStore(), "run-5",
                              start="nonexistent")
