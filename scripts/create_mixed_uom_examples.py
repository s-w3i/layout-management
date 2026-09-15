import csv
from collections import Counter
from pathlib import Path

source = Path("resources/data/mixed_uom_order_lines.csv")
output = Path("resources/data/mixed_uom_order_examples.csv")
with source.open(newline="", encoding="utf-8") as stream:
    rows = list(csv.DictReader(stream))
fields = ["Item or SKU", "UOM", "UOM ConversionQty", "Date", "Store ID", "UOM status"]
examples = {}
for row in rows:
    sku = row["Item or SKU"]
    examples.setdefault(sku, []).append(row)
output.parent.mkdir(parents=True, exist_ok=True)
with output.open("w", newline="", encoding="utf-8") as stream:
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for sku in sorted(examples):
        grouped = {}
        for row in examples[sku]:
            grouped.setdefault((row["UOM"], row["UOM ConversionQty"]), row)
        common_key = Counter(
            (row["UOM"], row["UOM ConversionQty"]) for row in examples[sku]
        ).most_common(1)[0][0]
        for status, key in (("COMMON", common_key), ("UNCOMMON", next(k for k in grouped if k != common_key))):
            row = grouped[key]
            writer.writerow({**{field: row[field] for field in fields[:-1]}, "UOM status": status})
print(f"Wrote {output}")
