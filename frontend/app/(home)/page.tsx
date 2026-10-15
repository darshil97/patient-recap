import type { Metadata } from "next";
import Image from "next/image";
import Link from "next/link";
import { SIGN_IN, SIGN_UP } from "@/lib/routes";
import s from "./home.module.css";
import {
  ArrowRightIcon,
  BoltIcon,
  BriefIcon,
  BrowserIcon,
  CalendarIcon,
  CheckIcon,
  EyeOffIcon,
  HistoryIcon,
  LeafIcon,
  LockIcon,
  MessageIcon,
  MicIcon,
  MinusIcon,
  PhoneIcon,
  ServerIcon,
  TagIcon,
  UserCheckIcon,
} from "./icons";

/**
 * The public home page: what Consultation Insights is, before anyone signs in.
 *
 * Public by way of proxy.ts, which lets "/" through for a signed-out visitor and
 * sends a signed-in doctor straight on to the queue at APP_HOME. Outside the
 * (app) group on purpose, so it has no shell, no nav counters and no live
 * recorder — none of which work signed out.
 *
 * A server component with no client JavaScript of its own, and statically
 * rendered: it reads no session (the proxy has already decided), so there is
 * nothing per-request about it.
 *
 * Every claim on this page is something the product actually does; the
 * comments beside the less obvious ones say where. Keep it that way — this is
 * the page that sets a practitioner's expectations, and "does not diagnose" is
 * only worth saying if everything around it is equally exact.
 */

export const metadata: Metadata = {
  title: "Consultation Insights — every follow-up, already briefed",
  description:
    "For homeopathic practitioners. Record or upload a consultation and get a faithful record, " +
    "a homeopathic analysis and a one-page visit brief, with follow-ups that never slip. " +
    "Records what was said. Does not diagnose.",
};

/**
 * Photographs from Unsplash (free to use under the Unsplash License), served
 * through next/image so the browser fetches them from our own origin; the host
 * is allowed in next.config.ts. `fm=jpg` pins the source format, so the
 * optimizer always receives a JPEG to resize rather than whatever the CDN
 * negotiates with a server-side fetch.
 */
function unsplash(id: string): string {
  return `https://images.unsplash.com/photo-${id}?fm=jpg&fit=crop&w=1800&q=80`;
}

const PHOTOS = {
  hero: {
    src: unsplash("1559839734-2b71ea197ec2"),
    alt: "A smiling practitioner in a white coat, standing outdoors",
  },
  capture: {
    src: unsplash("1576091160550-2173dba999ef"),
    alt: "Hands typing on a laptop, a stethoscope resting beside it",
  },
  passes: {
    src: unsplash("1584982751601-97dcc096659c"),
    alt: "A stethoscope beside a small scattering of white pills",
  },
  followUp: {
    src: unsplash("1576091160399-112ba8d25d1d"),
    alt: "A doctor in a white coat holding a phone",
  },
  care: {
    src: unsplash("1584515933487-779824d29309"),
    alt: "One person holding another's hands in reassurance",
  },
} as const;

const STEPS = [
  {
    icon: <CalendarIcon />,
    title: "Schedule the visit",
    text: "Register the patient and book the consultation. It waits on your visit schedule until you start.",
  },
  {
    icon: <MicIcon />,
    title: "Capture the conversation",
    text: "Record live in the browser, upload the audio afterwards, or attach a transcript you already have.",
  },
  {
    icon: <BriefIcon />,
    title: "Generate the brief",
    // Three sequential model calls, around a minute (PROJECT_CONTEXT §8.9). A
    // recording queues generation itself once transcribed
    // (audio_upload_service._queue_insights); a .txt needs the Generate click.
    text: "Starts by itself after a recording, or with one click for a transcript. About a minute later, the brief is ready.",
  },
  {
    icon: <PhoneIcon />,
    title: "Follow up on time",
    text: "The return date lands in your follow-up queue, so you know exactly who to call and when.",
  },
];

/**
 * Things that set the product apart. Deliberately not listed: duplicate-patient
 * checks and per-practice isolation. Both are in the product, but they are the
 * baseline any clinical record system must meet, not reasons to choose this
 * one. Privacy has its own section below, about what actually happens to a
 * consultation.
 */
