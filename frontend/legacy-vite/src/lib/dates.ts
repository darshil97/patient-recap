/**
 * Date formatting and "days until/since" helpers.
 *
 * Per frontend/README.md: the API deliberately omits `days_overdue` because
 * it depends on today and would go stale in a cached response. Every
 * "overdue" / "in N days" label in the UI is derived here, from `as_of`
 * (the stats endpoint's server date) rather than the browser's clock, so a
 * doctor's laptop with a wrong clock can't mislabel the queue.
 */

/** Parses a "YYYY-MM-DD" date string as a local calendar date (no time, no TZ shift). */
export function parseDateOnly(value: string): Date {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, (month ?? 1) - 1, day ?? 1);
}

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/** Whole days between two dates (b - a), ignoring time of day. */
export function daysBetween(a: Date, b: Date): number {
  const ms = startOfDay(b).getTime() - startOfDay(a).getTime();
  return Math.round(ms / 86_400_000);
}

const DATE_FMT = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
});

const DATETIME_FMT = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

/** "21 Jul 2026" */
export function formatDate(value: string | null | undefined): string {
  if (!value) return "";
  return DATE_FMT.format(parseDateOnly(value.slice(0, 10)));
}

/** "07 Aug 2026, 12:58" from an ISO datetime string. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "";
  return DATETIME_FMT.format(new Date(value));
}

export type DueUrgency = "overdue" | "soon" | "later";

/**
 * Classifies a due date relative to `asOf` (the server's current date).
 * "soon" = due within the next 7 days, matching the stats endpoint's
 * `due_next_7_days` bucket and the mockup's "Next 7 days" queue group.
 */
export function dueUrgency(dueEarliest: string, asOf: Date): DueUrgency {
  const days = daysBetween(asOf, parseDateOnly(dueEarliest));
  if (days < 0) return "overdue";
  if (days <= 7) return "soon";
  return "later";
}

/** "35 days overdue" / "in 3 days" / "today" — paired with a badge, never colour alone. */
export function dueLabel(dueEarliest: string, asOf: Date): string {
  const days = daysBetween(asOf, parseDateOnly(dueEarliest));
  if (days === 0) return "today";
  if (days < 0) return `${Math.abs(days)} day${Math.abs(days) === 1 ? "" : "s"} overdue`;
  return `in ${days} day${days === 1 ? "" : "s"}`;
}
