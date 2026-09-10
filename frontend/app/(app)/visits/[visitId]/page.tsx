import { VisitPage } from "@/views/VisitPage";

/** Route params are a Promise in Next 16, so await before passing them down. */
export default async function Page({
  params,
}: {
  params: Promise<{ visitId: string }>;
}) {
  const { visitId } = await params;
  return <VisitPage visitId={visitId} />;
}
