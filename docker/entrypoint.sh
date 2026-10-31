#!/bin/bash
set -euo pipefail

if [[ "${1:-}" == "postgres" ]]; then
    if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
        mkdir -p "$PGDATA"
        chmod 700 "$PGDATA"
        initdb -D "$PGDATA" --username="${POSTGRES_USER:-postgres}" --auth=trust --no-locale
        cat >>"$PGDATA/postgresql.conf" <<'EOF'
listen_addresses = '*'
geqo = off
max_parallel_workers = 0
max_parallel_workers_per_gather = 0
enable_self_join_elimination = off
EOF
        echo "host all all all trust" >>"$PGDATA/pg_hba.conf"
    fi
    exec postgres -D "$PGDATA"
fi

exec "$@"
