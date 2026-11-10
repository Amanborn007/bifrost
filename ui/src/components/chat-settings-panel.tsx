/**
 * Chat settings panel for the blast-radius research flow.
 *
 * Renders inside the chat sidebar as a collapsed disclosure (gear icon
 * + "Settings"). On expand, exposes the per-conversation knobs that
 * drive the blast-radius research flow:
 *
 *   - walk_depth (slider 1-6)
 *   - walk_breadth_soft_cap (slider 20-2000, log-ish marks)
 *   - confidence_floor (slider 0-1, with the T1-T5 cutpoints labeled)
 *   - edge_directions (segmented: upstream / downstream / both)
 *   - edge_types (multi-checkbox)
 *   - code_first_strict (toggle)
 *   - agents_per_repo_max (slider 1-8)
 *   - external_sources (multi-checkbox)
 *   - auto_extract_missing (toggle) + auto_extract_wait_s (slider)
 *   - research_full_depth (toggle)
 *   - research_include_slack (toggle)
 *   - research_include_github (toggle)
 *   - research_include_confluence (toggle)
 *
 * Changes are debounced and PATCHed; reset-to-defaults sends the full
 * factory defaults object to clear all overrides.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  RefreshCcw,
  Settings as SettingsIcon,
} from "lucide-react";
import { toast } from "sonner";
import { cn, describeError } from "@/lib/utils";
import {
  getChatSettings,
  updateChatSettings,
  type ChatSettings,
  type PartialChatSettings,
} from "@/lib/api";

const FACTORY_DEFAULTS: ChatSettings = {
  walk_depth: 2,
  walk_breadth_soft_cap: 200,
  confidence_floor: 0.55,
  edge_directions: "both",
  edge_types: [
    "calls",
    "depends_on",
    "owns",
    "produces_event",
    "consumes_event",
    "implements",
    "extends",
    "references",
    "imports",
    "calls_service",
    "publishes_to",
    "shares_topic",
  ],
  code_first_strict: true,
  agents_per_repo_max: 1,
  external_sources: ["jira", "confluence", "github", "slack", "gdocs"],
  auto_extract_missing: true,
  auto_extract_wait_s: 90.0,
  research_full_depth: false,
  research_include_slack: false,
  research_include_github: false,
  research_include_confluence: false,
};

const ALL_EDGE_TYPES = FACTORY_DEFAULTS.edge_types;
const ALL_EXTERNAL_SOURCES = FACTORY_DEFAULTS.external_sources;

const DEBOUNCE_MS = 350;

export function ChatSettingsPanel({ convId }: { convId: string | null }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);

  const settings = useQuery({
    queryKey: ["chat-settings", convId],
    queryFn: () => getChatSettings(convId!),
    enabled: !!convId && open,
    staleTime: 30_000,
  });

  // Local draft mirrors the fetched value so sliders are smooth (no
  // network roundtrip per pixel). Debounced PATCH flushes the diff.
  const [draft, setDraft] = useState<ChatSettings | null>(null);
  useEffect(() => {
    if (settings.data) setDraft(settings.data);
  }, [settings.data]);

  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastSentRef = useRef<ChatSettings | null>(null);

  const flushPatch = useMemo(
    () =>
      (next: ChatSettings) => {
        if (!convId) return;
        const last = lastSentRef.current ?? settings.data;
        if (!last) return;
        const patch: PartialChatSettings = {};
        // Only send fields that actually changed.
        for (const k of Object.keys(next) as Array<keyof ChatSettings>) {
          const a = JSON.stringify((next as Record<string, unknown>)[k as string]);
          const b = JSON.stringify((last as Record<string, unknown>)[k as string]);
          if (a !== b) {
            (patch as Record<string, unknown>)[k as string] =
              (next as Record<string, unknown>)[k as string];
          }
        }
        if (Object.keys(patch).length === 0) return;
        lastSentRef.current = next;
        updateChatSettings(convId, patch)
          .then((server) => {
            // Server normalised value (e.g., dropped unknown edge types) — sync.
            qc.setQueryData(["chat-settings", convId], server);
            setDraft(server);
            lastSentRef.current = server;
            // Invalidate the conversation detail so the next research turn
            // picks up the updated settings from the server.
            void qc.invalidateQueries({ queryKey: ["conversation", convId] });
          })
          .catch((err) => {
            toast.error(`Settings save failed: ${describeError(err)}`);
          });
      },
    [convId, qc, settings.data],
  );

  const updateField = <K extends keyof ChatSettings>(
    field: K,
    value: ChatSettings[K],
  ) => {
    setDraft((prev) => {
      const base = prev ?? settings.data ?? FACTORY_DEFAULTS;
      const next = { ...base, [field]: value };
      // Debounce the network call.
      if (flushTimerRef.current) clearTimeout(flushTimerRef.current);
      flushTimerRef.current = setTimeout(() => flushPatch(next), DEBOUNCE_MS);
      return next;
    });
  };

  const resetDefaults = async () => {
    if (!convId) return;
    if (flushTimerRef.current) clearTimeout(flushTimerRef.current);
    try {
      const server = await updateChatSettings(convId, FACTORY_DEFAULTS as PartialChatSettings);
      qc.setQueryData(["chat-settings", convId], server);
      setDraft(server);
      lastSentRef.current = server;
      void qc.invalidateQueries({ queryKey: ["conversation", convId] });
      toast.success("Settings reset to defaults");
    } catch (err) {
      toast.error(`Reset failed: ${describeError(err)}`);
    }
  };

  if (!convId) return null;

  const value = draft ?? settings.data ?? FACTORY_DEFAULTS;

  return (
    <div className="border-t border-border px-3 py-2">
      <button
        onClick={() => setOpen((s) => !s)}
        className="flex w-full items-center justify-between rounded px-2 py-1.5 text-[12px] text-fg-2 transition-colors hover:bg-bg-2 hover:text-fg-0"
      >
        <span className="inline-flex items-center gap-1.5">
          <SettingsIcon className="h-3 w-3" strokeWidth={1.75} />
          Research settings
        </span>
        <ChevronDown
          className={cn(
            "h-3 w-3 transition-transform",
            open && "rotate-180",
          )}
          strokeWidth={1.75}
        />
      </button>
      {open && (
        <div className="mt-2 space-y-3 rounded-md border border-border bg-bg-2 px-3 py-3">
          {settings.isLoading && !draft ? (
            <p className="text-[11px] text-fg-3">Loading…</p>
          ) : (
            <>
              {/* walk_depth */}
              <Slider
                label="Walk depth"
                hint="hops out from seed entities"
                min={1}
                max={6}
                step={1}
                value={value.walk_depth}
                onChange={(v) => updateField("walk_depth", v)}
              />
              {/* walk_breadth_soft_cap */}
              <Slider
                label="Walk breadth (soft cap)"
                hint="warns above this; doesn't truncate"
                min={20}
                max={2000}
                step={10}
                value={value.walk_breadth_soft_cap}
                onChange={(v) => updateField("walk_breadth_soft_cap", v)}
              />
              {/* confidence_floor */}
              <Slider
                label="Confidence floor"
                hint={`${tierLabel(value.confidence_floor)}`}
                min={0}
                max={1}
                step={0.05}
                value={value.confidence_floor}
                onChange={(v) =>
                  updateField("confidence_floor", Math.round(v * 100) / 100)
                }
              />
              {/* edge_directions */}
              <Field label="Walk direction">
                <Segmented
                  options={[
                    { label: "Upstream", value: "upstream" },
                    { label: "Downstream", value: "downstream" },
                    { label: "Both", value: "both" },
                  ]}
                  value={value.edge_directions}
                  onChange={(v) =>
                    updateField(
                      "edge_directions",
                      v as ChatSettings["edge_directions"],
                    )
                  }
                />
              </Field>
              {/* edge_types */}
              <Field label="Edge types to follow">
                <CheckList
                  options={ALL_EDGE_TYPES}
                  value={value.edge_types}
                  onChange={(v) => updateField("edge_types", v)}
                />
              </Field>
              {/* external_sources */}
              <Field label="External sources">
                <CheckList
                  options={ALL_EXTERNAL_SOURCES}
                  value={value.external_sources as string[]}
                  onChange={(v) =>
                    updateField(
                      "external_sources",
                      v as ChatSettings["external_sources"],
                    )
                  }
                />
              </Field>
              {/* code_first_strict */}
              <Toggle
                label="Code-first strict"
                hint="hedge claims not backed by code"
                value={value.code_first_strict}
                onChange={(v) => updateField("code_first_strict", v)}
              />
              {/* agents_per_repo_max */}
              <Slider
                label="Code agents per repo (max)"
                hint=">1 shards large repos across parallel agents"
                min={1}
                max={8}
                step={1}
                value={value.agents_per_repo_max}
                onChange={(v) => updateField("agents_per_repo_max", v)}
              />
              {/* auto_extract_missing */}
              <Toggle
                label="Auto-extract missing repos"
                hint="trigger extraction when blast radius surfaces manifest_only repos"
                value={value.auto_extract_missing}
                onChange={(v) => updateField("auto_extract_missing", v)}
              />
              {/* auto_extract_wait_s */}
              <Slider
                label="Auto-extract wait (s)"
                hint="how long to block answer for fresh extractions"
                min={0}
                max={300}
                step={10}
                value={value.auto_extract_wait_s}
                onChange={(v) => updateField("auto_extract_wait_s", v)}
              />
              {/* research_full_depth */}
              <Toggle
                label="Full in-depth research"
                hint="Research every found repo at full depth (slower, more complete)"
                value={value.research_full_depth}
                onChange={(v) => updateField("research_full_depth", v)}
              />
              {/* research_include_slack */}
              <Toggle
                label="Include Slack"
                hint="Search Slack channel history (adds latency, rarely cited)"
                value={value.research_include_slack}
                onChange={(v) => updateField("research_include_slack", v)}
              />
              {/* research_include_github */}
              <Toggle
                label="Include GitHub PRs"
                hint="Search PR history and issue comments"
                value={value.research_include_github}
                onChange={(v) => updateField("research_include_github", v)}
              />
              {/* research_include_confluence */}
              <Toggle
                label="Include Confluence (per-repo)"
                hint="Deep Confluence search per repo (org-wide still runs)"
                value={value.research_include_confluence}
                onChange={(v) => updateField("research_include_confluence", v)}
              />
              {/* Reset */}
              <button
                onClick={resetDefaults}
                className="mt-2 inline-flex w-full items-center justify-center gap-1 rounded border border-border bg-bg-1 px-2 py-1 text-[11.5px] font-medium text-fg-1 hover:bg-bg-3 hover:text-fg-0"
              >
                <RefreshCcw className="h-3 w-3" strokeWidth={1.75} />
                Reset to defaults
              </button>
              <p className="text-[10px] leading-snug text-fg-3">
                Settings apply to this conversation only and take effect
                on the next message.
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/* ─── Sub-controls ───────────────────────────────────────────── */

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="mb-1 block text-[10.5px] font-medium uppercase tracking-wide text-fg-2">
        {label}
      </label>
      {children}
    </div>
  );
}

