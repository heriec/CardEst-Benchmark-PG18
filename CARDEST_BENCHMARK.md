# Cardinality-estimation benchmark integration

This repository starts from PostgreSQL 18.6 and adds a cardinality-estimation
benchmark interface without relying on planner call order or a fixed-size
array.

## Stable identity

Every requested estimate is keyed by:

```text
(query_id, kind, sorted relation aliases)
```

- `query_id`: `cardest_benchmark.query_id` when explicitly set; otherwise
  PostgreSQL's normalized `queryId`.
- `kind`: `base` or `join`.
- `relations`: comma-separated aliases sorted bytewise, for example
  `ci,m,t`.

Aliases, rather than physical table names, distinguish self-joins.

## Dump a subplan manifest

Use an explicit workload query identity for reproducible files:

```sql
SET cardest_benchmark.query_id = 'job_1a';
SET cardest_benchmark.dump_file = '/absolute/path/subplans.tsv';
SET cardest_benchmark.dump_subplans = on;
EXPLAIN SELECT ...;
```

Each planner request appends one line:

```text
query_id<TAB>kind<TAB>relations<TAB>postgres_rows
```

The dump intentionally records planner requests, so a relation set may occur
more than once. Before using it as an estimates file, deduplicate by the first
three fields and verify that repeated keys agree about the intended estimate.

## Enumerate a workload and emit executable subplan SQL

`tools/cardest_workload.py` accepts the benchmark workload format used by
`workloads/stats_CEB/stats_CEB.sql`:

```text
true_cardinality||SELECT COUNT(*) FROM ... WHERE ...;
```

With the STATS database already loaded, enumerate PostgreSQL 18's relation
sets and materialize every join subquery with one command:

```bash
python3 tools/cardest_workload.py run \
  --workload workloads/stats_CEB/stats_CEB.sql \
  --id-prefix stats_CEB \
  --manifest /tmp/stats_CEB_subplans.tsv \
  --output /tmp/stats_CEB_join_subqueries.tsv \
  --kind join \
  --dsn stats
```

Connection arguments can be repeated, for example:

```bash
  --psql-arg=-h --psql-arg=127.0.0.1 \
  --psql-arg=-p --psql-arg=5432 \
  --psql-arg=-U --psql-arg=postgres
```

The output TSV contains:

```text
query_id  kind  relations  postgres_rows  sql
```

The `sql` column is directly executable `SELECT COUNT(*)` SQL. Predicates are
included exactly when all aliases referenced by that predicate belong to the
subplan. Use `--kind all` to include filtered single-table subqueries too.

For a two-step workflow, use `driver` to generate a psql script and
`materialize` after running that script:

```bash
python3 tools/cardest_workload.py driver \
  --workload workloads/stats_CEB/stats_CEB.sql \
  --id-prefix stats_CEB \
  --manifest /tmp/stats_CEB_subplans.tsv \
  --output /tmp/stats_CEB_driver.sql
psql -X -v ON_ERROR_STOP=1 -d stats -f /tmp/stats_CEB_driver.sql
python3 tools/cardest_workload.py materialize \
  --workload workloads/stats_CEB/stats_CEB.sql \
  --id-prefix stats_CEB \
  --manifest /tmp/stats_CEB_subplans.tsv \
  --output /tmp/stats_CEB_join_subqueries.tsv \
  --kind join
```

## Inject estimates

Prepare a tab-separated file with exactly four fields:

```text
# query_id  kind  sorted aliases  rows
job_1a	base	t	2528312
job_1a	join	ci,t	104857
job_1a	join	ci,m,t	7312
```

Then run:

```sql
SET cardest_benchmark.query_id = 'job_1a';
SET cardest_benchmark.estimates_file = '/absolute/path/estimates.tsv';
SET cardest_benchmark.strict = on;
SET cardest_benchmark.enabled = on;
EXPLAIN (ANALYZE, BUFFERS) SELECT ...;
```

The file is loaded dynamically into a backend-local hash table. There is no
10,000/200,000-entry limit. Duplicate keys, malformed values, overlong keys,
and missing estimates in strict mode fail immediately.

Set `cardest_benchmark.strict = off` only for diagnostics. Missing entries then
emit a warning and use PostgreSQL's estimate.

## PG18 controlled CE track

For comparisons intended to preserve the query's explicit join graph:

```sql
SET geqo = off;
SET max_parallel_workers = 0;
SET max_parallel_workers_per_gather = 0;
SET enable_self_join_elimination = off;
```

Other planner and execution GUCs must also be frozen and reported. A separate
native-PG18 track may leave self-join elimination enabled.

## Current scope

The keyed interface overrides:

- filtered base-relation cardinalities in `set_baserel_size_estimates()`;
- unparameterized join-relation cardinalities in
  `set_joinrel_size_estimates()`.

Parameterized path cardinalities are not yet externally keyed. PostgreSQL
still derives them internally and caps them by the injected unparameterized
join cardinality. Extending the file key with parameterization identity is a
separate compatibility step and must not fall back to ordinal matching.
