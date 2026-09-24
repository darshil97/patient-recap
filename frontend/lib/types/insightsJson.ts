/**
 * Hand-written types for `insights_json` = { layer_1, layer_2, layer_3, follow_up }.
 *
 * EVERY PROPERTY HERE IS OPTIONAL, ON PURPOSE.
 *
 * This file is an assertion, not a guarantee. The column is `Dict[str, Any]` in
 * Pydantic and `Record<string, never>` out of openapi-typescript, so nothing
 * validates these shapes at any boundary — they were read off a real row and
 * cross-checked against the prompts. The prompts are data (§12), so editing one
 * is enough to change what arrives, and the model can vary within a prompt.
 *
 * Declaring the fields required did not make them present; it only stopped the
 * compiler asking. One absent key threw on first render and blanked the page.
 * Optional everywhere pushes that question to every call site, which is where it
 * can actually be answered — render the section, or don't.
 *
 * Per backend_changes.md: Pydantic types this column as Dict[str, Any], so
 * OpenAPI generation produces an opaque object. These shapes were verified
 * against a real row (Vikas Gupta, visit 2) fetched live from the running API
 * and cross-checked against docs/PROJECT_CONTEXT.md §3 (follow-up model) and
 * §8.4 (two independent follow-up slots).
 *
 * Do not change `backend/prompts/*.md` or the pipeline based on assumptions
 * here — if a shape genuinely differs for another visit, treat unknown keys
 * as optional rather than widening this file to a guess.
 */

// ---- shared small shapes --------------------------------------------------

export interface FollowUpTiming {
  type?: string; // e.g. "relative" | "absolute"
  unit?: string | null; // e.g. "week" | "month" | "day"
  max_value?: number | null;
  min_value?: number | null;
  approximate?: boolean;
  absolute_date?: string | null;
}

export interface ScheduledVisit {
  stated?: string;
  timing?: FollowUpTiming;
  purpose?: string;
  stated_by?: string; // "doctor" | "patient" — see riya_6 inverted-label case, §8.4
  commitment?: string; // "definite" | ...
}

export interface ReturnEarlierIf {
  action?: string; // "contact_clinic" | "record_and_report" | ...
  stated?: string;
  urgency?: string; // "routine" | "urgent" | ...
  condition?: string;
}

export interface Layer1FollowUp {
  scheduled_visit?: ScheduledVisit | null;
  return_earlier_if?: ReturnEarlierIf[];
}

export interface ChiefComplaint {
  symptom?: string;
  location?: string;
  quality?: string;
  frequency?: string;
  duration?: string;
}

export interface SymptomTimeline {
  onset?: string;
  pattern?: string;
  progression?: string;
}

export interface LifestyleFactors {
  diet?: string;
  exercise?: string;
  work_stress?: string;
  caffeine_alcohol?: string;
  home_environment?: string;
}

export interface MentalEmotionalState {
  energy_level?: string;
  mood_observed?: string;
  sleep_pattern?: string;
  stressors_mentioned?: string[];
  patient_reported_emotions?: string;
}

export interface DataGap {
  area?: string;
  missing?: string;
}

export interface CurrentRemedy {
  remedy_or_medicine?: string;
  dosage?: string;
  duration?: string;
  effectiveness?: string;
}

/** What was literally said, no interpretation. */
export interface Layer1 {
  follow_up?: Layer1FollowUp;
  family_history?: string[];
  chief_complaints?: ChiefComplaint[];
  symptom_timeline?: SymptomTimeline;
  lifestyle_factors?: LifestyleFactors;
  past_medical_history?: string;
  mental_emotional_state?: MentalEmotionalState;
  data_gaps_or_ambiguities?: DataGap[];
  patient_concerns_or_fears?: string[];
  current_remedies_or_treatments?: CurrentRemedy[];
}

// ---- layer 2: homeopathic analysis ----------------------------------------

export interface PatternThemes {
  dominant_theme?: string;
  emotional_core?: string;
  secondary_themes?: string[];
}

