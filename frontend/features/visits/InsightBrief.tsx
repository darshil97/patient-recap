"use client";

import { useState } from "react";
import type { InsightOut } from "@/lib/api/insights";
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
  const { layer_1, layer_2, layer_3, follow_up } = insight.insights_json;

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

        {layer_3.critical_watch_outs.length > 0 && (
          <div className="card watch">
            <div className="card-head">
              <h2 className="card-title">Watch for</h2>
              <span className="muted" style={{ fontSize: 12 }}>
                {layer_3.critical_watch_outs.length} item
                {layer_3.critical_watch_outs.length === 1 ? "" : "s"}
              </span>
            </div>
            <div className="card-body card-body--tight">
              <ul className="watch-list">
                {layer_3.critical_watch_outs.map((item, i) => (
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
              <dd>{layer_3.prescribed_remedy.name}</dd>
              <dt>Potency</dt>
              <dd className={layer_3.prescribed_remedy.potency ? undefined : "none"}>
                {layer_3.prescribed_remedy.potency || "Not specified"}
              </dd>
              <dt>Dosing</dt>
              <dd>{layer_3.prescribed_remedy.frequency}</dd>
              <dt>Next step</dt>
              <dd>{layer_3.prescribed_remedy.next_step}</dd>
            </dl>
          </div>
        </div>

        {layer_3.follow_up_questions_for_next_visit.length > 0 && (
          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Ask next time</h2>
              <span className="muted" style={{ fontSize: 12 }}>
                {layer_3.follow_up_questions_for_next_visit.length} questions
              </span>
            </div>
            <div className="card-body">
              <ul className="qa">
                {layer_3.follow_up_questions_for_next_visit.map((q, i) => (
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

        {layer_3.lifestyle_recommendations.length > 0 && (
          <div className="card">
            <div className="card-head">
              <h2 className="card-title">Lifestyle advice given</h2>
            </div>
            <div className="card-body">
              <ul className="reco">
                {layer_3.lifestyle_recommendations.map((r, i) => (
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
            <p style={{ color: "var(--ink-2)" }}>{layer_3.homeopathic_picture.primary_theme}</p>
            <dl className="kv" style={{ marginTop: 10 }}>
              <dt>Constitutional type</dt>
              <dd className={layer_3.homeopathic_picture.constitutional_type ? undefined : "none"}>
                {layer_3.homeopathic_picture.constitutional_type || "Not determined"}
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
        {layer_1.data_gaps_or_ambiguities.length > 0 && (
          <GapsSection gaps={layer_1.data_gaps_or_ambiguities} />
        )}
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
  followUp: InsightOut["insights_json"]["follow_up"];
  asOf: Date;
  onSchedule: () => void;
}) {
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

        {followUp.come_sooner_if.length > 0 && (
          <div className="trigger">
            <div className="trigger-label">Come sooner if</div>
            {followUp.come_sooner_if.map((trigger, i) => (
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

function ClinicalRecordSection({ layer1 }: { layer1: InsightOut["insights_json"]["layer_1"] }) {
  const [open, setOpen] = useState(false);
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
        {layer1.chief_complaints.length > 0 && (
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
                {layer1.chief_complaints.map((c, i) => (
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
              <dd>{layer1.symptom_timeline.onset}</dd>
              <dt>Pattern</dt>
              <dd>{layer1.symptom_timeline.pattern}</dd>
              <dt>Progression</dt>
              <dd>{layer1.symptom_timeline.progression}</dd>
            </dl>
          </div>
          <div>
            <h3 style={{ fontSize: 12.5, marginBottom: 8 }}>Sleep, energy and mood</h3>
            <dl className="kv kv--stack">
              <dt>Sleep</dt>
              <dd className={layer1.mental_emotional_state.sleep_pattern ? undefined : "none"}>
                {layer1.mental_emotional_state.sleep_pattern || "Not recorded"}
              </dd>
              <dt>Energy</dt>
              <dd className={layer1.mental_emotional_state.energy_level ? undefined : "none"}>
                {layer1.mental_emotional_state.energy_level || "Not recorded"}
              </dd>
              <dt>Patient reported</dt>
              <dd className={layer1.mental_emotional_state.patient_reported_emotions ? undefined : "none"}>
                {layer1.mental_emotional_state.patient_reported_emotions || "Not recorded"}
              </dd>
              <dt>Mood observed by doctor</dt>
              <dd className={layer1.mental_emotional_state.mood_observed ? undefined : "none"}>
                {layer1.mental_emotional_state.mood_observed || "Not recorded"}
              </dd>
            </dl>
          </div>
        </div>

        <h3 style={{ fontSize: 12.5, margin: "20px 0 8px" }}>Lifestyle as reported</h3>
        <dl className="kv">
          {layer1.lifestyle_factors.diet && (
            <>
              <dt>Diet</dt>
              <dd>{layer1.lifestyle_factors.diet}</dd>
            </>
          )}
          {layer1.lifestyle_factors.caffeine_alcohol && (
            <>
              <dt>Caffeine / alcohol</dt>
              <dd>{layer1.lifestyle_factors.caffeine_alcohol}</dd>
            </>
          )}
          {layer1.lifestyle_factors.exercise && (
            <>
              <dt>Exercise</dt>
              <dd>{layer1.lifestyle_factors.exercise}</dd>
            </>
          )}
          {layer1.lifestyle_factors.home_environment && (
            <>
              <dt>Home</dt>
              <dd>{layer1.lifestyle_factors.home_environment}</dd>
            </>
          )}
          {layer1.lifestyle_factors.work_stress && (
            <>
              <dt>Work stress</dt>
              <dd>{layer1.lifestyle_factors.work_stress}</dd>
            </>
          )}
        </dl>
      </div>
    </details>
  );
}

function AnalysisSection({ layer2 }: { layer2: InsightOut["insights_json"]["layer_2"] }) {
  const [open, setOpen] = useState(false);
  const doctor = layer2.doctor_stated_analysis;
  const model = layer2.llm_inferred_patterns;
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
              <dd>{doctor.doctor_reasoning}</dd>
              <dt>Constitutional type</dt>
              <dd className={doctor.constitutional_type_mentioned ? undefined : "none"}>
                {doctor.constitutional_type_mentioned || "Not mentioned"}
              </dd>
              <dt>Miasm or themes</dt>
              <dd className={doctor.miasm_or_themes_mentioned ? undefined : "none"}>
                {doctor.miasm_or_themes_mentioned || "Not discussed"}
              </dd>
              <dt>Cautions given</dt>
              <dd>{doctor.contraindications_or_cautions_doctor_stated}</dd>
            </dl>

            {doctor.remedies_prescribed_or_discussed.length > 0 && (
              <>
                <h4 style={{ fontSize: 12, margin: "14px 0 8px" }}>Remedies prescribed or discussed</h4>
                {doctor.remedies_prescribed_or_discussed.map((r, i) => (
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
                  {doctor.modalities_doctor_noted.worse_from.map((m, i) => (
                    <li key={i}>{m}</li>
                  ))}
                </ul>
              </div>
              <div>
                <div className="mod-title">Better from</div>
                <ul className="mod-list">
                  {doctor.modalities_doctor_noted.better_from.map((m, i) => (
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

            {model.alternative_remedies_to_consider.map((r, i) => (
              <div className="remedy-opt" key={i}>
                <div className="remedy-opt-head">
                  <span className="remedy-name">{r.remedy_name}</span>
                  <span className={`conf ${CONFIDENCE_CLASS[r.confidence] ?? ""}`}>
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

            {model.questions_for_next_visit.length > 0 && (
              <>
                <h4 style={{ fontSize: 12, margin: "16px 0 6px" }}>Questions to consider</h4>
                <ul className="qa">
                  {model.questions_for_next_visit.map((q, i) => (
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
              <dd>{model.pattern_themes.dominant_theme}</dd>
              <dt>Emotional core</dt>
              <dd>{model.pattern_themes.emotional_core}</dd>
            </dl>
          </div>
        </div>
      </div>
    </details>
  );
}

function GapsSection({
  gaps,
}: {
  gaps: InsightOut["insights_json"]["layer_1"]["data_gaps_or_ambiguities"];
}) {
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
