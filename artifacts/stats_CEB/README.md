# PostgreSQL 18.6 STATS-CEB subplan export

Generated from `workloads/stats_CEB/stats_CEB.sql` using the Docker image built
from this repository and the simplified STATS CSV data.

Controlled planner settings:

- `geqo = off`
- `max_parallel_workers = 0`
- `max_parallel_workers_per_gather = 0`
- `enable_self_join_elimination = off`

Counts:

- workload queries: 146
- base relation requests: 632
- join relation requests: 2,603
- total unique keyed requests: 3,235
- duplicate keys: 0

Files:

- `pg18_subplan_manifest.tsv`: header plus all keyed base/join requests.
- `pg18_subplan_manifest.raw.tsv`: exact headerless backend dump accepted by
  the injection loader after replacing `postgres_rows` with method estimates.
- `pg18_join_subqueries.tsv`: 2,603 executable join `COUNT(*)` queries.
- `pg18_all_subqueries.tsv`: 3,235 executable base and join `COUNT(*)` queries.
- `pg18_driver.sql`: workload enumeration driver.
- `pg18_explain.log`: plans produced during enumeration.

Image used locally: `cardest-pg18:dev`, image ID
`sha256:5ddf72ca437af4edf8f18f4e523d771bb7932329c86bd86d1699fc52c4a626fd`.
