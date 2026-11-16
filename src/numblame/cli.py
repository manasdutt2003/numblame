"""Command-line interface for numblame."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .engine import NumblameError, Run, bisect, diff, list_runs, load_project, run, single_row
from .rewrite import SRC


def _table(columns: list[str], rows: list, indent: str = "  ") -> str:
    cells = [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [max(len(str(c)), *(len(r[i]) for r in cells)) if cells else len(str(c)) for i, c in enumerate(columns)]
    line = lambda values: indent + "  ".join(str(v).ljust(w) for v, w in zip(values, widths)).rstrip()
    return "\n".join([line(columns), line(["-" * w for w in widths]), *(line(r) for r in cells)])


def _show_row(row: dict) -> str:
    visible = {k: v for k, v in row.items() if k != SRC}
    return _table(list(visible), [list(visible.values())])


def cmd_run(args) -> int:
    project = load_project(args.project)
    run_id = run(project)
    stored = Run(args.project, run_id)
    print(f"Run {run_id}")
    for name in stored.sources + stored.steps:
        count = stored.con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        print(f"  {'source' if name in stored.sources else 'step  '}  {name}: {count} rows")
    stored.close()
    return 0


def cmd_runs(args) -> int:
    runs = list_runs(args.project)
    if not runs:
        print("No runs yet. Run `numblame run` first.")
    for run_id in runs:
        stored = Run(args.project, run_id)
        commit = (stored.meta.get("commit") or "")[:10] or "no git commit"
        print(f"{run_id}  {stored.meta['created']}  {commit}")
        stored.close()
    return 0


def cmd_why(args) -> int:
    stored = Run(args.project, args.run)
    if args.where is not None:
        if not args.target:
            raise NumblameError("With --where, give the table name: numblame why <table> --where \"...\"")
        matches = [(args.target, None, row) for row in stored.rows(args.target, args.where)]
        looked_for = f"rows of '{args.target}' where {args.where}"
    else:
        if not args.target:
            raise NumblameError("Give a value to look for, or a table with --where.")
        matches = stored.find_value(args.target, args.table)
        looked_for = f"the value {args.target}"
    if not matches:
        print(f"Nothing found for {looked_for} in run {stored.id}.")
        return 1

    for index, (table, column, row) in enumerate(matches[: args.matches]):
        if index:
            print()
        where = f", column '{column}'" if column else ""
        print(f"Found in '{table}'{where} (run {stored.id}):")
        print(_show_row(row))
        ids = list(row[SRC])
        print(f"\nIt was built from {len(ids)} source row(s):")
        for source, info in stored.source_rows(ids, limit=args.limit).items():
            shown = len(info["rows"])
            more = f", showing {shown}" if shown < info["count"] else ""
            print(f"\n  {source}: {info['count']} row(s){more}")
            print(_table(info["columns"], info["rows"], indent="    "))
        print("\nPath through the pipeline (rows fed by those source rows):")
        for step, count in stored.path(ids):
            marker = "  <- this row" if step == table else ""
            print(f"  {step}: {count} row(s){marker}")
    if len(matches) > args.matches:
        print(f"\n{len(matches) - args.matches} more match(es) not shown. Narrow with --table or --where, or raise --matches.")
    stored.close()
    return 0


def _print_diff(result: dict, before_id: str, after_id: str, column: str | None, measure: str | None) -> None:
    before, after = result["before"], result["after"]
    target = f"'{result['table']}'" + (f" where {result['where']}" if result["where"] else "")
    print(f"{target}: run {before_id} -> run {after_id}")
    if before is None or after is None:
        print(f"  The row {'did not exist before' if before is None else 'no longer exists'}.")
    names = list((after or before).keys())
    shown = [column] if column else names
    for name in shown:
        old = before.get(name) if before else None
        new = after.get(name) if after else None
        if old != new or column:
            delta = ""
            if isinstance(old, (int, float)) and isinstance(new, (int, float)):
                delta = f"  ({new - old:+g})"
            print(f"  {name}: {old} -> {new}{delta}")
    if before == after:
        print("  No column changed.")

    def amount(key) -> str:
        value = result["measure"].get(key)
        if value is None:
            return ""
        if key[0] == "removed":
            value = -value
        return f"; {measure} over these rows: {value:+,.2f}"

    print("\nWhy:")
    explained = False
    for reason, ids in result["removed"].items():
        explained = True
        print(f"  - {len(ids)} source row(s) no longer feed this row: {reason}{amount(('removed', reason))}")
        print(f"      e.g. {', '.join(ids[:5])}{' ...' if len(ids) > 5 else ''}")
    for reason, ids in result["added"].items():
        explained = True
        print(f"  + {len(ids)} source row(s) newly feed this row: {reason}{amount(('added', reason))}")
        print(f"      e.g. {', '.join(ids[:5])}{' ...' if len(ids) > 5 else ''}")
    if result["changed"]:
        explained = True
        ids = result["changed"]
        print(f"  ~ {len(ids)} source row(s) feed this row in both runs but their contents changed{amount(('changed', ''))}")
        print(f"      e.g. {', '.join(ids[:5])}{' ...' if len(ids) > 5 else ''}")
    if not explained:
        print("  The same source rows feed this row in both runs, with the same contents.")
        if before != after:
            print("  So the difference comes from how a step computes the value, not from which rows it uses.")
    if result["steps_changed"]:
        print(f"\nSteps whose SQL differs between the runs: {', '.join(result['steps_changed'])}")
    if result["sources_changed"]:
        print(f"Source files that differ between the runs: {', '.join(result['sources_changed'])}")


def cmd_diff(args) -> int:
    before, after = Run(args.project, args.before), Run(args.project, args.after)
    result = diff(before, after, args.table, args.where, args.measure)
    _print_diff(result, before.id, after.id, args.column, args.measure)
    before.close()
    after.close()
    return 0


def cmd_bisect(args) -> int:
    print(f"Looking for the commit that changed {args.table}.{args.column} between {args.good} and {args.bad}")
    found = bisect(Path(args.project), args.table, args.column, args.where, args.good, args.bad, log=print)
    print(f"\nFirst commit where the value changed (tested {found['tested']} of {found['candidates'] + 1} commits):")
    print(f"  {found['summary']}")
    print(f"  {args.column}: {found['value_before']} -> {found['value_after']}")
    if found["files"]:
        print("\n" + "\n".join("  " + line.strip() for line in found["files"].splitlines()))
    if found["value_after"] != found["bad_value"]:
        print(f"\nNote: at {args.bad} the value is {found['bad_value']}, so a later commit changed it again.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="numblame", description="git blame for numbers: trace any figure in a SQL pipeline back to its source rows.")
    parser.add_argument("--version", action="version", version=f"numblame {__version__}")
    parser.add_argument("-C", "--project", default=".", help="project folder containing numblame.toml (default: current folder)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the pipeline with lineage and store the result").set_defaults(func=cmd_run)
    sub.add_parser("runs", help="list stored runs").set_defaults(func=cmd_runs)

    why = sub.add_parser("why", help="show the source rows behind a value or an output row")
    why.add_argument("target", nargs="?", help="a value to search for, or a table name when used with --where")
    why.add_argument("--where", help="SQL filter selecting the output row(s), e.g. \"region = 'South'\"")
    why.add_argument("--table", help="only search this table for the value")
    why.add_argument("--run", default="latest", help="run ID, 'latest' or 'prev' (default: latest)")
    why.add_argument("--limit", type=int, default=10, help="source rows to print per source (default: 10)")
    why.add_argument("--matches", type=int, default=3, help="matching output rows to explain (default: 3)")
    why.set_defaults(func=cmd_why)

    diff_ = sub.add_parser("diff", help="explain how one output row changed between two runs")
    diff_.add_argument("table")
    diff_.add_argument("--where", help="SQL filter selecting exactly one row")
    diff_.add_argument("--column", help="only report this column")
    diff_.add_argument("--before", default="prev", help="earlier run (default: prev)")
    diff_.add_argument("--after", default="latest", help="later run (default: latest)")
    diff_.add_argument("--measure", help="source.column to total over the rows that came or went, e.g. orders.amount")
    diff_.set_defaults(func=cmd_diff)

    bisect_ = sub.add_parser("bisect", help="find the git commit that changed a value")
    bisect_.add_argument("table")
    bisect_.add_argument("column")
    bisect_.add_argument("--where", help="SQL filter selecting exactly one row")
    bisect_.add_argument("--good", required=True, help="commit where the value was right")
    bisect_.add_argument("--bad", default="HEAD", help="commit where the value is wrong (default: HEAD)")
    bisect_.set_defaults(func=cmd_bisect)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except NumblameError as error:
        print(f"numblame: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
