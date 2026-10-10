import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";

const startLiveRecording = vi.fn();
const putAudioChunk = vi.fn();
const finishWithRecovery = vi.fn();
const discardAudioUpload = vi.fn();
const completeVisit = vi.fn();

vi.mock("@/lib/api/audioUploads", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/audioUploads")>()),
  startLiveRecording: (...args: unknown[]) => startLiveRecording(...args),
  putAudioChunk: (...args: unknown[]) => putAudioChunk(...args),
  finishWithRecovery: (...args: unknown[]) => finishWithRecovery(...args),
  discardAudioUpload: (...args: unknown[]) => discardAudioUpload(...args),
  withRetry: <T,>(attempt: () => Promise<T>) => attempt(),
}));
vi.mock("@/lib/api/visits", () => ({
  completeVisit: (...args: unknown[]) => completeVisit(...args),
}));

const { LiveSessionProvider, useLiveSession } = await import(
  "@/features/recording/LiveSessionProvider"
);

/** Stands in for the browser's MediaRecorder: data is emitted on demand. */
class FakeRecorder extends EventTarget {
  static last: FakeRecorder | null = null;
  static isTypeSupported = (type: string) => type.startsWith("audio/webm");
  state: "inactive" | "recording" | "paused" = "inactive";
  timeslice: number | undefined;
  /** Handed over by the final dataavailable when stopped. */
  finalData: Blob = new Blob([]);

  constructor(
    readonly stream: unknown,
    readonly options: MediaRecorderOptions,
  ) {
    super();
    FakeRecorder.last = this;
  }
  start(timeslice?: number) {
    this.state = "recording";
    this.timeslice = timeslice;
  }
  pause() {
    this.state = "paused";
  }
  resume() {
    this.state = "recording";
  }
  requestData() {}
  stop() {
    this.state = "inactive";
    setTimeout(() => {
      this.emit(this.finalData);
      this.dispatchEvent(new Event("stop"));
    }, 0);
  }
  emit(data: Blob) {
    const event = new Event("dataavailable") as Event & { data: Blob };
    event.data = data;
    this.dispatchEvent(event);
  }
}

const track = { stop: vi.fn(), addEventListener: vi.fn() };
const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
const getUserMedia = vi.fn();

const visit = { visit_id: "v1", patient_name: "Riya Sharma", visit_number: 2 };
const upload = (over: Record<string, unknown> = {}) => ({
  upload_id: "u1",
  visit_id: "v1",
  status: "receiving",
  mode: "live",
  chunk_bytes: 4,
  ...over,
});

const wrapper = ({ children }: { children: ReactNode }) => (
  <LiveSessionProvider>{children}</LiveSessionProvider>
);

function sentIndices(): number[] {
  return putAudioChunk.mock.calls.map((call) => call[1] as number);
}

