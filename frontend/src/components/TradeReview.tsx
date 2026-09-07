"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  LoadingState,
  Panel,
  StatTile,
} from "@/components/ui";
import {
  reviewService,
  type EvidenceKind,
  type ReviewEvidence,
  type ReviewRating,
  type ReviewSection,
  type TradeReviewRow,
} from "@/lib/services";

/**
 * The AI review section of a trade. Sections 40 to 44.
 *
 * **The three kinds of claim are rendered differently.** §41. An OBSERVED line
 * shows its source; an INTERPRETED line is marked as a reading; a HYPOTHESIS is
 * marked as one and is visually quieter than both. Presenting a hypothesis with
 * the same weight as a recorded fact is the failure §41 exists to prevent, and
 * it is prevented here by the badge rather than by a convention.
 *
 * **UNKNOWN is neutral, not bad.** §12. A section the platform could not assess
 * shows the reason it could not, in a neutral tone — rating it red would say the
 * trade was poor when what happened is that a record was missing.
 *
 * **Every review shows its metadata.** §43: version, model, model version,
 * prompt version, confidence and status, because a review nobody can attribute
 * is a review nobody can audit. The review model and the PREDICTION model are
 * shown as separate rows — §21, they are different questions.
 *
 * **Regeneration adds a version.** §44. The control says so, and earlier
 * versions stay listed.
 */
const RATING_TONE: Record<ReviewRating, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  GOOD: "accent",
  FAIR: "warning",
  POOR: "critical",
  // Not a judgement about the trade. A statement about the record.
  UNKNOWN: "neutral",
};

const KIND_TONE: Record<EvidenceKind, "good" | "warning" | "critical" | "neutral" | "accent"> = {
  OBSERVED: "accent",
  INTERPRETED: "neutral",
  HYPOTHESIS: "warning",
};

const SECTION_LABELS: [keyof TradeReviewRow, string][] = [
  ["strategy_alignment", "Strategy alignment"],
  ["entry_quality", "Entry quality"],
  ["exit_quality", "Exit quality"],
  ["risk_quality", "Risk quality"],
  ["execution_quality", "Execution quality"],
];

function EvidenceLine({ item }: { item: ReviewEvidence }) {
  return (
    <li className="flex flex-wrap items-baseline gap-2 py-0.5">
      <Badge tone={KIND_TONE[item.kind] ?? "neutral"}>{item.kind}</Badge>
      <span className={item.kind === "HYPOTHESIS" ? "text-body text-muted" : "text-body"}>
        {item.statement}
      </span>
      {item.source ? (
        <span className="font-mono text-micro text-muted">({item.source})</span>
      ) : null}
    </li>
  );
}

