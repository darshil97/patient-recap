/**
 * Cuts a recorder's byte stream into exact-size pieces.
 *
 * MediaRecorder hands over blobs of whatever size it likes. The backend needs
 * every chunk of a live recording to be exactly `chunk_bytes` except the last
 * (backend/app/domain/audio.py, live_plan), because that is what lets it verify
 * the stitched file is byte-for-byte the recording. So blobs are joined and
 * re-cut here. The pieces join back into the same bytes, so the result is still
 * one valid file.
 *
 * Blob.slice is lazy: nothing is copied until a piece is uploaded.
 */
export interface Chunk {
  index: number;
  blob: Blob;
}

export class ExactChunker {
  private buffer: Blob = new Blob([]);
  private next = 0;

  constructor(readonly chunkBytes: number) {
    if (!(chunkBytes > 0)) throw new Error("chunkBytes must be positive");
  }

  /** Add recorded data; returns every piece that is now complete. */
  push(data: Blob): Chunk[] {
    if (data.size === 0) return [];
    this.buffer = new Blob([this.buffer, data]);
    const ready: Chunk[] = [];
    while (this.buffer.size >= this.chunkBytes) {
      ready.push({ index: this.next++, blob: this.buffer.slice(0, this.chunkBytes) });
      this.buffer = this.buffer.slice(this.chunkBytes);
    }
    return ready;
  }

  /** The recording has ended: the remainder, if any, is the last piece. */
  flush(): Chunk | null {
    if (this.buffer.size === 0) return null;
    const last = { index: this.next++, blob: this.buffer };
    this.buffer = new Blob([]);
    return last;
  }

  /** How many pieces have been handed out so far. */
  get emitted(): number {
    return this.next;
  }

  /** Bytes recorded but not yet in a piece. */
  get pendingBytes(): number {
    return this.buffer.size;
  }
}
