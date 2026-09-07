/**
 * Loading, empty and error. Three states every data surface must handle.
 *
 * `EmptyState` says "nothing here", never "all clear": an empty positions
 * table when the broker is unreachable is not the same as a flat book, and
 * the caller passes the reason so the two read differently.
 */
export function LoadingState({ what = "data" }: { what?: string }) {
  return (
    <p role="status" className="py-4 text-center text-body text-muted">
      Loading {what}…
    </p>
  );
}

export function EmptyState({ message, hint }: { message: string; hint?: string }) {
  return (
    <div className="py-4 text-center">
      <p className="text-body text-muted">{message}</p>
      {hint && <p className="mt-1 text-body text-muted">{hint}</p>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="py-4 text-center">
      <p className="text-body text-critical">{message}</p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-2 text-body text-accent hover:underline"
        >
          Try again
        </button>
      )}
    </div>
  );
}
