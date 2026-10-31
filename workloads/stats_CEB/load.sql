\set ON_ERROR_STOP on
\copy badges FROM '/datasets/stats/badges.csv' WITH (FORMAT csv, HEADER true)
\copy comments FROM '/datasets/stats/comments.csv' WITH (FORMAT csv, HEADER true)
\copy users FROM '/datasets/stats/users.csv' WITH (FORMAT csv, HEADER true)
\copy tags FROM '/datasets/stats/tags.csv' WITH (FORMAT csv, HEADER true)
\copy posts FROM '/datasets/stats/posts.csv' WITH (FORMAT csv, HEADER true)
\copy votes FROM '/datasets/stats/votes.csv' WITH (FORMAT csv, HEADER true)
\copy posthistory FROM '/datasets/stats/postHistory.csv' WITH (FORMAT csv, HEADER true)
\copy postlinks FROM '/datasets/stats/postLinks.csv' WITH (FORMAT csv, HEADER true)
