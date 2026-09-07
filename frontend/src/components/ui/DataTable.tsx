import { EmptyState, ErrorState, LoadingState } from "./States";

export interface Column<T> {
  key: string;
  header: string;
  align?: "left" | "right";
  render?: (row: T) => React.ReactNode;
}

interface Props<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  loading?: boolean;
  error?: string | null;
  empty?: string;
  emptyHint?: string;
  caption?: string;
}

/**
 * One table implementation for every list in the app.
 *
 * It always renders its header, so a reader can see which fields exist even
 * when there is nothing in them, and it distinguishes loading, error and
 * empty rather than showing "no rows" for all three.
 */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading = false,
  error = null,
  empty = "no rows",
  emptyHint,
  caption,
}: Props<T>) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-body">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead className="text-mini uppercase tracking-wider text-muted">
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                className={`border-b border-line pb-1.5 font-medium ${
                  c.align === "right" ? "text-right" : "text-left"
                }`}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="tabular">
          {loading || error || rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="p-0">
                {loading ? (
                  <LoadingState />
                ) : error ? (
                  <ErrorState message={error} />
                ) : (
                  <EmptyState message={empty} hint={emptyHint} />
                )}
              </td>
            </tr>
          ) : (
            rows.map((row) => (
              <tr
                key={rowKey(row)}
                className="border-t border-line/60 transition-colors hover:bg-surface-2/50"
              >
                {columns.map((c) => (
                  <td
                    key={c.key}
                    className={`py-1.5 ${c.align === "right" ? "text-right" : "text-left"}`}
                  >
                    {c.render ? c.render(row) : "—"}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
