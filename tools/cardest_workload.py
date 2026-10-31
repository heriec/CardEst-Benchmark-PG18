#!/usr/bin/env python3
"""Generate and materialize cardinality-benchmark subplans.

The supported workload format is one query per line:

    true_cardinality||SELECT COUNT(*) FROM ... WHERE ...;

`driver` creates a psql script that asks PostgreSQL to enumerate base/join
relation sets through cardest_benchmark.dump_subplans. `materialize` combines
that manifest with the original SQL and emits executable COUNT(*) subqueries.
`run` performs both steps using psql.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile
from collections import OrderedDict
from typing import Iterable


IDENT = r'[A-Za-z_][A-Za-z0-9_$]*'
ALIAS_REF_RE = re.compile(rf'\b({IDENT})\s*\.')
QUERY_RE = re.compile(
    r'^\s*SELECT\s+COUNT\s*\(\s*\*\s*\)\s+FROM\s+(.*?)\s+WHERE\s+(.*?)\s*;?\s*$',
    re.IGNORECASE | re.DOTALL,
)
TABLE_RE = re.compile(rf'^\s*(.*?)\s+(?:AS\s+)?({IDENT})\s*$', re.IGNORECASE | re.DOTALL)


@dataclasses.dataclass(frozen=True)
class TableRef:
    sql: str
    alias: str


@dataclasses.dataclass(frozen=True)
class Predicate:
    sql: str
    aliases: frozenset[str]


@dataclasses.dataclass(frozen=True)
class WorkloadQuery:
    query_id: str
    ordinal: int
    true_cardinality: str
    sql: str
    tables: tuple[TableRef, ...]
    predicates: tuple[Predicate, ...]


@dataclasses.dataclass(frozen=True)
class ManifestEntry:
    query_id: str
    kind: str
    aliases: tuple[str, ...]
    postgres_rows: str


def split_top_level(text: str, delimiter: str) -> list[str]:
    """Split on a delimiter outside quotes and parentheses."""
    result: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    i = 0
    delimiter_upper = delimiter.upper()

    while i < len(text):
        char = text[i]
        if quote:
            if char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char in "'\"":
            quote = char
            i += 1
            continue
        if char == '(':
            depth += 1
            i += 1
            continue
        if char == ')':
            depth -= 1
            if depth < 0:
                raise ValueError(f'unbalanced parentheses in: {text}')
            i += 1
            continue

        if depth == 0:
            if delimiter == ',' and char == ',':
                result.append(text[start:i].strip())
                start = i + 1
                i += 1
                continue
            if delimiter != ',' and text[i:i + len(delimiter)].upper() == delimiter_upper:
                before_ok = i == 0 or not (text[i - 1].isalnum() or text[i - 1] in '_$')
                end = i + len(delimiter)
                after_ok = end == len(text) or not (text[end].isalnum() or text[end] in '_$')
                if before_ok and after_ok:
                    result.append(text[start:i].strip())
                    start = end
                    i = end
                    continue
        i += 1

    if quote or depth != 0:
        raise ValueError(f'unbalanced SQL expression: {text}')
    result.append(text[start:].strip())
    return result


def parse_table(table_sql: str) -> TableRef:
    match = TABLE_RE.match(table_sql)
    if not match:
        raise ValueError(f'table reference must have an alias: {table_sql}')
    return TableRef(sql=table_sql.strip(), alias=match.group(2))


def parse_workload(path: pathlib.Path, id_prefix: str | None = None) -> list[WorkloadQuery]:
    prefix = id_prefix or path.stem
    queries: list[WorkloadQuery] = []
    for line_number, raw_line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if '||' not in line:
            raise ValueError(f'{path}:{line_number}: expected true_cardinality||SQL')
        true_cardinality, sql = line.split('||', 1)
        true_cardinality = true_cardinality.strip()
        sql = sql.strip()
        match = QUERY_RE.match(sql)
        if not match:
            raise ValueError(
                f'{path}:{line_number}: expected SELECT COUNT(*) FROM ... WHERE ...;'
            )
        tables = tuple(parse_table(item) for item in split_top_level(match.group(1), ','))
        aliases_by_lower = {table.alias.lower(): table.alias for table in tables}
        if len(aliases_by_lower) != len(tables):
            raise ValueError(f'{path}:{line_number}: duplicate table alias')

        predicates: list[Predicate] = []
        for expression in split_top_level(match.group(2), 'AND'):
            referenced_lower = {alias.lower() for alias in ALIAS_REF_RE.findall(expression)}
            unknown = referenced_lower - aliases_by_lower.keys()
            if unknown:
                raise ValueError(
                    f'{path}:{line_number}: unknown aliases {sorted(unknown)} in {expression}'
                )
            if not referenced_lower:
                raise ValueError(
                    f'{path}:{line_number}: predicate has no qualified alias: {expression}'
                )
            predicates.append(
                Predicate(
                    sql=expression.strip(),
                    aliases=frozenset(aliases_by_lower[alias] for alias in referenced_lower),
                )
            )

        ordinal = len(queries) + 1
        queries.append(
            WorkloadQuery(
                query_id=f'{prefix}_{ordinal:04d}',
                ordinal=ordinal,
                true_cardinality=true_cardinality,
                sql=sql.rstrip(';') + ';',
                tables=tables,
                predicates=tuple(predicates),
            )
        )
    return queries


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def driver_sql(queries: Iterable[WorkloadQuery], manifest: pathlib.Path) -> str:
    lines = [
        r'\set ON_ERROR_STOP on',
        'SET geqo = off;',
        'SET max_parallel_workers = 0;',
        'SET max_parallel_workers_per_gather = 0;',
        'SET enable_self_join_elimination = off;',
        'SET cardest_benchmark.enabled = off;',
        'SET cardest_benchmark.dump_subplans = on;',
        f'SET cardest_benchmark.dump_file = {sql_literal(str(manifest.resolve()))};',
        '',
    ]
    for query in queries:
        lines.extend(
            [
                f'-- workload query {query.ordinal}: {query.query_id}',
                f'SET cardest_benchmark.query_id = {sql_literal(query.query_id)};',
                f'EXPLAIN (COSTS OFF) {query.sql}',
                '',
            ]
        )
    return '\n'.join(lines)


def read_manifest(path: pathlib.Path) -> list[ManifestEntry]:
    entries: OrderedDict[tuple[str, str, tuple[str, ...]], ManifestEntry] = OrderedDict()
    with path.open(newline='', encoding='utf-8') as handle:
        reader = csv.reader(handle, delimiter='\t')
        for line_number, fields in enumerate(reader, 1):
            if not fields or (fields[0].lstrip().startswith('#')):
                continue
            if len(fields) != 4:
                raise ValueError(f'{path}:{line_number}: expected four tab-separated fields')
            query_id, kind, relations, postgres_rows = (field.strip() for field in fields)
            if kind not in {'base', 'join'}:
                raise ValueError(f'{path}:{line_number}: invalid kind {kind!r}')
            aliases = tuple(relations.split(',')) if relations else ()
            if tuple(sorted(aliases)) != aliases or len(set(aliases)) != len(aliases):
                raise ValueError(
                    f'{path}:{line_number}: aliases must be unique and sorted: {relations}'
                )
            key = (query_id, kind, aliases)
            entry = ManifestEntry(query_id, kind, aliases, postgres_rows)
            previous = entries.get(key)
            if previous and previous.postgres_rows != postgres_rows:
                raise ValueError(
                    f'{path}:{line_number}: repeated key {key} has inconsistent PostgreSQL rows '
                    f'{previous.postgres_rows} and {postgres_rows}'
                )
            entries.setdefault(key, entry)
    return list(entries.values())


def materialize_subquery(query: WorkloadQuery, aliases: tuple[str, ...]) -> str:
    selected = set(aliases)
    table_by_alias = {table.alias: table for table in query.tables}
    unknown = selected - table_by_alias.keys()
    if unknown:
        raise ValueError(f'{query.query_id}: manifest contains unknown aliases {sorted(unknown)}')
    if not selected:
        raise ValueError(f'{query.query_id}: empty relation set')

    tables = [table.sql for table in query.tables if table.alias in selected]
    predicates = [
        predicate.sql
        for predicate in query.predicates
        if predicate.aliases <= selected
    ]
    sql = f"SELECT COUNT(*) FROM {', '.join(tables)}"
    if predicates:
        sql += ' WHERE ' + ' AND '.join(predicates)
    return sql + ';'


def materialized_rows(
    queries: list[WorkloadQuery], entries: list[ManifestEntry], kind: str
) -> list[tuple[str, str, str, str, str]]:
    query_by_id = {query.query_id: query for query in queries}
    rows: list[tuple[str, str, str, str, str]] = []
    seen_queries: set[str] = set()
    for entry in entries:
        if kind != 'all' and entry.kind != kind:
            continue
        query = query_by_id.get(entry.query_id)
        if query is None:
            raise ValueError(f'manifest has unknown query_id {entry.query_id!r}')
        seen_queries.add(entry.query_id)
        rows.append(
            (
                entry.query_id,
                entry.kind,
                ','.join(entry.aliases),
                entry.postgres_rows,
                materialize_subquery(query, entry.aliases),
            )
        )
    missing = [query.query_id for query in queries if query.query_id not in seen_queries]
    if missing:
        raise ValueError(f'manifest contains no selected subplans for {len(missing)} queries: {missing[:5]}')
    return rows


def write_subqueries(path: pathlib.Path, rows: Iterable[tuple[str, str, str, str, str]]) -> None:
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle, delimiter='\t', lineterminator='\n')
        writer.writerow(('query_id', 'kind', 'relations', 'postgres_rows', 'sql'))
        writer.writerows(rows)


def command_driver(args: argparse.Namespace) -> None:
    queries = parse_workload(args.workload, args.id_prefix)
    args.output.write_text(driver_sql(queries, args.manifest), encoding='utf-8')
    print(f'wrote {len(queries)} workload queries to {args.output}')


def command_materialize(args: argparse.Namespace) -> None:
    queries = parse_workload(args.workload, args.id_prefix)
    entries = read_manifest(args.manifest)
    rows = materialized_rows(queries, entries, args.kind)
    write_subqueries(args.output, rows)
    print(f'wrote {len(rows)} unique {args.kind} subplans to {args.output}')


def command_run(args: argparse.Namespace) -> None:
    queries = parse_workload(args.workload, args.id_prefix)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile('w', suffix='.sql', encoding='utf-8', delete=False) as handle:
        driver_path = pathlib.Path(handle.name)
        handle.write(driver_sql(queries, args.manifest))
    try:
        command = [args.psql, '-X', '-v', 'ON_ERROR_STOP=1']
        if args.dsn:
            command.extend(['--dbname', args.dsn])
        command.extend(args.psql_arg)
        print('running:', shlex.join(command), file=sys.stderr)
        with driver_path.open('rb') as stdin:
            subprocess.run(command, stdin=stdin, check=True)
    finally:
        driver_path.unlink(missing_ok=True)
    entries = read_manifest(args.manifest)
    rows = materialized_rows(queries, entries, args.kind)
    write_subqueries(args.output, rows)
    print(
        f'enumerated {len(rows)} unique {args.kind} subplans from {len(queries)} workload queries'
    )


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--workload', type=pathlib.Path, required=True)
    parser.add_argument('--id-prefix', help='query-id prefix; defaults to workload filename stem')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)

    driver = subparsers.add_parser('driver', help='write a psql subplan-enumeration script')
    add_common(driver)
    driver.add_argument('--manifest', type=pathlib.Path, required=True)
    driver.add_argument('--output', type=pathlib.Path, required=True)
    driver.set_defaults(func=command_driver)

    materialize = subparsers.add_parser(
        'materialize', help='turn a PostgreSQL manifest into executable COUNT(*) SQL'
    )
    add_common(materialize)
    materialize.add_argument('--manifest', type=pathlib.Path, required=True)
    materialize.add_argument('--output', type=pathlib.Path, required=True)
    materialize.add_argument('--kind', choices=('join', 'base', 'all'), default='join')
    materialize.set_defaults(func=command_materialize)

    run = subparsers.add_parser('run', help='enumerate with psql and materialize subqueries')
    add_common(run)
    run.add_argument('--manifest', type=pathlib.Path, required=True)
    run.add_argument('--output', type=pathlib.Path, required=True)
    run.add_argument('--kind', choices=('join', 'base', 'all'), default='join')
    run.add_argument('--psql', default='psql')
    run.add_argument('--dsn', help='psql connection string or database name')
    run.add_argument(
        '--psql-arg', action='append', default=[],
        help='extra psql argument, repeatable (for example --psql-arg=-p --psql-arg=5433)',
    )
    run.set_defaults(func=command_run)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
