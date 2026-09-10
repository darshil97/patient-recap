"use client";

import { useState } from "react";
import { Modal, ModalBody, ModalFoot } from "../../components/Modal";
import { createPatient, type Patient } from "@/lib/api/patients";
import { ApiError } from "@/lib/api/client";

interface DuplicateInfo {
  message: string;
  existingPatientId: string;
  existingExternalId?: string;
}

/**
 * New patient registration. Handles the duplicate_patient 409 per
 * frontend/README.md: offer the existing record or resubmit with
 * allow_duplicate: true — never block outright, since siblings sharing a
 * name and birthday are real.
 */
export function NewPatientModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (patient: Patient) => void;
}) {
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [dob, setDob] = useState("");
  const [gender, setGender] = useState<"male" | "female" | "others" | "">("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [duplicate, setDuplicate] = useState<DuplicateInfo | null>(null);

  const canSubmit = firstName.trim().length > 0 && dob.length > 0;

  async function submit(allowDuplicate: boolean) {
    setSubmitting(true);
    setError(null);
    try {
      const patient = await createPatient({
        first_name: firstName.trim(),
        last_name: lastName.trim() || undefined,
        date_of_birth: dob,
        gender: gender || undefined,
        phone: phone.trim() || undefined,
        email: email.trim() || undefined,
        allow_duplicate: allowDuplicate,
      });
      onCreated(patient);
    } catch (err) {
      if (err instanceof ApiError && err.code === "duplicate_patient") {
        setDuplicate({
          message: err.message,
          existingPatientId: String(err.details.existing_patient_id ?? ""),
          existingExternalId: err.details.existing_external_id as string | undefined,
        });
      } else {
        setError(err instanceof ApiError ? err.message : "Could not register the patient.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal title="New patient" onClose={onClose}>
      <ModalBody>
        {error && <div className="error-banner">{error}</div>}

        {duplicate && (
          <div className="notice notice--info" role="alert">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
              <circle cx="8" cy="8" r="6" />
              <path d="M8 5v3.5M8 11h.01" />
            </svg>
            <span>
              <span className="notice-title">Possible duplicate.</span>{" "}
              {duplicate.message}{" "}
              {duplicate.existingExternalId && (
                <>Existing record: <strong>{duplicate.existingExternalId}</strong>.</>
              )}{" "}
              If this is really a different person, submit again below.
            </span>
          </div>
        )}

        <div className="form-row">
          <div className="form-group">
            <label className="form-label" htmlFor="np-first">
              First name
            </label>
            <input
              id="np-first"
              className="input"
              value={firstName}
              onChange={(e) => setFirstName(e.target.value)}
            />
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="np-last">
              Last name
            </label>
            <input
              id="np-last"
              className="input"
              value={lastName}
              onChange={(e) => setLastName(e.target.value)}
            />
          </div>
        </div>

        <div className="form-row">
          <div className="form-group">
            <label className="form-label" htmlFor="np-dob">
              Date of birth
            </label>
            <input
              id="np-dob"
              type="date"
              className="input"
              value={dob}
              onChange={(e) => setDob(e.target.value)}
              max={new Date().toISOString().slice(0, 10)}
            />
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="np-gender">
              Gender
            </label>
            <select
              id="np-gender"
              className="select"
              value={gender}
              onChange={(e) => setGender(e.target.value as "male" | "female" | "others" | "")}
            >
              <option value="">Not specified</option>
              <option value="male">Male</option>
              <option value="female">Female</option>
              <option value="others">Other</option>
            </select>
          </div>
        </div>

        <div className="form-row">
          <div className="form-group">
            <label className="form-label" htmlFor="np-phone">
              Phone
            </label>
            <input
              id="np-phone"
              className="input"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
            />
          </div>
          <div className="form-group">
            <label className="form-label" htmlFor="np-email">
              Email
            </label>
            <input
              id="np-email"
              type="email"
              className="input"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
        </div>
        <p className="form-hint">
          A clinic number is assigned automatically once registered.
        </p>
      </ModalBody>
      <ModalFoot>
        <button type="button" className="btn" onClick={onClose}>
          Cancel
        </button>
        {duplicate ? (
          <button
            type="button"
            className="btn btn--primary"
            disabled={submitting}
            onClick={() => submit(true)}
          >
            {submitting ? "Registering…" : "Register as a new patient"}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn--primary"
            disabled={!canSubmit || submitting}
            onClick={() => submit(false)}
          >
            {submitting ? "Registering…" : "Register patient"}
          </button>
        )}
      </ModalFoot>
    </Modal>
  );
}
