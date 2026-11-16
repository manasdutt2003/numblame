"""Run a SQL pipeline with row-level lineage, and answer questions about its numbers."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .rewrite import SRC, add_lineage

STATE_DIR = ".numblame"
META = "__nb_meta"
SOURCE_IDS = "__nb_source_ids"


class NumblameError(Exception):
    """An error the user can act on; shown without a traceback."""


@dataclass
class Source:
    name: str
    path: Path
    key: list[str] = field(default_factory=list)


@dataclass
class Step:
    name: str
    sql: str


@dataclass
class Project:
    root: Path
    sources: list[Source]
    steps: list[Step]


def load_project(root: Path, data_fallback: Path | None = None) -> Project:
    """Read numblame.toml and the step files under ``root``.

    ``data_fallback`` is used by bisect: when a source file is not tracked in git,
    it is taken from the working tree instead.
    """
    root = Path(root).resolve()
    config_path = root / "numblame.toml"
    if not config_path.exists():
        raise NumblameError(f"No numblame.toml in {root}. Run this inside a project, or pass --project.")
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))

    sources = []
    for name, spec in (config.get("sources") or {}).items():
        path = root / spec["path"]
        if not path.exists() and data_fallback is not None:
            path = Path(data_fallback) / spec["path"]
        if not path.exists():
            raise NumblameError(f"Source '{name}': file not found at {spec['path']}.")
        key = spec.get("key") or []
        sources.append(Source(name, path, [key] if isinstance(key, str) else list(key)))
    if not sources:
        raise NumblameError("numblame.toml declares no [sources].")

    steps_dir = root / config.get("steps", "steps")
    steps = []
    for file in sorted(steps_dir.glob("*.sql")):
        name = file.stem.lstrip("0123456789").lstrip("_-") or file.stem
        sql = file.read_text(encoding="utf-8").strip().rstrip(";").strip()
        if sql:
            steps.append(Step(name, sql))
    if not steps:
        raise NumblameError(f"No .sql step files found in {steps_dir}.")
    return Project(root, sources, steps)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _reader(path: Path) -> str:
    literal = "'" + str(path).replace("\\", "/").replace("'", "''") + "'"
    if path.suffix.lower() == ".parquet":
        return f"read_parquet({literal})"
    return f"read_csv_auto({literal})"


def _git_commit(root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def execute(project: Project, con: duckdb.DuckDBPyConnection, lineage: bool = True) -> None:
    """Load the sources and run every step into ``con``."""
    if lineage:
        con.execute(f"CREATE TABLE {SOURCE_IDS} (source VARCHAR, id VARCHAR, rowhash VARCHAR)")
    for source in project.sources:
        table = _quote(source.name)
        if not lineage:
            con.execute(f"CREATE TABLE {table} AS SELECT * FROM {_reader(source.path)}")
            continue
        con.execute(f"CREATE TEMP TABLE __nb_raw AS SELECT * FROM {_reader(source.path)}")
        columns = [row[0] for row in con.execute("DESCRIBE __nb_raw").fetchall()]
        missing = [k for k in source.key if k not in columns]
        if missing:
            raise NumblameError(f"Source '{source.name}': key column(s) {missing} not in the file.")
        as_text = lambda cols: "concat_ws('|', " + ", ".join(f"coalesce(CAST({_quote(c)} AS VARCHAR), '')" for c in cols) + ")"
        rowhash = f"md5({as_text(columns)})"
        if source.key:
            row_id = as_text(source.key)
        else:
            # No key declared: identify a row by its content, numbering exact duplicates.
            row_id = f"{rowhash} || '#' || CAST(row_number() OVER (PARTITION BY {rowhash}) AS VARCHAR)"
        prefix = "'" + source.name.replace("'", "''") + ":'"
        con.execute(
            f"CREATE TABLE {table} AS SELECT *, [{prefix} || {row_id}] AS {SRC}, {rowhash} AS __nb_hash FROM __nb_raw"
        )
        duplicates = con.execute(f"SELECT count(*) - count(DISTINCT {SRC}[1]) FROM {table}").fetchone()[0]
        if duplicates:
            raise NumblameError(f"Source '{source.name}': key {source.key} is not unique ({duplicates} duplicate rows).")
        con.execute(f"INSERT INTO {SOURCE_IDS} SELECT '{source.name}', {SRC}[1], __nb_hash FROM {table}")
        con.execute(f"ALTER TABLE {table} DROP COLUMN __nb_hash")
        con.execute("DROP TABLE __nb_raw")

    for step in project.steps:
        try:
            sql = add_lineage(step.sql) if lineage else step.sql
            con.execute(f"CREATE TABLE {_quote(step.name)} AS {sql}")
        except NumblameError:
            raise
        except Exception as error:
            raise NumblameError(f"Step '{step.name}' failed: {error}") from error


def run(project: Project) -> str:
    """Run the pipeline with lineage, store the result, and return the run ID."""
    state = project.root / STATE_DIR
    (state / "runs").mkdir(parents=True, exist_ok=True)
    (state / "objects").mkdir(parents=True, exist_ok=True)
    (state / ".gitignore").write_text("*\n", encoding="utf-8")

    source_hashes = {}
    for source in project.sources:
        digest = _sha256(source.path)
        source_hashes[source.name] = digest
        stored = state / "objects" / digest
        if not stored.exists():
            shutil.copyfile(source.path, stored)

    fingerprint = hashlib.sha256(json.dumps([source_hashes, [(s.name, s.sql) for s in project.steps]], sort_keys=True).encode()).hexdigest()
    created = datetime.now(timezone.utc)
    run_id = f"{created.strftime('%Y%m%dT%H%M%S%f')}-{fingerprint[:8]}"
    path = state / "runs" / f"{run_id}.duckdb"
    con = duckdb.connect(str(path))
    try:
        execute(project, con, lineage=True)
        meta = {
            "run_id": run_id,
            "created": created.isoformat(timespec="seconds"),
            "commit": _git_commit(project.root),
            "sources": source_hashes,
            "steps": {s.name: s.sql for s in project.steps},
            "step_order": [s.name for s in project.steps],
        }
        con.execute(f"CREATE TABLE {META} AS SELECT ? AS meta", [json.dumps(meta)])
    except Exception:
        con.close()
        path.unlink(missing_ok=True)
        raise
    con.close()
    return run_id


def list_runs(root: Path) -> list[str]:
    runs = Path(root) / STATE_DIR / "runs"
    return sorted(p.stem for p in runs.glob("*.duckdb")) if runs.exists() else []


class Run:
    """A stored run, opened read-only."""

    def __init__(self, root: Path, ref: str = "latest"):
        runs = list_runs(root)
        if not runs:
            raise NumblameError("No runs yet. Run `numblame run` first.")
        if ref == "latest":
            run_id = runs[-1]
        elif ref == "prev":
            if len(runs) < 2:
                raise NumblameError("There is no previous run yet.")
            run_id = runs[-2]
        else:
            matches = [r for r in runs if r == ref or r.startswith(ref) or r.endswith(ref)]
            if len(matches) != 1:
                raise NumblameError(f"Run '{ref}' matches {len(matches)} runs. Use `numblame runs` to list them.")
            run_id = matches[0]
        self.id = run_id
        self.con = duckdb.connect(str(Path(root) / STATE_DIR / "runs" / f"{run_id}.duckdb"), read_only=True)
        self.meta = json.loads(self.con.execute(f"SELECT meta FROM {META}").fetchone()[0])
        self.steps: list[str] = self.meta["step_order"]
        self.sources: list[str] = list(self.meta["sources"])

    def close(self) -> None:
        self.con.close()

    def columns(self, table: str) -> list[str]:
        return [r[0] for r in self.con.execute(f"DESCRIBE {_quote(table)}").fetchall() if r[0] != SRC]

    def check_table(self, table: str) -> None:
        if table not in self.steps and table not in self.sources:
            raise NumblameError(f"No table '{table}' in run {self.id}. Tables: {', '.join(self.sources + self.steps)}.")

    def rows(self, table: str, where: str | None = None) -> list[dict]:
        """Rows of a table as dicts; the lineage list is under the ``_src`` key."""
        self.check_table(table)
        sql = f"SELECT * FROM {_quote(table)}" + (f" WHERE {where}" if where else "")
        try:
            cursor = self.con.execute(sql)
        except duckdb.Error as error:
            raise NumblameError(f"Could not filter '{table}' with `{where}`: {error}") from error
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def find_value(self, value: str, table: str | None = None) -> list[tuple[str, str, dict]]:
        """Find cells equal to ``value``. Returns (table, column, row) for each match."""
        try:
            number = float(value.replace(",", ""))
        except ValueError:
            number = None
        hits = []
        for name in [table] if table else list(reversed(self.steps)):
            self.check_table(name)
            for column in self.columns(name):
                col = _quote(column)
                condition = f"CAST({col} AS VARCHAR) = ?"
                params: list = [value]
                if number is not None:
                    condition += f" OR abs(TRY_CAST({col} AS DOUBLE) - ?) < 1e-9"
                    params.append(number)
                cursor = self.con.execute(f"SELECT * FROM {_quote(name)} WHERE {condition} LIMIT 20", params)
                names = [d[0] for d in cursor.description]
                hits.extend((name, column, dict(zip(names, row))) for row in cursor.fetchall())
        return hits

    def source_rows(self, ids: list[str], limit: int | None = None) -> dict[str, dict]:
        """Resolve source-row IDs to their rows, grouped by source table."""
        result = {}
        for source in self.sources:
            mine = [i for i in ids if i.startswith(source + ":")]
            if not mine:
                continue
            cursor = self.con.execute(
                f"SELECT * EXCLUDE ({SRC}) FROM {_quote(source)} WHERE {SRC}[1] IN (SELECT unnest(?))"
                + (f" LIMIT {int(limit)}" if limit else ""),
                [mine],
            )
            names = [d[0] for d in cursor.description]
            result[source] = {"count": len(mine), "columns": names, "rows": cursor.fetchall()}
        return result

    def path(self, ids: list[str]) -> list[tuple[str, int]]:
        """For each step, how many of its rows were fed by any of ``ids``."""
        return [
            (step, self.con.execute(f"SELECT count(*) FROM {_quote(step)} WHERE list_has_any({SRC}, ?)", [ids]).fetchone()[0])
            for step in self.steps
        ]

    def source_hashes(self, ids: list[str]) -> dict[str, str]:
        rows = self.con.execute(f"SELECT id, rowhash FROM {SOURCE_IDS} WHERE id IN (SELECT unnest(?))", [ids]).fetchall()
        return dict(rows)

    def first_step_missing(self, row_id: str) -> str | None:
        """The first step with no row fed by ``row_id``: where that source row was dropped."""
        for step in self.steps:
            if not self.con.execute(f"SELECT count(*) FROM {_quote(step)} WHERE list_contains({SRC}, ?)", [row_id]).fetchone()[0]:
                return step
        return None

    def measure(self, spec: str, ids: list[str]) -> float | None:
        """Sum ``source.column`` over the given source rows."""
        source, _, column = spec.partition(".")
        if source not in self.sources or not column:
            raise NumblameError(f"--measure must be source.column, for example {self.sources[0]}.amount.")
        mine = [i for i in ids if i.startswith(source + ":")]
        if not mine:
            return None
        try:
            return self.con.execute(
                f"SELECT sum(TRY_CAST({_quote(column)} AS DOUBLE)) FROM {_quote(source)} WHERE {SRC}[1] IN (SELECT unnest(?))", [mine]
            ).fetchone()[0]
        except duckdb.Error as error:
            raise NumblameError(f"Could not sum {spec}: {error}") from error


def single_row(run: Run, table: str, where: str | None) -> dict | None:
    rows = run.rows(table, where)
    if len(rows) > 1:
        raise NumblameError(f"'{table}' has {len(rows)} matching rows in run {run.id}. Narrow it to one with --where.")
    return rows[0] if rows else None


def diff(a: Run, b: Run, table: str, where: str | None, measure: str | None = None) -> dict:
    """Explain how one output row changed between run ``a`` (before) and run ``b`` (after)."""
    row_a, row_b = single_row(a, table, where), single_row(b, table, where)
    ids_a = set(row_a[SRC]) if row_a else set()
    ids_b = set(row_b[SRC]) if row_b else set()

    hashes_a, hashes_b = a.source_hashes(list(ids_a | ids_b)), b.source_hashes(list(ids_a | ids_b))
    changed = sorted(i for i in ids_a & ids_b if hashes_a.get(i) != hashes_b.get(i))

    def classify(ids: set[str], other: Run, other_hashes: dict, gone: str, dropped: str) -> dict[str, list[str]]:
        buckets: dict[str, list[str]] = {}
        for row_id in sorted(ids):
            if row_id not in other_hashes:
                reason = gone
            else:
                step = other.first_step_missing(row_id)
                reason = dropped.format(step=step) if step else "feeds a different row of this table"
            buckets.setdefault(reason, []).append(row_id)
        return buckets

    removed = classify(ids_a - ids_b, b, hashes_b, "no longer in the input", "now dropped at step '{step}'")
    added = classify(ids_b - ids_a, a, hashes_a, "new in the input", "was dropped at step '{step}' before")

    result = {
        "table": table,
        "where": where,
        "before": {k: v for k, v in (row_a or {}).items() if k != SRC} or None,
        "after": {k: v for k, v in (row_b or {}).items() if k != SRC} or None,
        "removed": removed,
        "added": added,
        "changed": changed,
        "steps_changed": [s for s in b.steps if a.meta["steps"].get(s) != b.meta["steps"].get(s)]
        + [s for s in a.steps if s not in b.meta["steps"]],
        "sources_changed": [s for s in b.sources if a.meta["sources"].get(s) != b.meta["sources"].get(s)],
        "measure": {},
    }
    if measure:
        for label, run_, groups in (("removed", a, removed), ("added", b, added)):
            for reason, ids in groups.items():
                result["measure"][(label, reason)] = run_.measure(measure, ids)
        if changed:
            before, after = a.measure(measure, changed), b.measure(measure, changed)
            result["measure"][("changed", "")] = (after or 0) - (before or 0)
    return result


def value_at_commit(repo: Path, subdir: str, commit: str, table: str, column: str, where: str | None, data_fallback: Path):
    """Check out the project as of ``commit`` into a temp folder, run it, and read one value."""
    cmd = ["git", "-C", str(repo), "archive", "--format=tar", commit]
    if subdir:
        cmd += ["--", subdir]
    archive = subprocess.run(cmd, capture_output=True)
    if archive.returncode != 0:
        raise NumblameError(f"git archive failed for {commit[:10]}: {archive.stderr.decode(errors='replace').strip()}")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(tmp, filter="data")
        project = load_project(Path(tmp) / subdir, data_fallback=data_fallback)
        con = duckdb.connect()
        try:
            execute(project, con, lineage=False)
            sql = f"SELECT {_quote(column)} FROM {_quote(table)}" + (f" WHERE {where}" if where else "")
            rows = con.execute(sql).fetchall()
        except duckdb.Error as error:
            raise NumblameError(f"At {commit[:10]}: {error}") from error
        finally:
            con.close()
    if len(rows) > 1:
        raise NumblameError(f"At {commit[:10]} the filter matches {len(rows)} rows. Narrow it to one with --where.")
    return rows[0][0] if rows else None


def bisect(root: Path, table: str, column: str, where: str | None, good: str, bad: str, log=lambda message: None) -> dict:
    """Find the first commit after ``good`` at which the value differs from its value at ``good``."""
    root = Path(root).resolve()

    def git(*args: str) -> str:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        if out.returncode != 0:
            raise NumblameError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
        return out.stdout.strip()

    repo = Path(git("rev-parse", "--show-toplevel"))
    subdir = git("rev-parse", "--show-prefix").rstrip("/")
    good_sha, bad_sha = git("rev-parse", good), git("rev-parse", bad)
    commits = git("rev-list", "--reverse", "--ancestry-path", f"{good_sha}..{bad_sha}").split()
    if not commits:
        raise NumblameError(f"'{good}' is not an ancestor of '{bad}', or they are the same commit.")

    cache: dict[str, object] = {}

    def value(commit: str):
        if commit not in cache:
            cache[commit] = value_at_commit(repo, subdir, commit, table, column, where, data_fallback=root)
            log(f"  {commit[:10]}  {column} = {cache[commit]}")
        return cache[commit]

    good_value, bad_value = value(good_sha), value(bad_sha)
    if good_value == bad_value:
        raise NumblameError(f"The value is {good_value} at both commits, so there is nothing to find.")

    low, high = 0, len(commits) - 1  # commits[high] is known to differ from the good value
    while low < high:
        middle = (low + high) // 2
        if value(commits[middle]) != good_value:
            high = middle
        else:
            low = middle + 1
    culprit = commits[low]
    return {
        "commit": culprit,
        "summary": git("log", "-1", "--format=%h %an, %ad: %s", "--date=short", culprit),
        "files": git("show", "--stat", "--format=", culprit),
        "good_value": good_value,
        "value_before": value(commits[low - 1]) if low else good_value,
        "value_after": value(culprit),
        "bad_value": bad_value,
        "tested": len(cache),
        "candidates": len(commits),
    }
