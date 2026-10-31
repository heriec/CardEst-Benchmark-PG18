/*-------------------------------------------------------------------------
 *
 * cardest_benchmark.h
 *    Cardinality-estimation benchmark integration.
 *
 *-------------------------------------------------------------------------
 */
#ifndef CARDEST_BENCHMARK_H
#define CARDEST_BENCHMARK_H

#include "nodes/pathnodes.h"

extern PGDLLIMPORT bool cardest_benchmark_enabled;
extern PGDLLIMPORT bool cardest_benchmark_strict;
extern PGDLLIMPORT bool cardest_benchmark_dump_subplans;
extern PGDLLIMPORT char *cardest_benchmark_estimates_file;
extern PGDLLIMPORT char *cardest_benchmark_dump_file;
extern PGDLLIMPORT char *cardest_benchmark_query_id;

extern double cardest_benchmark_baserel_rows(PlannerInfo *root,
											 RelOptInfo *rel,
											 double postgres_rows);
extern double cardest_benchmark_joinrel_rows(PlannerInfo *root,
											 RelOptInfo *rel,
											 double postgres_rows);

#endif							/* CARDEST_BENCHMARK_H */
