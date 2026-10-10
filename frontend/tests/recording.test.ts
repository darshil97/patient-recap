import { describe, expect, it } from "vitest";

import { ExactChunker } from "@/lib/recording/chunker";
import { pickRecordingFormat } from "@/lib/recording/format";
import { SilenceDetector, formatElapsed, rmsLevel } from "@/lib/recording/level";
import { LiveUploader } from "@/lib/recording/liveUploader";

const bytes = (n: number, start = 0) =>
  new Uint8Array(Array.from({ length: n }, (_, i) => (start + i) % 251));

async function joined(blobs: Blob[]): Promise<Uint8Array> {
  return new Uint8Array(await new Blob(blobs).arrayBuffer());
}

/**
 * The chunker is the browser half of the backend's live_plan rule: every piece
 * but the last exactly chunk_bytes, and the pieces joined are the recording.
 * Get either wrong and finish refuses the recording, or worse, stitches
 * something that is not what was said.
 */
describe("ExactChunker", () => {
  it("cuts whatever the recorder hands over into exact pieces", async () => {
    const chunker = new ExactChunker(10);
    const pieces = [
      ...chunker.push(new Blob([bytes(7, 0)])),
      ...chunker.push(new Blob([bytes(15, 7)])),
      ...chunker.push(new Blob([bytes(3, 22)])),
    ];
    expect(pieces.map((p) => p.index)).toEqual([0, 1]);
    expect(pieces.map((p) => p.blob.size)).toEqual([10, 10]);
    expect(chunker.pendingBytes).toBe(5);

    const last = chunker.flush();
    expect(last).toMatchObject({ index: 2 });
    expect(last?.blob.size).toBe(5);
    expect(chunker.emitted).toBe(3);
    expect(await joined([...pieces, last!].map((p) => p.blob))).toEqual(bytes(25));
  });

  it("ends on a full piece when the recording is an exact multiple", () => {
    const chunker = new ExactChunker(4);
    expect(chunker.push(new Blob([bytes(8)])).map((p) => p.blob.size)).toEqual([4, 4]);
    expect(chunker.flush()).toBeNull();
    expect(chunker.emitted).toBe(2);
  });

  it("ignores empty blobs and flushes nothing when nothing was recorded", () => {
    const chunker = new ExactChunker(4);
    expect(chunker.push(new Blob([]))).toEqual([]);
    expect(chunker.flush()).toBeNull();
    expect(chunker.emitted).toBe(0);
  });

  it("handles one blob spanning many pieces", () => {
    const chunker = new ExactChunker(3);
    const pieces = chunker.push(new Blob([bytes(11)]));
    expect(pieces.map((p) => [p.index, p.blob.size])).toEqual([
      [0, 3],
      [1, 3],
      [2, 3],
    ]);
    expect(chunker.flush()?.blob.size).toBe(2);
  });
});

