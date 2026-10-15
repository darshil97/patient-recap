import type { Chunk } from "./chunker";

/**
 * Sends a live recording's pieces to the backend while it is still recording.
 *
 * One at a time, in order. That is what keeps the server's chunks gap-free,
 * which finish relies on (backend live_plan). A transient failure (network
 * down, BFF timeout, Render restarting) is retried with backoff for as long as
 * it takes. A consultation cannot be repeated, so giving up after N attempts
 * would only throw away audio that will get through once the connection is
 * back. Recording carries on meanwhile; pieces wait in memory.
 *
 * A failure the server means (409: the upload was discarded or the visit
 * cancelled elsewhere; 413: the size ceiling) stops it. Nothing after that
 * can be stored.
 */

export interface UploaderSnapshot {
  /** Pieces the server has confirmed. */
  acknowledged: number;
  acknowledgedBytes: number;
  /** Pieces waiting to go up, including the one in flight. */
  waiting: number;
  /** The last attempt failed and is being retried. */
  retrying: boolean;
  /** The failure that stopped it, if one did. */
  failure: unknown;
}

export interface LiveUploaderOptions {
  isTransient: (err: unknown) => boolean;
  onChange?: (snapshot: UploaderSnapshot) => void;
  onFatal?: (err: unknown) => void;
  baseDelayMs?: number;
  maxDelayMs?: number;
  sleep?: (ms: number) => Promise<void>;
}

type Waiter = { resolve: () => void; reject: (err: unknown) => void };

export class LiveUploader {
  private queue: Chunk[] = [];
  private running = false;
  private stopped = false;
  private failure: unknown = null;
  private acknowledged = 0;
  private acknowledgedBytes = 0;
  private retrying = false;
  private waiters: Waiter[] = [];

  constructor(
    private readonly send: (index: number, blob: Blob) => Promise<unknown>,
    private readonly options: LiveUploaderOptions,
  ) {}

  enqueue(chunk: Chunk): void {
    if (this.stopped || this.failure) return;
    this.queue.push(chunk);
    this.notify();
    void this.run();
  }

  /** Resolves once every piece enqueued so far is confirmed; rejects if it stopped on a failure. */
  drain(): Promise<void> {
    if (this.failure) return Promise.reject(this.failure);
    if (this.stopped || (!this.running && this.queue.length === 0)) return Promise.resolve();
    return new Promise((resolve, reject) => this.waiters.push({ resolve, reject }));
  }

  /** Abandon whatever is still queued (the recording was discarded). */
  stop(): void {
    this.stopped = true;
    this.queue = [];
    this.notify();
    this.settle();
  }

  snapshot(): UploaderSnapshot {
    return {
      acknowledged: this.acknowledged,
      acknowledgedBytes: this.acknowledgedBytes,
      waiting: this.queue.length,
      retrying: this.retrying,
      failure: this.failure,
    };
  }

  private async run(): Promise<void> {
    if (this.running) return;
    this.running = true;
    const base = this.options.baseDelayMs ?? 1000;
    const max = this.options.maxDelayMs ?? 30_000;
    const sleep = this.options.sleep ?? ((ms) => new Promise<void>((r) => setTimeout(r, ms)));
    let delay = base;

    while (this.queue.length > 0 && !this.stopped && !this.failure) {
      const head = this.queue[0];
      try {
        await this.send(head.index, head.blob);
        if (this.stopped) break;
        this.queue.shift();
        this.acknowledged += 1;
        this.acknowledgedBytes += head.blob.size;
        this.retrying = false;
        delay = base;
        this.notify();
      } catch (err) {
        if (this.stopped) break;
        if (this.options.isTransient(err)) {
          this.retrying = true;
          this.notify();
          await sleep(delay);
          delay = Math.min(delay * 2, max);
          continue;
        }
        this.failure = err;
        this.retrying = false;
        this.notify();
        this.options.onFatal?.(err);
      }
    }

    this.running = false;
    this.settle();
  }

  private settle(): void {
    // Stopping releases waiters at once, even with a send still in flight:
    // whatever it returns no longer matters.
    if (this.running && !this.stopped) return;
    if (!this.failure && !this.stopped && this.queue.length > 0) return;
    const waiters = this.waiters;
    this.waiters = [];
    for (const waiter of waiters) {
      if (this.failure) waiter.reject(this.failure);
      else waiter.resolve();
    }
  }

  private notify(): void {
    this.options.onChange?.(this.snapshot());
  }
}
