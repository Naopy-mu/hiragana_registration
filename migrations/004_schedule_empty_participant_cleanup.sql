-- Apply after 003. Registers one hourly job; it does not run cleanup now.
BEGIN;
CREATE EXTENSION IF NOT EXISTS pg_cron WITH SCHEMA pg_catalog;
SELECT cron.schedule(
    'hiragana-purge-empty-participants',
    '0 * * * *',
    'SELECT public.purge_empty_participants();'
);
COMMIT;