export interface FollowUpQuestion {
  question?: string;
  why_ask?: string;
}

export interface PotentialModalities {
  additional_worse_from?: string[];
  additional_better_from?: string[];
}

export interface AlternativeRemedy {
  confidence?: "high" | "medium" | "low" | string;
  remedy_name?: string;
  why_it_fits?: string;
  when_to_consider?: string;
}

/** Model pattern-matching. Suggestions only — never merged into the record. */
export interface LlmInferredPatterns {
  pattern_themes?: PatternThemes;
  confidence_disclaimer?: string;
  questions_for_next_visit?: FollowUpQuestion[];
  red_flags_or_considerations?: string;
  potential_modalities_to_explore?: PotentialModalities;
  alternative_remedies_to_consider?: AlternativeRemedy[];
}

export interface DoctorNotedModalities {
  worse_from?: string[];
  better_from?: string[];
}

export interface PrescribedRemedy {
  remedy_name?: string;
  potency?: string;
  frequency?: string;
  reasoning_from_doctor?: string;
}

/** What the doctor actually said on the call. */
export interface DoctorStatedAnalysis {
  doctor_reasoning?: string;
  modalities_doctor_noted?: DoctorNotedModalities;
  miasm_or_themes_mentioned?: string;
  constitutional_type_mentioned?: string | null;
  remedies_prescribed_or_discussed?: PrescribedRemedy[];
  contraindications_or_cautions_doctor_stated?: string;
}

export interface Layer2 {
  llm_inferred_patterns?: LlmInferredPatterns;
  doctor_stated_analysis?: DoctorStatedAnalysis;
}

// ---- layer 3: care plan ----------------------------------------------------

export interface FollowUpPlan {
  type?: string; // "scheduled" | "conditional_only" | "not_discussed"
  purpose?: string;
  due_date?: string | null;
  stated_as?: string;
  due_window?: string | null;
  come_sooner_if?: ReturnEarlierIf[];
  confirm_with_clinic?: boolean;
}

export interface PatientSnapshot {
  name?: string;
  call_date?: string;
  age_gender?: string;
}

export interface PrescribedRemedySummary {
  name?: string;
  potency?: string;
  frequency?: string;
  next_step?: string;
}

export interface KeyModalities {
  worse_from?: string[];
  better_from?: string[];
}

export interface HomeopathicPicture {
  primary_theme?: string;
  key_modalities?: KeyModalities;
  constitutional_type?: string | null;
}

export interface LifestyleRecommendation {
  area?: string;
  action?: string;
}

/** The skimmable, doctor-facing visit brief. */
export interface Layer3 {
  notes?: string;
  chief_issue?: string;
  follow_up_plan?: FollowUpPlan;
  patient_snapshot?: PatientSnapshot;
  prescribed_remedy?: PrescribedRemedySummary;
  critical_watch_outs?: string[];
  focus_for_next_call?: string;
  homeopathic_picture?: HomeopathicPicture;
  lifestyle_recommendations?: LifestyleRecommendation[];
  follow_up_questions_for_next_visit?: FollowUpQuestion[];
}

// ---- resolved follow-up (Python, not the LLM — see PROJECT_CONTEXT.md §8.2) --

export interface ResolvedFollowUp {
  type?: string; // "scheduled" | "conditional_only" | "not_discussed"
  basis?: string; // "relative" | "absolute" | ...
  stated?: string;
  purpose?: string;
  warnings?: string[];
  call_date?: string;
  stated_by?: string;
  commitment?: string;
  confidence?: "high" | "medium" | "low" | string;
  due_latest?: string | null;
  approximate?: boolean;
  due_earliest?: string | null;
  window_label?: string | null;
  come_sooner_if?: ReturnEarlierIf[];
  days_from_call?: number | null;
  needs_human_confirmation?: boolean;
}

/** The full payload of InsightOut.insights_json. */
export interface InsightsJson {
  layer_1?: Layer1;
  layer_2?: Layer2;
  layer_3?: Layer3;
  follow_up?: ResolvedFollowUp;
}