function SectionCard({ label, section }: { label: string; section: ReviewSection | null }) {
  if (!section) return null;
  return (
    <div className="rounded border border-line p-2">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-mini font-semibold uppercase tracking-wide text-muted">
          {label}
        </span>
        <Badge tone={RATING_TONE[section.rating] ?? "neutral"}>{section.rating}</Badge>
      </div>
      {section.rating === "UNKNOWN" && section.unavailable_reason ? (
        <p className="text-body text-muted">{section.unavailable_reason}</p>
      ) : null}
      {section.evidence.length ? (
        <ul className="mt-1">
          {section.evidence.map((item, index) => (
            <EvidenceLine key={`${item.kind}-${index}`} item={item} />
          ))}
        </ul>
      ) : null}
      {section.warnings.length ? (
        <ul className="mt-1 space-y-0.5 text-body text-warning">
          {section.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function TradeReview({ tradeId }: { tradeId: string }) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ["trade-review", tradeId],
    queryFn: () => reviewService.forTrade(tradeId),
  });

  const generate = useMutation({
    mutationFn: () => reviewService.generate(tradeId),
    onSuccess: () => client.invalidateQueries({ queryKey: ["trade-review", tradeId] }),
  });
  const regenerate = useMutation({
    mutationFn: (reviewId: string) => reviewService.regenerate(reviewId),
    onSuccess: () => client.invalidateQueries({ queryKey: ["trade-review", tradeId] }),
  });

  if (query.isLoading) return <LoadingState what="the review" />;
  if (query.isError) return <ErrorState message="The review could not be read." />;
  const envelope = query.data;
  if (!envelope) return null;

  if (!envelope.available) {
    return (
      <Panel title="AI review" level={33}>
        <EmptyState
          message="NO REVIEW"
          hint={envelope.why ?? "No review has been generated for this trade."}
        />
        {envelope.eligible ? (
          <div className="mt-2">
            <Button onClick={() => generate.mutate()} disabled={generate.isPending}>
              {generate.isPending ? "Generating…" : "Generate review"}
            </Button>
            <p className="mt-1 text-body text-muted">
              Generation never alters the trade. If it fails, the review is marked failed and
              the trade is untouched.
            </p>
          </div>
        ) : null}
      </Panel>
    );
  }

  const review = envelope.review as TradeReviewRow;
  const failed = review.status === "FAILED";

  return (
    <Panel
      title="AI review"
      level={33}
      right={
        <Button
          onClick={() => regenerate.mutate(review.id)}
          disabled={regenerate.isPending}
          variant="secondary"
        >
          {regenerate.isPending ? "Working…" : "Regenerate"}
        </Button>
      }
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Badge tone={failed ? "critical" : "accent"}>{review.status}</Badge>
        {review.outcome ? <Badge tone="neutral">{review.outcome}</Badge> : null}
        <Badge tone="neutral">{review.environment}</Badge>
        <Badge tone="neutral">{`v${review.review_version}`}</Badge>
        {review.compliance ? (
          <Badge tone={review.compliance === "COMPLIANT" ? "accent" : "neutral"}>
            {review.compliance}
          </Badge>
        ) : null}
      </div>

      {failed ? (
        <p className="mb-3 text-body text-critical">
          {review.error ?? "The review could not be produced."} The trade itself is unaffected —
          review generation never blocks or alters a trade.
        </p>
      ) : null}

      {review.summary ? <p className="mb-3 text-body">{review.summary}</p> : null}

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile
          label="Confidence"
          value={review.confidence === null ? undefined : `${(review.confidence * 100).toFixed(0)}%`}
          note="from data completeness"
        />
        <StatTile
          label="Data recorded"
          value={
            review.completeness
              ? `${(review.completeness.fraction * 100).toFixed(0)}%`
              : undefined
          }
          note={review.completeness ? `${review.completeness.missing.length} fields missing` : undefined}
        />
        <StatTile label="Review model" value={review.attribution.review_model ?? undefined} />
        <StatTile
          label="Prediction model"
          value={review.attribution.prediction_model ?? undefined}
          note={review.attribution.prediction_model_version ?? "none recorded"}
        />
      </div>

      <div className="mt-4 grid gap-3 md:grid-cols-2">
        {SECTION_LABELS.map(([key, label]) => (
          <SectionCard key={key} label={label} section={review[key] as ReviewSection | null} />
        ))}
      </div>

      {review.key_factors.length ? (
        <section className="mt-4">
          <h4 className="mb-1 text-mini uppercase tracking-wide text-muted">Key factors</h4>
          <ul>
            {review.key_factors.map((item, index) => (
              <EvidenceLine key={`kf-${index}`} item={item} />
            ))}
          </ul>
        </section>
      ) : null}

      {review.lessons.length ? (
        <section className="mt-4">
          <h4 className="mb-1 text-mini uppercase tracking-wide text-muted">Lessons</h4>
          <ul>
            {review.lessons.map((item, index) => (
              <EvidenceLine key={`ls-${index}`} item={item} />
            ))}
          </ul>
        </section>
      ) : null}

      {review.follow_up_questions.length ? (
        <section className="mt-4">
          <h4 className="mb-1 text-mini uppercase tracking-wide text-muted">
            Follow-up questions
          </h4>
          <ul className="space-y-0.5 text-body text-muted">
            {review.follow_up_questions.map((q) => (
              <li key={q}>· {q}</li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="mt-4 border-t border-line pt-2">
        <h4 className="mb-1 text-mini uppercase tracking-wide text-muted">Review metadata</h4>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-body md:grid-cols-3">
          <Meta label="Version" value={String(review.review_version)} />
          <Meta label="Schema" value={review.schema_version} />
          <Meta label="Prompt" value={review.prompt_version ?? "—"} />
          <Meta
            label="Review model"
            value={`${review.attribution.review_model ?? "—"} ${review.attribution.review_model_version ?? ""}`.trim()}
          />
          <Meta
            label="Prediction model"
            value={`${review.attribution.prediction_model ?? "—"} ${review.attribution.prediction_model_version ?? ""}`.trim()}
          />
          <Meta label="Generated" value={review.completed_at?.slice(0, 16).replace("T", " ") ?? "—"} />
        </dl>
        <p className="mt-2 text-body text-muted">
          The review model and the prediction model are different: one wrote this review, the
          other made the original trade prediction. UNKNOWN on a section means the platform did
          not record what was needed to assess it — not that the trade was poor.
        </p>
      </section>
    </Panel>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd className="font-mono">{value}</dd>
    </>
  );
}
