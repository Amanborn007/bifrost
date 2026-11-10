import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import mermaid from "mermaid";
import { Copy, Download, Loader2, Maximize2, X, ZoomIn, ZoomOut } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";

/**
 * <Mermaid> — renders ```mermaid fenced blocks as SVG, inline +
 * click-to-expand modal viewer.
 *
 * Every diagram across Bifrost (synthesis answers, HLD views, future
 * incident timelines) goes through this component so they share:
 *   - dark theme tuned to Bifrost chrome
 *   - automatic HLD/LLD label extraction from `%% HLD: title` header
 *   - click-to-expand modal with zoom, copy source, download SVG
 *   - Escape key to close modal
 *
 * See ``src/bifrost/research/diagram_standards.py`` for the generation
 * rules the backend follows.
 */

let _initialised = false;

function _initMermaid() {
  if (_initialised) return;
  _initialised = true;
  mermaid.initialize({
    startOnLoad: false,
    theme: "dark",
    securityLevel: "strict",
    fontFamily: "var(--font-mono), ui-monospace, monospace",
    themeVariables: {
      background: "transparent",
      primaryColor: "#1f2937",
      primaryTextColor: "#e5e7eb",
      primaryBorderColor: "#475569",
      lineColor: "#64748b",
      secondaryColor: "#111827",
      tertiaryColor: "#0b1220",
      actorBkg: "#1f2937",
      actorBorder: "#475569",
      actorTextColor: "#f1f5f9",
      noteBkgColor: "#0f172a",
      noteBorderColor: "#334155",
      noteTextColor: "#cbd5e1",
    },
    flowchart: { curve: "basis", htmlLabels: true, padding: 12 },
    sequence: {
      diagramMarginX: 24,
      diagramMarginY: 16,
      actorMargin: 60,
      messageFontSize: 12,
      noteFontSize: 11,
    },
  });
}

/** Defence in depth behind securityLevel "strict": the SVG is inserted
 * via dangerouslySetInnerHTML, so drop anything executable first. */
function _sanitizeSvg(svg: string): string {
  const doc = new DOMParser().parseFromString(svg, "text/html");
  const root = doc.body.querySelector("svg");
  if (!root) throw new Error("Mermaid output contained no <svg> element.");
  root
    .querySelectorAll("script, iframe, object, embed")
    .forEach((el) => el.remove());
  for (const el of [root, ...Array.from(root.querySelectorAll("*"))]) {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      const value = attr.value.replace(/[\s\p{Cc}]/gu, "").toLowerCase();
      if (name.startsWith("on") || value.startsWith("javascript:")) {
        el.removeAttribute(attr.name);
      }
    }
  }
  return root.outerHTML;
}

/** Parse the canonical `%% HLD: Title` / `%% LLD: Title` header that
 * our diagram_standards.py demands. Returns {type, title} or null. */
function _extractLabel(code: string): { type: string; title: string } | null {
  const m = code.match(/^\s*%%\s*(HLD|LLD)\s*:\s*(.+?)\s*$/m);
  if (!m) return null;
  return { type: m[1], title: m[2] };
}

/** Mermaid emits SVGs with fixed ``width="600"`` / ``height="400"``
 * (matching the natural diagram size) and a max-width style. Both
 * fight against fluid layout — inline figures stay tiny on wide
 * screens, and the modal's "Expand" button can't upscale past the
 * SVG's intrinsic size. Strip the size attrs + max-width style and
 * pin the viewBox so the consumer (figure / modal wrapper) controls
 * sizing via CSS. Returns the rewritten SVG string + the parsed
 * viewBox dimensions for the auto-fit math.
 */
