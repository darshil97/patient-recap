import { describe, expect, it } from "vitest";
import {
  daysBetween,
  dueLabel,
  dueUrgency,
  formatDate,
  parseDateOnly,
} from "@/lib/dates";

/**
 * These decide whether the queue says "overdue", which is the whole signal of
 * the follow-ups screen. They are also where a timezone bug hides: every label
 * here is derived from `as_of`, the *server's* date, precisely so a laptop with
 * a wrong clock cannot mislabel a queue.
 */
describe("parseDateOnly", () => {
  it("reads a date string as a local calendar date, not UTC midnight", () => {
    const d = parseDateOnly("2026-02-16");
    expect(d.getFullYear()).toBe(2026);
    expect(d.getMonth()).toBe(1); // February
    expect(d.getDate()).toBe(16);
  });

  it("does not shift across a timezone boundary", () => {
    // `new Date("2026-02-16")` parses as UTC midnight and renders as the 15th
    // anywhere west of Greenwich. This is the bug the helper exists to avoid.
    expect(parseDateOnly("2026-02-16").getDate()).toBe(16);
  });
});

describe("daysBetween", () => {
  it("ignores the time of day", () => {
    const a = new Date(2026, 1, 16, 23, 59);
    const b = new Date(2026, 1, 17, 0, 1);
    expect(daysBetween(a, b)).toBe(1);
  });

  it("counts backwards as negative", () => {
    expect(daysBetween(new Date(2026, 1, 20), new Date(2026, 1, 16))).toBe(-4);
  });

  it("crosses a month boundary", () => {
    expect(daysBetween(new Date(2026, 0, 28), new Date(2026, 1, 4))).toBe(7);
  });
});

describe("dueUrgency", () => {
  const asOf = new Date(2026, 1, 16);

  it("is overdue strictly before today", () => {
    expect(dueUrgency("2026-02-15", asOf)).toBe("overdue");
  });

  it("is soon, not overdue, on the day itself", () => {
    expect(dueUrgency("2026-02-16", asOf)).toBe("soon");
  });

  it("is soon up to and including seven days out", () => {
    expect(dueUrgency("2026-02-23", asOf)).toBe("soon");
  });

  it("is later on the eighth day", () => {
    // The boundary matters: it has to agree with the stats endpoint's
    // due_next_7_days bucket, or the chip and the list disagree.
    expect(dueUrgency("2026-02-24", asOf)).toBe("later");
  });
});

describe("dueLabel", () => {
  const asOf = new Date(2026, 1, 16);

  it("says today rather than 'in 0 days'", () => {
    expect(dueLabel("2026-02-16", asOf)).toBe("today");
  });

  it("singularises one day", () => {
    expect(dueLabel("2026-02-15", asOf)).toBe("1 day overdue");
    expect(dueLabel("2026-02-17", asOf)).toBe("in 1 day");
  });

  it("pluralises the rest", () => {
    expect(dueLabel("2026-01-12", asOf)).toBe("35 days overdue");
    expect(dueLabel("2026-02-19", asOf)).toBe("in 3 days");
  });
});

describe("formatDate", () => {
  it("renders an empty string for a missing date rather than 'Invalid Date'", () => {
    expect(formatDate(null)).toBe("");
    expect(formatDate(undefined)).toBe("");
  });

  it("accepts a full ISO datetime and uses only the date part", () => {
    expect(formatDate("2026-02-16T18:30:00Z")).toBe(formatDate("2026-02-16"));
  });
});