describe("LiveUploader", () => {
  const noWait = () => Promise.resolve();
  const transient = (err: unknown) => err instanceof TypeError;
  const piece = (index: number, size = 4) => ({ index, blob: new Blob([bytes(size)]) });

  it("sends pieces one at a time, in order", async () => {
    const sent: number[] = [];
    let inFlight = 0;
    let maxInFlight = 0;
    const uploader = new LiveUploader(
      async (index) => {
        inFlight += 1;
        maxInFlight = Math.max(maxInFlight, inFlight);
        await Promise.resolve();
        sent.push(index);
        inFlight -= 1;
      },
      { isTransient: transient, sleep: noWait },
    );
    [0, 1, 2, 3].forEach((i) => uploader.enqueue(piece(i)));
    await uploader.drain();
    expect(sent).toEqual([0, 1, 2, 3]);
    expect(maxInFlight).toBe(1);
    expect(uploader.snapshot()).toMatchObject({ acknowledged: 4, acknowledgedBytes: 16, waiting: 0 });
  });

  it("retries a transient failure until it gets through, without skipping ahead", async () => {
    const attempts: number[] = [];
    let failures = 3;
    const delays: number[] = [];
    const uploader = new LiveUploader(
      async (index) => {
        attempts.push(index);
        if (index === 1 && failures-- > 0) throw new TypeError("offline");
      },
      {
        isTransient: transient,
        baseDelayMs: 100,
        maxDelayMs: 300,
        sleep: async (ms) => {
          delays.push(ms);
        },
      },
    );
    [0, 1, 2].forEach((i) => uploader.enqueue(piece(i)));
    await uploader.drain();
    expect(attempts).toEqual([0, 1, 1, 1, 1, 2]);
    expect(delays).toEqual([100, 200, 300]);
    expect(uploader.snapshot()).toMatchObject({ acknowledged: 3, retrying: false });
  });

  it("stops on a failure the server means, and drain says so", async () => {
    const refusal = Object.assign(new Error("discarded"), { status: 409 });
    const fatal: unknown[] = [];
    const sent: number[] = [];
    const uploader = new LiveUploader(
      async (index) => {
        if (index === 1) throw refusal;
        sent.push(index);
      },
      { isTransient: transient, sleep: noWait, onFatal: (err) => fatal.push(err) },
    );
    [0, 1, 2].forEach((i) => uploader.enqueue(piece(i)));
    await expect(uploader.drain()).rejects.toBe(refusal);
    expect(sent).toEqual([0]);
    expect(fatal).toEqual([refusal]);
    expect(uploader.snapshot()).toMatchObject({ acknowledged: 1, failure: refusal });
    uploader.enqueue(piece(3));
    expect(uploader.snapshot().waiting).toBe(2); // nothing new accepted
  });

  it("drain resolves at once when nothing is queued", async () => {
    const uploader = new LiveUploader(async () => {}, { isTransient: transient });
    await expect(uploader.drain()).resolves.toBeUndefined();
  });

  it("stop abandons the queue and releases anyone waiting", async () => {
    let release: () => void = () => {};
    const uploader = new LiveUploader(
      () => new Promise<void>((resolve) => (release = resolve)),
      { isTransient: transient, sleep: noWait },
    );
    uploader.enqueue(piece(0));
    uploader.enqueue(piece(1));
    const waiting = uploader.drain();
    uploader.stop();
    await expect(waiting).resolves.toBeUndefined();
    release();
    expect(uploader.snapshot().acknowledged).toBe(0);
  });
});

describe("pickRecordingFormat", () => {
  it("prefers WebM/Opus", () => {
    expect(pickRecordingFormat(() => true)).toEqual({
      mimeType: "audio/webm;codecs=opus",
      extension: ".webm",
    });
  });

  it("falls back to MP4, which is what Safari records", () => {
    expect(pickRecordingFormat((t) => t === "audio/mp4")?.extension).toBe(".mp4");
  });

  it("is null when nothing is supported", () => {
    expect(pickRecordingFormat(() => false)).toBeNull();
  });
});

describe("level", () => {
  it("is zero for silence and rises with signal", () => {
    expect(rmsLevel(new Uint8Array(512).fill(128))).toBe(0);
    const loud = Uint8Array.from({ length: 512 }, (_, i) => (i % 2 ? 160 : 96));
    expect(rmsLevel(loud)).toBeGreaterThan(0.5);
  });

  it("warns only after the threshold of continuous silence", () => {
    const detector = new SilenceDetector(0, 30_000, 0.02);
    expect(detector.update(0.3, 1_000)).toBe(false);
    expect(detector.update(0, 20_000)).toBe(false);
    expect(detector.update(0, 31_000)).toBe(true);
    expect(detector.update(0.5, 31_500)).toBe(false);
    detector.reset(100_000);
    expect(detector.update(0, 110_000)).toBe(false);
  });

  it("formats elapsed time", () => {
    expect(formatElapsed(0)).toBe("0:00");
    expect(formatElapsed(65_400)).toBe("1:05");
    expect(formatElapsed(3_725_000)).toBe("1:02:05");
  });
});