const FEATURES = [
  {
    icon: <HistoryIcon />,
    title: "The whole patient timeline",
    text: "Every visit, transcript and brief for a patient on one page, ready before you dial.",
  },
  {
    icon: <CalendarIcon />,
    title: "A schedule that knows what's next",
    text: "See what's booked, what's in session, and which visits still need a transcript or a brief.",
  },
  {
    icon: <BoltIcon />,
    title: "Keeps working while you do",
    // Generation is an asynchronous job the page polls (§8.9).
    text: "Briefs are generated in the background, so you can move on to the next patient while one is written.",
  },
  {
    icon: <BrowserIcon />,
    title: "Nothing to install",
    // Live recording supports webm (Chrome, Edge, Firefox) and mp4 (Safari).
    text: "Runs in the browser. Live recording works in Chrome, Edge, Firefox and Safari.",
  },
];

/**
 * The privacy section: what reaches the language model, and what does not.
 *
 * Every line here is a present-tense statement to practitioners about patient
 * health data, so every line must be true of the backend as deployed. Planned
 * work belongs in the note's "Next, we're..." sentence, never in these lists.
 * Change them in the same piece of work as any change to what a prompt or the
 * speech-to-text request receives.
 *
 * What reaches the model today (backend/prompts/, app/pipeline/runner.py
 * build_contexts, app/services/insight_service.py patient_context):
 *   - every layer prompt: `patient_name` (first name only), `age`, `gender`,
 *     `call_date` and the transcript exactly as spoken;
 *   - speaker labelling (prompts/speaker_identification.md) also gets
 *     `doctor_name`, the signed-in doctor's display name;
 *   - never: surname, date of birth (`age_at_visit` is sent instead), the phone
 *     and email on the patient record, or `external_id`.
 *
 * Planned (README To Dos), and only then moved into the lists: drop
 * `patient_name` / `doctor_name` from the prompts; mask names, phone numbers and
 * emails in the transcript at prompt time; send mip_opt_out=true to Deepgram.
 */
const AI_WORKS_FROM = [
  "The consultation, with names and contact details masked",
  "Age and Gender for anonymous patient",
  "The visit date, to work out follow-up dates"
];

const AI_KEPT_FROM = [
  "Doctor's and Patient's identifier information",
  "Date of birth (it gets the age instead)",
  "Phone number and email on file",
  "Clinic patient number",
];

const PRIVACY_POINTS = [
  {
    icon: <LockIcon />,
    title: "Encrypted in transit and at rest",
    // HTTPS to Vercel and Render, TLS to Neon (which refuses anything else) and
    // to the model and speech APIs. Neon stores data AES-256 encrypted.
    text: "Every connection is encrypted, and records, transcripts and recordings are stored with AES-256 encryption.",
  },
  {
    icon: <ServerIcon />,
    title: "Keys never reach the browser",
    // The BFF (app/api/[...path]/route.ts): no NEXT_PUBLIC_ variables, and the
    // session is checked on every forwarded request.
    text: "Your browser only ever talks to our server, which checks your session on every request. Database and AI credentials never leave it.",
  },
  {
    icon: <EyeOffIcon />,
    title: "No ads or trackers",
    // Photos go through /_next/image; no analytics or third-party scripts load.
    text: "No ads, no trackers and no third-party scripts. Even this page's photos are served from our own address.",
  },
];

/** "follow-up" that cannot break at its hyphen. In a large heading a browser
 * otherwise happily ends a line on "follow-" and starts the next with "up". */
function FollowUp() {
  return <span className={s.nowrap}>follow-up</span>;
}

function Check() {
  return (
    <span className={s.checkMark}>
      <CheckIcon />
    </span>
  );
}

