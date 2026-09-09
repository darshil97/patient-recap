import { dueLabel, dueUrgency } from "../lib/dates";

const URGENCY_CLASS: Record<string, string> = {
  overdue: "badge--overdue",
  soon: "badge--soon",
  later: "",
};

/** "35 days overdue" / "in 3 days" pill, coloured by urgency relative to `asOf`. */
export function DueBadge({
  dueEarliest,
  asOf,
}: {
  dueEarliest: string;
  asOf: Date;
}) {
  const urgency = dueUrgency(dueEarliest, asOf);
  return (
    <span className={`badge badge--dot ${URGENCY_CLASS[urgency]}`}>
      {dueLabel(dueEarliest, asOf)}
    </span>
  );
}
