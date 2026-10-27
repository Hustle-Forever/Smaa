import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";

/**
 * Catches a render-time throw and shows it, instead of letting React unmount
 * the whole tree.
 *
 * Until this existed there was no boundary anywhere in the app, which meant one
 * bad row from an API -- a null where a string was expected, a shape that
 * changed -- took the entire page to a white screen with the message only in
 * the console. The sidebar went with it, so there was not even a way to
 * navigate off the broken page.
 *
 * Mounted around the page content and keyed by tab, so it wraps the surface
 * that can crash while leaving the chrome alive, and navigating to another tab
 * remounts it clean without anyone having to press anything.
 *
 * Deliberately NOT a toast: a toast is for something that failed while the page
 * kept working. This is the page not working.
 */
interface Props {
  children: ReactNode;
  /** Shown in the message so the user can say which surface broke. */
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
    // No logging service is wired up, and the repo keeps console clean on
    // purpose, so this is the one place a stack is worth printing: without it
    // the trace is gone the moment the fallback renders.
    console.error("Render error", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="card p-8 max-w-xl" role="alert">
        <div className="flex items-start gap-3">
          <AlertTriangle
            className="w-5 h-5 text-danger shrink-0 mt-0.5"
            strokeWidth={2}
            aria-hidden
          />
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-fg">
              {this.props.label
                ? `Something broke on ${this.props.label}`
                : "Something broke on this page"}
            </h2>
            <p className="text-sm text-muted mt-1 leading-relaxed">
              The rest of the app is still fine, you can switch tabs and come
              back. If it keeps happening, this message is the useful part:
            </p>
            <pre className="mt-3 max-h-40 overflow-auto rounded-lg bg-bg-muted p-3 text-2xs font-mono text-fg whitespace-pre-wrap break-words">
              {String(error?.message || error)}
            </pre>
            <button
              type="button"
              onClick={() => this.setState({ error: null })}
              className="btn btn-ghost text-xs mt-4"
            >
              <RotateCcw className="w-3.5 h-3.5" aria-hidden />
              Try rendering again
            </button>
          </div>
        </div>
      </div>
    );
  }
}
