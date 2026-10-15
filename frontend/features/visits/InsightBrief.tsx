"use client";

import { useState } from "react";
import type { InsightOut } from "@/lib/api/insights";
import type {
  DataGap,
  Layer1,
  Layer2,
  Layer3,
  ResolvedFollowUp,
} from "@/lib/types/insightsJson";
import type { Visit } from "@/lib/api/visits";
import { formatDate, formatDateTime, dueUrgency } from "../../lib/dates";
import { TranscriptSection } from "./TranscriptSection";

const CONFIDENCE_CLASS: Record<string, string> = {
  high: "conf--high",
  low: "conf--low",
};

/**
 * The visit brief: renders InsightOut.insights_json (layer_1/2/3 + follow_up)
 * following the same reading order as frontend/mockups/visit-brief.html —
 * what happened, what could go wrong, what's being taken, what to ask next,
 * with "Watch for" first in DOM order so it reaches a screen reader before
 * the analysis.
 */
export function InsightBrief({
  visit,
  insight,
  asOf,
  onScheduleFollowUp,
}: {
  visit: Visit;
  insight: InsightOut;
  asOf: Date;
  onScheduleFollowUp: () => void;
}) {
  const payload = insight.insights_json ?? {};
  const layer_1: Layer1 = payload.layer_1 ?? {};
  const layer_2: Layer2 = payload.layer_2 ?? {};
  const layer_3: Layer3 = payload.layer_3 ?? {};
  const follow_up: ResolvedFollowUp = payload.follow_up ?? {};

  // Collections default to empty and nested objects to {}, so a section whose
  // data did not arrive renders as absent rather than throwing. `insights_json`
  // is stored as whatever the model returned — nothing validates its shape at
  // any boundary — so "this key is always there" was never true, only unasked.
  const watchOuts = layer_3.critical_watch_outs ?? [];
  const nextQuestions = layer_3.follow_up_questions_for_next_visit ?? [];
  const lifestyle = layer_3.lifestyle_recommendations ?? [];
  const remedy = layer_3.prescribed_remedy ?? {};
  const picture = layer_3.homeopathic_picture ?? {};
  const gaps = layer_1.data_gaps_or_ambiguities ?? [];

  // Layer 3 is the brief. Without it there is nothing to skim, and an empty
  // shell of headings would read as "this consultation had no findings" rather
  // than "this record is incomplete". The API always runs all three layers now,
  // so this covers rows written before that was true.
  const hasCarePlan = Boolean(payload.layer_3);

  // Stale-brief detection per frontend/README.md: if the transcript was
  // replaced after this brief was generated, the brief describes a different
  // consultation than the one on file. This is a clinical-safety warning, not
  // a cosmetic one.
  const isStale =
    visit.transcript_uploaded_at &&
    insight.generated_at &&
    new Date(visit.transcript_uploaded_at) > new Date(insight.generated_at);

  return (
    <div className="brief">
      <div>
        {!hasCarePlan && (
          <div className="notice notice--critical">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
              <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
              <path d="M8 6.6v2.9M8 11.2h.01" />
            </svg>
            <span>
              <span className="notice-title">This brief is incomplete.</span> The
              care plan is missing from the stored record, so what follows is
              only part of the consultation. Read the transcript below, and
              regenerate to rebuild the brief from it.
            </span>
          </div>
        )}

        {isStale && (
          <div className="notice notice--critical">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
              <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
              <path d="M8 6.6v2.9M8 11.2h.01" />
            </svg>
            <span>
              <span className="notice-title">This brief may be out of date.</span> The
              transcript was replaced after this brief was compiled. Recompile before
              relying on it.
            </span>
          </div>
        )}

        <div className="card">
          <div className="card-head">
            <h2 className="card-title">Where things stand</h2>
          </div>
          <div className="card-body">
            <p className="lede">{layer_3.chief_issue}</p>
            {layer_3.notes && (
              <p style={{ color: "var(--ink-2)", marginTop: 10, fontSize: 13.5 }}>{layer_3.notes}</p>
            )}
          </div>
        </div>

        {watchOuts.length > 0 && (
          <div className="card watch">
            <div className="card-head">
              <h2 className="card-title">Watch for</h2>
              <span className="muted" style={{ fontSize: 12 }}>
                {watchOuts.length} item
                {watchOuts.length === 1 ? "" : "s"}
              </span>
            </div>
            <div className="card-body card-body--tight">
              <ul className="watch-list">
                {watchOuts.map((item, i) => (
                  <li key={i}>
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
                      <path d="M8 2.8 14.2 13H1.8L8 2.8Z" />
                      <path d="M8 6.6v2.9M8 11.2h.01" />
                    </svg>
                    <span className={item.includes("\u2192") ? "refer" : undefined}>{item}</span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}

        <div className="card">
          <div className="card-head">
            <h2 className="card-title">Currently taking</h2>
            <span className="tag-recorded">From the consultation</span>
          </div>
          <div className="card-body">
            <dl className="kv kv--stack">
              <dt>Remedy</dt>
              <dd className={remedy.name ? undefined : "none"}>
                {remedy.name || "Not recorded"}
              </dd>
              <dt>Potency</dt>
              <dd className={remedy.potency ? undefined : "none"}>
                {remedy.potency || "Not specified"}
              </dd>
              <dt>Dosing</dt>
              <dd className={remedy.frequency ? undefined : "none"}>
                {remedy.frequency || "Not recorded"}
              </dd>
              <dt>Next step</dt>
              <dd className={remedy.next_step ? undefined : "none"}>
                {remedy.next_step || "Not recorded"}
              </dd>
            </dl>
          </div>
        </div>

        {nextQuestions.length > 0 && (
          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Ask next time</h2>
              <span className="muted" style={{ fontSize: 12 }}>
                {nextQuestions.length} questions
              </span>
            </div>
            <div className="card-body">
              <ul className="qa">
                {nextQuestions.map((q, i) => (
                  <li key={i}>
                    <p className="q">{q.question}</p>
                    <p className="why">
                      <span className="why-label">Why</span>
                      {q.why_ask}
                    </p>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}

        {lifestyle.length > 0 && (
          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Lifestyle advice given</h2>
            </div>
            <div className="card-body">
              <ul className="reco">
                {lifestyle.map((r, i) => (
                  <li key={i}>
                    <div className="area">{r.area}</div>
                    <div className="action">{r.action}</div>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </div>

      <aside className="rail">
        <FollowUpRail followUp={follow_up} asOf={asOf} onSchedule={onScheduleFollowUp} />

        {layer_3.focus_for_next_call && (
          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Focus next time</h2>
            </div>
            <div className="card-body">
              <p style={{ color: "var(--ink-2)" }}>{layer_3.focus_for_next_call}</p>
            </div>
          </div>
        )}

        <div className="card">
          <div className="card-head">
            <h2 className="card-title">Pattern</h2>
          </div>
          <div className="card-body">
            <p style={{ color: "var(--ink-2)" }}>
              {picture.primary_theme ?? "Not recorded"}
            </p>
            <dl className="kv" style={{ marginTop: 10 }}>
              <dt>Constitutional type</dt>
              <dd className={picture.constitutional_type ? undefined : "none"}>
                {picture.constitutional_type || "Not determined"}
              </dd>
            </dl>
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <h2 className="card-title">How this was produced</h2>
          </div>
          <div className="card-body prov">
            <dl>
              <dt>Generated</dt>
              <dd>{formatDateTime(insight.generated_at)}</dd>
              <dt>Model</dt>
              <dd>{insight.model_name}</dd>
              <dt>Prompts</dt>
              <dd>{insight.prompt_version ?? "—"}</dd>
              <dt>Extraction</dt>
              <dd>{insight.extraction_version ?? "—"}</dd>
              <dt>Tokens</dt>
              <dd>{insight.total_tokens?.toLocaleString() ?? "—"}</dd>
            </dl>
            <p style={{ marginTop: 10, lineHeight: 1.5 }}>
              Compiled from the consultation transcript. Check it against the{" "}
              <a href="#transcript">transcript</a> before acting on anything unexpected.
            </p>
          </div>
        </div>
      </aside>

      <div style={{ gridColumn: "1 / -1" }}>
        <ClinicalRecordSection layer1={layer_1} />
        <AnalysisSection layer2={layer_2} />
        {gaps.length > 0 && <GapsSection gaps={gaps} />}
        <TranscriptSection visitId={visit.visit_id} filename={visit.transcript_filename} />
      </div>
    </div>
  );
}

function FollowUpRail({
  followUp,
  asOf,
  onSchedule,
}: {
  followUp: ResolvedFollowUp;
  asOf: Date;
  onSchedule: () => void;
}) {
  const comeSoonerIf = followUp.come_sooner_if ?? [];
  return (
    <div className="card">
      <div className="card-head">
        <h2 className="card-title">Follow-up</h2>
      </div>
      <div className="card-body">
        {followUp.type === "scheduled" && followUp.due_earliest ? (
          <>
            <div style={{ fontSize: 17, fontWeight: 600 }}>{formatDate(followUp.due_earliest)}</div>
            <div style={{ fontSize: 12.5, marginTop: 2 }}>
              <span
                className={`badge badge--dot ${
                  dueUrgency(followUp.due_earliest, asOf) === "overdue"
                    ? "badge--overdue"
                    : dueUrgency(followUp.due_earliest, asOf) === "soon"
                      ? "badge--soon"
                      : ""
                }`}
              >
                {followUp.window_label ?? "scheduled"}
              </span>
            </div>
          </>
        ) : (
          <p className="muted">
            {followUp.type === "conditional_only"
              ? "No fixed date — return if the trigger below occurs."
              : "Not discussed on this call."}
          </p>
        )}

        {followUp.stated && (
          <p className="verbatim">
            <span className="verbatim-label">Doctor said</span>
            &ldquo;{followUp.stated}&rdquo;
          </p>
        )}

        {comeSoonerIf.length > 0 && (
          <div className="trigger">
            <div className="trigger-label">Come sooner if</div>
            {comeSoonerIf.map((trigger, i) => (
              <div key={i} style={{ marginTop: 4 }}>
                {trigger.condition}
              </div>
            ))}
          </div>
        )}

        {followUp.type === "scheduled" && (
          <button
            className="btn btn--primary"
            style={{ width: "100%", justifyContent: "center", marginTop: 12 }}
            onClick={onSchedule}
          >
            Schedule this visit
          </button>
        )}
      </div>
    </div>
  );
}

function ClinicalRecordSection({ layer1 }: { layer1: Layer1 }) {
  const [open, setOpen] = useState(false);
  const complaints = layer1.chief_complaints ?? [];
  const timeline = layer1.symptom_timeline ?? {};
  const state = layer1.mental_emotional_state ?? {};
  const living = layer1.lifestyle_factors ?? {};
  return (
    <details className="section" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        <svg className="chev" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M6 3.5 10.5 8 6 12.5" />
        </svg>
        Clinical record
        <span className="section-note">what was said on the call, without interpretation</span>
      </summary>
      <div className="section-body">
        {complaints.length > 0 && (
          <>
            <h3 style={{ fontSize: 12.5, marginBottom: 8 }}>Complaints</h3>
            <table className="complaints">
              <thead>
                <tr>
                  <th scope="col">Symptom</th>
                  <th scope="col">Location</th>
                  <th scope="col">Quality</th>
                  <th scope="col">Frequency now</th>
                </tr>
              </thead>
              <tbody>
                {complaints.map((c, i) => (
                  <tr key={i}>
                    <td>{c.symptom}</td>
                    <td>{c.location}</td>
                    <td>{c.quality}</td>
                    <td>{c.frequency}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}

        <div className="mod-grid" style={{ marginTop: 20 }}>
          <div>
            <h3 style={{ fontSize: 12.5, marginBottom: 8 }}>Course</h3>
            <dl className="kv kv--stack">
              <dt>Onset</dt>
              <dd className={timeline.onset ? undefined : "none"}>
                {timeline.onset || "Not recorded"}
              </dd>
              <dt>Pattern</dt>
              <dd className={timeline.pattern ? undefined : "none"}>
                {timeline.pattern || "Not recorded"}
              </dd>
              <dt>Progression</dt>
              <dd className={timeline.progression ? undefined : "none"}>
                {timeline.progression || "Not recorded"}
              </dd>
            </dl>
          </div>
          <div>
            <h3 style={{ fontSize: 12.5, marginBottom: 8 }}>Sleep, energy and mood</h3>
            <dl className="kv kv--stack">
              <dt>Sleep</dt>
              <dd className={state.sleep_pattern ? undefined : "none"}>
                {state.sleep_pattern || "Not recorded"}
              </dd>
              <dt>Energy</dt>
              <dd className={state.energy_level ? undefined : "none"}>
                {state.energy_level || "Not recorded"}
              </dd>
              <dt>Patient reported</dt>
              <dd className={state.patient_reported_emotions ? undefined : "none"}>
                {state.patient_reported_emotions || "Not recorded"}
              </dd>
              <dt>Mood observed by doctor</dt>
              <dd className={state.mood_observed ? undefined : "none"}>
                {state.mood_observed || "Not recorded"}
              </dd>
            </dl>
          </div>
        </div>

        <h3 style={{ fontSize: 12.5, margin: "20px 0 8px" }}>Lifestyle as reported</h3>
        <dl className="kv">
          {living.diet && (
            <>
              <dt>Diet</dt>
              <dd>{living.diet}</dd>
            </>
          )}
          {living.caffeine_alcohol && (
            <>
              <dt>Caffeine / alcohol</dt>
              <dd>{living.caffeine_alcohol}</dd>
            </>
          )}
          {living.exercise && (
            <>
              <dt>Exercise</dt>
              <dd>{living.exercise}</dd>
            </>
          )}
          {living.home_environment && (
            <>
              <dt>Home</dt>
              <dd>{living.home_environment}</dd>
            </>
          )}
          {living.work_stress && (
            <>
              <dt>Work stress</dt>
              <dd>{living.work_stress}</dd>
            </>
          )}
        </dl>
      </div>
    </details>
  );
}

function AnalysisSection({ layer2 }: { layer2: Layer2 }) {
  const [open, setOpen] = useState(false);
  const doctor = layer2.doctor_stated_analysis ?? {};
  const model = layer2.llm_inferred_patterns ?? {};
  const doctorRemedies = doctor.remedies_prescribed_or_discussed ?? [];
  const modalities = doctor.modalities_doctor_noted ?? {};
  const alternatives = model.alternative_remedies_to_consider ?? [];
  const modelQuestions = model.questions_for_next_visit ?? [];
  const themes = model.pattern_themes ?? {};
  return (
    <details className="section" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        <svg className="chev" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M6 3.5 10.5 8 6 12.5" />
        </svg>
        Homeopathic analysis
        <span className="section-note">the doctor&rsquo;s own reasoning, then model suggestions, kept apart</span>
      </summary>
      <div className="section-body">
        <p className="provenance-note">
          The two panels below are different kinds of thing. The first records what the
          doctor said on the call. The second is pattern-matching by the model: useful as
          a prompt for thinking, not a clinical opinion, and never merged into the record.
        </p>

        <div className="panel">
          <div className="panel-head">
            <h3 className="panel-title">Recorded from the consultation</h3>
            <span className="tag-recorded">Doctor stated</span>
          </div>
          <div className="panel-body">
            <dl className="kv kv--stack">
              <dt>Reasoning</dt>
              <dd className={doctor.doctor_reasoning ? undefined : "none"}>
                {doctor.doctor_reasoning || "Not recorded"}
              </dd>
              <dt>Constitutional type</dt>
              <dd className={doctor.constitutional_type_mentioned ? undefined : "none"}>
                {doctor.constitutional_type_mentioned || "Not mentioned"}
              </dd>
              <dt>Miasm or themes</dt>
              <dd className={doctor.miasm_or_themes_mentioned ? undefined : "none"}>
                {doctor.miasm_or_themes_mentioned || "Not discussed"}
              </dd>
              <dt>Cautions given</dt>
              <dd className={
                doctor.contraindications_or_cautions_doctor_stated ? undefined : "none"
              }>
                {doctor.contraindications_or_cautions_doctor_stated || "None given"}
              </dd>
            </dl>

            {doctorRemedies.length > 0 && (
              <>
                <h4 style={{ fontSize: 12, margin: "14px 0 8px" }}>Remedies prescribed or discussed</h4>
                {doctorRemedies.map((r, i) => (
                  <div className="remedy-opt" key={i}>
                    <div className="remedy-opt-head">
                      <span className="remedy-name">{r.remedy_name}</span>
                      <span className="conf">{r.potency || "Potency not specified"}</span>
                    </div>
                    <p style={{ color: "var(--ink-2)", fontSize: 13 }}>
                      {r.frequency}. {r.reasoning_from_doctor}
                    </p>
                  </div>
                ))}
              </>
            )}

            <div className="mod-grid" style={{ marginTop: 16 }}>
              <div>
                <div className="mod-title">Worse from</div>
                <ul className="mod-list">
                  {(modalities.worse_from ?? []).map((m, i) => (
                    <li key={i}>{m}</li>
                  ))}
                </ul>
              </div>
              <div>
                <div className="mod-title">Better from</div>
                <ul className="mod-list">
                  {(modalities.better_from ?? []).map((m, i) => (
                    <li key={i}>{m}</li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        </div>

        <div className="panel panel--suggested">
          <div className="panel-head">
            <h3 className="panel-title">Possibilities to consider</h3>
            <span className="tag-suggested">Suggested &middot; not clinician-verified</span>
          </div>
          <div className="panel-body">
            <p className="disclaimer">{model.confidence_disclaimer}</p>

            {alternatives.map((r, i) => (
              <div className="remedy-opt" key={i}>
                <div className="remedy-opt-head">
                  <span className="remedy-name">{r.remedy_name}</span>
                  <span className={`conf ${CONFIDENCE_CLASS[r.confidence ?? ""] ?? ""}`}>
                    {r.confidence === "high"
                      ? "Strong match"
                      : r.confidence === "low"
                        ? "Weak match"
                        : "Possible"}
                  </span>
                </div>
                <p style={{ color: "var(--ink-2)", fontSize: 13 }}>{r.why_it_fits}</p>
                <p style={{ fontSize: 12.5, marginTop: 4 }}>
                  <span className="why-label">Consider if</span>
                  {r.when_to_consider}
                </p>
              </div>
            ))}

            {modelQuestions.length > 0 && (
              <>
                <h4 style={{ fontSize: 12, margin: "16px 0 6px" }}>Questions to consider</h4>
                <ul className="qa">
                  {modelQuestions.map((q, i) => (
                    <li key={i}>
                      <p className="q">{q.question}</p>
                      <p className="why">
                        <span className="why-label">Why</span>
                        {q.why_ask}
                      </p>
                    </li>
                  ))}
                </ul>
              </>
            )}

            <h4 style={{ fontSize: 12, margin: "16px 0 6px" }}>Themes</h4>
            <dl className="kv kv--stack">
              <dt>Dominant</dt>
              <dd className={themes.dominant_theme ? undefined : "none"}>
                {themes.dominant_theme || "Not identified"}
              </dd>
              <dt>Emotional core</dt>
              <dd className={themes.emotional_core ? undefined : "none"}>
                {themes.emotional_core || "Not identified"}
              </dd>
            </dl>
          </div>
        </div>
      </div>
    </details>
  );
}

function GapsSection({ gaps }: { gaps: DataGap[] }) {
  const [open, setOpen] = useState(false);
  return (
    <details className="section" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        <svg className="chev" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
          <path d="M6 3.5 10.5 8 6 12.5" />
        </svg>
        Gaps in this consultation
        <span className="section-note">
          {gaps.length} thing{gaps.length === 1 ? "" : "s"} worth covering next time
        </span>
      </summary>
      <div className="section-body">
        <p className="provenance-note">
          These are things the conversation did not establish &mdash; useful as a
          checklist for the next call, not a criticism of the consultation.
        </p>
        <ul className="gaps">
          {gaps.map((gap, i) => (
            <li key={i}>
              <div className="area">{gap.area}</div>
              <div className="missing">{gap.missing}</div>
            </li>
          ))}
        </ul>
      </div>
    </details>
  );
}
