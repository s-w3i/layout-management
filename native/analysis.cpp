#include "model.hpp"
#include <chrono>
#include <regex>

namespace layout {
static int day_number(const std::string &s) {
  static const std::regex format("[0-9]{4}-[0-9]{2}-[0-9]{2}");
  if (!std::regex_match(s, format))
    throw std::runtime_error("Dates must use YYYY-MM-DD");
  auto date = std::chrono::year{std::stoi(s.substr(0, 4))} /
              unsigned(std::stoi(s.substr(5, 2))) /
              unsigned(std::stoi(s.substr(8, 2)));
  if (!date.ok())
    throw std::runtime_error("Invalid calendar date: " + s);
  return int(std::chrono::sys_days{date}.time_since_epoch().count());
}
struct Demand {
  double quantity = 0, raw_quantity = 0, frequency = 0, conversion = 0;
  std::set<std::string> days;
  json req = json::object();
};
static std::map<std::string, Demand> aggregate(const json &data,
                                               const json &settings,
                                               std::string &first,
                                               std::string &last) {
  auto start = text(settings, "start_date"), end = text(settings, "end_date");
  if (!start.empty())
    day_number(start);
  if (!end.empty())
    day_number(end);
  if (!start.empty() && !end.empty() && start > end)
    throw std::runtime_error("Start date must be on or before end date");
  std::map<std::string, Demand> totals;
  for (auto &row : array(data, "daily")) {
    auto day = text(row, "date"), sku = text(row, "sku");
    if (sku.empty())
      throw std::runtime_error("Demand contains an empty SKU");
    day_number(day);
    if ((!start.empty() && day < start) || (!end.empty() && day > end))
      continue;
    if (first.empty() || day < first)
      first = day;
    if (last.empty() || day > last)
      last = day;
    auto &t = totals[sku];
    t.quantity += std::max(0., num(row, "quantity"));
    t.raw_quantity += num(row, "raw_quantity", num(row, "quantity"));
    t.frequency += num(row, "frequency", 1);
    auto conversion = num(row, "conversion", 1);
    t.conversion = std::max(t.conversion, conversion > 0 ? conversion : 1);
    if (!std::isfinite(t.quantity) || !std::isfinite(t.raw_quantity) ||
        t.frequency < 0 || t.frequency > 2000000)
      throw std::runtime_error(
          "Demand quantities or frequencies exceed supported limits");
    t.days.insert(day);
    auto req = object(row, "requirements");
    for (auto &[k, v] : req.items())
      if (!empty(v) && number(v) > 0)
        t.req[k] = std::max(num(t.req, k), number(v));
  }
  if (totals.empty())
    throw std::runtime_error("No valid order rows in the selected date range");
  return totals;
}

static json velocity_rows(const std::map<std::string, Demand> &totals,
                          const json &settings) {
  double a = num(settings, "a_limit", 0.8), b = num(settings, "b_limit", 0.95);
  if (!(0 < a && a < b && b <= 1))
    throw std::runtime_error("ABC thresholds require 0 < A < B ≤ 1");
  std::vector<std::string> sorted;
  double total = 0;
  for (auto &[sku, t] : totals) {
    sorted.push_back(sku);
    total += t.frequency;
  }
  std::sort(sorted.begin(), sorted.end(), [&](auto &x, auto &y) {
    return std::pair{-totals.at(x).frequency, x} <
           std::pair{-totals.at(y).frequency, y};
  });
  json rows = json::array();
  double cumulative = 0;
  for (auto &sku : sorted) {
    const auto &t = totals.at(sku);
    cumulative += t.frequency;
    double share = total ? cumulative / total : 0;
    json row = {{"sku", sku},
                {"pick_frequency", t.frequency},
                {"total_quantity_ea", rounded(t.raw_quantity, 4)},
                {"active_days", t.days.size()},
                {"cumulative_frequency_share", rounded(share)},
                {"velocity_class", rows.empty() || share <= a ? "A"
                                   : share <= b               ? "B"
                                                              : "C"},
                {"sku_requirements", t.req}};
    for (auto &[key, value] : t.req.items())
      row["req_" + key] = value;
    rows.push_back(row);
  }
  return rows;
}

struct Box {
  int l = 1, s = 1;
  double weight = 0;
};
static std::vector<std::vector<int>> placements(int l, int s, int levels,
                                                int slots) {
  std::vector<std::vector<int>> all;
  for (int a = 0; a + l <= levels; ++a)
    for (int b = 0; b + s <= slots; ++b) {
      std::vector<int> cells;
      for (int i = 0; i < l; ++i)
        for (int j = 0; j < s; ++j)
          cells.push_back((a + i) * slots + b + j);
      all.push_back(cells);
    }
  return all;
}
static std::pair<int, bool> rack_count(std::vector<Box> loads, int levels,
                                       int slots, double maximum) {
  if (loads.empty())
    return {0, true};
  int capacity = levels * slots;
  for (auto &b : loads)
    if (b.l > levels || b.s > slots || b.weight > maximum + 1e-9)
      return {0, false};
  bool simple = true;
  double totalWeight = 0;
  int totalArea = 0;
  for (auto &b : loads) {
    totalWeight += b.weight;
    totalArea += b.l * b.s;
    simple =
        simple && b.l == 1 && b.s == 1 && b.weight <= maximum / capacity + 1e-9;
  }
  if (simple)
    return {int((loads.size() + capacity - 1) / capacity), true};
  std::stable_sort(loads.begin(), loads.end(), [&](const Box &a, const Box &b) {
    return std::tuple{a.l * a.s / double(capacity),
                      std::isfinite(maximum) ? a.weight / maximum : 0., a.l,
                      a.s, a.weight} >
           std::tuple{b.l * b.s / double(capacity),
                      std::isfinite(maximum) ? b.weight / maximum : 0., b.l,
                      b.s, b.weight};
  });
  struct Bin {
    std::vector<bool> used;
    double weight = 0;
    int occupied = 0;
  };
  std::vector<Bin> bins;
  for (auto &box : loads) {
    std::tuple<int, double, int, int> best{capacity + 1, INF, -1, -1};
    auto ps = placements(box.l, box.s, levels, slots);
    for (int i = 0; i < int(bins.size()); ++i)
      if (bins[i].weight + box.weight <= maximum + 1e-9)
        for (int j = 0; j < int(ps.size()); ++j) {
          bool free = true;
          for (int cell : ps[j])
            free = free && !bins[i].used[cell];
          if (free)
            best = std::min(
                best, std::tuple{capacity - bins[i].occupied - box.l * box.s,
                                 maximum - bins[i].weight - box.weight, i, j});
        }
    int i = std::get<2>(best), j = std::get<3>(best);
    if (i < 0) {
      bins.push_back({std::vector<bool>(capacity), 0, 0});
      i = int(bins.size()) - 1;
      j = 0;
    }
    for (int cell : ps[j])
      bins[i].used[cell] = true;
    bins[i].weight += box.weight;
    bins[i].occupied += box.l * box.s;
  }
  int lower = int(std::ceil(totalArea / double(capacity)));
  if (std::isfinite(maximum))
    lower = std::max(lower, int(std::ceil(totalWeight / maximum)));
  return {int(bins.size()), int(bins.size()) == lower};
}

json calculate_stock(const json &request) {
  auto settings = object(request, "settings");
  auto data = object(request, "orders");
  std::string first, last;
  auto totals = aggregate(data, settings, first, last);
  int minDays = integer(settings, "minimum_stock_days", 2, 1, 3650),
      bufferDays = integer(settings, "buffer_stock_days", 1, 0, 3650),
      days = day_number(last) - day_number(first) + 1;
  auto g = geometry(request.at("project"));
  json attributes = object(request, "attributes");
  auto velocity = velocity_rows(totals, settings);
  std::map<std::string, json> velocityBySku;
  for (auto &row : velocity)
    velocityBySku[text(row, "sku")] = row;
  json rows = json::array(), combinations = json::array();
  std::map<std::string, std::vector<Box>> groupLoads;
  std::map<std::string, json> groups;
  std::vector<std::string> keys;
  for (auto &k : array(settings, "combination_attributes"))
    keys.push_back(str(k));
  if (keys.empty())
    for (auto &d : g.catalog)
      if (text(d, "value_type") == "boolean" &&
          text(d, "key") != "oversize_capable" && flag(d, "enabled", true))
        keys.push_back(text(d, "key"));
  bool physical = true;
  for (auto &k : PHYSICAL) {
    bool found = false;
    for (auto &d : g.catalog)
      found = found || text(d, "key") == k;
    physical = physical && found;
  }
  if (physical && std::find(keys.begin(), keys.end(), "oversize") == keys.end())
    keys.push_back("oversize");
  auto slot = g.standard;
  auto override = object(settings, "slot_dimensions");
  slot.update(override);
  double slotMax = cap(slot, PHYSICAL[3]),
         rackMax = g.amr ? cap(g.machine, PHYSICAL[3]) : INF;
  for (auto &[sku, t] : totals) {
    json req = attributes.contains(sku) ? attributes[sku] : t.req;
    auto p = profile(req, g);
    double daily = t.quantity / days;
    if (daily * (minDays + bufferDays) > 1e9)
      throw std::runtime_error("Stock target exceeds one billion units; review "
                               "the selected coverage period");
    int minimum = int(std::ceil(daily * minDays)),
        required = int(std::ceil(daily * (minDays + bufferDays)));
    double conversion = t.conversion > 0 ? t.conversion : 1;
    if (required / conversion > 1e9 ||
        std::ceil(required / conversion) * conversion > 1e9)
      throw std::runtime_error(
          "Purchasing-unit conversion exceeds supported stock limits");
    int minimumUom = int(std::ceil(minimum / conversion)),
        totalUom = int(std::ceil(required / conversion)),
        slotted = int(std::ceil(totalUom * conversion));
    json row = velocityBySku[sku];
    row["sku_requirements"] = req;
    row.update({{"observation_start", first},
                {"observation_end", last},
                {"observation_days", days},
                {"total_demand_ea", rounded(t.quantity, 4)},
                {"total_demand_uom", rounded(t.quantity / conversion, 4)},
                {"average_daily_demand_ea", rounded(daily, 4)},
                {"average_daily_demand_uom", rounded(daily / conversion, 4)},
                {"minimum_stock_days", minDays},
                {"buffer_stock_days", bufferDays},
                {"minimum_stock_ea", minimum},
                {"minimum_stock_uom", minimumUom},
                {"minimum_buffer_stock_ea", required - minimum},
                {"minimum_buffer_stock_uom", totalUom - minimumUom},
                {"total_required_ea", required},
                {"total_slotted_ea", slotted},
                {"total_required_uom", totalUom},
                {"total_slotted_uom", totalUom},
                {"uom_conversion_qty", conversion}});
    std::array<double, 3> dims;
    bool known = true;
    for (int i = 0; i < 3; ++i) {
      dims[i] = num(req, PHYSICAL[i]);
      known = known && dims[i] > 0;
    }
    int dimensionalUnits = known ? 0 : 1, units = 0, span = 1, vl = 1, hs = 1;
    std::string status =
        known ? "CALCULATED" : "CALCULATED_UNVERIFIED_DIMENSIONS";
    if (known) {
      std::sort(dims.begin(), dims.end());
      do {
        double count = 1;
        for (int i = 0; i < 3; ++i)
          count *= std::floor(num(slot, PHYSICAL[i]) / dims[i] + 1e-12);
        dimensionalUnits =
            std::max(dimensionalUnits, int(std::min(count, 1e9)));
      } while (std::next_permutation(dims.begin(), dims.end()));
    }
    double weight = num(req, PHYSICAL[3]);
    int weightUnits =
        std::isfinite(slotMax)
            ? (weight > 0
                   ? std::max(1,
                              int(std::min(1e9, std::floor(slotMax / weight))))
                   : 1)
            : 0;
    int requiredSlots = 0;
    std::vector<Box> boxes;
    if (dimensionalUnits > 0) {
      units = weightUnits ? std::min(dimensionalUnits, weightUnits)
                          : dimensionalUnits;
      requiredSlots = int(std::ceil(slotted / double(units)));
    } else {
      auto fp = footprint(req, slot, g.levels, g.slots);
      vl = fp.first;
      hs = fp.second;
      span = vl * hs;
      if (span > 0) {
        requiredSlots = slotted * span;
        status = "CALCULATED_MULTI_SLOT";
      } else
        status = "ITEM_DOES_NOT_FIT";
    }
    if (requiredSlots > 1000000)
      throw std::runtime_error(
          "Stock calculation exceeds one million physical cells; review "
          "missing dimensions or quantities");
    int loadCount = span > 0 ? int(std::ceil(requiredSlots / double(span))) : 0;
    for (int i = 0; i < loadCount; ++i)
      boxes.push_back({vl, hs,
                       (slotted / loadCount + (i < slotted % loadCount)) *
                           std::max(0., weight)});
    auto [racks, proven] = rack_count(boxes, g.levels, g.slots, rackMax);
    if (weight <= 0 && status.starts_with("CALCULATED"))
      status = "CALCULATED_UNVERIFIED_PHYSICAL";
    else if (loadCount && !proven)
      status =
          racks ? "CALCULATED_FEASIBLE_RACK_UPPER_BOUND" : "ITEM_DOES_NOT_FIT";
    row.update(
        {{"units_per_slot", units},
         {"slots_per_unit", span},
         {"required_slots",
          status == "ITEM_DOES_NOT_FIT" ? json(nullptr) : json(requiredSlots)},
         {"required_racks", racks},
         {"occupied_level_span", vl},
         {"occupied_horizontal_slot_span", hs},
         {"rack_calculation_status", status}});
    json signature = json::object();
    std::string label;
    for (auto &k : keys) {
      json val = k == "oversize"   ? json(p.reserve())
                 : req.contains(k) ? req[k]
                                   : json(nullptr);
      signature[k] = val;
      if (!label.empty())
        label += " · ";
      label +=
          k + "=" + (val.is_boolean() ? (val.get<bool>() ? "T" : "F") : "?");
    }
    if (label.empty())
      label = "all SKUs";
    row["attribute_combination"] = label;
    auto key = signature.dump();
    if (!groups.count(key))
      groups[key] = {
          {"attributes", signature}, {"attribute_combination", label},
          {"sku_count", 0},          {"total_required_ea", 0},
          {"required_slots", 0},     {"unresolved_skus", 0}};
    auto &gr = groups[key];
    gr["sku_count"] = num(gr, "sku_count") + 1;
    gr["total_required_ea"] = num(gr, "total_required_ea") + slotted;
    gr["required_slots"] = num(gr, "required_slots") + requiredSlots;
    if (status == "ITEM_DOES_NOT_FIT" || weight <= 0)
      gr["unresolved_skus"] = num(gr, "unresolved_skus") + 1;
    groupLoads[key].insert(groupLoads[key].end(), boxes.begin(), boxes.end());
    rows.push_back(row);
  }
  for (auto &[key, gr] : groups) {
    auto [racks, proven] =
        rack_count(groupLoads[key], g.levels, g.slots, rackMax);
    gr["required_racks"] = racks;
    gr["minimum_proven"] = proven;
    combinations.push_back(gr);
  }
  return {{"rows", rows},         {"combinations", combinations},
          {"settings", settings}, {"date_start", first},
          {"date_end", last},     {"observation_days", days}};
}

json analyze(const json &request) {
  auto settings = object(request, "settings"), data = object(request, "orders");
  std::string first, last;
  auto totals = aggregate(data, settings, first, last);
  auto rows = velocity_rows(totals, settings);
  std::map<std::string, int> indices;
  int index = 0;
  for (auto &[sku, _] : totals)
    indices[sku] = index++;
  std::vector<int> frequency(indices.size());
  std::unordered_map<uint64_t, int> pairs;
  int groups = 0;
  json stores = json::array();
  std::set<std::string> storeSet;
  for (auto &basket : array(data, "baskets")) {
    auto date = text(basket, "date");
    if (date < first || date > last)
      continue;
    std::vector<int> skus;
    for (auto &s : array(basket, "skus"))
      if (indices.count(str(s)))
        skus.push_back(indices[str(s)]);
    std::sort(skus.begin(), skus.end());
    skus.erase(std::unique(skus.begin(), skus.end()), skus.end());
    if (skus.empty())
      continue;
    ++groups;
    storeSet.insert(text(basket, "store"));
    for (int a : skus)
      ++frequency[a];
    for (size_t i = 0; i < skus.size(); ++i)
      for (size_t j = i + 1; j < skus.size(); ++j)
        ++pairs[(uint64_t(skus[i]) << 32) | uint32_t(skus[j])];
  }
  struct Pair {
    uint64_t key;
    int shared;
    double cosine;
  };
  std::vector<Pair> ranked;
  for (auto &[key, count] : pairs) {
    int a = int(key >> 32), b = int(key & 0xffffffff);
    ranked.push_back(
        {key, count, count / std::sqrt(double(frequency[a]) * frequency[b])});
  }
  std::sort(ranked.begin(), ranked.end(), [](const Pair &a, const Pair &b) {
    return std::tuple{-a.cosine, -a.shared, a.key} <
           std::tuple{-b.cosine, -b.shared, b.key};
  });
  std::vector<std::string> names(indices.size());
  for (auto &[sku, id] : indices)
    names[id] = sku;
  json relationships = json::array();
  for (size_t i = 0; i < std::min<size_t>(ranked.size(), 100); ++i) {
    auto p = ranked[i];
    relationships.push_back({{"sku_a", names[p.key >> 32]},
                             {"sku_b", names[p.key & 0xffffffff]},
                             {"shared_store_days", p.shared},
                             {"cosine", p.cosine}});
  }
  json classes = {{"A", 0}, {"B", 0}, {"C", 0}};
  double lines = 0;
  for (auto &row : rows) {
    auto cl = text(row, "velocity_class");
    classes[cl] = num(classes, cl) + 1;
    lines += num(row, "pick_frequency");
  }
  for (auto &s : storeSet)
    stores.push_back(s);
  return {{"rows", rows},
          {"relationships", relationships},
          {"summary",
           {{"sku_count", rows.size()},
            {"line_count", lines},
            {"store_day_count", groups},
            {"store_count", storeSet.size()},
            {"relationship_count", ranked.size()},
            {"date_start", first},
            {"date_end", last},
            {"classes", classes}}},
          {"stores", stores},
          {"settings", settings}};
}
} // namespace layout
