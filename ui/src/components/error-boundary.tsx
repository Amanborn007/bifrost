import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RefreshCcw } from "lucide-react";

/**
 * ErrorBoundary catches render-time exceptions in child components
 * and shows a useful diagnostic instead of a blank page.
 *
 * Why this exists: a single render-time throw inside (say) the
 * handover detail's body Markdown renderer can take down the
 * entire page. The user sees nothing, the console has the error
 * but they may not be looking. With this boundary, the user gets
 * a clear "this part of the page crashed" panel + the error
 * message + a Retry button to remount.
 */
interface Props {
  children: ReactNode;
  /** Friendly label shown in the fallback ("Handover detail"). */
  label?: string;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Log the original stack so DevTools shows the offending line.
    console.error(
      `[ErrorBoundary${this.props.label ? ` · ${this.props.label}` : ""}]`,
      error,
      info.componentStack,
    );
  }

  reset = () => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      return (
        <div className="mx-auto max-w-[720px] px-8 py-12">
          <div className="rounded-lg border border-err-soft bg-err-soft px-5 py-4">
            <div className="mb-2 flex items-center gap-2 text-[13px] font-semibold text-err">
              <AlertTriangle className="h-4 w-4" strokeWidth={1.75} />
              {this.props.label
                ? `${this.props.label} crashed while rendering`
                : "This page crashed while rendering"}
            </div>
            <p className="text-[12.5px] text-fg-1">
              The page hit a JavaScript error and stopped halfway through.
              The data may still be fine — this is usually a UI bug. Try
              the retry button; if it keeps crashing, file an issue with
              the message below.
            </p>
            <pre className="mt-3 max-h-[200px] overflow-auto rounded border border-border bg-bg-2 p-3 text-[11.5px] leading-relaxed text-fg-1">
              {this.state.error.message}
              {this.state.error.stack ? (
                <>
                  {"\n\n"}
                  {this.state.error.stack.split("\n").slice(0, 6).join("\n")}
                </>
              ) : null}
            </pre>
            <button
              onClick={this.reset}
              className="mt-3 inline-flex items-center gap-1 rounded-md border border-border bg-bg-1 px-2.5 py-1 text-[11.5px] font-medium text-fg-1 hover:bg-bg-2 hover:text-fg-0"
            >
              <RefreshCcw className="h-3 w-3" strokeWidth={1.75} />
              Retry
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
