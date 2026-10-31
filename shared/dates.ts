import type { Row } from "./model.js";

export interface DateBounds {
  date_start: string;
  date_end: string;
}
export interface DateRange {
  start_date?: string;
  end_date?: string;
}

function validDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return (
    Number.isFinite(parsed.getTime()) &&
    parsed.toISOString().slice(0, 10) === value
  );
}

export function dateRangeError(range: DateRange, bounds: DateBounds): string {
  if (
    !validDate(bounds.date_start) ||
    !validDate(bounds.date_end) ||
    bounds.date_start > bounds.date_end
  )
    return "Order history has no valid date range.";
  for (const [label, value] of [
    ["From date", range.start_date],
    ["Through date", range.end_date],
  ]) {
    if (value == null || value === "") continue;
    if (typeof value !== "string" || !validDate(value))
      return `${label} must be a valid calendar date.`;
    if (value < bounds.date_start || value > bounds.date_end)
      return `${label} must be between ${bounds.date_start} and ${bounds.date_end}, the dates in the selected order history.`;
  }
  if (range.start_date && range.end_date && range.start_date > range.end_date)
    return "From date must be on or before Through date.";
  return "";
}

export function initialDateRange(
  settings: DateRange,
  bounds: DateBounds,
): Required<DateRange> {
  if (dateRangeError(settings, bounds))
    return { start_date: bounds.date_start, end_date: bounds.date_end };
  return {
    start_date: settings.start_date || bounds.date_start,
    end_date: settings.end_date || bounds.date_end,
  };
}

export function orderDateBounds(orders: Row): DateBounds {
  const dates = new Set<string>();
  for (const rows of [orders.daily, orders.baskets])
    if (Array.isArray(rows))
      for (const row of rows) dates.add(String(row?.date ?? ""));
  if (!dates.size) throw new Error("Order history has no valid dates.");
  let first = "",
    last = "";
  for (const value of dates) {
    if (!validDate(value))
      throw new Error(`Invalid calendar date in order history: ${value}`);
    if (!first || value < first) first = value;
    if (!last || value > last) last = value;
  }
  return { date_start: first, date_end: last };
}
