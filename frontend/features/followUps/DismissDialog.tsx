"use client";

import { useState } from "react";
import { Modal, ModalBody, ModalFoot } from "../../components/Modal";

const PRESET_REASONS = [
  "Feeling better, no follow-up needed",
  "Declined to return",
  "Could not be reached",
  "Moved away or transferred care",
];

const OTHER = "Other";

/**
 * frontend/README.md: dismissed_reason is required, so dismissal is never a
 * single click. Presets plus free text, per the product decision.
 */
export function DismissDialog({
  patientName,
  onCancel,
  onConfirm,
  submitting,
}: {
  patientName: string;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
  submitting: boolean;
}) {
  const [selected, setSelected] = useState<string>(PRESET_REASONS[0]);
  const [freeText, setFreeText] = useState("");

  const reason = selected === OTHER ? freeText.trim() : selected;
  const canSubmit = reason.length > 0;

  return (
    <Modal
      title="Dismiss follow-up"
      subtitle={`${patientName} will leave the follow-ups queue.`}
      onClose={onCancel}
    >
      <ModalBody>
        <div className="radio-group" role="radiogroup" aria-label="Reason for dismissing">
          {[...PRESET_REASONS, OTHER].map((option) => (
            <label
              key={option}
              className={`radio-option ${selected === option ? "radio-option--checked" : ""}`}
            >
              <input
                type="radio"
                name="dismiss-reason"
                checked={selected === option}
                onChange={() => setSelected(option)}
              />
              {option}
            </label>
          ))}
        </div>
        {selected === OTHER && (
          <div className="form-group" style={{ marginTop: 10 }}>
            <label className="form-label" htmlFor="dismiss-free-text">
              Reason
            </label>
            <textarea
              id="dismiss-free-text"
              className="textarea"
              value={freeText}
              onChange={(e) => setFreeText(e.target.value)}
              placeholder="Describe why this follow-up is being dismissed"
            />
          </div>
        )}
        <p className="form-hint" style={{ marginTop: 10 }}>
          This is reversible — reopen it later from the dismissed filter.
        </p>
      </ModalBody>
      <ModalFoot>
        <button type="button" className="btn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="btn btn--danger"
          disabled={!canSubmit || submitting}
          onClick={() => onConfirm(reason)}
        >
          {submitting ? "Dismissing…" : "Dismiss"}
        </button>
      </ModalFoot>
    </Modal>
  );
}
