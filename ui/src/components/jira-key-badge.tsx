import { ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";

/** A Jira key that links out only when the API supplied a URL; otherwise plain text. */
export function JiraKeyBadge({
  jiraKey,
  url,
  className,
}: {
  jiraKey: string;
  url: string | null;
  className?: string;
}) {
  const base = cn("badge badge-info font-mono", className);
  if (!url) {
    return <span className={base}>{jiraKey}</span>;
  }
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className={cn(base, "hover:opacity-80")}
      title="Open in Jira"
    >
      {jiraKey}
      <ExternalLink className="h-2.5 w-2.5" />
    </a>
  );
}
