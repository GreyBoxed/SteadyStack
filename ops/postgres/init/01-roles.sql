-- Two roles, because the append-only invariant is enforced by the database,
-- not by application discipline.
--
--   steadystack_owner : owns the schema, runs migrations, has DDL rights
--   steadystack_app   : the runtime connection; UPDATE/DELETE revoked on the
--                   append-only tables once they exist (see the
--                   revoke_append_only migration)
--
-- Django connects as steadystack_app for normal operation and as steadystack_owner
-- only to migrate.

CREATE ROLE steadystack_app WITH LOGIN PASSWORD 'steadystack_app_dev';

ALTER DATABASE steadystack OWNER TO steadystack_owner;

\connect steadystack

GRANT CONNECT ON DATABASE steadystack TO steadystack_app;
GRANT USAGE ON SCHEMA public TO steadystack_app;

-- Anything the owner creates from here on is usable by the app role by
-- default; individual append-only tables narrow this afterwards.
ALTER DEFAULT PRIVILEGES FOR ROLE steadystack_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO steadystack_app;
ALTER DEFAULT PRIVILEGES FOR ROLE steadystack_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO steadystack_app;
