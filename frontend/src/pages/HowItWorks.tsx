import { Reveal } from "@/lib/reveal";

const STAGES = [
  {
    title: "Text extraction",
    model: "PyMuPDF + Tesseract OCR",
    body: "Each page is read directly from the PDF's text layer where one exists, falling back to OCR for scanned or image-only pages. Output is split into page-level chunks that downstream stages work from.",
  },
  {
    title: "Character extraction",
    model: "Ollama / Groq / Gemini",
    body: "An LLM reads each chunk and pulls out characters, aliases, and a visual description. Aliases (e.g. \"Holmes\" and \"Sherlock Holmes\") are merged with a union-find pass so the same person doesn't fragment into duplicates across chapters.",
  },
  {
    title: "Scene & prompt generation",
    model: "same LLM provider",
    body: "Each page gets a narrative summary and an image prompt, built against the running \"visual bible\" of already-established characters so a character drawn on page 40 still looks like themselves on page 41.",
  },
  {
    title: "Illustration",
    model: "Diffusers / Pollinations / Workers AI",
    body: "The page prompt is rendered into an image. Locally this runs on Diffusers with a GPU; in the cloud it's Cloudflare Workers AI or Pollinations, chosen per deployment via one environment variable.",
  },
  {
    title: "Storage & delivery",
    model: "local disk / Nhost Storage",
    body: "Source PDFs and generated illustrations are served straight back to the reader — from local disk in dev, or an Nhost storage bucket once deployed.",
  },
];

const COMPONENTS = [
  ["Provider abstraction", "LLM and image backends are swappable via env vars — no code changes to move from a local Ollama box to Groq or Gemini."],
  ["Union-find aliasing", "Character name variants merge order-independently, instead of a first-match-wins string comparison."],
  ["JSON-structured prompts", "Character and scene extraction ask the LLM for JSON directly, with a retry on malformed output, instead of parsing free-text headings."],
  ["Neon + Nhost", "Serverless Postgres (Neon) and managed object storage (Nhost) — both free tier, both reached over plain HTTPS from the backend."],
];

const LIMITS = [
  "Free-tier hosting spins down when idle — the first request after a quiet period can take 30-60s.",
  "Cloud LLM/image models are retired or renamed by their providers over time; a hardcoded model name can 404 without warning.",
  "Dense multi-character scenes are the weak point for image generation — some providers drop characters when three or more appear in one prompt.",
  "OCR fallback quality depends on scan quality; a poor scan produces poor extracted text.",
  "Single shared API key, not per-user accounts — fine for one operator, not for multi-tenant use.",
];

export default function HowItWorks() {
  return (
    <div className="mx-auto max-w-4xl px-6 py-16 sm:py-20">
      <Reveal className="max-w-2xl">
        <p className="text-sm font-semibold uppercase tracking-[0.18em] text-primary">
          How it works
        </p>
        <h1 className="mt-3 text-3xl font-bold tracking-tight text-foreground sm:text-4xl">
          One pipeline, five stages, per page.
        </h1>
        <p className="mt-4 text-base leading-relaxed text-muted-foreground">
          Every uploaded PDF runs the same path: extract the text, work out who's on the page and
          what's happening, then generate an illustration that stays consistent with what came
          before it.
        </p>
      </Reveal>

      <ol className="mt-12 space-y-4">
        {STAGES.map((s, i) => (
          <Reveal as="li" key={s.title} delay={i * 60}>
            <div className="rounded-2xl border border-border bg-card p-6">
              <div className="grid grid-cols-[auto_minmax(0,1fr)] gap-4">
                <span className="num font-mono text-xs text-primary">
                  {String(i + 1).padStart(2, "0")}
                </span>
                <div className="min-w-0">
                  <div className="flex items-baseline justify-between gap-3">
                    <h2 className="truncate text-base font-semibold text-foreground">{s.title}</h2>
                    <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
                      {s.model}
                    </span>
                  </div>
                  <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{s.body}</p>
                </div>
              </div>
            </div>
          </Reveal>
        ))}
      </ol>

      <Reveal className="mt-16">
        <h2 className="text-xl font-semibold text-foreground">Why these components</h2>
        <dl className="mt-4 divide-y divide-border rounded-2xl border border-border bg-card">
          {COMPONENTS.map(([k, v]) => (
            <div key={k} className="grid gap-1 p-4 sm:grid-cols-[11rem_minmax(0,1fr)] sm:gap-4">
              <dt className="font-mono text-xs text-primary">{k}</dt>
              <dd className="text-sm text-muted-foreground">{v}</dd>
            </div>
          ))}
        </dl>
      </Reveal>

      <Reveal className="mt-16">
        <h2 className="text-xl font-semibold text-foreground">Limitations</h2>
        <ul className="mt-4 space-y-2">
          {LIMITS.map((l) => (
            <li key={l} className="flex gap-3 text-sm text-muted-foreground">
              <span className="mt-2 size-1 shrink-0 rounded-full bg-primary" aria-hidden="true" />
              <span>{l}</span>
            </li>
          ))}
        </ul>
      </Reveal>
    </div>
  );
}
