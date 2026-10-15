import { describe, expect, it } from "vitest";

import { lifecycleState } from "@/features/visits/lifecycleState";
import { checkUploadFile } from "@/lib/api/uploadChecks";

type Args = Parameters<typeof lifecycleState>[0];

const visit = (over: Partial<Args["visit"]> = {}): Args["visit"] => ({
  visit_status: "scheduled",
  transcript_path: null,
  has_insights: false,
  ...over,
});

const state = (over: Partial<Args> = {}) =>
  lifecycleState({
    visit: visit(),
    job: null,
    recording: null,
    sending: false,
    recordingHere: false,
    ...over,
  });

/**
 * The visit page shows exactly one card, so the order these are checked in is
 * the behaviour. Each case below is a situation where two of them are true.
 */
describe("lifecycleState", () => {
  it("offers Start session and upload for a visit not held yet", () => {
    expect(state()).toBe("scheduled");
  });

  it("shows the recorder for a visit in session", () => {
    expect(state({ visit: visit({ visit_status: "in_progress" }) })).toBe("in_session");
  });

  it("keeps the recorder on screen while End session completes the visit", () => {
    // The visit is already completed, the recording not yet handed over.
    expect(
      state({
        visit: visit({ visit_status: "completed" }),
        recording: { status: "receiving", mode: "live" },
        recordingHere: true,
      }),
    ).toBe("recording_live");
  });

  it("calls a live recording interrupted only when it is not this tab's", () => {
    const recording = { status: "receiving", mode: "live" } as const;
    expect(state({ visit: visit({ visit_status: "in_progress" }), recording })).toBe(
      "interrupted",
    );
    expect(state({ visit: visit({ visit_status: "completed" }), recording })).toBe("interrupted");
    expect(
      state({ visit: visit({ visit_status: "in_progress" }), recording, recordingHere: true }),
    ).toBe("recording_live");
  });

  it("does not treat an unfinished file upload as an interrupted recording", () => {
    expect(
      state({
        visit: visit({ visit_status: "completed" }),
        recording: { status: "receiving", mode: "file" },
      }),
    ).toBe("add_transcript");
  });

  it("follows a recording being transcribed, whatever the visit says", () => {
    // Just handed over: the visit reload may not have landed yet.
    expect(
      state({
        visit: visit({ visit_status: "in_progress" }),
        recording: { status: "processing", mode: "live" },
      }),
    ).toBe("transcribing");
    expect(state({ visit: visit({ visit_status: "scheduled" }), sending: true })).toBe(
      "transcribing",
    );
  });

  it("puts a running job and a cancelled visit first", () => {
    expect(state({ job: { status: "running" } })).toBe("compiling");
    expect(state({ visit: visit({ visit_status: "cancelled" }), recordingHere: true })).toBe(
      "cancelled",
    );
  });

  it("falls through to the existing transcript and brief states", () => {
    expect(state({ visit: visit({ visit_status: "completed" }) })).toBe("add_transcript");
    expect(
      state({ visit: visit({ visit_status: "completed", transcript_path: "transcripts/x.txt" }) }),
    ).toBe("generate");
  });
});

describe("checkUploadFile", () => {
  const file = (content: BlobPart[], name: string) => new File(content, name);

  it("accepts a recording and a UTF-8 transcript", async () => {
    expect(await checkUploadFile(file([new Uint8Array(10)], "visit.m4a"))).toBeNull();
    expect(await checkUploadFile(file(["DOCTOR: Hello"], "visit.txt"))).toBeNull();
  });

  it("refuses what the server would, before the visit is completed", async () => {
    expect(await checkUploadFile(file([], "visit.mp3"))).toMatch(/empty/);
    expect(await checkUploadFile(file(["x"], "visit.docx"))).toMatch(/\.txt/);
    expect(await checkUploadFile(file(["   \n "], "visit.txt"))).toMatch(/no text/);
    expect(await checkUploadFile(file([new Uint8Array([0xff, 0xfe, 0x00])], "visit.txt"))).toMatch(
      /UTF-8/,
    );
    expect(await checkUploadFile(file(["x".repeat(256 * 1024 + 1)], "visit.txt"))).toMatch(
      /256 KB/,
    );
  });
});
