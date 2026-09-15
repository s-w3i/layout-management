import csv
from pathlib import Path

path = Path("resources/data/mixed_uom_order_examples.csv")
with path.open(newline="", encoding="utf-8") as stream:
    rows = list(csv.reader(stream))
header, data = rows[0], rows[1:]
output, previous = [], None
for row in data:
    if not row:
        continue
    if previous is not None and row[0] != previous:
        output.append([])
    output.append(row)
    previous = row[0]
with path.open("w", newline="", encoding="utf-8") as stream:
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(output)
print("Added spacing between SKU groups")
