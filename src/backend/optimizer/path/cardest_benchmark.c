/*-------------------------------------------------------------------------
 *
 * cardest_benchmark.c
 *    Stable subplan identification and cardinality injection for the
 *    end-to-end cardinality-estimation benchmark.
 *
 * Estimates are keyed by query identity and the sorted set of base-relation
 * aliases.  This deliberately avoids depending on planner call order.
 *
 * File format (tab-separated, comments start with '#'):
 *
 *   query_id<TAB>kind<TAB>relations<TAB>rows
 *
 * kind is "base" or "join".  relations is the comma-separated, sorted set
 * of aliases, for example "ci,m,t".  Use cardest_benchmark.dump_subplans to
 * produce an exact manifest accepted by this loader; add the rows column to
 * turn that manifest into an estimates file.
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <ctype.h>
#include <errno.h>
#include <math.h>

#include "catalog/pg_type_d.h"
#include "lib/stringinfo.h"
#include "nodes/pathnodes.h"
#include "optimizer/cardest_benchmark.h"
#include "storage/fd.h"
#include "utils/hsearch.h"
#include "utils/memutils.h"

bool		cardest_benchmark_enabled = false;
bool		cardest_benchmark_strict = true;
bool		cardest_benchmark_dump_subplans = false;
char	   *cardest_benchmark_estimates_file = NULL;
char	   *cardest_benchmark_dump_file = NULL;
char	   *cardest_benchmark_query_id = NULL;

typedef struct CardEstKey
{
	char		query_id[NAMEDATALEN];
	char		kind[8];
	char		relations[1024];
}			CardEstKey;

typedef struct CardEstEntry
{
	CardEstKey	key;
	double		rows;
	bool		used;
	int			source_line;
}			CardEstEntry;

static HTAB *cardest_estimates = NULL;
static MemoryContext cardest_context = NULL;
static char *loaded_estimates_file = NULL;

static int
cardest_string_ptr_cmp(const void *a, const void *b)
{
	const char *const *sa = (const char *const *) a;
	const char *const *sb = (const char *const *) b;

	return strcmp(*sa, *sb);
}

static void
cardest_copy_field(char *dst, Size dstlen, const char *src,
				   const char *field_name, int source_line)
{
	Size		len = strlen(src);

	if (len >= dstlen)
		ereport(ERROR,
				(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
				 errmsg("cardinality benchmark %s is too long at line %d",
						field_name, source_line),
				 errdetail("Maximum length is %zu bytes, but the value has %zu bytes.",
						   dstlen - 1, len)));
	memcpy(dst, src, len + 1);
}

static char *
cardest_trim(char *value)
{
	char	   *end;

	while (*value && isspace((unsigned char) *value))
		value++;
	end = value + strlen(value);
	while (end > value && isspace((unsigned char) end[-1]))
		*--end = '\0';
	return value;
}

static const char *
cardest_effective_query_id(PlannerInfo *root, char *buffer, Size buffer_size)
{
	if (cardest_benchmark_query_id && cardest_benchmark_query_id[0] != '\0')
		return cardest_benchmark_query_id;

	if (root && root->parse && root->parse->queryId != UINT64CONST(0))
	{
		snprintf(buffer, buffer_size, UINT64_FORMAT,
				 (uint64) root->parse->queryId);
		return buffer;
	}

	if (cardest_benchmark_strict)
		ereport(ERROR,
				(errmsg("cardinality benchmark query identity is unavailable"),
				 errhint("Set cardest_benchmark.query_id before planning the query.")));

	return "0";
}

static char *
cardest_relations_string(PlannerInfo *root, Relids relids)
{
	int			member = -1;
	int			count = 0;
	int			capacity = 8;
	char	  **aliases;
	StringInfoData result;
	int			i;

	aliases = palloc(sizeof(char *) * capacity);
	while ((member = bms_next_member(relids, member)) >= 0)
	{
		RangeTblEntry *rte;
		const char *alias;

		if (member <= 0 || member >= root->simple_rel_array_size ||
			root->simple_rte_array[member] == NULL)
			ereport(ERROR,
					(errmsg("cannot identify cardinality benchmark relation %d",
							member)));

		rte = root->simple_rte_array[member];
		alias = rte->eref ? rte->eref->aliasname : NULL;
		if (alias == NULL || alias[0] == '\0')
			ereport(ERROR,
					(errmsg("cardinality benchmark relation %d has no alias",
							member)));

		if (count == capacity)
		{
			capacity *= 2;
			aliases = repalloc(aliases, sizeof(char *) * capacity);
		}
		aliases[count++] = pstrdup(alias);
	}

	qsort(aliases, count, sizeof(char *), cardest_string_ptr_cmp);
	initStringInfo(&result);
	for (i = 0; i < count; i++)
	{
		if (i > 0)
			appendStringInfoChar(&result, ',');
		appendStringInfoString(&result, aliases[i]);
		pfree(aliases[i]);
	}
	pfree(aliases);
	return result.data;
}

static void
cardest_unload(void)
{
	if (cardest_context != NULL)
	{
		MemoryContextDelete(cardest_context);
		cardest_context = NULL;
	}
	cardest_estimates = NULL;
	loaded_estimates_file = NULL;
}

static void
cardest_load_estimates(void)
{
	FILE	   *file;
	char		line[8192];
	int			line_number = 0;
	HASHCTL		ctl;
	MemoryContext old_context;

	if (!cardest_benchmark_estimates_file ||
		cardest_benchmark_estimates_file[0] == '\0')
		ereport(ERROR,
				(errmsg("cardest_benchmark.estimates_file is not set")));

	if (loaded_estimates_file &&
		strcmp(loaded_estimates_file, cardest_benchmark_estimates_file) == 0)
		return;

	cardest_unload();
	cardest_context = AllocSetContextCreate(TopMemoryContext,
											"CardEst benchmark estimates",
											ALLOCSET_DEFAULT_SIZES);
	old_context = MemoryContextSwitchTo(cardest_context);
	loaded_estimates_file = pstrdup(cardest_benchmark_estimates_file);

	MemSet(&ctl, 0, sizeof(ctl));
	ctl.keysize = sizeof(CardEstKey);
	ctl.entrysize = sizeof(CardEstEntry);
	ctl.hcxt = cardest_context;
	cardest_estimates = hash_create("CardEst benchmark estimates", 1024, &ctl,
									HASH_ELEM | HASH_BLOBS | HASH_CONTEXT);

	file = AllocateFile(cardest_benchmark_estimates_file, "r");
	if (file == NULL)
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not open cardinality estimates file \"%s\": %m",
						cardest_benchmark_estimates_file)));

	while (fgets(line, sizeof(line), file) != NULL)
	{
		char	   *fields[4];
		char	   *cursor;
		char	   *endptr;
		int			field_count = 0;
		double		rows;
		CardEstKey	key;
		CardEstEntry *entry;
		bool		found;

		line_number++;
		if (strchr(line, '\n') == NULL && !feof(file))
			ereport(ERROR,
					(errmsg("cardinality estimates line %d is too long",
							line_number)));

		cursor = cardest_trim(line);
		if (*cursor == '\0' || *cursor == '#')
			continue;

		while (field_count < 4)
		{
			fields[field_count++] = cursor;
			cursor = strchr(cursor, '\t');
			if (cursor == NULL)
				break;
			*cursor++ = '\0';
		}
		if (field_count != 4 || cursor != NULL)
			ereport(ERROR,
					(errmsg("invalid cardinality estimates line %d",
							line_number),
					 errdetail("Expected four tab-separated fields: query_id, kind, relations, rows.")));

		for (field_count = 0; field_count < 4; field_count++)
			fields[field_count] = cardest_trim(fields[field_count]);

		if (strcmp(fields[1], "base") != 0 && strcmp(fields[1], "join") != 0)
			ereport(ERROR,
					(errmsg("invalid cardinality estimate kind \"%s\" at line %d",
							fields[1], line_number)));

		errno = 0;
		rows = strtod(fields[3], &endptr);
		if (errno != 0 || endptr == fields[3] || *cardest_trim(endptr) != '\0' ||
			!isfinite(rows) || rows < 0.0)
			ereport(ERROR,
					(errmsg("invalid cardinality estimate \"%s\" at line %d",
							fields[3], line_number)));

		MemSet(&key, 0, sizeof(key));
		cardest_copy_field(key.query_id, sizeof(key.query_id), fields[0],
						   "query_id", line_number);
		cardest_copy_field(key.kind, sizeof(key.kind), fields[1],
						   "kind", line_number);
		cardest_copy_field(key.relations, sizeof(key.relations), fields[2],
						   "relations", line_number);

		entry = hash_search(cardest_estimates, &key, HASH_ENTER, &found);
		if (found)
			ereport(ERROR,
					(errmsg("duplicate cardinality estimate at line %d",
							line_number),
					 errdetail("Key is query_id=%s, kind=%s, relations=%s; first defined at line %d.",
							   key.query_id, key.kind, key.relations,
							   entry->source_line)));
		entry->rows = rows;
		entry->used = false;
		entry->source_line = line_number;
	}

	if (ferror(file))
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not read cardinality estimates file \"%s\": %m",
						cardest_benchmark_estimates_file)));
	FreeFile(file);
	MemoryContextSwitchTo(old_context);
}

static void
cardest_dump_request(const CardEstKey * key, double postgres_rows)
{
	FILE	   *file;

	if (!cardest_benchmark_dump_subplans)
		return;
	if (!cardest_benchmark_dump_file || cardest_benchmark_dump_file[0] == '\0')
		ereport(ERROR,
				(errmsg("cardest_benchmark.dump_file is not set")));

	file = AllocateFile(cardest_benchmark_dump_file, "a");
	if (file == NULL)
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not open cardinality benchmark dump file \"%s\": %m",
						cardest_benchmark_dump_file)));
	fprintf(file, "%s\t%s\t%s\t%.17g\n",
			key->query_id, key->kind, key->relations, postgres_rows);
	if (FreeFile(file) != 0)
		ereport(ERROR,
				(errcode_for_file_access(),
				 errmsg("could not close cardinality benchmark dump file \"%s\": %m",
						cardest_benchmark_dump_file)));
}

static double
cardest_rows(PlannerInfo *root, Relids relids, const char *kind,
			 double postgres_rows)
{
	char		query_id_buffer[32];
	const char *query_id;
	char	   *relations;
	CardEstKey	key;
	CardEstEntry *entry;

	if (!cardest_benchmark_enabled && !cardest_benchmark_dump_subplans)
		return postgres_rows;

	query_id = cardest_effective_query_id(root, query_id_buffer,
										  sizeof(query_id_buffer));
	relations = cardest_relations_string(root, relids);

	MemSet(&key, 0, sizeof(key));
	cardest_copy_field(key.query_id, sizeof(key.query_id), query_id,
					   "query_id", 0);
	cardest_copy_field(key.kind, sizeof(key.kind), kind, "kind", 0);
	cardest_copy_field(key.relations, sizeof(key.relations), relations,
					   "relations", 0);
	pfree(relations);

	cardest_dump_request(&key, postgres_rows);

	if (!cardest_benchmark_enabled)
		return postgres_rows;

	cardest_load_estimates();
	entry = hash_search(cardest_estimates, &key, HASH_FIND, NULL);
	if (entry == NULL)
	{
		if (cardest_benchmark_strict)
			ereport(ERROR,
					(errmsg("missing cardinality estimate"),
					 errdetail("No estimate for query_id=%s, kind=%s, relations=%s.",
							   key.query_id, key.kind, key.relations)));
		ereport(WARNING,
				(errmsg("missing cardinality estimate for query_id=%s, kind=%s, relations=%s; using PostgreSQL estimate",
						key.query_id, key.kind, key.relations)));
		return postgres_rows;
	}

	entry->used = true;
	return entry->rows;
}

double
cardest_benchmark_baserel_rows(PlannerInfo *root, RelOptInfo *rel,
							   double postgres_rows)
{
	return cardest_rows(root, rel->relids, "base", postgres_rows);
}

double
cardest_benchmark_joinrel_rows(PlannerInfo *root, RelOptInfo *rel,
							   double postgres_rows)
{
	return cardest_rows(root, rel->relids, "join", postgres_rows);
}
