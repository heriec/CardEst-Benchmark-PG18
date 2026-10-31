# PostgreSQL 18.6 JOB subplan export

Generated from the canonical 113-query JOB workload in
`workloads/job/job.sql`, using the IMDB data mounted from
`/data1/cwh/datasets/imdb` and the `JOB-index.sql` index set.

Controlled planner settings:

- `geqo = off`
- `max_parallel_workers = 0`
- `max_parallel_workers_per_gather = 0`
- `enable_self_join_elimination = off`

Counts:

- workload queries: 113
- base relation requests: 977
- join relation requests: 72,759
- total unique keyed requests: 73,736
- duplicate keys: 0
- largest relation set: 17 aliases

Files:

- `pg18_subplan_manifest.tsv`: header plus all keyed base/join requests.
- `pg18_subplan_manifest.raw.tsv`: exact headerless backend dump accepted by
  the injection loader after replacing `postgres_rows` with method estimates.
- `pg18_join_subqueries.tsv`: 72,759 executable join `COUNT(*)` queries.
- `pg18_all_subqueries.tsv`: 73,736 executable base and join `COUNT(*)` queries.
- `pg18_driver.sql`: workload enumeration driver.
- `pg18_explain.log`: plans produced during enumeration.
