# numblame

`git blame` for numbers. Point at any figure in the output of a SQL pipeline and numblame
tells you which source rows produced it, why it differs from the last run, and which
commit changed it.

It runs your pipeline on [DuckDB](https://duckdb.org) and rewrites each query so that
every output row carries the IDs of the source rows behind it.

```
$ numblame why 2766.19
Found in 'revenue_by_region', column 'revenue' (run 20261116T174450753648-c5cc7f2b):
  region  orders  revenue
  ------  ------  -------
  South   9       2766.19

It was built from 11 source row(s):

  orders: 9 row(s), showing 4
    order_id  customer_id  order_date  status     amount
    --------  -----------  ----------  ---------  ------
    O008      C03          2026-10-18  completed  522.4
    O009      C03          2026-10-07  completed  522.66
    O016      C02          2026-10-11  completed  165.17
    O018      C02          2026-10-27  completed  524.26

  customers: 2 row(s)
    customer_id  name        region  segment
    -----------  ----------  ------  ---------
    C02          Customer 2  South   retail
    C03          Customer 3  South   wholesale

Path through the pipeline (rows fed by those source rows):
  clean_orders: 9 row(s)
  order_revenue: 9 row(s)
  revenue_by_region: 1 row(s)  <- this row
```

## Install

Requires Python 3.11 or newer.

```bash
git clone https://github.com/manasdutt2003/numblame
cd numblame
pip install -e .
```

## Try the example

```bash
cd examples/shop
numblame run
numblame why revenue_by_region --where "region = 'South'"
```

Now break something. Add `AND amount < 500` to `steps/01_clean_orders.sql`, delete the
`O016` line from `data/orders.csv`, and run again:

```
$ numblame run
$ numblame diff revenue_by_region --where "region = 'South'" --measure orders.amount
'revenue_by_region' where region = 'South': run 20261116T174450753648-c5cc7f2b -> run 20261116T174451888319-f3b0f191
  orders: 9 -> 5  (-4)
  revenue: 2766.19 -> 1031.7  (-1734.49)

Why:
  - 3 source row(s) no longer feed this row: now dropped at step 'clean_orders'; orders.amount over these rows: -1,569.32
      e.g. orders:O008, orders:O009, orders:O018
  - 1 source row(s) no longer feed this row: no longer in the input; orders.amount over these rows: -165.17
      e.g. orders:O016

Steps whose SQL differs between the runs: clean_orders
Source files that differ between the runs: orders
```

The two causes are separated: three rows are still in the input but a changed step now
filters them out, and one row has gone from the input file.

## Commands

| Command | What it does |
| --- | --- |
| `numblame run` | Runs the pipeline with lineage and stores the result under `.numblame/` |
| `numblame runs` | Lists stored runs |
| `numblame why <value>` | Finds the value in the output tables and shows the source rows behind it |
| `numblame why <table> --where "<filter>"` | Same, for the output rows the filter selects |
| `numblame diff <table> --where "<filter>"` | Explains how one output row changed between two runs (default: previous and latest) |
| `numblame bisect <table> <column> --good <commit>` | Finds the git commit that changed a value |

Run any command with `-h` for its options. Use `-C <folder>` to point at a project
without changing directory.

### bisect

`bisect` reruns the pipeline at commits between a known-good commit and a bad one
(default `HEAD`), using a binary search, and reports the first commit at which the
value differs from its value at the good commit.

```
$ numblame bisect revenue_by_region revenue --where "region = 'South'" --good v1
Looking for the commit that changed revenue_by_region.revenue between v1 and HEAD
  8bd0b39aff  revenue = 2766.19
  501b0f1072  revenue = 1196.87
  e90be9dd31  revenue = 1196.87
  0580bd5a93  revenue = 2766.19

First commit where the value changed (tested 4 of 7 commits):
  e90be9d Demo, 2026-11-16: Exclude orders of 500 or more as suspected test data
  revenue: 2766.19 -> 1196.87

  steps/01_clean_orders.sql | 1 +
  1 file changed, 1 insertion(+)
```

Source files that are tracked in git are taken from each commit. Untracked source files
are taken from the working tree, so in that case bisect only finds changes to the SQL.

## Setting up a project

A project is a folder with a `numblame.toml`, input files and SQL steps:

```
my-pipeline/
  numblame.toml
  data/orders.csv
  steps/01_clean_orders.sql
  steps/02_revenue_by_region.sql
```

```toml
steps = "steps"

[sources.orders]
path = "data/orders.csv"      # .csv or .parquet
key = ["order_id"]            # optional
```

- Steps run in file-name order. Each file holds one `SELECT` and creates a table named
  after the file with the leading number removed (`01_clean_orders.sql` creates
  `clean_orders`). Later steps can read earlier ones.
- `key` names the column or columns that identify a row. With a key, `diff` can tell
  that a row's contents changed. Without one, rows are identified by their content,
  so an edited row shows up as one row removed and one added.

## How it works

Every source table gets a hidden `_src` column holding a one-element list with the row's
ID, such as `['orders:O016']`. Before a step runs, its SQL is parsed with
[sqlglot](https://github.com/tobymao/sqlglot) and one column is added to each `SELECT`:

| Query shape | Lineage of an output row |
| --- | --- |
| Filter, projection, window function | The row's own `_src` |
| Join | The `_src` lists of the joined rows, concatenated |
| `GROUP BY` or aggregate | The `_src` lists of every row in the group |
| `SELECT DISTINCT` | Rewritten to `GROUP BY ALL`, so merged duplicates merge their lineage |
| CTE, subquery in `FROM`, `UNION ALL` | Each part is rewritten the same way |

For example, this step:

```sql
SELECT region, sum(amount) AS revenue FROM order_revenue GROUP BY region
```

runs as:

```sql
SELECT region, sum(amount) AS revenue,
       list_distinct(flatten(list(order_revenue._src))) AS _src
FROM order_revenue GROUP BY region
```

Each run is stored as a DuckDB file in `.numblame/runs/`, with a manifest of the SQL, the
git commit and a SHA-256 of every input file. A copy of each input is kept in
`.numblame/objects/` under its hash.

## Limitations

- **Row-level, not column-level.** Lineage says which source rows fed an output row. It
  does not say which of them fed a particular column.
- **Influence through subqueries in `WHERE` is not recorded.** In
  `WHERE amount > (SELECT avg(amount) FROM orders)`, the rows behind the average are not
  part of the lineage. The same goes for `IN (SELECT ...)` and for window functions:
  a row's lineage is itself, not its window.
- **`--measure` is a total, not an attribution.** It sums a source column over the rows
  that came or went. That equals the change in the output only when the output is a
  plain sum of that column.
- **Unsupported SQL is rejected, not guessed at:** `UNION`, `INTERSECT` and `EXCEPT`
  (a lineage column would change their result), `DISTINCT ON`, and table functions in
  `FROM`.
- **Cost.** Lineage lists grow with group size, so a total over ten million rows carries
  ten million IDs. It has only been run on small data so far.
- **`bisect` assumes one change point.** If the value changed and changed back in
  between, it finds one of the changes, as `git bisect` would.

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```

## License

MIT
