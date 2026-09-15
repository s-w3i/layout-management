import csv
from pathlib import Path

path = Path("resources/data/mixed_uom_order_lines.csv")
with path.open(newline="", encoding="utf-8") as stream:
    rows = list(csv.reader(stream))
header, data = rows[0], rows[1:]
output = []
previous = None
for row in data:
    if not row:
        continue
    sku = row[3]
    if previous is not None and sku != previous:
        output.append([])
    output.append(row)
    previous = sku
with path.open("w", newline="", encoding="utf-8") as stream:
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(output)
print(f"Added spacing between {len({row[3] for row in data if row})} SKU groups")
