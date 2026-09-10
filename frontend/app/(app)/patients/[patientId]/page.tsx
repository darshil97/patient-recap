import { PatientDetailPage } from "@/views/PatientDetailPage";

/** Route params are a Promise in Next 16, so await before passing them down. */
export default async function Page({
  params,
}: {
  params: Promise<{ patientId: string }>;
}) {
  const { patientId } = await params;
  return <PatientDetailPage patientId={patientId} />;
}
