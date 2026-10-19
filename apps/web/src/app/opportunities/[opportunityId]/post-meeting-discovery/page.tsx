import { PostMeetingDiscoveryPanel } from "@/components/PostMeetingDiscoveryPanel";

interface PostMeetingDiscoveryPageProps {
  params: Promise<{ opportunityId: string }>;
}

export default async function PostMeetingDiscoveryPage({ params }: PostMeetingDiscoveryPageProps) {
  const { opportunityId } = await params;
  return <PostMeetingDiscoveryPanel opportunityId={opportunityId} />;
}
