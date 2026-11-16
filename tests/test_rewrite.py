import duckdb
import pytest

from numblame.rewrite import UnsupportedSQL, add_lineage


@pytest.fixture
def con():
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES "
        "(1, 'a', 10, ['orders:1']), (2, 'a', 20, ['orders:2']), (3, 'b', 30, ['orders:3']), (4, 'z', 40, ['orders:4'])"
        ") t(id, cust, amount, _src)"
    )
    con.execute(
        "CREATE TABLE customers AS SELECT * FROM (VALUES ('a', 'North', ['customers:a']), ('b', 'South', ['customers:b'])) t(cust, region, _src)"
    )
    return con


def run(con, sql):
    """Run the rewritten query; return rows as dicts with _src as a sorted list."""
    cursor = con.execute(add_lineage(sql))
    names = [d[0] for d in cursor.description]
    rows = [dict(zip(names, row)) for row in cursor.fetchall()]
    for row in rows:
        row["_src"] = sorted(row["_src"])
    return rows


def test_filter_keeps_each_rows_own_lineage(con):
    rows = run(con, "SELECT id, amount FROM orders WHERE amount > 15 ORDER BY id")
    assert [(r["id"], r["_src"]) for r in rows] == [(2, ["orders:2"]), (3, ["orders:3"]), (4, ["orders:4"])]


def test_join_combines_both_sides(con):
    rows = run(con, "SELECT o.id, c.region FROM orders o JOIN customers c ON o.cust = c.cust ORDER BY o.id")
    assert rows[0] == {"id": 1, "region": "North", "_src": ["customers:a", "orders:1"]}
    assert len(rows) == 3


def test_left_join_without_a_match_keeps_the_left_lineage(con):
    rows = run(con, "SELECT o.id, c.region FROM orders o LEFT JOIN customers c ON o.cust = c.cust WHERE o.id = 4")
    assert rows == [{"id": 4, "region": None, "_src": ["orders:4"]}]


def test_group_by_collects_the_whole_group(con):
    rows = run(con, "SELECT cust, sum(amount) AS total FROM orders GROUP BY cust ORDER BY cust")
    assert rows[0] == {"cust": "a", "total": 30, "_src": ["orders:1", "orders:2"]}


def test_aggregate_without_group_by(con):
    rows = run(con, "SELECT count(*) AS n FROM orders")
    assert rows == [{"n": 4, "_src": ["orders:1", "orders:2", "orders:3", "orders:4"]}]


def test_distinct_merges_lineage_of_duplicates(con):
    rows = run(con, "SELECT DISTINCT cust FROM orders ORDER BY cust")
    assert [r["cust"] for r in rows] == ["a", "b", "z"]
    assert rows[0]["_src"] == ["orders:1", "orders:2"]


def test_star_does_not_duplicate_the_lineage_column(con):
    rows = run(con, "SELECT * FROM orders o JOIN customers c ON o.cust = c.cust WHERE o.id = 3")
    assert list(rows[0]).count("_src") == 1
    assert rows[0]["_src"] == ["customers:b", "orders:3"]


def test_cte_and_subquery_carry_lineage_through(con):
    sql = """
        WITH big AS (SELECT id, cust, amount FROM orders WHERE amount >= 20)
        SELECT r.region, sum(b.amount) AS total
        FROM big b JOIN (SELECT cust, region FROM customers) r ON b.cust = r.cust
        GROUP BY r.region ORDER BY r.region
    """
    rows = run(con, sql)
    assert rows == [
        {"region": "North", "total": 20, "_src": ["customers:a", "orders:2"]},
        {"region": "South", "total": 30, "_src": ["customers:b", "orders:3"]},
    ]


def test_union_all(con):
    rows = run(con, "SELECT id FROM orders WHERE id = 1 UNION ALL SELECT id FROM orders WHERE id = 2")
    assert sorted(tuple(r["_src"]) for r in rows) == [("orders:1",), ("orders:2",)]


def test_window_function_is_not_treated_as_an_aggregate(con):
    rows = run(con, "SELECT id, sum(amount) OVER () AS total FROM orders ORDER BY id")
    assert len(rows) == 4
    assert rows[0] == {"id": 1, "total": 100, "_src": ["orders:1"]}


def test_results_are_unchanged_apart_from_the_extra_column(con):
    sql = "SELECT cust, count(*) AS n, sum(amount) AS total FROM orders GROUP BY cust ORDER BY cust"
    plain = con.execute(sql).fetchall()
    traced = [row[:-1] for row in con.execute(add_lineage(sql)).fetchall()]
    assert traced == plain


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders UNION SELECT id FROM orders",
        "SELECT id FROM orders EXCEPT SELECT id FROM orders",
        "SELECT DISTINCT ON (cust) cust, id FROM orders",
        "SELECT 1 AS one",
        "SELECT * FROM read_csv_auto('x.csv')",
        "DELETE FROM orders",
    ],
)
def test_unsupported_sql_is_rejected_rather_than_silently_wrong(sql):
    with pytest.raises(UnsupportedSQL):
        add_lineage(sql)
