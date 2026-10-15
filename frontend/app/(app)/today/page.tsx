import { TodayPage } from "@/views/TodayPage";

/**
 * The signed-in home: the follow-up queue, who needs calling back.
 *
 * Lives at /today rather than "/" because "/" is the public home page that
 * describes the product (app/(home)/page.tsx). proxy.ts sends a signed-in
 * doctor who opens "/" straight here, and NeonAuthUIProvider's redirectTo
 * lands every sign-in here too.
 */
export default function Page() {
  return <TodayPage />;
}
