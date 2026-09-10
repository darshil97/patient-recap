"use client";

import { useRouter } from "next/navigation";

/** The Today/Schedule tab switcher shared by both queue pages. */
export function QueueTabs({
  active,
  followUpsCount,
  scheduleCount,
}: {
  active: "followUps" | "schedule";
  followUpsCount?: number;
  scheduleCount?: number;
}) {
  const router = useRouter();
  return (
    <div className="tabs" role="tablist" aria-label="Clinic queues">
      <button
        className="tab"
        role="tab"
        aria-selected={active === "schedule"}
        onClick={() => router.push("/schedule")}
      >
        Visit schedule
        {scheduleCount !== undefined && <span className="count">{scheduleCount}</span>}
      </button>
      <button
        className="tab"
        role="tab"
        aria-selected={active === "followUps"}
        onClick={() => router.push("/follow-ups")}
      >
        Follow-ups
        {followUpsCount !== undefined && <span className="count">{followUpsCount}</span>}
      </button>
    </div>
  );
}