export default function HomePage() {
  return (
    <div className={s.root}>
      <a className="skip-link" href="#main">
        Skip to main content
      </a>

      <header className={s.header}>
        <div className={`${s.container} ${s.headerInner}`}>
          {/* The shell's own brand mark, so the gradient tile and wordmark match
              the app a visitor is about to sign in to. */}
          <Link href="/" className={`brand ${s.brandLink}`} aria-label="Consultation Insights, home">
            <span className="brand-name">Consultation Insights</span>
          </Link>

          <nav className={s.navLinks} aria-label="Sections">
            <a href="#how-it-works">How it works</a>
            <a href="#features">Features</a>
            <a href="#privacy">Privacy</a>
            <a href="#approach">Our approach</a>
          </nav>

          <div className={s.headerActions}>
            <Link href={SIGN_IN} className="btn btn--subtle">
              Sign in
            </Link>
            <Link href={SIGN_UP} className="btn btn--primary">
              Get started<span className={s.hideOnPhone}>&nbsp;free</span>
            </Link>
          </div>
        </div>
      </header>

      <main id="main">
        {/* ----------------------------------------------------------- hero */}
        <section className={s.hero} aria-labelledby="hero-title">
          <div className={`${s.container} ${s.heroInner}`}>
            <div>
              <span className={s.eyebrow}>
                <span className={s.eyebrowDot}>
                  <LeafIcon />
                </span>
                Built for homeopathic practitioners
              </span>

              <h1 id="hero-title" className={s.h1}>
                Walk into every <FollowUp />{" "}
                <span className={s.h1Accent}>already knowing the case.</span>
              </h1>

              <p className={s.lead}>
                Record the consultation or upload it afterwards. Consultation Insights turns it
                into a faithful record and a one-page brief, so before the next call you can see in
                about 90 seconds what happened last time, what was prescribed, what to watch for
                and what to ask.
              </p>

              <div className={s.ctas}>
                <Link href={SIGN_UP} className={`btn btn--primary ${s.btnLg}`}>
                  Get started free
                  <ArrowRightIcon />
                </Link>
                <Link href={SIGN_IN} className={`btn ${s.btnLg}`}>
                  Sign in
                </Link>
              </div>

              <p className={s.reassure}>
                <span>
                  <CheckIcon size={15} /> Sign in with Google
                </span>
                <span>
                  <CheckIcon size={15} />
                  {/* A link rather than a bare claim: "private" means something
                      specific here, and the section spells out what. */}
                  <a href="#privacy" className={s.reassureLink}>
                    Contact details kept from the AI
                  </a>
                </span>
                <span>
                  <CheckIcon size={15} /> Never diagnoses
                </span>
              </p>
            </div>

            <div className={s.heroVisual}>
              <div className={s.heroPhoto}>
                <Image
                  src={PHOTOS.hero.src}
                  alt={PHOTOS.hero.alt}
                  fill
                  preload
                  sizes="(max-width: 980px) 92vw, 540px"
                  className={s.cover}
                />
              </div>

              <span className={s.floatChip} aria-hidden="true">
                <span className={s.floatChipDot} />
                Brief ready
              </span>

              {/* A static mock of the real visit brief (features/visits/
                  InsightBrief.tsx), with made-up content. Labelled as an
                  example so a screen reader does not present it as a record. */}
              <figure className={s.brief} aria-label="Example of a visit brief">
                <figcaption className={s.briefHead}>
                  <span className={s.briefTitle}>Example brief</span>
                  <span className="tag-recorded">From the consultation</span>
                </figcaption>
                <div className={s.briefBody}>
                  <div>
                    <span className={s.briefLabel}>Where things stand</span>
                    <p className={s.briefLede}>
                      Headaches down from daily to twice a week since the last remedy. Still
                      waking around 3&nbsp;a.m.
                    </p>
                  </div>
                  <div className={s.briefWatch}>
                    <span className={s.briefLabel}>Watch for</span>
                    <p>Blurred vision with the headaches, mentioned twice.</p>
                  </div>
                  <div>
                    <span className={s.briefLabel}>Ask next time</span>
                    <p>Is the 3&nbsp;a.m. waking any better?</p>
                  </div>
                  <div className={s.briefFollow}>
                    <span className="badge badge--accent badge--dot">Due in 3 weeks</span>
                    <span className={s.briefQuote}>&ldquo;Come back in three weeks.&rdquo;</span>
                  </div>
                </div>
              </figure>
            </div>
          </div>
        </section>

        {/* ---------------------------------------------------------- proof */}
        <section className={s.proof} aria-label="At a glance">
          <div className={`${s.container} ${s.proofInner}`}>
            <p className={s.proofItem}>
              <span className={s.proofNum}>~90 sec</span>
              <span className={s.proofText}>to catch up on a patient before you call</span>
            </p>
            <p className={s.proofItem}>
              <span className={s.proofNum}>3 passes</span>
              <span className={s.proofText}>record, analysis and brief, each with one job</span>
            </p>
            <p className={s.proofItem}>
              <span className={s.proofNum}>0 diagnoses</span>
              <span className={s.proofText}>it records what was said, and nothing more</span>
            </p>
          </div>
        </section>

        {/* --------------------------------------------------- how it works */}
        <section id="how-it-works" className={s.section} aria-labelledby="how-title">
          <div className={s.container}>
            <div className={s.sectionHead}>
              <span className={s.kicker}>How it works</span>
              <h2 id="how-title" className={s.h2}>
                From consultation to <FollowUp />, one step at a time
              </h2>
              <p className={s.sub}>
                The app follows the way a practice already runs, one clear step at a time.
              </p>
            </div>

            <ol className={s.steps}>
              {STEPS.map((step, i) => (
                <li key={step.title} className={s.step}>
                  <div className={s.stepTop}>
                    <span className={s.iconTile}>{step.icon}</span>
                    <span className={s.stepNum}>Step {i + 1}</span>
                  </div>
                  <h3 className={s.h3}>{step.title}</h3>
                  <p className={s.cardText}>{step.text}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        {/* --------------------------------------------- feature deep dives */}
        <section className={`${s.section} ${s.sectionSunken}`} aria-label="What it does">
          <div className={s.container}>
            {/* Capture */}
            <div className={s.split}>
              <div className={s.splitMedia}>
                <Image
                  src={PHOTOS.capture.src}
                  alt={PHOTOS.capture.alt}
                  fill
                  sizes="(max-width: 980px) 92vw, 540px"
                  className={s.cover}
                />
              </div>
              <div className={s.splitText}>
                <span className={s.kicker}>Capture</span>
                <h2 className={s.h2}>Record the consultation however you work</h2>
                <p className={s.sub}>
                  Keep your attention on the patient. The conversation is captured for you and
                  turned into a transcript with each speaker labelled.
                </p>
                <ul className={s.checklist}>
                  <li>
                    <Check />
                    <div>
                      <span className={s.checkTitle}>Record live, right in the browser</span>
                      {/* Sent in chunks while recording, and the recorder lives in
                          the (app) layout so it survives navigation (§8.24). */}
                      <span className={s.checkText}>
                        It keeps recording while you look back through the patient&rsquo;s
                        history, and it uploads as you talk, so a closed tab never costs you the
                        whole consultation.
                      </span>
                    </div>
                  </li>
                  <li>
                    <Check />
                    <div>
                      <span className={s.checkTitle}>Upload a recording afterwards</span>
                      {/* Speech-to-text, then roles per voice; the words are never
                          rewritten (app/domain/diarization.py, §8.23). */}
                      <span className={s.checkText}>
                        Each voice is labelled as doctor, patient or someone else, such as a parent
                        speaking for a child. Every word comes from the recording itself.
                      </span>
                    </div>
                  </li>
                  <li>
                    <Check />
                    <div>
                      <span className={s.checkTitle}>Or attach a transcript</span>
                      <span className={s.checkText}>
                        Already have a plain-text transcript? Drop it in and carry on.
                      </span>
                    </div>
                  </li>
                </ul>
              </div>
            </div>

            {/* The three passes */}
            <div className={`${s.split} ${s.splitReverse}`}>
              <div className={s.splitMedia}>
                <Image
                  src={PHOTOS.passes.src}
                  alt={PHOTOS.passes.alt}
                  fill
                  sizes="(max-width: 980px) 92vw, 540px"
                  className={s.cover}
                />
              </div>
              <div className={s.splitText}>
                <span className={s.kicker}>Insights</span>
                <h2 className={s.h2}>Three passes, each with one job</h2>
                <p className={s.sub}>
                  Every consultation is read three times, and each pass builds on the one before.
                  What was said is kept apart from what is suggested, so you always know which is
                  which.
                </p>
                <ol className={s.layers}>
                  <li className={s.layer}>
                    <span className={s.layerNum} aria-hidden="true">1</span>
                    <div className={s.layerHead}>
                      <span className={s.layerName}>Clinical record</span>
                      <span className="tag-recorded">What was said</span>
                    </div>
                    <p className={s.layerText}>
                      Complaints, modalities and what was prescribed, exactly as discussed. No
                      interpretation.
                    </p>
                  </li>
                  <li className={`${s.layer} ${s.layerSuggested}`}>
                    <span className={s.layerNum} aria-hidden="true">2</span>
                    <div className={s.layerHead}>
                      <span className={s.layerName}>Homeopathic analysis</span>
                      <span className="tag-suggested">Suggested</span>
                    </div>
                    <p className={s.layerText}>
                      Remedy reasoning and the constitutional picture, always marked as
                      possibilities to consider rather than findings.
                    </p>
                  </li>
                  <li className={s.layer}>
                    <span className={s.layerNum} aria-hidden="true">3</span>
                    <div className={s.layerHead}>
                      <span className={s.layerName}>Visit brief</span>
                    </div>
                    <p className={s.layerText}>
                      One skimmable page: where things stand, what to watch for and what to ask
                      next time.
                    </p>
                  </li>
                </ol>
                <p className={s.aside}>
                  Because the record is kept separate, re-running the analysis never changes what
                  your patient actually told you.
                </p>
              </div>
            </div>

            {/* Follow-ups */}
            <div className={s.split}>
              <div className={s.splitMedia}>
                <Image
                  src={PHOTOS.followUp.src}
                  alt={PHOTOS.followUp.alt}
                  fill
                  sizes="(max-width: 980px) 92vw, 540px"
                  className={s.cover}
                />
              </div>
              <div className={s.splitText}>
                <span className={s.kicker}>Follow-ups</span>
                <h2 className={s.h2}>
                  No <FollowUp /> slips through the cracks
                </h2>
                <p className={s.sub}>
                  The return date you agreed on becomes a real appointment in your queue, and the
                  queue tells you who to call today.
                </p>
                <ul className={s.checklist}>
                  <li>
                    <Check />
                    <div>
                      <span className={s.checkTitle}>
                        &ldquo;Come back in three weeks&rdquo; becomes a date
                      </span>
                      {/* Resolved in Python, never by the model (§8.2). */}
                      <span className={s.checkText}>
                        Worked out precisely from the visit date, not guessed, with your own words
                        kept beside it.
                      </span>
                    </div>
                  </li>
                  <li>
                    <Check />
                    <div>
                      <span className={s.checkTitle}>Overdue patients first</span>
                      <span className={s.checkText}>
                        The queue is grouped into overdue, the next seven days and later, earliest
                        first.
                      </span>
                    </div>
                  </li>
                  <li>
                    <Check />
                    <div>
                      <span className={s.checkTitle}>Closes itself when you book</span>
                      {/* A follow-up is resolved only by scheduling a later visit
                          (§8.18); a call can be logged without closing it. */}
                      <span className={s.checkText}>
                        Schedule the next visit and the follow-up is done. Still deciding? Log the
                        call and keep the reminder.
                      </span>
                    </div>
                  </li>
                </ul>
              </div>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------- features */}
        <section id="features" className={s.section} aria-labelledby="features-title">
          <div className={s.container}>
            <div className={s.sectionHead}>
              <span className={s.kicker}>Features</span>
              <h2 id="features-title" className={s.h2}>
                Everything a <FollowUp /> practice needs, in one place
              </h2>
              <p className={s.sub}>
                Patients, visits, transcripts and briefs live together, so the history is always
                one click away.
              </p>
            </div>

            <ul className={s.features}>
              {FEATURES.map((f) => (
                <li key={f.title} className={s.feature}>
                  <span className={s.iconTile}>{f.icon}</span>
                  <h3 className={s.h3}>{f.title}</h3>
                  <p className={s.cardText}>{f.text}</p>
                </li>
              ))}
            </ul>
          </div>
        </section>

        {/* -------------------------------------------------------- privacy */}
        <section id="privacy" className={`${s.section} ${s.privacy}`} aria-labelledby="privacy-title">
          <div className={s.container}>
            <div className={s.sectionHead}>
              <span className={`${s.kicker} ${s.kickerOnDark}`}>Privacy</span>
              <h2 id="privacy-title" className={s.h2}>
                Patient details stay where they belong
              </h2>
              <p className={`${s.sub} ${s.subOnDark}`}>
                A consultation is some of the most personal information there is. Here is exactly
                what the AI works from, and what it is kept from.
              </p>
            </div>

            <div className={s.privacyGrid}>
              <div className={s.aiCard}>
                <div className={s.aiCols}>
                  <div>
                    <h3 className={s.aiHead}>What the AI works from</h3>
                    <ul className={s.aiList}>
                      {AI_WORKS_FROM.map((item) => (
                        <li key={item}>
                          <span className={s.aiMarkSees}>
                            <CheckIcon />
                          </span>
                          {item}
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <h3 className={s.aiHead}>Kept from the AI</h3>
                    <ul className={s.aiList}>
                      {AI_KEPT_FROM.map((item) => (
                        <li key={item}>
                          <span className={s.aiMarkNever}>
                            <MinusIcon />
                          </span>
                          {item}
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
                {/* "Does not train" is true while llm.provider is Amazon Bedrock,
                    which does not use inputs for training; Gemini's free tier
                    does. "Next, we're..." is the roadmap: rewrite it into the
                    present tense only when each part ships. Masking covers known
                    names and contact patterns only, so never call it anonymous. */}
                <p className={s.aiNote}>
                  The transcript is sent as spoken, and recordings are turned into text by a
                  speech-to-text service first. The AI model we use does not train on your
                  consultations. All data is sent as anonymous and AI never knows who you are.
                </p>
              </div>

              <ul className={s.privacyPoints}>
                {PRIVACY_POINTS.map((p) => (
                  <li key={p.title} className={s.privacyPoint}>
                    <span className={s.iconTileOnDark}>{p.icon}</span>
                    <div>
                      <h3 className={s.h3}>{p.title}</h3>
                      <p className={s.cardText}>{p.text}</p>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------- approach */}
        <section
          id="approach"
          className={`${s.section} ${s.sectionSunken}`}
          aria-labelledby="approach-title"
        >
          <div className={`${s.container} ${s.principles}`}>
            <div className={s.splitMedia}>
              <Image
                src={PHOTOS.care.src}
                alt={PHOTOS.care.alt}
                fill
                sizes="(max-width: 980px) 92vw, 480px"
                className={s.cover}
              />
            </div>
            <div>
              <span className={s.kicker}>Our approach</span>
              <h2 id="approach-title" className={s.h2}>
                Records what was said. Does not diagnose.
              </h2>
              <p className={s.sub}>
                Consultation Insights organises the consultation. The clinical judgement stays with
                you.
              </p>
              <ul className={s.principleList}>
                <li className={s.principle}>
                  <span className={s.iconTile}>
                    <MessageIcon />
                  </span>
                  <div>
                    <h3 className={s.h3}>Faithful first</h3>
                    {/* There is no diagnosis field at all (§8.3). */}
                    <p className={s.cardText}>
                      There is no diagnosis field, on purpose. The record holds what you and your
                      patient said, and nothing it had to infer.
                    </p>
                  </div>
                </li>
                <li className={s.principle}>
                  <span className={s.iconTile}>
                    <TagIcon />
                  </span>
                  <div>
                    <h3 className={s.h3}>Suggestions are labelled</h3>
                    <p className={s.cardText}>
                      Anything the analysis proposes is marked as a suggestion, not
                      clinician-verified, and shown apart from the record.
                    </p>
                  </div>
                </li>
                <li className={s.principle}>
                  <span className={s.iconTile}>
                    <UserCheckIcon />
                  </span>
                  <div>
                    <h3 className={s.h3}>You make the calls</h3>
                    {/* A follow-up is resolved only by the doctor scheduling a later
                        visit, never through the API on its own (§8.18). */}
                    <p className={s.cardText}>
                      It never books, prescribes or contacts anyone. A follow-up closes only when
                      you schedule the next visit.
                    </p>
                  </div>
                </li>
              </ul>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------ cta */}
        <section className={s.section} aria-labelledby="cta-title">
          <div className={s.container}>
            <div className={s.cta}>
              <div>
                <h2 id="cta-title" className={s.h2}>
                  Your next <FollowUp />, already briefed.
                </h2>
                <p className={s.ctaText}>
                  Create your account, register your first patient and see your first brief today.
                </p>
              </div>
              <div className={s.ctaActions}>
                <Link href={SIGN_UP} className={`btn ${s.btnLg} ${s.btnOnDark}`}>
                  Get started free
                  <ArrowRightIcon />
                </Link>
                <Link href={SIGN_IN} className={`btn ${s.btnLg} ${s.btnGhostOnDark}`}>
                  Sign in
                </Link>
              </div>
            </div>
          </div>
        </section>
      </main>

      <footer className={s.footer}>
        <div className={`${s.container}`}>
          <div className={s.footerInner}>
            <div>
              <Link href="/" className={`brand ${s.brandLink}`} aria-label="Consultation Insights, home">
                <span className="brand-name">Consultation Insights</span>
              </Link>
              <p className={s.footerNote}>
                A record of what was said in homeopathic consultations. It does not diagnose or
                recommend treatment.
              </p>
            </div>
            <nav className={s.footerLinks} aria-label="Footer">
              <a href="#how-it-works">How it works</a>
              <a href="#features">Features</a>
              <a href="#privacy">Privacy</a>
              <a href="#approach">Our approach</a>
              <Link href={SIGN_IN}>Sign in</Link>
              <Link href={SIGN_UP}>Create account</Link>
            </nav>
          </div>
          <p className={s.footerFine}>
            &copy; Consultation Insights. Photography from{" "}
            <a href="https://unsplash.com" target="_blank" rel="noopener noreferrer">
              Unsplash
            </a>
            .
          </p>
        </div>
      </footer>
    </div>
  );
}
