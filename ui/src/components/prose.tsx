import { memo, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { cn } from "@/lib/utils";
import { Mermaid } from "@/components/mermaid";

/**
 * Split markdown at a top-level `## Sources` / `### Sources` heading.
 * Everything from that heading onwards is rendered in a collapsible
 * <details> below the main answer.
 */
function _splitOnSources(md: string): { main: string; sources: string | null } {
  const re = /\n(##{1,2}\s+(?:Sources|Footnotes|Citations)[^\n]*)\n/i;
  const m = md.match(re);
  if (!m || m.index === undefined) return { main: md, sources: null };
  const idx = m.index;
  const main = md.slice(0, idx);
  const sources = md.slice(idx + 1);
  return { main, sources };
}

/**
 * Pull the leading TL;DR section out so we can render it as a hero
 * lead block. Returns the TL;DR body (no heading) and the rest of
 * the markdown. If absent, ``tldr = null`` and ``rest = md``.
 */
function _extractTLDR(md: string): { tldr: string | null; rest: string } {
  const m = md.match(
    /^(#{2,3})\s+(?:TL;?DR|Summary)\s*\n([\s\S]*?)(?=\n#{2,3}\s|\n\n#{2,3}\s|$)/i,
  );
  if (!m) return { tldr: null, rest: md };
  const tldr = m[2].trim();
  const before = md.slice(0, m.index ?? 0);
  const after = md.slice((m.index ?? 0) + m[0].length);
  const rest = (before + after).replace(/^\s*\n/, "");
  return { tldr, rest };
}

/**
 * Prose — clean editorial typography for synthesis answers.
 *
 * Design principle: maximize reading comfort, minimize decoration.
 * Big bright body text, calm inline code, clear h2/h3 hierarchy by
 * SIZE not color, generous vertical rhythm. Nothing fights the
 * content.
 *
 * Tuning (the "highlight stripe under bold" + accent dots felt
 * gimmicky):
 *   - Body 17px / 1.78 — large enough to read across rooms.
 *   - Bold is just font-weight 700; same color as body. Hierarchy
 *     comes from weight, not background fills.
 *   - Headings carry size jumps: h1 24px, h2 21px, h3 18px, h4 13px
 *     uppercase. The eye finds them by scale alone.
 *   - TL;DR rendered as a quiet callout (3px accent left bar +
 *     17.5px body text) — distinct without being loud.
 *   - Inline `code` uses a subtle bg only (no border).
 *   - Citations [^N] = small accent superscript pill.
 *   - Tables = clean lines + zebra hover, no card wrapper.
 *   - No bg around answers in chat — let the chat surface breathe.
 *   - Reading column ~70ch.
 */
export const Prose = memo(_ProseImpl);

function _ProseImpl({
  children,
  className,
}: {
  children: string;
  className?: string;
}) {
  const { main, sources } = _splitOnSources(children);
  const { tldr, rest } = _extractTLDR(main);

  const renderCode = (props: ComponentPropsWithoutRef<"code">) => {
    const { className, children, ...rest } = props;
    const match = /language-(\w+)/.exec(className || "");
    const content = String(children).replace(/\n$/, "");
    if (!match && !content.includes("\n")) {
      return (
        <code className={className} {...rest}>
          {children}
        </code>
      );
    }
    if (match?.[1] === "mermaid") {
      return <Mermaid code={content} />;
    }
    return (
      <SyntaxHighlighter
        language={match?.[1] ?? "text"}
        style={oneDark}
        PreTag="div"
        customStyle={{
          margin: "1.25rem 0",
          padding: "1rem 1.125rem",
          fontSize: "13.5px",
          lineHeight: "1.62",
          background: "var(--bg-1)",
          border: "1px solid var(--border)",
          borderRadius: "0.625rem",
        }}
        codeTagProps={{ style: { fontFamily: "var(--font-mono)" } }}
      >
        {content}
      </SyntaxHighlighter>
    );
  };

  const renderSection = (props: ComponentPropsWithoutRef<"section">) => {
    const { className: secClass, children: secChildren } = props;
    if (secClass === "footnotes") {
      return (
        <details className="prose-footnote-details">
          <summary>Sources &amp; footnotes</summary>
          <section className={secClass}>{secChildren}</section>
        </details>
      );
    }
    return <section className={secClass}>{secChildren}</section>;
  };

  return (
    <div className={cn("prose-bifrost", className)}>
      <style>{`
        /* ── Base ──────────────────────────────────────────────── */
        .prose-bifrost {
          font-size: 17px;
          line-height: 1.78;
          color: var(--fg-0);
          font-weight: 400;
          letter-spacing: 0;
          word-spacing: 0.005em;
          font-feature-settings: "ss01", "cv11";
        }
        .prose-bifrost > :first-child { margin-top: 0; }
        .prose-bifrost > :last-child { margin-bottom: 0; }

        /* ── TL;DR lead ────────────────────────────────────────── */
        .prose-bifrost .tldr-hero {
          margin: 0 0 2rem 0;
          padding: 0 0 0 1.125rem;
          border-left: 3px solid var(--accent);
        }
        .prose-bifrost .tldr-hero .tldr-label {
          display: inline-block;
          font-family: var(--font-mono);
          font-size: 11px;
          font-weight: 700;
          letter-spacing: 0.16em;
          text-transform: uppercase;
          color: var(--accent);
          margin-bottom: 0.5rem;
        }
        .prose-bifrost .tldr-hero p {
          font-size: 17.5px;
          line-height: 1.7;
          color: var(--fg-0);
          margin: 0.5rem 0;
        }
        .prose-bifrost .tldr-hero p:first-of-type { margin-top: 0; }
        .prose-bifrost .tldr-hero p:last-of-type { margin-bottom: 0; }
        .prose-bifrost .tldr-hero strong { font-weight: 700; color: var(--fg-0); }

        /* ── Headings — pure size hierarchy ───────────────────── */
        .prose-bifrost h1 {
          font-size: 26px; font-weight: 700; letter-spacing: -0.015em;
          line-height: 1.25; margin: 2.5rem 0 1rem; color: var(--fg-0);
        }
        .prose-bifrost h2 {
          font-size: 22px; font-weight: 700; letter-spacing: -0.012em;
          line-height: 1.3; margin: 2.75rem 0 1rem; color: var(--fg-0);
          padding-bottom: 0.45rem;
          border-bottom: 1px solid var(--border);
        }
        .prose-bifrost h3 {
          font-size: 19px; font-weight: 700; letter-spacing: -0.008em;
          line-height: 1.35; margin: 2.25rem 0 0.875rem; color: var(--fg-0);
        }
        .prose-bifrost h4 {
          font-size: 12px; font-weight: 700;
          text-transform: uppercase; letter-spacing: 0.1em;
          color: var(--fg-2);
          margin: 1.75rem 0 0.625rem;
        }

        /* ── Paragraphs ────────────────────────────────────────── */
        .prose-bifrost p {
          margin: 1.125rem 0;
          color: var(--fg-0);
        }
        .prose-bifrost h2 + p,
        .prose-bifrost h3 + p {
          /* Section openers stay at body size — hierarchy is in the
             heading, not the lead. Keep prose flow even. */
          margin-top: 0.875rem;
        }

        /* ── Emphasis — pure weight, same color ────────────────── */
        .prose-bifrost strong {
          font-weight: 700;
          color: var(--fg-0);
        }
        .prose-bifrost em {
          font-style: italic;
          color: var(--fg-0);
        }

        /* ── Inline code — subtle, no border ───────────────────── */
        .prose-bifrost code {
          font-family: var(--font-mono);
          font-size: 0.86em;
          background: color-mix(in oklch, var(--bg-2) 65%, transparent);
          color: var(--fg-0);
          padding: 0.1rem 0.38rem;
          border-radius: 0.3rem;
          font-weight: 500;
        }

        /* ── Lists ─────────────────────────────────────────────── */
        .prose-bifrost ul,
        .prose-bifrost ol {
          margin: 1.125rem 0;
          padding-left: 1.625rem;
        }
        .prose-bifrost ul { list-style: disc; }
        .prose-bifrost ol { list-style: decimal; }
        .prose-bifrost li {
          margin: 0.55rem 0;
          color: var(--fg-0);
          padding-left: 0.25rem;
        }
        .prose-bifrost li::marker { color: var(--fg-3); }
        .prose-bifrost li > p { margin: 0.4rem 0; }
        .prose-bifrost ul ul,
        .prose-bifrost ol ol,
        .prose-bifrost ul ol,
        .prose-bifrost ol ul { margin: 0.4rem 0 0.4rem; }

        /* ── Blockquote = pull quote ───────────────────────────── */
        .prose-bifrost blockquote {
          margin: 1.5rem 0;
          padding: 0.25rem 0 0.25rem 1.25rem;
          border-left: 3px solid var(--accent);
          font-size: 17px;
          line-height: 1.65;
          color: var(--fg-0);
          font-style: normal;
        }
        .prose-bifrost blockquote p { margin: 0.5rem 0; }
        .prose-bifrost blockquote em { color: var(--fg-1); font-style: italic; }

        /* ── Tables ────────────────────────────────────────────── */
        .prose-bifrost table {
          margin: 1.75rem 0;
          width: 100%;
          border-collapse: collapse;
          font-size: 14px;
          line-height: 1.55;
        }
        .prose-bifrost th {
          padding: 0.6rem 0.75rem;
          text-align: left;
          font-size: 11px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          color: var(--fg-2);
          border-bottom: 2px solid var(--border);
        }
        .prose-bifrost td {
          padding: 0.7rem 0.75rem;
          border-bottom: 1px solid color-mix(in oklch, var(--border) 50%, transparent);
          color: var(--fg-0);
          vertical-align: top;
        }
        .prose-bifrost tbody tr:last-child td { border-bottom: 0; }
        .prose-bifrost tbody tr:hover {
          background: color-mix(in oklch, var(--bg-2) 25%, transparent);
        }

        /* ── HR ────────────────────────────────────────────────── */
        .prose-bifrost hr {
          margin: 2.25rem 0;
          border: 0;
          border-top: 1px solid var(--border);
        }

        /* ── Links ─────────────────────────────────────────────── */
        .prose-bifrost a {
          color: var(--accent);
          text-decoration: underline;
          text-decoration-thickness: 1px;
          text-decoration-color: color-mix(in oklch, var(--accent) 35%, transparent);
          text-underline-offset: 3px;
        }
        .prose-bifrost a:hover {
          text-decoration-color: var(--accent);
        }

        /* ── Citation markers [^N] ─────────────────────────────── */
        .prose-bifrost sup a {
          font-family: var(--font-mono);
          font-size: 10px;
          font-weight: 600;
          color: var(--accent);
          text-decoration: none;
          padding: 0.1rem 0.3rem;
          margin: 0 0.05rem;
          border-radius: 3px;
          background: color-mix(in oklch, var(--accent-soft) 60%, transparent);
          line-height: 1;
          vertical-align: super;
        }
        .prose-bifrost sup a:hover {
          background: var(--accent-soft);
        }

        /* ── Footnotes section (collapsible) ───────────────────── */
        .prose-bifrost .prose-footnote-details {
          margin-top: 2.25rem;
          padding-top: 1.25rem;
          border-top: 1px solid var(--border);
        }
        .prose-bifrost .prose-footnote-details > summary {
          cursor: pointer;
          font-size: 11.5px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.1em;
          color: var(--fg-2);
          padding: 0.25rem 0 0.25rem 1.125rem;
          position: relative;
          list-style: none;
        }
        .prose-bifrost .prose-footnote-details > summary::-webkit-details-marker { display: none; }
        .prose-bifrost .prose-footnote-details > summary::before {
          content: '▸';
          position: absolute;
          left: 0; top: 50%;
          transform: translateY(-50%);
          color: var(--fg-3);
          transition: transform 120ms ease;
          font-size: 11px;
        }
        .prose-bifrost .prose-footnote-details[open] > summary::before {
          transform: translateY(-50%) rotate(90deg);
        }
        .prose-bifrost .prose-footnote-details > summary:hover { color: var(--fg-0); }
        .prose-bifrost .footnotes ol {
          margin: 0.875rem 0 0;
          padding-left: 1.75rem;
          font-size: 13.5px;
          line-height: 1.65;
          color: var(--fg-1);
        }
        .prose-bifrost .footnotes li { margin: 0.5rem 0; }
        .prose-bifrost .footnotes li p { margin: 0.25rem 0; }
        .prose-bifrost .data-footnote-backref {
          color: var(--fg-3);
          margin-left: 0.4rem;
          text-decoration: none;
        }
      `}</style>

      {tldr && (
        <aside className="tldr-hero" aria-label="Summary">
          <div className="tldr-label">TL;DR</div>
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{ code: renderCode, section: renderSection }}
          >
            {tldr}
          </ReactMarkdown>
        </aside>
      )}

      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{ code: renderCode, section: renderSection }}
      >
        {rest}
      </ReactMarkdown>

      {sources && (
        <details className="prose-footnote-details">
          <summary>Sources &amp; footnotes</summary>
          <div style={{ paddingTop: "0.75rem" }}>
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{ code: renderCode, section: renderSection }}
            >
              {sources}
            </ReactMarkdown>
          </div>
        </details>
      )}
    </div>
  );
}
