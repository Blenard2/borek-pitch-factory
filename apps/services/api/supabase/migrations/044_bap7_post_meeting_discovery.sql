-- BAP-7: Discovery 2, the post-meeting Discovery document.
-- A separate document from the pre-meeting Discovery (discovery_paper / discovery_paper_versions),
-- which it never changes. The opportunity holds the working document (including its generating
-- and failed states); every generated or edited document is a version row. Draft rows may be
-- updated; approved rows are immutable, and an edit after approval is a new draft version.

ALTER TABLE opportunities
  ADD COLUMN IF NOT EXISTS post_meeting_discovery JSONB;

CREATE TABLE IF NOT EXISTS public.post_meeting_discovery_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id UUID NOT NULL REFERENCES public.opportunities(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    document_id UUID NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'approved')),
    document_json JSONB NOT NULL,
    -- Identity of the content and of the meeting basis it was generated from (sha256 hex).
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    basis_hash TEXT NOT NULL CHECK (basis_hash ~ '^[0-9a-f]{64}$'),
    discovery1_version_id UUID NOT NULL REFERENCES public.discovery_paper_versions(id),
    created_by UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at TIMESTAMPTZ,
    approved_by UUID
);

CREATE UNIQUE INDEX IF NOT EXISTS post_meeting_discovery_versions_opportunity_version_key
    ON public.post_meeting_discovery_versions (opportunity_id, version_number);

ALTER TABLE public.post_meeting_discovery_versions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "users_own_post_meeting_discovery_versions" ON public.post_meeting_discovery_versions;
CREATE POLICY "users_own_post_meeting_discovery_versions"
    ON public.post_meeting_discovery_versions
    FOR ALL
    USING (
        opportunity_id IN (
            SELECT id FROM public.opportunities
            WHERE created_by = auth.uid()
        )
    );

CREATE OR REPLACE FUNCTION public.prevent_approved_post_meeting_discovery_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'approved' THEN
            RAISE EXCEPTION 'Approved Discovery 2 versions are immutable';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'approved' THEN
        RAISE EXCEPTION 'Approved Discovery 2 versions are immutable';
    END IF;

    IF NEW.status NOT IN ('draft', 'approved') THEN
        RAISE EXCEPTION 'Invalid Discovery 2 version status';
    END IF;

    IF NEW.opportunity_id <> OLD.opportunity_id
       OR NEW.version_number <> OLD.version_number
       OR NEW.discovery1_version_id <> OLD.discovery1_version_id
       OR NEW.basis_hash <> OLD.basis_hash THEN
        RAISE EXCEPTION 'The identity and sources of a Discovery 2 version cannot change';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS post_meeting_discovery_versions_approved_immutable ON public.post_meeting_discovery_versions;
CREATE TRIGGER post_meeting_discovery_versions_approved_immutable
BEFORE UPDATE OR DELETE ON public.post_meeting_discovery_versions
FOR EACH ROW EXECUTE FUNCTION public.prevent_approved_post_meeting_discovery_mutation();
