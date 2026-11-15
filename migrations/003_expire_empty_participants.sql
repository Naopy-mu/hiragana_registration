-- Apply after 002. This defines cleanup but does not schedule or run it.
BEGIN;

-- Keep only credential hashes after removing an empty participant. This stops
-- an idempotent registration retry from reviving the old bearer token.
CREATE TABLE IF NOT EXISTS public.expired_registrations (
    registration_key_hash varchar(64) PRIMARY KEY,
    token_hash varchar(64) NOT NULL UNIQUE,
    expired_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.expired_registrations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.expired_registrations FROM PUBLIC, anon, authenticated;

CREATE INDEX IF NOT EXISTS ix_participants_empty_expiry
    ON public.participants (registered_at, id)
    WHERE status = 'registered' AND pdf_path IS NULL AND submitted_at IS NULL;

CREATE OR REPLACE FUNCTION public.reject_expired_registration()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    -- A deferred check also covers an INSERT that waited on the old unique
    -- registration key while cleanup deleted that old participant.
    IF EXISTS (SELECT 1 FROM public.participants WHERE id = NEW.id)
       AND EXISTS (
           SELECT 1 FROM public.expired_registrations
           WHERE registration_key_hash = NEW.registration_key_hash
              OR token_hash = NEW.token_hash
       ) THEN
        RAISE EXCEPTION 'Registration has expired; use a new registration key'
            USING ERRCODE = '23505';
    END IF;
    RETURN NULL;
END;
$$;
REVOKE ALL ON FUNCTION public.reject_expired_registration() FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS reject_expired_registration ON public.participants;
CREATE CONSTRAINT TRIGGER reject_expired_registration
    AFTER INSERT ON public.participants
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public.reject_expired_registration();

CREATE OR REPLACE FUNCTION public.purge_empty_participants()
RETURNS integer LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    cutoff timestamptz := statement_timestamp() - interval '24 hours';
    candidate record;
    removed integer := 0;
BEGIN
    FOR candidate IN
        SELECT p.id, p.registration_key_hash, p.token_hash
        FROM public.participants AS p
        WHERE p.registered_at <= cutoff
          AND p.status = 'registered'
          AND p.pdf_path IS NULL AND p.submitted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM public.pages AS page WHERE page.submission_id = p.submission_id)
        ORDER BY p.registered_at, p.id
        LIMIT 1000
        FOR UPDATE OF p SKIP LOCKED
    LOOP
        -- This separate statement gets a fresh READ COMMITTED snapshot after
        -- locking the parent. Never cascade-delete a newly registered photo.
        IF EXISTS (
            SELECT 1 FROM public.pages AS page
            JOIN public.participants AS p ON p.submission_id = page.submission_id
            WHERE p.id = candidate.id
        ) THEN
            CONTINUE;
        END IF;
        INSERT INTO public.expired_registrations (registration_key_hash, token_hash)
        VALUES (candidate.registration_key_hash, candidate.token_hash);
        DELETE FROM public.participants WHERE id = candidate.id;
        removed := removed + 1;
    END LOOP;
    RETURN removed;
END;
$$;
REVOKE ALL ON FUNCTION public.purge_empty_participants() FROM PUBLIC, anon, authenticated;

COMMIT;
