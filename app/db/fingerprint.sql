-- The catalog fingerprint of rmp_db's public schema (specs/WP-26.md WP26-03, D-17), alembic_version left out.
-- One read-only SELECT of pg_catalog, so its result does not depend on the connecting role's privileges. It returns one
-- row with one jsonb column, fingerprint, which psql prints on one line: app/db/fingerprint.py runs it on a connection,
-- and the programme's checks of the live database run this file in the probe form (psql as postgres, read-only) and
-- compare the saved output offline. The probe form runs it as the superuser, so tests/test_migrations.py allows in it
-- only the catalog and formatting functions it needs.
-- It holds no row data and no column order. A constraint's definition drops the " NOT VALID" PostgreSQL appends to an
-- unvalidated one, so validity lives in "validated" only. Partitioned and foreign tables are tables of their relkind;
-- an identity column's own sequence is part of the column. Definitions name public's tables as a session with the
-- default search_path prints them.
SELECT jsonb_build_object(
  'tables', (
    SELECT coalesce(jsonb_agg(jsonb_build_object(
             'name', c.relname, 'kind', c.relkind, 'persistence', c.relpersistence)
           ORDER BY c.relname COLLATE "C"), '[]'::jsonb)
      FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'f') AND c.relname <> 'alembic_version'),
  'columns', (
    SELECT coalesce(jsonb_agg(jsonb_build_object(
             'table', c.relname, 'column', a.attname, 'type', format_type(a.atttypid, a.atttypmod),
             'not_null', a.attnotnull, 'default', pg_get_expr(d.adbin, d.adrelid),
             'identity', a.attidentity, 'generated', a.attgenerated)
           ORDER BY c.relname COLLATE "C", a.attname COLLATE "C"), '[]'::jsonb)
      FROM pg_catalog.pg_attribute a
      JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
      LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'f') AND c.relname <> 'alembic_version'
       AND a.attnum > 0 AND NOT a.attisdropped),
  'indexes', (
    SELECT coalesce(jsonb_agg(jsonb_build_object(
             'name', ic.relname, 'table', tc.relname, 'definition', pg_get_indexdef(i.indexrelid),
             'unique', i.indisunique, 'valid', i.indisvalid)
           ORDER BY ic.relname COLLATE "C"), '[]'::jsonb)
      FROM pg_catalog.pg_index i
      JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
      JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = tc.relnamespace
     WHERE n.nspname = 'public' AND tc.relname <> 'alembic_version'),
  'constraints', (
    SELECT coalesce(jsonb_agg(jsonb_build_object(
             'table', tc.relname, 'name', k.conname, 'type', k.contype, 'validated', k.convalidated,
             'definition', regexp_replace(pg_get_constraintdef(k.oid), ' NOT VALID$', ''))
           ORDER BY tc.relname COLLATE "C", k.conname COLLATE "C"), '[]'::jsonb)
      FROM pg_catalog.pg_constraint k
      JOIN pg_catalog.pg_class tc ON tc.oid = k.conrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = tc.relnamespace
     WHERE n.nspname = 'public' AND tc.relname <> 'alembic_version'),
  'extensions', (
    SELECT coalesce(jsonb_agg(jsonb_build_object('name', e.extname, 'version', e.extversion)
           ORDER BY e.extname COLLATE "C"), '[]'::jsonb)
      FROM pg_catalog.pg_extension e),
  -- Every other object in public that no extension owns: views, materialised views and sequences; enums, domains
  -- and free-standing composite types; functions and procedures; user triggers; rules other than a view's _RETURN;
  -- row-level security policies.
  'other_objects', (
    SELECT coalesce(jsonb_agg(jsonb_build_object('kind', o.kind, 'name', o.name)
           ORDER BY o.kind COLLATE "C", o.name COLLATE "C"), '[]'::jsonb)
      FROM (
        SELECT CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' ELSE 'sequence' END AS kind,
               c.relname::text AS name
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('v', 'm', 'S')
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d
                            WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid
                              AND d.deptype IN ('e', 'i'))
        UNION ALL
        SELECT CASE t.typtype WHEN 'e' THEN 'enum' WHEN 'd' THEN 'domain' ELSE 'composite type' END,
               t.typname::text
          FROM pg_catalog.pg_type t
          JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
          LEFT JOIN pg_catalog.pg_class c ON c.oid = t.typrelid
         WHERE n.nspname = 'public' AND (t.typtype IN ('e', 'd') OR (t.typtype = 'c' AND c.relkind = 'c'))
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d
                            WHERE d.classid = 'pg_catalog.pg_type'::regclass AND d.objid = t.oid
                              AND d.deptype = 'e')
        UNION ALL
        SELECT CASE p.prokind WHEN 'p' THEN 'procedure' WHEN 'a' THEN 'aggregate' WHEN 'w' THEN 'window function'
               ELSE 'function' END,
               p.proname::text
          FROM pg_catalog.pg_proc p
          JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public'
           AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d
                            WHERE d.classid = 'pg_catalog.pg_proc'::regclass AND d.objid = p.oid
                              AND d.deptype = 'e')
        UNION ALL
        SELECT 'trigger', c.relname || '.' || g.tgname
          FROM pg_catalog.pg_trigger g
          JOIN pg_catalog.pg_class c ON c.oid = g.tgrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND NOT g.tgisinternal
        UNION ALL
        SELECT 'rule', c.relname || '.' || r.rulename
          FROM pg_catalog.pg_rewrite r
          JOIN pg_catalog.pg_class c ON c.oid = r.ev_class
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND r.rulename <> '_RETURN'
        UNION ALL
        SELECT 'policy', c.relname || '.' || y.polname
          FROM pg_catalog.pg_policy y
          JOIN pg_catalog.pg_class c ON c.oid = y.polrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
      ) AS o)
) AS fingerprint
