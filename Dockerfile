FROM debian:bookworm AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential bison flex libreadline-dev zlib1g-dev libicu-dev pkg-config ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/postgresql
COPY . .

RUN ./configure \
      --prefix=/opt/postgresql/18.6 \
      --with-system-tzdata=/usr/share/zoneinfo \
      --enable-cassert \
      CFLAGS='-O2 -g' \
    && make -j"$(nproc)" \
    && make install

FROM debian:bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates libreadline8 zlib1g libicu72 python3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 999 postgres \
    && useradd --uid 999 --gid postgres --home-dir /var/lib/postgresql --create-home postgres

COPY --from=builder /opt/postgresql/18.6 /opt/postgresql/18.6
COPY docker/entrypoint.sh /usr/local/bin/cardest-entrypoint
COPY tools/cardest_workload.py /opt/cardest/tools/cardest_workload.py
COPY workloads /opt/cardest/workloads

ENV PATH=/opt/postgresql/18.6/bin:$PATH
ENV PGDATA=/var/lib/postgresql/data
ENV POSTGRES_DB=postgres
ENV POSTGRES_USER=postgres

RUN chmod +x /usr/local/bin/cardest-entrypoint /opt/cardest/tools/cardest_workload.py \
    && mkdir -p /var/lib/postgresql/data /artifacts \
    && chown -R postgres:postgres /var/lib/postgresql /artifacts

USER postgres
EXPOSE 5432
ENTRYPOINT ["cardest-entrypoint"]
CMD ["postgres"]
