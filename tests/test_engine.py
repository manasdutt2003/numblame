import subprocess

import pytest

from numblame.cli import main
from numblame.engine import NumblameError, Run, bisect, diff, load_project, run

ORDERS = """order_id,customer_id,status,amount
O1,C1,completed,100
O2,C1,completed,50
O3,C2,completed,70
O4,C2,cancelled,999
O5,C1,completed,30
"""
CUSTOMERS = "customer_id,region\nC1,North\nC2,South\n"
CONFIG = """
[sources.orders]
path = "data/orders.csv"
key = ["order_id"]

[sources.customers]
path = "data/customers.csv"
key = ["customer_id"]
"""
CLEAN = "SELECT order_id, customer_id, amount FROM orders WHERE status = 'completed'"
BY_REGION = """SELECT c.region, sum(o.amount) AS revenue
FROM clean_orders o JOIN customers c ON o.customer_id = c.customer_id
GROUP BY c.region"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "steps").mkdir()
    (tmp_path / "data" / "orders.csv").write_text(ORDERS)
    (tmp_path / "data" / "customers.csv").write_text(CUSTOMERS)
    (tmp_path / "numblame.toml").write_text(CONFIG)
    (tmp_path / "steps" / "01_clean_orders.sql").write_text(CLEAN)
    (tmp_path / "steps" / "02_revenue_by_region.sql").write_text(BY_REGION)
    return tmp_path


def do_run(root):
    return run(load_project(root))


def test_why_traces_a_value_to_its_source_rows(project):
    stored = Run(project, do_run(project))
    hits = stored.find_value("180")
    assert [(table, column) for table, column, _ in hits] == [("revenue_by_region", "revenue")]
    ids = sorted(hits[0][2]["_src"])
    assert ids == ["customers:C1", "orders:O1", "orders:O2", "orders:O5"]

    sources = stored.source_rows(ids)
    assert sources["orders"]["count"] == 3
    assert sorted(row[0] for row in sources["orders"]["rows"]) == ["O1", "O2", "O5"]
    assert stored.path(ids) == [("clean_orders", 3), ("revenue_by_region", 1)]
    stored.close()


def test_diff_reports_a_row_removed_from_the_input(project):
    first = do_run(project)
    (project / "data" / "orders.csv").write_text(ORDERS.replace("O2,C1,completed,50\n", ""))
    second = do_run(project)

    result = diff(Run(project, first), Run(project, second), "revenue_by_region", "region = 'North'", "orders.amount")
    assert result["before"]["revenue"] == 180 and result["after"]["revenue"] == 130
    assert result["removed"] == {"no longer in the input": ["orders:O2"]}
    assert result["added"] == {} and result["changed"] == []
    assert result["measure"][("removed", "no longer in the input")] == 50
    assert result["sources_changed"] == ["orders"] and result["steps_changed"] == []


def test_diff_names_the_step_that_now_drops_a_row(project):
    first = do_run(project)
    (project / "steps" / "01_clean_orders.sql").write_text(CLEAN + " AND amount >= 50")
    second = do_run(project)

    result = diff(Run(project, first), Run(project, second), "revenue_by_region", "region = 'North'")
    assert result["removed"] == {"now dropped at step 'clean_orders'": ["orders:O5"]}
    assert result["steps_changed"] == ["clean_orders"] and result["sources_changed"] == []


def test_diff_reports_added_rows_and_changed_contents(project):
    first = do_run(project)
    changed = ORDERS.replace("O1,C1,completed,100", "O1,C1,completed,125").replace("O4,C2,cancelled,999", "O4,C2,completed,999")
    (project / "data" / "orders.csv").write_text(changed)
    second = do_run(project)
    a, b = Run(project, first), Run(project, second)

    north = diff(a, b, "revenue_by_region", "region = 'North'", "orders.amount")
    assert north["changed"] == ["orders:O1"]
    assert north["measure"][("changed", "")] == 25
    assert north["removed"] == {} and north["added"] == {}

    south = diff(a, b, "revenue_by_region", "region = 'South'")
    assert south["added"] == {"was dropped at step 'clean_orders' before": ["orders:O4"]}


def test_diff_needs_exactly_one_row(project):
    first = do_run(project)
    with pytest.raises(NumblameError, match="Narrow it to one"):
        diff(Run(project, first), Run(project, first), "revenue_by_region", None)


def test_rows_without_a_key_are_identified_by_content(project):
    (project / "numblame.toml").write_text(CONFIG.replace('key = ["order_id"]\n', ""))
    (project / "data" / "orders.csv").write_text(ORDERS + "O5,C1,completed,30\n")  # exact duplicate row
    stored = Run(project, do_run(project))
    ids = stored.rows("revenue_by_region", "region = 'North'")[0]["_src"]
    order_ids = [i for i in ids if i.startswith("orders:")]
    assert len(order_ids) == len(set(order_ids)) == 4
    stored.close()


def test_duplicate_keys_are_rejected(project):
    (project / "data" / "orders.csv").write_text(ORDERS + "O1,C2,completed,5\n")
    with pytest.raises(NumblameError, match="not unique"):
        do_run(project)


def test_a_failing_step_names_itself_and_leaves_no_run_behind(project):
    (project / "steps" / "02_revenue_by_region.sql").write_text("SELECT nope FROM clean_orders")
    with pytest.raises(NumblameError, match="Step 'revenue_by_region' failed"):
        do_run(project)
    assert list((project / ".numblame" / "runs").glob("*.duckdb")) == []


def git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
    )


def test_bisect_finds_the_commit_that_changed_the_value(project):
    git(project, "init", "-q", "-b", "main")
    (project / ".gitignore").write_text(".numblame/\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "initial pipeline")
    git(project, "tag", "good")

    edits = [
        ("README.md", "notes", "add readme"),
        ("steps/02_revenue_by_region.sql", BY_REGION + "\nORDER BY c.region", "sort output"),
        ("steps/01_clean_orders.sql", CLEAN + " AND amount >= 50", "ignore small orders"),
        ("README.md", "more notes", "update readme"),
        ("steps/02_revenue_by_region.sql", BY_REGION + "\nORDER BY 1", "sort by position"),
    ]
    for path, content, message in edits:
        (project / path).write_text(content)
        git(project, "add", "-A")
        git(project, "commit", "-q", "-m", message)

    found = bisect(project, "revenue_by_region", "revenue", "region = 'North'", "good", "HEAD")
    assert "ignore small orders" in found["summary"]
    assert (found["value_before"], found["value_after"]) == (180, 150)
    assert found["tested"] < found["candidates"] + 1  # fewer runs than a linear scan


def test_cli_end_to_end(project, capsys):
    assert main(["-C", str(project), "run"]) == 0
    assert main(["-C", str(project), "why", "180"]) == 0
    out = capsys.readouterr().out
    assert "built from 4 source row(s)" in out and "O5" in out

    (project / "steps" / "01_clean_orders.sql").write_text(CLEAN + " AND amount >= 50")
    assert main(["-C", str(project), "run"]) == 0
    assert main(["-C", str(project), "diff", "revenue_by_region", "--where", "region = 'North'", "--measure", "orders.amount"]) == 0
    out = capsys.readouterr().out
    assert "revenue: 180 -> 150" in out
    assert "now dropped at step 'clean_orders'; orders.amount over these rows: -30.00" in out

    assert main(["-C", str(project), "why", "no-such-value"]) == 1
    assert main(["-C", str(project), "diff", "missing_table"]) == 2
