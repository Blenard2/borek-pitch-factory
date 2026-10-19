-- BAP-3: files the owner uploads as additional attachments of a follow-up email.
-- The bytes are kept in the private storage bucket "email_attachments" under
-- <opportunity_id>/<attachment_id><extension>; the row holds the identity, the safe display name
-- and the checksum. Whether a file goes into the email is part of the email draft's attachment
-- selection (key "external:<id>"), so it follows the draft's revision and confirmation rules.
--
-- Lifecycle: a row is created "pending", the bytes are stored, then the row becomes "ready";
-- only ready rows are offered. Deleting a row - one attachment, or all of them through the
-- cascade of a deleted opportunity - records its storage path in
-- email_attachment_storage_cleanup in the same transaction (trigger below). The API removes the
-- bytes from that queue with the service role and keeps failed entries for the next sweep, so
-- no stored file is ever left without a record of where it is.

CREATE TABLE IF NOT EXISTS public.email_attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    opportunity_id UUID NOT NULL REFERENCES public.opportunities(id) ON DELETE CASCADE,
    journey_stage TEXT NOT NULL,
    file_name TEXT NOT NULL CHECK (char_length(file_name) BETWEEN 1 AND 160),
    extension TEXT NOT NULL CHECK (extension IN ('.pdf', '.docx', '.xlsx', '.pptx', '.png', '.jpg', '.jpeg', '.txt', '.csv')),
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 10485760),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    storage_path TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready')),
    created_by UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS email_attachments_opportunity_stage_idx
    ON public.email_attachments (opportunity_id, journey_stage, created_at);

ALTER TABLE public.email_attachments ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "users_own_email_attachments" ON public.email_attachments;
CREATE POLICY "users_own_email_attachments"
    ON public.email_attachments
    FOR ALL
    USING (
        opportunity_id IN (
            SELECT id FROM public.opportunities
            WHERE created_by = auth.uid()
        )
    );

-- Storage paths whose bytes still have to be removed. No policy: only the service role reads or
-- writes it (the objects of a deleted opportunity are no longer reachable under owner policies).
CREATE TABLE IF NOT EXISTS public.email_attachment_storage_cleanup (
    storage_path TEXT PRIMARY KEY CHECK (
        storage_path ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(pdf|docx|xlsx|pptx|png|jpg|jpeg|txt|csv)$'
    ),
    opportunity_id UUID NOT NULL,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT
);

ALTER TABLE public.email_attachment_storage_cleanup ENABLE ROW LEVEL SECURITY;

-- SECURITY DEFINER: the owner deleting an attachment (or an opportunity) has no access to the
-- queue, but the path is recorded all the same.
CREATE OR REPLACE FUNCTION public.queue_email_attachment_storage_cleanup()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.email_attachment_storage_cleanup (storage_path, opportunity_id)
    VALUES (OLD.storage_path, OLD.opportunity_id)
    ON CONFLICT (storage_path) DO NOTHING;
    RETURN OLD;
END;
$$;

REVOKE ALL ON FUNCTION public.queue_email_attachment_storage_cleanup() FROM PUBLIC;

DROP TRIGGER IF EXISTS email_attachments_queue_storage_cleanup ON public.email_attachments;
CREATE TRIGGER email_attachments_queue_storage_cleanup
AFTER DELETE ON public.email_attachments
FOR EACH ROW EXECUTE FUNCTION public.queue_email_attachment_storage_cleanup();

INSERT INTO storage.buckets (id, name, public)
VALUES ('email_attachments', 'email_attachments', false)
ON CONFLICT (id) DO UPDATE SET public = false;

DROP POLICY IF EXISTS "users_own_email_attachment_objects" ON storage.objects;
CREATE POLICY "users_own_email_attachment_objects"
    ON storage.objects
    FOR ALL
    TO authenticated
    USING (
        bucket_id = 'email_attachments'
        AND (storage.foldername(name))[1] IN (
            SELECT id::text FROM public.opportunities WHERE created_by = auth.uid()
        )
    )
    WITH CHECK (
        bucket_id = 'email_attachments'
        AND (storage.foldername(name))[1] IN (
            SELECT id::text FROM public.opportunities WHERE created_by = auth.uid()
        )
    );
