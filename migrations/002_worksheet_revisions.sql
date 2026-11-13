-- Run after 001_initial.sql, before deploying worksheet-management support.
BEGIN;
CREATE TABLE IF NOT EXISTS public.study_revisions (
    id varchar(36) PRIMARY KEY,
    manifest_json text NOT NULL,
    pdf_path text,
    original_name varchar(255) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS public.study_state (
    id integer PRIMARY KEY CHECK (id = 1),
    active_revision_id varchar(36) NOT NULL REFERENCES public.study_revisions(id),
    activated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.participants ADD COLUMN IF NOT EXISTS worksheet_revision_id varchar(36)
    REFERENCES public.study_revisions(id);
ALTER TABLE public.study_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.study_state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.study_revisions, public.study_state FROM anon, authenticated;
COMMIT;
