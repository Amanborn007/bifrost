/**
 * Collaboration affordances on handover docs.
 *
 * A "ask about this handover" entry point that lets a reader pivot
 * from passive reading into an active clarifier session pre-scoped
 * to the doc (and optionally a specific section). The downstream
 * /collaboration/new page reads ``from_handover`` + ``section`` from
 * the query string and seeds the requirement form with the right
 * matched_doc_ids so the clarifier doesn't re-discover scope.
 */

import { MessageCirclePlus, MessageSquare } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { cn } from "@/lib/utils";

interface HandoverAskButtonProps {
  handoverId: string;
  // Carried through purely for the eventual title hint + analytics
  // breadcrumb the new page can render. The actual scope binding
  // happens server-side from ``from_handover``.
  scopeKind: string;
  scopeValue: string;
  className?: string;
}

/**
 * Primary "Ask about this handover" CTA for the detail page header
 * row. Match the existing primary-action styling used elsewhere on
 * the handover detail page (accent fill, small icon + label).
 */
export function HandoverAskButton({
  handoverId,
  scopeKind,
  scopeValue,
  className,
}: HandoverAskButtonProps) {
  const navigate = useNavigate();
  const onClick = () => {
    navigate(`/collaboration/new?from_handover=${encodeURIComponent(handoverId)}`);
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11.5px] font-medium",
        "bg-accent text-accent-fg hover:bg-accent-hover",
        className,
      )}
      title={`Open a collaboration session pre-scoped to this handover (${scopeKind} · ${scopeValue})`}
      aria-label={`Ask about this handover for ${scopeValue}`}
    >
      <MessageCirclePlus className="h-3.5 w-3.5" strokeWidth={1.75} />
      Ask about this handover
    </button>
  );
}

interface AskAboutSectionButtonProps {
  handoverId: string;
  sectionKey: string;
  sectionTitle: string;
  className?: string;
}

/**
 * Per-section ghost button. Sits inline with a section header so the
 * reader can ask "I have a question about *this* section specifically"
 * without scrolling back to the page header.
 */
export function AskAboutSectionButton({
  handoverId,
  sectionKey,
  sectionTitle,
  className,
}: AskAboutSectionButtonProps) {
  const navigate = useNavigate();
  const onClick = () => {
    const qs = new URLSearchParams({
      from_handover: handoverId,
      section: sectionKey,
    });
    navigate(`/collaboration/new?${qs.toString()}`);
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-1.5 py-0.5 text-[10.5px] font-medium text-fg-2",
        "hover:bg-bg-2 hover:text-fg-0",
        className,
      )}
      title={`Open a collaboration session about the "${sectionTitle}" section`}
      aria-label={`Ask about the ${sectionTitle} section`}
    >
      <MessageSquare className="h-3 w-3" strokeWidth={1.75} />
      Ask about this section
    </button>
  );
}