function Slider({
  label,
  hint,
  min,
  max,
  step,
  value,
  onChange,
}: {
  label: string;
  hint?: string;
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <label className="text-[10.5px] font-medium uppercase tracking-wide text-fg-2">
          {label}
        </label>
        <span className="font-mono text-[11px] text-fg-1">{value}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="h-1 w-full cursor-pointer appearance-none rounded-full bg-bg-3 accent-accent"
      />
      {hint && <p className="mt-0.5 text-[10px] text-fg-3">{hint}</p>}
    </div>
  );
}

function Segmented<T extends string>({
  options,
  value,
  onChange,
}: {
  options: Array<{ label: string; value: T }>;
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="inline-flex w-full overflow-hidden rounded-md border border-border bg-bg-1">
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={cn(
            "flex-1 px-2 py-1 text-[11px] font-medium transition-colors",
            value === opt.value
              ? "bg-accent text-accent-fg"
              : "text-fg-2 hover:bg-bg-2 hover:text-fg-0",
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

function CheckList({
  options,
  value,
  onChange,
}: {
  options: readonly string[];
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const set = new Set(value);
  return (
    <ul className="space-y-1">
      {options.map((opt) => (
        <li key={opt}>
          <label className="flex cursor-pointer items-center gap-2 text-[11.5px] text-fg-1">
            <input
              type="checkbox"
              checked={set.has(opt)}
              onChange={(e) => {
                const next = new Set(set);
                if (e.target.checked) next.add(opt);
                else next.delete(opt);
                onChange(Array.from(next));
              }}
              className="h-3 w-3 cursor-pointer accent-accent"
            />
            <span className="font-mono">{opt}</span>
          </label>
        </li>
      ))}
    </ul>
  );
}

function Toggle({
  label,
  hint,
  value,
  onChange,
}: {
  label: string;
  hint?: string;
  value: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-2">
      <input
        type="checkbox"
        checked={value}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 h-3 w-3 cursor-pointer accent-accent"
      />
      <span className="flex-1">
        <span className="text-[11.5px] font-medium text-fg-1">{label}</span>
        {hint && (
          <span className="block text-[10px] text-fg-3">{hint}</span>
        )}
      </span>
    </label>
  );
}

/* ─── Helpers ─────────────────────────────────────────────────── */

function tierLabel(floor: number): string {
  if (floor >= 0.85) return "T1+T2 only (code-verified / config-declared)";
  if (floor >= 0.55) return "T3+ (drops T4 docs / T5 fuzzy)";
  if (floor > 0) return `≥${floor.toFixed(2)} (allows lower tiers)`;
  return "no floor (follow every edge)";
}
