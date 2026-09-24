import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";

const getJob = vi.fn();
const getLatestJob = vi.fn();

vi.mock("@/lib/api/insights", () => ({
  getJob: (...args: unknown[]) => getJob(...args),
  getLatestJob: (...args: unknown[]) => getLatestJob(...args),
}));

const { useInsightJob } = await import("@/hooks/useInsightJob");

const job = (over: Record<string, unknown> = {}) => ({
  job_id: "job-1",
  visit_id: "visit-1",
  status: "running",
  layers_completed: 1,
  stop_after: 3,
  regenerate: false,
  model_name: null,
  insight_id: null,
  error_code: null,
  error_message: null,
  total_tokens: null,
  ...over,
});

/**
 * The two failures this hook had were both about *not stopping*: a timer that
 * outlived the component, and a catch that re-armed forever. Both are invisible
 * in the UI — the brief just looks like it is still compiling — so they need a
 * test that watches the request count rather than the render.
 */
describe("useInsightJob", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    getJob.mockReset();
    getLatestJob.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("stops polling once the job reaches a terminal status", async () => {
    getLatestJob.mockResolvedValue(job({ status: "running" }));
    getJob.mockResolvedValue(job({ status: "succeeded", layers_completed: 3 }));

    const { result } = renderHook(() => useInsightJob("visit-1"));
    await waitFor(() => expect(getLatestJob).toHaveBeenCalled());

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await waitFor(() => expect(result.current.isTerminal).toBe(true));

    const callsAtTerminal = getJob.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(getJob.mock.calls.length).toBe(callsAtTerminal);
  });

  it("stops polling when the component unmounts mid-generation", async () => {
    // The regression: cleanup cleared the pending timer, but a request already
    // in flight resolved afterwards and scheduled a fresh one, so navigating
    // away left a poll running for the rest of the session.
    getLatestJob.mockResolvedValue(job({ status: "running" }));
    getJob.mockResolvedValue(job({ status: "running" }));

    const { unmount } = renderHook(() => useInsightJob("visit-1"));
    await waitFor(() => expect(getLatestJob).toHaveBeenCalled());

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    unmount();

    const callsAtUnmount = getJob.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(getJob.mock.calls.length).toBe(callsAtUnmount);
  });

  it("does not re-arm when a request resolves after unmount", async () => {
    // The exact regression, reproduced: hold the response open, unmount, then
    // let it land. Clearing the timer cannot help here — the continuation is
    // already attached to a promise, and the old code scheduled the next poll
    // from inside it. Only a flag the continuation itself checks can stop this.
    getLatestJob.mockResolvedValue(job({ status: "running" }));

    let release: (value: unknown) => void = () => {};
    getJob.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );

    const { unmount } = renderHook(() => useInsightJob("visit-1"));
    await waitFor(() => expect(getLatestJob).toHaveBeenCalled());

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await waitFor(() => expect(getJob).toHaveBeenCalledTimes(1));

    unmount();

    await act(async () => {
      release(job({ status: "running" }));
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(getJob).toHaveBeenCalledTimes(1);
  });

  it("gives up after repeated failures instead of retrying forever", async () => {
    getLatestJob.mockResolvedValue(job({ status: "running" }));
    getJob.mockRejectedValue(new Error("network"));

    const { result } = renderHook(() => useInsightJob("visit-1"));
    await waitFor(() => expect(getLatestJob).toHaveBeenCalled());

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(getJob.mock.calls.length).toBeLessThanOrEqual(5);
  });

  it("stops immediately on a 401 rather than polling a dead session", async () => {
    getLatestJob.mockResolvedValue(job({ status: "running" }));
    getJob.mockRejectedValue(
      new ApiError(401, { code: "unauthorized", message: "Sign in to continue." }),
    );

    const { result } = renderHook(() => useInsightJob("visit-1"));
    await waitFor(() => expect(getLatestJob).toHaveBeenCalled());

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await waitFor(() => expect(result.current.error).toMatch(/session expired/i));
    expect(getJob).toHaveBeenCalledTimes(1);
  });

  it("resumes polling for a job that was already running on page load", async () => {
    getLatestJob.mockResolvedValue(job({ status: "queued" }));
    getJob.mockResolvedValue(job({ status: "running", layers_completed: 2 }));

    const { result } = renderHook(() => useInsightJob("visit-1"));
    await waitFor(() => expect(result.current.job?.status).toBe("queued"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await waitFor(() => expect(result.current.job?.layers_completed).toBe(2));
  });
});
