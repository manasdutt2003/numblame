# Architecture

numblame has three modules under `src/numblame/`.

| Module | Responsibility |
| --- | --- |
| `rewrite.py` | Takes the SQL of one step and returns the same query with an extra `_src` output column holding the IDs of the source rows behind each output row. Uses [sqlglot](https://github.com/tobymare/sqlglot) to parse and rebuild the query. |
| `engine.py` | Reads `numblame.toml` and the step files, loads sources into DuckDB with a `_src` column, runs each rewritten step, stores the run, and answers questions (`why`, `diff`, `bisect`) from stored runs. |
| `cli.py` | Argument parsing, output formatting, logging and exit codes. Contains no lineage logic. |

## Data flow of `numblame run`

1. `load_project` reads `numblame.toml` and `steps/*.sql` in file-name order.
2. Each source file (CSV or Parquet) is loaded into a DuckDB table. Each row gets a `_src` value such as `['orders:O016']`, built from the declared `key` columns, or from a content hash when no key is declared. A non-unique key is an error.
3. Each step is passed through `add_lineage` and created as a table named after the file.
4. The database is stored as `.numblame/runs/<run id>.duckdb`. A manifest table records the SQL of every step, the git commit (if any) and a SHA-256 of each input file. Input files are copied to `.numblame/objects/<hash>`.
5. If any step fails, the partial run file is deleted and the error names the step.

## How lineage is combined

See the table in the README. In short: filters and projections keep the row's `_src`; joins concatenate the `_src` lists of the joined rows; `GROUP BY` or aggregates flatten and de-duplicate the lists of the group; `DISTINCT` is rewritten as `GROUP BY ALL`; CTEs, derived tables and `UNION ALL` are rewritten recursively.

SQL whose meaning a lineage column would change (`UNION`, `INTERSECT`, `EXCEPT`, `DISTINCT ON`, table functions in `FROM`, a `SELECT` with no `FROM`) raises `UnsupportedSQL` instead of producing a guess.

## Questions answered from stored runs

* `why` looks up a row by value or by `--where`, then resolves its `_src` IDs back to source rows and counts, per step, how many rows those IDs feed.
* `diff` compares the `_src` sets of one output row in two runs and classifies each source row that came or went as: no longer in the input, newly in the input, or dropped or kept by a changed step. It also reports rows present in both runs whose content hash changed.
* `bisect` re-runs the pipeline at commits between a good and a bad commit using a binary search, and reports the first commit where the value differs.

## Configuration

| Setting | Where | Default |
| --- | --- | --- |
| Project folder | `-C/--project` or `NUMBLAME_PROJECT` | current folder |
| Log level | `-v/--verbose` (DEBUG) or `NUMBLAME_LOG_LEVEL` | `WARNING` |
| Sources, keys, steps folder | `numblame.toml` | `steps = "steps"` |

Logs go to stderr; command results go to stdout. Exit codes: `0` success, `1` nothing found (`why`), `2` error.

## Docker

```bash
docker build -t numblame .
docker run --rm -v "$PWD/examples/shop:/project" numblame run
docker run --rm -v "$PWD/examples/shop:/project" numblame why revenue_by_region --where "region = 'South'"
```

The image includes `git` because `bisect` and the run manifest call it. Run state is written to `.numblame/` inside the mounted folder.

## Design decisions and limits

* **Row-level lineage, not column-level.** This keeps the rewrite small enough to reason about.
* **Reject rather than guess.** Unsupported SQL fails loudly.
* **DuckDB file per run.** Runs are self-contained and can be opened read-only, at the cost of disk space proportional to the data plus lineage lists.
* **Lineage lists grow with group size.** It has only been run on small data so far.

See the README's Limitations section for the full list.
