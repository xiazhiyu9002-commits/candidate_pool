import type { ReactNode } from "react";

export interface DataColumn<T> {
  key: string;
  header: ReactNode;
  render?: (row: T) => ReactNode;
  align?: "left" | "center" | "right";
  width?: number | string;
  minWidth?: number | string;
  sticky?: "left" | "right";
  className?: string;
  headerClassName?: string;
}

export interface DataTableProps<T> {
  columns: DataColumn<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  loading?: boolean;
  empty?: ReactNode;
  className?: string;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading = false,
  empty,
  className,
}: DataTableProps<T>) {
  return (
    <div className={["ui-table-scroll", className].filter(Boolean).join(" ")}>
      <table className="ui-table">
        <thead>
          <tr>
            {columns.map((col) => {
              const stickyClass = col.sticky ? `ui-table__cell--sticky-${col.sticky}` : undefined;
              return (
                <th
                  key={col.key}
                  data-align={col.align}
                  style={{ width: col.width, minWidth: col.minWidth }}
                  className={[col.headerClassName, stickyClass].filter(Boolean).join(" ")}
                >
                  {col.header}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((col) => {
                const stickyClass = col.sticky ? `ui-table__cell--sticky-${col.sticky}` : undefined;
                return (
                  <td
                    key={col.key}
                    data-align={col.align}
                    style={{ width: col.width, minWidth: col.minWidth }}
                    className={[col.className, stickyClass].filter(Boolean).join(" ")}
                  >
                    {col.render ? col.render(row) : null}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {!loading && rows.length === 0 && empty != null && (
        <div className="ui-table__empty">{empty}</div>
      )}
    </div>
  );
}
