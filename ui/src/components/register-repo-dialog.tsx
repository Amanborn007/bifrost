import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { X, Loader2, Check } from "lucide-react";
import { cn, describeError } from "@/lib/utils";
import { registerRepo, triggerExtraction, listDomains } from "@/lib/api";

/**
 * Register repo dialog — produces a new repo and optionally kicks off
 * an extraction immediately. Invoked from the Dashboard.
 */
export function RegisterRepoDialog({
  open,
  onClose,
  onRegistered,
}: {
  open: boolean;
  onClose: () => void;
  onRegistered: () => void;
}) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [domainId, setDomainId] = useState<string>("");
  const [description, setDescription] = useState("");
  const [extractImmediately, setExtractImmediately] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  const domains = useQuery({
    queryKey: ["domains"],
    queryFn: listDomains,
    enabled: open,
  });

  useEffect(() => {
    if (!open) return;
    // Reset each time the dialog opens
    setName("");
    setUrl("");
    setDomainId("");
    setDescription("");
    setExtractImmediately(true);
    setSubmitting(false);
  }, [open]);

  // Auto-fill name from URL basename
  useEffect(() => {
    if (!name && url) {
      const match = url.replace(/\.git$/, "").match(/\/([^/]+)$/);
      if (match) setName(match[1]);
    }
  }, [url, name]);

  const canSubmit = name.trim().length > 0 && url.trim().length > 0 && !submitting;

  const onSubmit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!canSubmit) return;
    try {
      setSubmitting(true);
      const repo = await registerRepo({
        name: name.trim(),
        url: url.trim(),
        domain_id: domainId || null,
        description: description.trim() || undefined,
      });
      toast.success(`Registered ${name}`);

      if (extractImmediately) {
        try {
          await triggerExtraction(repo.id);
          toast.success(`Extraction queued for ${name}`);
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          toast.error(`Repo registered but extraction failed: ${msg}`);
        }
      }
      onRegistered();
      onClose();
    } catch (err) {
      toast.error(
        `Registration failed: ${describeError(err)}`,
      );
    } finally {
      setSubmitting(false);
    }
  };

  // Esc closes
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-full max-w-[500px] overflow-hidden rounded-xl border border-border bg-bg-1 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="register-title"
      >
        <header className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <h2 id="register-title" className="text-[16px] font-semibold text-fg-0">
            Register repository
          </h2>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-fg-2 hover:bg-bg-2 hover:text-fg-0"
            aria-label="Close"
          >
            <X className="h-4 w-4" strokeWidth={1.75} />
          </button>
        </header>

        <form onSubmit={onSubmit} className="space-y-4 px-5 py-5">
          <Field label="Git URL" hint="HTTPS or SSH repository URL">
            <input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://github.com/org/repo"
              autoFocus
              required
              className="h-9 w-full rounded-md border border-border bg-bg-2 px-3 font-mono text-[13px] text-fg-0 placeholder:text-fg-3 focus:border-border-focus focus:outline-none"
            />
          </Field>

          <Field label="Display name" hint="Shown across the UI">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-service"
              required
              className="h-9 w-full rounded-md border border-border bg-bg-2 px-3 text-[13.5px] text-fg-0 placeholder:text-fg-3 focus:border-border-focus focus:outline-none"
            />
          </Field>

          <Field label="Domain" hint="Optional grouping">
            <select
              value={domainId}
              onChange={(e) => setDomainId(e.target.value)}
              className="h-9 w-full rounded-md border border-border bg-bg-2 px-3 text-[13.5px] text-fg-0 focus:border-border-focus focus:outline-none"
            >
              <option value="">None</option>
              {(domains.data ?? []).map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Description" hint="Optional — what does this service do?">
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Order integration layer that bridges…"
              rows={2}
              className="w-full resize-none rounded-md border border-border bg-bg-2 px-3 py-2 text-[13.5px] leading-relaxed text-fg-0 placeholder:text-fg-3 focus:border-border-focus focus:outline-none"
            />
          </Field>

          <label className="flex cursor-pointer items-start gap-2.5 rounded-md border border-border bg-bg-2 px-3 py-2.5">
            <input
              type="checkbox"
              checked={extractImmediately}
              onChange={(e) => setExtractImmediately(e.target.checked)}
              className="mt-0.5 h-3.5 w-3.5 accent-accent"
            />
            <div className="flex-1">
              <div className="text-[13.5px] font-medium text-fg-0">
                Start extraction immediately
              </div>
              <p className="mt-0.5 text-[12px] text-fg-2">
                Clone the repo and run the 7-phase extraction pipeline as soon
                as registration succeeds.
              </p>
            </div>
          </label>
        </form>

        <footer className="flex items-center justify-end gap-2 border-t border-border bg-bg-0 px-5 py-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-3 py-1.5 text-[13px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onSubmit()}
            disabled={!canSubmit}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-[13px] font-medium transition-colors",
              canSubmit
                ? "bg-accent text-accent-fg hover:bg-accent-hover"
                : "bg-bg-3 text-fg-3",
            )}
          >
            {submitting ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" strokeWidth={2} />
                Registering…
              </>
            ) : (
              <>
                <Check className="h-3.5 w-3.5" strokeWidth={2} />
                Register repository
              </>
            )}
          </button>
        </footer>
      </div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="mb-1 block text-[12px] font-medium text-fg-1">
        {label}
      </label>
      {children}
      {hint && <p className="mt-1 text-[11px] text-fg-3">{hint}</p>}
    </div>
  );
}