function _normalizeSvg(svg: string): {
  svg: string;
  viewBoxW: number;
  viewBoxH: number;
} {
  let viewBoxW = 0;
  let viewBoxH = 0;
  // Pull viewBox first so we know the logical aspect ratio.
  const vbMatch = svg.match(
    /\bviewBox=["']([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)\s+([\d.-]+)["']/i,
  );
  if (vbMatch) {
    viewBoxW = parseFloat(vbMatch[3]) || 0;
    viewBoxH = parseFloat(vbMatch[4]) || 0;
  }
  // Drop fixed width/height so CSS controls size.
  let out = svg
    .replace(/<svg([^>]*?)\swidth=["'][^"']*["']/i, "<svg$1")
    .replace(/<svg([^>]*?)\sheight=["'][^"']*["']/i, "<svg$1");
  // Remove inline ``style="max-width: ..."`` injected by mermaid that
  // caps the SVG to its natural width on wide modals.
  out = out.replace(
    /(<svg[^>]*?\sstyle=["'])([^"']*)(["'])/i,
    (_match, p1, p2, p3) =>
      p1 +
      p2.replace(/max-width:\s*[^;"']+;?/gi, "").trim() +
      p3,
  );
  // Hint to the renderer: take 100% of the wrapper width, preserve
  // aspect ratio. ``height: auto`` only works when CSS is in charge.
  out = out.replace(
    /<svg(\s[^>]*?)?>/i,
    (full, attrs) => {
      const a = attrs ?? "";
      // Preserve any pre-existing style attribute we already cleaned.
      if (/\sstyle=/i.test(a)) {
        return full.replace(
          /(<svg[^>]*?\sstyle=["'])([^"']*)(["'])/i,
          (_m, p1, p2, p3) => p1 + p2 + ";display:block;width:100%;height:auto;" + p3,
        );
      }
      return `<svg${a} style="display:block;width:100%;height:auto">`;
    },
  );
  return { svg: out, viewBoxW, viewBoxH };
}

export function Mermaid({
  code,
  className,
}: {
  code: string;
  className?: string;
}) {
  const [svg, setSvg] = useState<string>("");
  const [vbW, setVbW] = useState(0);
  const [vbH, setVbH] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  // Lazy-render: defer the parse/render until the figure scrolls into
  // view. With ~10-20 mermaid diagrams in a long synthesis answer, the
  // initial render was a known UI freezer. IntersectionObserver hands
  // off rendering to whenever the user actually scrolls each diagram
  // close. ``onScreen`` flips true once and stays true.
  const figureRef = useRef<HTMLDivElement | null>(null);
  const [onScreen, setOnScreen] = useState(false);

  const label = _extractLabel(code);

  useEffect(() => {
    if (onScreen) return;
    const el = figureRef.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      // External-API fallback: no IntersectionObserver, so render eagerly.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setOnScreen(true);
      return;
    }
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setOnScreen(true);
          obs.disconnect();
        }
      },
      // Start rendering 200px before the figure enters the viewport so
      // it's painted by the time the user scrolls to it.
      { rootMargin: "200px 0px" },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [onScreen]);

  useEffect(() => {
    if (!onScreen) return;
    _initMermaid();
    let cancelled = false;
    const id = `mmd-${Math.random().toString(36).slice(2, 9)}`;
    // Pre-validate the source so a malformed diagram lands in our
    // tasteful collapsible error UI rather than mermaid's red-bomb
    // "Syntax error in text — mermaid version 11.14.0" SVG that
    // dominates the chat. ``suppressErrors: true`` makes parse return
    // ``false`` instead of throwing on invalid input.
    let parseError = "";
    try {
      const ok = (mermaid as unknown as {
        parse: (s: string, o?: { suppressErrors?: boolean }) => Promise<unknown> | unknown;
      }).parse(code, { suppressErrors: true });
      // mermaid 10+ may return a Promise; await it.
      Promise.resolve(ok)
        .then((res) => {
          if (cancelled) return;
          if (res === false) {
            parseError = "Mermaid couldn't parse this diagram.";
            setError(parseError);
            setSvg("");
            return;
          }
          mermaid
            .render(id, code)
            .then((out) => {
              if (cancelled) return;
              // Defensive: if mermaid still produced its bomb SVG
              // (parse passed but render failed gracefully), surface
              // our error UI instead of rendering the bomb.
              if (
                /aria-roledescription="error"|class="error-text"/.test(out.svg)
              ) {
                setError("Mermaid render produced an error diagram.");
                setSvg("");
                return;
              }
              const norm = _normalizeSvg(_sanitizeSvg(out.svg));
              setSvg(norm.svg);
              setVbW(norm.viewBoxW);
              setVbH(norm.viewBoxH);
              setError(null);
            })
            .catch((e) => {
              if (cancelled) return;
              setError(e instanceof Error ? e.message : String(e));
              setSvg("");
            });
        })
        .catch((e) => {
          if (cancelled) return;
          setError(e instanceof Error ? e.message : String(e));
          setSvg("");
        });
    } catch (e) {
      // External-system sync: mermaid.parse can throw synchronously on
      // malformed input; surface it in our error UI rather than crashing.
      parseError = e instanceof Error ? e.message : String(e);
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setError(parseError);
      setSvg("");
    }
    return () => {
      cancelled = true;
    };
  }, [code, onScreen]);

  if (error) {
    // Surface the failure inline by default — the previous
    // click-to-expand "details" UI made these failures invisible. The diagram label, parser
    // error, and source are all shown immediately.
    const labelLine = label ? `${label.type}: ${label.title}` : "Diagram";
    return (
      <figure
        ref={figureRef}
        className={cn(
          "my-4 overflow-hidden rounded-md border border-warn/50 bg-warn-soft/20",
          className,
        )}
      >
        <figcaption className="flex items-center gap-2 border-b border-warn/40 bg-warn-soft/40 px-3 py-1.5 text-[11.5px] text-warn">
          <span className="font-mono text-[10px] font-semibold uppercase tracking-wide">
            ⚠ Render failed
          </span>
          <span className="truncate text-fg-1">{labelLine}</span>
          <button
            type="button"
            onClick={() => navigator.clipboard.writeText(code).then(
              () => toast.success("Diagram source copied"),
              () => toast.error("Couldn't copy"),
            )}
            className="ml-auto inline-flex items-center gap-1 rounded px-2 py-0.5 text-[10.5px] text-fg-2 hover:bg-bg-2 hover:text-fg-0"
            title="Copy source so you can fix it locally"
          >
            <Copy className="h-3 w-3" strokeWidth={1.75} />
            Copy
          </button>
        </figcaption>
        <div className="space-y-1 px-3 py-2">
          <p className="text-[11px] text-warn">{error}</p>
          <pre className="max-h-48 overflow-auto rounded bg-bg-0 p-2 font-mono text-[10.5px] leading-snug text-fg-1">
            {code}
          </pre>
        </div>
      </figure>
    );
  }

  // Placeholder while we wait for the IntersectionObserver to mount
  // the diagram OR for mermaid to finish rendering. The min-height
  // is generous enough to keep the page-scroll position stable when
  // the SVG eventually pops in (no jank shift on viewport entry).
  if (!onScreen || !svg) {
    return (
      <div
        ref={figureRef}
        className={cn(
          "my-4 flex min-h-[140px] items-center justify-center rounded-lg border border-border bg-bg-0 text-[11.5px] text-fg-3",
          className,
        )}
      >
        <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" strokeWidth={1.75} />
        Rendering diagram…
      </div>
    );
  }

  return (
    <>
      <figure
        ref={figureRef}
        className={cn(
          "group relative my-4 overflow-hidden rounded-lg border border-border bg-bg-0",
          className,
        )}
      >
        {label && (
          <figcaption className="flex items-center gap-2 border-b border-border bg-bg-1 px-3 py-1.5 text-[11px]">
            <span
              className={cn(
                "inline-flex items-center rounded px-1.5 py-0.5 font-mono text-[9.5px] font-semibold uppercase tracking-wide",
                label.type === "HLD"
                  ? "bg-info-soft text-info"
                  : "bg-accent-soft text-accent",
              )}
            >
              {label.type}
            </span>
            <span className="truncate text-fg-1">{label.title}</span>
            <button
              onClick={() => setOpen(true)}
              className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10.5px] text-fg-3 opacity-0 transition-opacity hover:bg-bg-2 hover:text-fg-0 group-hover:opacity-100"
              title="Open in full screen"
            >
              <Maximize2 className="h-3 w-3" strokeWidth={1.75} />
              Expand
            </button>
          </figcaption>
        )}
        <button
          onClick={() => setOpen(true)}
          className={cn(
            // Cap inline diagrams at a comfortable reading height —
            // a tall sequence diagram shouldn't take over the chat.
            // Click expands to full screen for the rest.
            "block w-full cursor-zoom-in p-4 text-left",
            "[&>svg]:max-h-[520px] [&>svg]:mx-auto",
          )}
          aria-label="Open diagram in full screen"
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </figure>

      {open && (
        <DiagramModal
          svg={svg}
          viewBoxW={vbW}
          viewBoxH={vbH}
          code={code}
          label={label}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}

/* ─── Full-screen modal with pan/zoom + copy/download ─────── */

function DiagramModal({
  svg,
  viewBoxW,
  viewBoxH,
  code,
  label,
  onClose,
}: {
  svg: string;
  /** Logical width from the SVG's viewBox — the size the diagram
   * was authored at. Used for auto-fit math without measuring. */
  viewBoxW: number;
  viewBoxH: number;
  code: string;
  label: { type: string; title: string } | null;
  onClose: () => void;
}) {
  const [zoom, setZoom] = useState(1);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const svgWrapperRef = useRef<HTMLDivElement | null>(null);
  const [didAutoFit, setDidAutoFit] = useState(false);
  // Remember the auto-fit zoom so the "Reset" button (clicking the
  // percentage badge / pressing 0) restores to "fit", not 100%.
  const autoFitZoomRef = useRef<number>(1);

  // Auto-fit on first paint. The math used to clamp at 1.0 ("don't
  // upscale tiny diagrams"), which made small SVGs look postage-
  // stamp-sized in a giant black canvas. SVGs are vectors — they
  // upscale crisply — so allow up to 3x. The user can still pinch/
  // wheel-zoom to anything between 0.2 and 4.0.
  //
  // We prefer the parsed viewBox dimensions (passed in) over
  // getBBox() because getBBox() returned weird values on
  // foreignObject-heavy mermaid output. Fall back to getBBox /
  // clientWidth if viewBox parsing failed upstream.
  useLayoutEffect(() => {
    if (didAutoFit) return;
    const container = containerRef.current;
    const wrapper = svgWrapperRef.current;
    if (!container || !wrapper) return;

    let svgWidth = viewBoxW;
    let svgHeight = viewBoxH;
    if (!svgWidth || !svgHeight) {
      const svgEl = wrapper.querySelector("svg");
      if (!svgEl) return;
      svgWidth =
        (svgEl as SVGSVGElement).viewBox?.baseVal?.width
        || (svgEl as SVGGraphicsElement).getBBox?.().width
        || svgEl.clientWidth
        || 0;
      svgHeight =
        (svgEl as SVGSVGElement).viewBox?.baseVal?.height
        || (svgEl as SVGGraphicsElement).getBBox?.().height
        || svgEl.clientHeight
        || 0;
    }
    if (!svgWidth || !svgHeight) return;

    const cw = container.clientWidth - 64;   // 32px padding each side
    const ch = container.clientHeight - 64;
    if (cw <= 0 || ch <= 0) return;

    const fit = Math.min(cw / svgWidth, ch / svgHeight);
    // Allow upscale to 3x for tiny diagrams (vector-crisp), and
    // clamp downscale at 0.2 so giant diagrams remain navigable.
    const next = Math.min(Math.max(fit, 0.2), 3);
    autoFitZoomRef.current = next;
    setZoom(next);
    setDidAutoFit(true);
  }, [didAutoFit, svg, viewBoxW, viewBoxH]);

  // Escape to close + +/-/0 zoom shortcuts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "+" || (e.key === "=" && e.shiftKey))
        setZoom((z) => Math.min(z + 0.2, 4));
      else if (e.key === "-" || e.key === "_")
        setZoom((z) => Math.max(z - 0.2, 0.2));
      else if (e.key === "0") setZoom(autoFitZoomRef.current);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Wheel zoom (Ctrl/Cmd+wheel only — regular wheel scrolls)
  const onWheel = useCallback((e: React.WheelEvent) => {
    if (!(e.metaKey || e.ctrlKey)) return;
    e.preventDefault();
    setZoom((z) => {
      const next = z - e.deltaY * 0.002;
      return Math.min(Math.max(next, 0.2), 4);
    });
  }, []);

  const copySource = async () => {
    await navigator.clipboard.writeText(code);
    toast.success("Diagram source copied");
  };

  const downloadSvg = () => {
    const blob = new Blob([svg], { type: "image/svg+xml" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${(label?.title || "diagram").replace(/\s+/g, "-").toLowerCase()}.svg`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-bg-0/95 backdrop-blur-sm"
      onClick={(e) => {
        // Click on backdrop (not on controls or svg) closes
        if (e.target === e.currentTarget) onClose();
      }}
    >
      {/* Header bar */}
      <header className="flex shrink-0 items-center gap-3 border-b border-border bg-bg-1 px-4 py-2">
        {label && (
          <>
            <span
              className={cn(
                "inline-flex items-center rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wide",
                label.type === "HLD"
                  ? "bg-info-soft text-info"
                  : "bg-accent-soft text-accent",
              )}
            >
              {label.type}
            </span>
            <span className="text-[13px] font-semibold text-fg-0">
              {label.title}
            </span>
          </>
        )}

        <div className="ml-auto flex items-center gap-1">
          <button
            onClick={() => setZoom((z) => Math.max(z - 0.2, 0.2))}
            className="rounded p-1.5 text-fg-2 hover:bg-bg-2 hover:text-fg-0"
            title="Zoom out (−)"
            aria-label="Zoom out"
          >
            <ZoomOut className="h-3.5 w-3.5" strokeWidth={1.75} />
          </button>
          <button
            onClick={() => setZoom(autoFitZoomRef.current)}
            className="min-w-[56px] rounded px-2 py-1 text-center font-mono text-[11.5px] text-fg-2 hover:bg-bg-2 hover:text-fg-0"
            title="Reset to fit-to-screen (0)"
          >
            {Math.round(zoom * 100)}%
          </button>
          <button
            onClick={() => setZoom((z) => Math.min(z + 0.2, 4))}
            className="rounded p-1.5 text-fg-2 hover:bg-bg-2 hover:text-fg-0"
            title="Zoom in (+)"
            aria-label="Zoom in"
          >
            <ZoomIn className="h-3.5 w-3.5" strokeWidth={1.75} />
          </button>
          <div className="mx-1 h-5 w-px bg-border" />
          <button
            onClick={copySource}
            className="inline-flex items-center gap-1 rounded px-2 py-1 text-[11.5px] text-fg-2 hover:bg-bg-2 hover:text-fg-0"
            title="Copy source"
          >
            <Copy className="h-3 w-3" strokeWidth={1.75} />
            Source
          </button>
          <button
            onClick={downloadSvg}
            className="inline-flex items-center gap-1 rounded px-2 py-1 text-[11.5px] text-fg-2 hover:bg-bg-2 hover:text-fg-0"
            title="Download SVG"
          >
            <Download className="h-3 w-3" strokeWidth={1.75} />
            SVG
          </button>
          <div className="mx-1 h-5 w-px bg-border" />
          <button
            onClick={onClose}
            className="rounded p-1.5 text-fg-2 hover:bg-bg-2 hover:text-fg-0"
            title="Close (Esc)"
            aria-label="Close"
          >
            <X className="h-4 w-4" strokeWidth={1.75} />
          </button>
        </div>
      </header>

      {/* Scrollable, zoomable diagram area.
          We size the SVG via an explicit ``width`` (in CSS px) on the
          wrapper instead of ``transform: scale()`` — that way the
          scrollable container sees the real dimensions of the upscaled
          diagram, scrollbars appear when needed, and there's no jump
          on zoom transitions. The SVG itself is normalized to
          ``width:100%; height:auto`` so this px width fully drives
          rendered size while preserving aspect ratio. */}
      <div
        ref={containerRef}
        className="min-h-0 flex-1 overflow-auto"
        onWheel={onWheel}
      >
        <div
          ref={svgWrapperRef}
          className="mx-auto p-8"
          style={{
            // Logical viewBox width × zoom = px width to render at.
            // Falls back to a reasonable default if viewBox missing.
            width: `${(viewBoxW || 800) * zoom}px`,
            maxWidth: "100%",
            transition: didAutoFit ? "width 80ms ease-out" : "none",
          }}
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      </div>

      {/* Footer hint */}
      <footer className="flex shrink-0 items-center justify-center gap-4 border-t border-border bg-bg-1 px-4 py-1.5 text-[10.5px] text-fg-3">
        <span>
          <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
            Esc
          </kbd>{" "}
          close
        </span>
        <span>
          <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
            +
          </kbd>{" "}
          /{" "}
          <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
            −
          </kbd>{" "}
          zoom
        </span>
        <span>
          <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
            0
          </kbd>{" "}
          fit-to-screen
        </span>
        <span>
          <kbd className="rounded border border-border bg-bg-2 px-1 font-mono text-[10px]">
            ⌘/Ctrl + wheel
          </kbd>{" "}
          zoom
        </span>
      </footer>
    </div>
  );
}
