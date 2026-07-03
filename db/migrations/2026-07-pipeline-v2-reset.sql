-- ONE-TIME reset at pipeline v2 deploy (2026-07). Run ONCE in the Supabase SQL
-- editor AFTER applying the v2 ALTER TABLE block in supabase_schema.sql.
--
-- Why: before v2, pool/curve addresses were counted as team members, so the
-- learning tables are poisoned (false rug co-appearances → false hard-SKIPs).
-- Raw observational tables are kept; memory re-matures from clean v2 data.
-- The matching local SQLite reset is done by scripts/reset_learning_tables.py.
TRUNCATE wallet_graph;
TRUNCATE funder_reputation;