describe("LiveSessionProvider", () => {
  beforeEach(() => {
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    Object.defineProperty(navigator, "mediaDevices", {
      value: { getUserMedia },
      configurable: true,
    });
    getUserMedia.mockReset().mockResolvedValue(stream);
    track.stop.mockReset();
    startLiveRecording.mockReset().mockResolvedValue(upload());
    putAudioChunk.mockReset().mockResolvedValue(upload());
    finishWithRecovery.mockReset().mockResolvedValue(upload({ status: "processing" }));
    discardAudioUpload.mockReset().mockResolvedValue(upload({ status: "abandoned" }));
    completeVisit.mockReset().mockResolvedValue({});
    FakeRecorder.last = null;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("records into a live upload, sending exact pieces as they fill", async () => {
    const { result } = renderHook(() => useLiveSession(), { wrapper });
    let problem: string | null = "unset";
    await act(async () => {
      problem = await result.current.start(visit);
    });

    expect(problem).toBeNull();
    expect(getUserMedia).toHaveBeenCalledWith(
      expect.objectContaining({ audio: expect.objectContaining({ echoCancellation: false }) }),
    );
    expect(startLiveRecording).toHaveBeenCalledWith("v1", {
      filename: "consultation.webm",
      replace: false,
    });
    const recorder = FakeRecorder.last!;
    expect(recorder.options).toMatchObject({
      mimeType: "audio/webm;codecs=opus",
      audioBitsPerSecond: 32000,
    });
    expect(recorder.timeslice).toBe(10_000);
    expect(result.current.session?.phase).toBe("recording");

    // 10 bytes in 4-byte pieces: two go now, 2 bytes wait for more.
    await act(async () => {
      recorder.emit(new Blob([new Uint8Array(10)]));
    });
    await waitFor(() => expect(sentIndices()).toEqual([0, 1]));
    expect(putAudioChunk.mock.calls.every((call) => call[0] === "u1")).toBe(true);
    expect(putAudioChunk.mock.calls.map((call) => (call[2] as Blob).size)).toEqual([4, 4]);
  });

  it("ends by saving every piece, then completing the visit, then finishing", async () => {
    const { result } = renderHook(() => useLiveSession(), { wrapper });
    await act(async () => {
      await result.current.start(visit);
    });
    const recorder = FakeRecorder.last!;
    await act(async () => {
      recorder.emit(new Blob([new Uint8Array(10)]));
    });
    recorder.finalData = new Blob([new Uint8Array(3)]);

    let accepted: unknown = null;
    await act(async () => {
      accepted = await result.current.end();
    });

    // 2 waiting + 3 final = one full piece and a 1-byte last piece.
    expect(sentIndices()).toEqual([0, 1, 2, 3]);
    expect(putAudioChunk.mock.calls.map((call) => (call[2] as Blob).size)).toEqual([4, 4, 4, 1]);
    expect(completeVisit).toHaveBeenCalledWith("v1");
    expect(finishWithRecovery).toHaveBeenCalledWith("u1", 4);
    const lastPut = Math.max(...putAudioChunk.mock.invocationCallOrder);
    expect(completeVisit.mock.invocationCallOrder[0]).toBeGreaterThan(lastPut);
    expect(finishWithRecovery.mock.invocationCallOrder[0]).toBeGreaterThan(
      completeVisit.mock.invocationCallOrder[0],
    );
    expect(accepted).toMatchObject({ status: "processing" });
    expect(track.stop).toHaveBeenCalled();
    expect(result.current.session).toBeNull();
  });

  it("explains a blocked microphone and opens nothing on the server", async () => {
    getUserMedia.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    const { result } = renderHook(() => useLiveSession(), { wrapper });
    let problem: string | null = null;
    await act(async () => {
      problem = await result.current.start(visit);
    });
    expect(problem).toMatch(/blocked/i);
    expect(startLiveRecording).not.toHaveBeenCalled();
    expect(result.current.session).toBeNull();
  });

  it("releases the microphone if the server refuses to start", async () => {
    startLiveRecording.mockRejectedValue(
      new ApiError(409, { code: "conflict", message: "Start the session before recording." }),
    );
    const { result } = renderHook(() => useLiveSession(), { wrapper });
    let problem: string | null = null;
    await act(async () => {
      problem = await result.current.start(visit);
    });
    expect(problem).toBe("Start the session before recording.");
    expect(track.stop).toHaveBeenCalled();
    expect(result.current.session).toBeNull();
  });

  it("refuses a second session while one is running", async () => {
    const { result } = renderHook(() => useLiveSession(), { wrapper });
    await act(async () => {
      await result.current.start(visit);
    });
    let problem: string | null = null;
    await act(async () => {
      problem = await result.current.start({ ...visit, visit_id: "v2" });
    });
    expect(problem).toMatch(/already in progress/i);
    expect(startLiveRecording).toHaveBeenCalledTimes(1);
  });

  it("stops with an explanation when the recording was ended elsewhere", async () => {
    putAudioChunk.mockRejectedValue(
      new ApiError(409, { code: "conflict", message: "This upload is abandoned." }),
    );
    const { result } = renderHook(() => useLiveSession(), { wrapper });
    await act(async () => {
      await result.current.start(visit);
    });
    await act(async () => {
      FakeRecorder.last!.emit(new Blob([new Uint8Array(4)]));
    });
    await waitFor(() => expect(result.current.session?.phase).toBe("failed"));
    expect(result.current.session?.error).toMatch(/ended elsewhere/i);
    await waitFor(() => expect(track.stop).toHaveBeenCalled());
    expect(completeVisit).not.toHaveBeenCalled();
  });

  it("discards an empty recording instead of finishing it", async () => {
    const { result } = renderHook(() => useLiveSession(), { wrapper });
    await act(async () => {
      await result.current.start(visit);
    });
    let accepted: unknown = "unset";
    await act(async () => {
      accepted = await result.current.end();
    });
    expect(accepted).toBeNull();
    expect(finishWithRecovery).not.toHaveBeenCalled();
    expect(discardAudioUpload).toHaveBeenCalledWith("u1");
    expect(result.current.session?.phase).toBe("failed");
  });

  it("discard throws the recording away on the server", async () => {
    const { result } = renderHook(() => useLiveSession(), { wrapper });
    await act(async () => {
      await result.current.start(visit);
    });
    await act(async () => {
      await result.current.discard();
    });
    expect(discardAudioUpload).toHaveBeenCalledWith("u1");
    expect(track.stop).toHaveBeenCalled();
    expect(result.current.session).toBeNull();
  });

  it("does not count paused time", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { result } = renderHook(() => useLiveSession(), { wrapper });
      await act(async () => {
        await result.current.start(visit);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      act(() => result.current.pause());
      const atPause = result.current.elapsedMs;
      expect(atPause).toBeGreaterThanOrEqual(4_000);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(result.current.elapsedMs).toBe(atPause);
      expect(FakeRecorder.last!.state).toBe("paused");
    } finally {
      vi.useRealTimers();
    }
  });
});
