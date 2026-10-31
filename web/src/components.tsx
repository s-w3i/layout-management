import { useId, useRef, useState, type ReactNode } from "react";
import {
  ArrowUpRight,
  Upload,
  Search,
  ChevronLeft,
  ChevronRight,
  SlidersHorizontal,
  ArrowUp,
  ArrowDown,
  ArrowUpDown,
} from "lucide-react";
import type { Row } from "../../shared/model";
import {
  filterTableRows,
  sortTableRows,
  type TableFilter,
  type TableSort,
} from "../../shared/table";
export const count = (n: unknown) =>
  Number(n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 1 });
export function Field({
  label,
  hint,
  children,
  className = "",
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={`field ${className}`}>
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
export function Panel({
  title,
  subtitle,
  children,
  action,
  className = "",
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-title">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}
export function UploadButton({
  label,
  accept,
  onFile,
  disabled = false,
  small = false,
}: {
  label: string;
  accept: string;
  onFile: (file: File) => void;
  disabled?: boolean;
  small?: boolean;
}) {
  const ref = useRef<HTMLInputElement>(null);
  return (
    <>
      <input
        ref={ref}
        type="file"
        hidden
        accept={accept}
        className="sr-only"
        tabIndex={-1}
        aria-label={label}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onFile(file);
          e.target.value = "";
        }}
      />
      <button
        disabled={disabled}
        className={small ? "button button-small" : "button"}
        onClick={() => ref.current?.click()}
      >
        <Upload size={15} />
        {label}
      </button>
    </>
  );
}
export function Metric({
  label,
  value,
  detail,
  tone = "",
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: string;
}) {
  return (
    <div className={`metric ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </div>
  );
}
export function Empty({
  title,
  detail,
  children,
}: {
  title: string;
  detail: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-symbol">
        <ArrowUpRight size={22} />
      </div>
      <h3>{title}</h3>
      <p>{detail}</p>
      {children}
    </div>
  );
}
export function Table({
  rows,
  columns,
  searchable = false,
  empty = "No rows to show",
  label = "Data table",
  filters = [],
  sortable = false,
}: {
  rows: Row[];
  columns: {
    key: string;
    label: string;
    render?: (value: any, row: Row) => ReactNode;
  }[];
  searchable?: boolean;
  empty?: string;
  label?: string;
  filters?: TableFilter[];
  sortable?: boolean;
}) {
  const [query, setQuery] = useState(""),
    [page, setPage] = useState(0);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [selections, setSelections] = useState<Record<string, string>>({});
  const [sort, setSort] = useState<TableSort | null>(null);
  const filterId = useId();
  const activeFilters = Object.values(selections).filter(Boolean).length;
  const filtered = sortTableRows(
    filterTableRows(rows, query, selections, filters),
    sort,
  );
  const pages = Math.ceil(filtered.length / 25),
    current = Math.min(page, Math.max(0, pages - 1)),
    visible = filtered.slice(current * 25, (current + 1) * 25);
  return (
    <div className="table-block">
      {(searchable || filters.length > 0) && (
        <div className="table-search">
          {searchable && (
            <>
              <Search size={15} aria-hidden="true" />
              <input
                aria-label={`Search ${label}`}
                placeholder="Search SKU, rack or address…"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setPage(0);
                }}
              />
            </>
          )}
          {filters.length > 0 && (
            <button
              type="button"
              className="button button-small table-filter-toggle"
              aria-expanded={filtersOpen}
              aria-controls={filterId}
              onClick={() => setFiltersOpen(!filtersOpen)}
            >
              <SlidersHorizontal size={15} aria-hidden="true" />
              Filters{activeFilters > 0 ? ` (${activeFilters})` : ""}
            </button>
          )}
          <span role="status">
            {count(filtered.length)}
            {filters.length > 0 ? ` of ${count(rows.length)}` : ""} records
          </span>
        </div>
      )}
      {filters.length > 0 && (
        <div id={filterId} className="table-filters" hidden={!filtersOpen}>
          {filters.map((filter) => (
            <label key={filter.key} className="field">
              <span>{filter.label}</span>
              <select
                aria-label={`Filter ${label} by ${filter.label}`}
                value={selections[filter.key] ?? ""}
                onChange={(event) => {
                  setSelections({
                    ...selections,
                    [filter.key]: event.target.value,
                  });
                  setPage(0);
                }}
              >
                <option value="">All</option>
                {filter.options.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          ))}
          <button
            type="button"
            className="button button-small"
            disabled={!activeFilters}
            onClick={() => {
              setSelections({});
              setPage(0);
            }}
          >
            Clear filters
          </button>
        </div>
      )}
      <div className="table-scroll">
        <table aria-label={label}>
          <thead>
            <tr>
              {columns.map((c) => (
                <th
                  key={c.key}
                  scope="col"
                  aria-sort={
                    sortable
                      ? sort?.key === c.key
                        ? sort.direction
                        : "none"
                      : undefined
                  }
                >
                  {sortable ? (
                    <button
                      type="button"
                      className="table-sort"
                      aria-label={`Sort ${c.label} ${sort?.key === c.key && sort.direction === "ascending" ? "descending" : "ascending"}`}
                      onClick={() => {
                        setSort({
                          key: c.key,
                          direction:
                            sort?.key === c.key &&
                            sort.direction === "ascending"
                              ? "descending"
                              : "ascending",
                        });
                        setPage(0);
                      }}
                    >
                      {c.label}
                      {sort?.key === c.key ? (
                        sort.direction === "ascending" ? (
                          <ArrowUp size={13} aria-hidden="true" />
                        ) : (
                          <ArrowDown size={13} aria-hidden="true" />
                        )
                      ) : (
                        <ArrowUpDown size={13} aria-hidden="true" />
                      )}
                    </button>
                  ) : (
                    c.label
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, index) => (
              <tr
                key={String(
                  row.inventory_load_id ?? row.sku ?? row.rack_id ?? index,
                )}
              >
                {columns.map((c) => (
                  <td key={c.key}>
                    {c.render
                      ? c.render(row[c.key], row)
                      : row[c.key] == null || row[c.key] === ""
                        ? "—"
                        : typeof row[c.key] === "number"
                          ? count(row[c.key])
                          : String(row[c.key])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {!filtered.length && <div className="table-empty">{empty}</div>}
      </div>
      {filtered.length > 25 && (
        <div className="table-pagination">
          <span>
            {current * 25 + 1}–{Math.min((current + 1) * 25, filtered.length)}{" "}
            of {count(filtered.length)}
          </span>
          <button
            className="icon-button"
            aria-label="Previous page"
            disabled={current === 0}
            onClick={() => setPage(current - 1)}
          >
            <ChevronLeft size={17} />
          </button>
          <button
            className="icon-button"
            aria-label="Next page"
            disabled={current + 1 >= pages}
            onClick={() => setPage(current + 1)}
          >
            <ChevronRight size={17} />
          </button>
        </div>
      )}
    </div>
  );
}
export function Badge({
  children,
  tone = "",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
