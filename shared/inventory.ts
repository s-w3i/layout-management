export const stockStatuses = [
  "available",
  "quarantine",
  "damaged",
  "blocked",
] as const;
export const quantityScale = 1_000_000;
export function exactQuantity(micro: number): string {
  if (!Number.isSafeInteger(micro)) throw new Error("Invalid stored quantity");
  const value = BigInt(micro),
    absolute = value < 0n ? -value : value;
  const fraction = String(absolute % 1_000_000n)
    .padStart(6, "0")
    .replace(/0+$/, "");
  return `${value < 0n ? "-" : ""}${absolute / 1_000_000n}${fraction ? "." + fraction : ""}`;
}

// Exact millionths of a base unit; never round quantities during stock posting.
export function scaledQuantity(value: unknown, signed = false): number {
  const text = String(value ?? "").trim();
  if (!(signed ? /^-?\d+(?:\.\d{1,6})?$/ : /^\d+(?:\.\d{1,6})?$/).test(text))
    throw new Error(
      "Quantity must be a decimal with at most six decimal places",
    );
  const negative = text.startsWith("-");
  const [whole, fraction = ""] = text.replace(/^-/, "").split(".");
  const result =
    (BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, "0"))) *
    (negative ? -1n : 1n);
  if (
    result > BigInt(Number.MAX_SAFE_INTEGER) ||
    result < -BigInt(Number.MAX_SAFE_INTEGER)
  )
    throw new Error("Quantity exceeds supported precision");
  return Number(result);
}
