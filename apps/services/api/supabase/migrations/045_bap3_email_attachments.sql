-- BAP-3: files the owner uploads as additional attachments of a follow-up email.
-- The bytes are kept in the private storage bucket "email_attachments" under
-- <opportunity_id>/<attachment_id><extension>; the row holds the identity, the safe display name
-- and the checksum. Whether a file goes into the email is part of the email draft's attachment
-- selection (key "external:<id>"), so it follows the draft's revision and confirmation rules.

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
