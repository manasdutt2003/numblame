"""Rewrite a SELECT so that every output row carries the IDs of the source rows behind it.

Every table the pipeline reads has a hidden ``_src`` column: a list of source-row IDs.
The rewrite adds one more output column to each SELECT that combines the ``_src`` lists
of the tables it reads:

* plain SELECT or JOIN: concatenate the lists of the joined rows
* GROUP BY or aggregate: collect the lists of every row in the group
* SELECT DISTINCT: treated as GROUP BY ALL, so duplicates merge their lineage
* CTEs, derived tables and UNION ALL: rewritten recursively

Lineage is row-level. It records which source rows fed an output row, not which
columns. Rows that only influence a result through a scalar or IN subquery in
WHERE are not recorded.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

SRC = "_src"


class UnsupportedSQL(ValueError):
    """Raised for SQL that cannot be given lineage without changing its meaning."""


def add_lineage(sql: str) -> str:
    """Return ``sql`` with a trailing ``_src`` lineage column added to its result."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        raise UnsupportedSQL("A step must be a single SELECT statement.")
    _rewrite(tree)
    return tree.sql(dialect="duckdb")


def _rewrite(node: exp.Expression) -> None:
    with_ = node.args.get("with_") or node.args.get("with")
    if with_:
        for cte in with_.expressions:
            _rewrite(cte.this)

    if isinstance(node, exp.SetOperation):
        if not isinstance(node, exp.Union) or node.args.get("distinct") is not False:
            raise UnsupportedSQL(
                "Only UNION ALL is supported. UNION, INTERSECT and EXCEPT compare whole rows, "
                "so a lineage column would change their result."
            )
        _rewrite(node.this)
        _rewrite(node.expression)
        return

    if isinstance(node, exp.Subquery):
        _rewrite(node.this)
        return

    if not isinstance(node, exp.Select):
        raise UnsupportedSQL(f"Cannot add lineage to {type(node).__name__}.")

    _rewrite_select(node)


def _rewrite_select(select: exp.Select) -> None:
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is None:
        raise UnsupportedSQL("A SELECT without FROM has no source rows to trace.")

    relations = [from_.this] + [join.this for join in select.args.get("joins") or []]
    aliases = []
    for relation in relations:
        if isinstance(relation, exp.Subquery):
            _rewrite(relation.this)
            if not relation.alias:
                raise UnsupportedSQL("Give every subquery in FROM an alias.")
        elif not isinstance(relation, exp.Table) or not isinstance(relation.this, exp.Identifier):
            raise UnsupportedSQL(
                f"Unsupported relation in FROM: {relation.sql(dialect='duckdb')[:60]}. "
                "Read files as declared sources instead of calling table functions."
            )
        aliases.append(relation.alias_or_name)

    distinct = select.args.get("distinct")
    if distinct is not None:
        if distinct.args.get("on") is not None:
            raise UnsupportedSQL("DISTINCT ON is not supported; use GROUP BY or QUALIFY.")
        # DISTINCT merges duplicate rows, so their lineage has to merge too.
        select.set("distinct", None)
        if select.args.get("group") is None:
            select.set("group", exp.Group(all=True))

    for item in select.expressions:
        star = item if isinstance(item, exp.Star) else item.this if isinstance(item, exp.Column) and isinstance(item.this, exp.Star) else None
        if star is not None:
            excluded = list(star.args.get("except_") or [])
            excluded.append(exp.column(SRC))
            star.set("except_", excluded)

    parts = [_src_of(alias, nullable=len(aliases) > 1) for alias in aliases]
    lineage = parts[0]
    for part in parts[1:]:
        lineage = exp.func("list_concat", lineage, part)

    if select.args.get("group") is not None or _has_own_aggregate(select):
        lineage = exp.func("list_distinct", exp.func("flatten", exp.func("list", lineage)))

    select.append("expressions", exp.alias_(lineage, SRC))


def _src_of(alias: str, nullable: bool) -> exp.Expression:
    column = exp.column(SRC, table=alias)
    if not nullable:
        return column
    # An outer join leaves _src NULL on the missing side.
    return exp.func("coalesce", column, exp.cast(exp.Array(expressions=[]), "VARCHAR[]"))


def _has_own_aggregate(select: exp.Select) -> bool:
    """True if the SELECT aggregates at its own level (not in a window or a nested query)."""
    for agg in select.find_all(exp.AggFunc):
        parent = agg.parent
        own = True
        while parent is not None and parent is not select:
            if isinstance(parent, (exp.Window, exp.Select)):
                own = False
                break
            parent = parent.parent
        if own:
            return True
    return False
