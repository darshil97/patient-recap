"use client";

import { useState } from "react";
import { Modal, ModalBody, ModalFoot } from "../../components/Modal";
import { scheduleVisit } from "@/lib/api/visits";
import { ApiError } from "@/lib/api/client";
import { useStatsContext } from "../../hooks/StatsContext";

/**
 * Schedule Visit. Per PROJECT_CONTEXT.md §8.5, visit_date is required and
 * never defaulted — it anchors every relative follow-up. Scheduling this
 * visit also auto-resolves any open follow-up for the patient (§6 of
 * backend_changes.md), which is why this modal is reachable both from the
 * follow-ups queue and from a patient's own page.
 */
export function ScheduleVisitModal({
  patientId,
  patientName,
  onClose,
  onScheduled,
}: {
  patientId: string;
  patientName: string;
  onClose: () => void;
  onScheduled: () => void;
}) {
  const { stats } = useStatsContext();
  // The server's date, via /stats. `new Date().toISOString()` is UTC, so east of
  // Greenwich it returns yesterday for the first hours of every local day.
  //
  // This one matters more than the others. visit_date anchors every relative
  // follow-up (§8.5): "come back in three weeks" is resolved against it, so a
  // date that is a day out moves the appointment it computes by a day.
  const today = stats?.as_of ?? new Date().toISOString().slice(0, 10);
  const [visitDate, setVisitDate] = useState(today);
  const [visitTime, setVisitTime] = useState("");
  const [visitType, setVisitType] = useState<"initial" | "follow_up" | "">("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit() {
    setSubmitting(true);
    setError(null);
    try {
      // No transcript_provider. Where the backend stores transcripts is its own
      // decision (local disk or a Neon Object Storage bucket) and the client
      // cannot know it — the field is gone from VisitCreate. It was a no-op here
      // anyway, since a freshly scheduled visit has no transcript to store, but
      // sending it would have overridden the server's configured backend the
      // moment this call grew a transcript_path.
      await scheduleVisit({
        patient_id: patientId,
        visit_date: visitDate,
        visit_time: visitTime || undefined,
        visit_type: visitType || undefined,
        visit_status: "scheduled",
      });
      onScheduled();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not schedule the visit.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      title="Schedule visit"
      subtitle={patientName}
      onClose={onClose}
    >
      <ModalBody>
        {error && <div className="error-banner">{error}</div>}
        <div className="form-row">
          <div className="form-group">
            <label className="form-label" htmlFor="visit-date">
              Visit date
            </label>
            <input
              id="visit-date"
              type="date"
              className="input"
              value={visitDate}
              onChange={(e) => setVisitDate(e.target.value)}
            />
            <p className="form-hint">
              The real consultation date — it anchors follow-up timing.
            </p>
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="visit-time">
              Time (optional)
            </label>
            <input
              id="visit-time"
              type="time"
              className="input"
              value={visitTime}
              onChange={(e) => setVisitTime(e.target.value)}
            />
          </div>
        </div>
        <div className="form-group">
          <label className="form-label" htmlFor="visit-type">
            Visit type
          </label>
          <select
            id="visit-type"
            className="select"
            value={visitType}
            onChange={(e) => setVisitType(e.target.value as "initial" | "follow_up" | "")}
          >
            <option value="">Auto (initial for visit 1, follow-up otherwise)</option>
            <option value="initial">Initial</option>
            <option value="follow_up">Follow-up</option>
          </select>
        </div>
      </ModalBody>
      <ModalFoot>
        <button type="button" className="btn" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="btn btn--primary"
          disabled={!visitDate || submitting}
          onClick={handleSubmit}
        >
          {submitting ? "Scheduling…" : "Schedule visit"}
        </button>
      </ModalFoot>
    </Modal>
  );
}
