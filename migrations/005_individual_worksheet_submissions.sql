-- Apply after 004, before deploying independent worksheet submission support.
BEGIN;
CREATE TABLE IF NOT EXISTS public.worksheet_submissions (
    submission_id varchar(36) NOT NULL REFERENCES public.participants(submission_id) ON DELETE CASCADE,
    worksheet_id varchar(36) NOT NULL REFERENCES public.study_revisions(id),
    pdf_path text NOT NULL,
    blank_qrs_json text NOT NULL DEFAULT '[]',
    submitted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (submission_id, worksheet_id)
);
ALTER TABLE public.worksheet_submissions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.worksheet_submissions FROM PUBLIC, anon, authenticated;
-- A first individual submission also sets participants.submitted_at atomically.
-- The existing empty-ID cleanup excludes that row, including all-blank submissions.
COMMIT;
