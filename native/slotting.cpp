#include "affinity.hpp"
#include "model.hpp"
#include <chrono>
#include <unordered_set>

namespace layout {
static std::vector<bool> operational_routes(const Geometry &g) {
  std::vector<bool> reachable(g.racks.size());
  for (auto &[_, node] : g.stations) {
    auto forward = distances(g, node, false, g.amr),
         backward = distances(g, node, true, g.amr);
    for (size_t rack = 0; rack < g.racks.size(); ++rack) {
      auto n = g.racks[rack].node;
      reachable[rack] = reachable[rack] || (std::isfinite(forward[n]) &&
                                            std::isfinite(backward[n]));
    }
  }
  return reachable;
}
struct State {
  std::set<std::string> classes, buckets;
  bool oversize = false;
  double weight = 0;
};
struct Engine {
  Geometry g;
  json settings;
  std::vector<Cell> cells;
  std::vector<Load> loads;
  std::vector<int> owner, reserved_owner;
  std::vector<State> states;
  std::map<std::pair<std::string, int>, int> skuCounts;
  std::map<std::string, std::vector<int>> skuPositions;
  std::set<std::string> configured;
  std::map<std::tuple<int, int, int>, int> lookup;
  std::map<std::string, double> zoneDemand, zoneCapacity;
  int sameLimit = 12;
  bool ergonomics = true, balance = false;
  double rackMax = INF;
  int reservationNodes = 0;
  bool completePlan = true;

  Engine(const json &request)
      : g(geometry(request.at("project"))),
        settings(object(request, "settings")) {
    if (g.racks.empty())
      throw std::runtime_error("Add at least one rack to the warehouse");
    if (g.stations.empty())
      throw std::runtime_error("Add at least one workstation to the warehouse");
    sameLimit = integer(settings, "maximum_same_sku_slots_per_rack",
                        g.levels * g.slots, 1, 256);
    ergonomics = flag(settings, "ergonomic_weight_heuristic", true);
    balance = flag(settings, "zone_workload_enabled");
    rackMax = g.amr ? cap(g.machine, PHYSICAL[3]) : INF;
    for (auto &r : g.racks) {
      auto attrs = effective(r.zone, g.local);
      for (auto &[k, v] : attrs.items())
        configured.insert(k);
    }
    for (auto &definition : g.catalog) {
      auto key = text(definition, "key");
      if (!flag(definition, "enabled", true))
        configured.erase(key);
    }
    auto rows = array(request, "sku_rows");
    if (rows.empty())
      throw std::runtime_error(
          "Load demand and calculate stock requirements first");
    std::set<std::string> skus;
    for (auto row : rows) {
      auto sku = text(row, "sku");
      if (sku.empty() || !skus.insert(sku).second)
        throw std::runtime_error("SKU identifiers must be nonempty and unique");
      if (!row.contains("required_slots") || empty(row["required_slots"]) ||
          !row.contains("total_slotted_ea") || empty(row["total_slotted_ea"]) ||
          !row.contains("total_required_ea") || empty(row["total_required_ea"]))
        throw std::runtime_error(
            "Stock requirements are missing or unresolved for SKU " + sku);
      double slotValue = num(row, "required_slots"),
             qtyValue = num(row, "total_slotted_ea"),
             target = num(row, "total_required_ea");
      if (slotValue < 0 || qtyValue < 0 || target < 0 ||
          (target == 0) != (slotValue == 0) || qtyValue + 1e-9 < target)
        throw std::runtime_error("Stock quantities are inconsistent for SKU " +
                                 sku);
      if (slotValue > 200000 || qtyValue > 1e9 || target > 1e9 ||
          num(row, "slots_per_unit", 1) > 256)
        throw std::runtime_error("Requested stock exceeds engine limits");
      int requiredSlots = int(std::ceil(slotValue)),
          span = std::max(1, int(std::ceil(num(row, "slots_per_unit", 1))));
      if (requiredSlots > 200000 || qtyValue > 1e9)
        throw std::runtime_error("Requested stock exceeds engine limits");
      if (requiredSlots == 0 || qtyValue == 0)
        continue;
      int count = (requiredSlots + span - 1) / span;
      int quantity = int(std::ceil(qtyValue));
      json req = object(row, "sku_requirements");
      for (auto &[k, v] : row.items())
        if (k.starts_with("req_") && !empty(v)) {
          auto key = k.substr(4);
          if (std::find(PHYSICAL.begin(), PHYSICAL.end(), key) !=
              PHYSICAL.end())
            req[key] = number(v);
          else if (v.is_boolean())
            req[key] = v;
          else {
            auto value = str(v);
            std::transform(value.begin(), value.end(), value.begin(),
                           ::tolower);
            if (value == "true" || value == "yes" || value == "1" ||
                value == "y")
              req[key] = true;
            else if (value == "false" || value == "no" || value == "0" ||
                     value == "n")
              req[key] = false;
            else
              throw std::runtime_error("Requirement " + key +
                                       " must be Boolean");
          }
        }
      for (auto &[key, v] : req.items()) {
        if (std::find(PHYSICAL.begin(), PHYSICAL.end(), key) !=
            PHYSICAL.end()) {
          double n = number(v);
          if (n < 0 || (key != PHYSICAL[3] && n == 0))
            throw std::runtime_error(
                "SKU dimensions must be positive and weight nonnegative");
          req[key] = n;
        } else if (!v.is_boolean())
          throw std::runtime_error("SKU attribute " + key + " must be Boolean");
      }
      auto p = profile(req, g);
      int width = std::max(3, int(std::to_string(count).size()));
      for (int i = 1; i <= count; ++i) {
        Load l;
        l.sku = sku;
        l.id = sku + "#Q" + padded(i, width);
        l.source = row;
        l.requirements = req;
        l.profile = p;
        l.quantity = quantity / count + (i <= quantity % count);
        l.weight = num(req, PHYSICAL[3]) * l.quantity;
        l.index = i;
        l.count = count;
        l.velocity = text(row, "velocity_class", "C");
        l.frequency = num(row, "pick_frequency");
        loads.push_back(l);
      }
      if (loads.size() > 200000)
        throw std::runtime_error(
            "At most 200,000 inventory loads may be generated");
    }
    auto classRank = [](const std::string &c) {
      return c == "A" ? 0 : c == "B" ? 1 : c == "C" ? 2 : 9;
    };
    std::stable_sort(
        loads.begin(), loads.end(), [&](const Load &a, const Load &b) {
          return std::tuple{classRank(a.velocity),
                            a.profile.enabled && a.profile.exception(),
                            -a.frequency, a.sku, a.index} <
                 std::tuple{classRank(b.velocity),
                            b.profile.enabled && b.profile.exception(),
                            -b.frequency, b.sku, b.index};
        });
    for (int i = 0; i < int(loads.size()); ++i)
      loads[i].abc_rank = i + 1;
    adjust_oversize_weight();
    std::set<std::tuple<std::string, int, int>> slotBuffers;
    for (auto &b : array(object(g.project, "storage_layout"), "buffers"))
      if (text(b, "buffer_level") == "slot")
        slotBuffers.insert({text(b, "grid_waypoint"), integer(b, "level", 0),
                            integer(b, "slot", 0)});
    int unit = 0;
    for (int ri = 0; ri < int(g.racks.size()); ++ri) {
      auto &r = g.racks[ri];
      for (int l = 1; l <= g.levels; ++l)
        for (int s = 1; s <= g.slots; ++s) {
          if (g.buffer_model && !g.amr && !slotBuffers.count({r.id, l, s}))
            continue;
          Cell c;
          c.rack = ri;
          c.level = l;
          c.slot = s;
          c.buffer = r.bay + (g.amr ? "" : "/L" + padded(l) + "/S" + padded(s));
          c.address = r.zone + "/" + r.aisle + "/" + r.bay + "/L" + padded(l) +
                      "/S" + padded(s);
          c.static_address = g.buffer_model
                                 ? r.zone + "/" + r.aisle + "/" + c.buffer
                                 : c.address;
          c.unit = (g.amr                    ? "SHELF_"
                    : g.handling == "Pallet" ? "PALLET_"
                                             : "TOTE_") +
                   padded(g.amr ? r.rank : ++unit, 3);
          c.dynamic =
              g.amr ? c.unit + "/L" + padded(l) + "/S" + padded(s) : c.unit;
          c.attrs = effective(c.address, g.local);
          lookup[{ri, l, s}] = int(cells.size());
          cells.push_back(c);
          zoneCapacity[r.zone] += 1;
        }
    }
    if (cells.size() > 1000000)
      throw std::runtime_error(
          "At most one million storage cells are supported");
    owner.assign(cells.size(), -1);
    reserved_owner = owner;
    states.resize(g.racks.size());
  }

  void adjust_oversize_weight() {
    if (!configured.count(PHYSICAL[3]))
      return;
    std::set<std::string> zones;
    for (auto &r : g.racks)
      zones.insert(r.zone);
    for (auto &zone : zones) {
      auto attrs = effective(zone, g.local);
      if (!flag(attrs, "oversize_capable"))
        continue;
      double maximum = cap(attrs, PHYSICAL[3]);
      for (auto &l : loads)
        if (booleans_fit(l, attrs))
          maximum = std::max(maximum, num(l.requirements, PHYSICAL[3]));
      if (std::isfinite(maximum) && maximum > cap(attrs, PHYSICAL[3]))
        g.local[zone][PHYSICAL[3]] = maximum;
    }
  }
  bool booleans_fit(const Load &l, const json &attrs) const {
    for (auto &[key, value] : l.requirements.items())
      if (value.is_boolean() && configured.count(key)) {
        bool enabled = true;
        for (auto &d : g.catalog)
          if (text(d, "key") == key)
            enabled = flag(d, "enabled", true);
        if (enabled && flag(attrs, key) != value.get<bool>())
          return false;
      }
    return true;
  }
  std::vector<int> candidate(int li, int cell, bool check_occupied = true,
                             bool check_reservations = true) const {
    const auto &l = loads[li];
    const auto &c = cells[cell];
    const auto &attrs = c.attrs;
    const auto &p = l.profile;
    if (check_occupied && owner[cell] >= 0)
      return {};
    if (check_reservations && reserved_owner[cell] >= 0 &&
        reserved_owner[cell] != li)
      return {};
    if (l.reserved && check_reservations && !l.cells.empty() &&
        cell != l.cells.front())
      return {};
    if (!booleans_fit(l, attrs))
      return {};
    if (p.reserve() && !flag(attrs, "oversize_capable"))
      return {};
    if (p.overweight() && !p.weight_disabled &&
        c.level != std::min(2, g.levels))
      return {};
    if (configured.count(PHYSICAL[3]) &&
        num(l.requirements, PHYSICAL[3]) > cap(attrs, PHYSICAL[3]) + 1e-9)
      return {};
    if (states[c.rack].weight + l.weight > rackMax + 1e-9 && check_occupied)
      return {};
    int ls = 1, ss = 1;
    bool completeDimensions = true;
    for (int i = 0; i < 3; ++i)
      completeDimensions =
          completeDimensions && num(l.requirements, PHYSICAL[i]) > 0;
    if (p.enabled && completeDimensions) {
      auto fp = footprint(l.requirements, attrs, g.levels - c.level + 1,
                          g.slots - c.slot + 1);
      ls = fp.first;
      ss = fp.second;
      if (!ls || !ss)
        return {};
      // The legacy engine extends footprints only for known oversize stock.
      if (!p.volumetric && (ls > 1 || ss > 1))
        return {};
    }
    std::vector<int> result;
    for (int a = 0; a < ls; ++a)
      for (int b = 0; b < ss; ++b) {
        auto it = lookup.find({c.rack, c.level + a, c.slot + b});
        if (it == lookup.end())
          return {};
        int ci = it->second;
        if (check_occupied && owner[ci] >= 0)
          return {};
        if (check_reservations && reserved_owner[ci] >= 0 &&
            reserved_owner[ci] != li)
          return {};
        if (!booleans_fit(l, cells[ci].attrs))
          return {};
        for (int k = 0; k < 3; ++k)
          if (cap(cells[ci].attrs, PHYSICAL[k]) < cap(attrs, PHYSICAL[k]))
            return {};
        result.push_back(ci);
      }
    if (check_occupied && skuCounts.contains({l.sku, c.rack}) &&
        skuCounts.at({l.sku, c.rack}) + int(result.size()) > sameLimit)
      return {};
    if (int(result.size()) > sameLimit)
      return {};
    if (l.reserved && check_reservations && result != l.cells)
      return {};
    return result;
  }

  void reserve() {
    struct Record {
      int load, largest = 0;
      std::vector<std::vector<int>> choices;
    };
    std::vector<Record> records;
    for (int i = 0; i < int(loads.size()); ++i)
      if (loads[i].profile.reserve()) {
        Record record{i, 0, {}};
        for (int ci = 0; ci < int(cells.size()); ++ci) {
          auto fp = candidate(i, ci, false, false);
          if (!fp.empty()) {
            record.largest = std::max(record.largest, int(fp.size()));
            record.choices.push_back(fp);
          }
        }
        std::stable_sort(
            record.choices.begin(), record.choices.end(),
            [&](auto &a, auto &b) {
              auto &ca = cells[a[0]];
              auto &cb = cells[b[0]];
              return std::tuple{g.racks[ca.rack].distance,
                                g.racks[ca.rack].rank, ca.level, ca.slot} <
                     std::tuple{g.racks[cb.rack].distance,
                                g.racks[cb.rack].rank, cb.level, cb.slot};
            });
        if (!record.choices.empty())
          records.push_back(std::move(record));
      }
    std::vector<bool> used(cells.size());
    std::vector<int> rackUse(g.racks.size());
    std::map<std::pair<std::string, int>, int> counts;
    std::vector<int> chosen(records.size(), -1);
    auto viable = [&](int i, const std::vector<int> &fp) {
      for (int c : fp)
        if (used[c])
          return false;
      return counts[{loads[records[i].load].sku, cells[fp[0]].rack}] +
                 int(fp.size()) <=
             sameLimit;
    };
    auto priority = [&](int i, const std::vector<int> &fp) {
      const auto &l = loads[records[i].load];
      int rack = cells[fp[0]].rack;
      bool adjacent = false, sameLevel = false;
      for (size_t k = 0; k < records.size(); ++k)
        if (chosen[k] >= 0 && loads[records[k].load].sku == l.sku)
          for (int a : records[k].choices[chosen[k]])
            for (int b : fp) {
              auto &x = cells[a];
              auto &y = cells[b];
              if (x.rack == y.rack &&
                  std::abs(x.level - y.level) + std::abs(x.slot - y.slot) == 1)
                adjacent = true;
              if (x.rack == y.rack && x.level == y.level &&
                  std::abs(x.slot - y.slot) == 1)
                sameLevel = true;
            }
      return std::vector<int>{
          rackUse[rack] ? 0 : 1, l.count > 1 ? -counts[{l.sku, rack}] : 0,
          l.count > 1 ? -int(sameLevel) : 0, l.count > 1 ? -int(adjacent) : 0};
    };
    std::function<bool(int)> dfs = [&](int remaining) {
      ++reservationNodes;
      if (reservationNodes > 250000)
        return false;
      if (!remaining)
        return true;
      int selected = -1;
      std::tuple<int, int, int> key{1000000000, 0, 0};
      std::vector<int> choices;
      for (int i = 0; i < int(records.size()); ++i)
        if (chosen[i] < 0) {
          std::vector<int> valid;
          for (int j = 0; j < int(records[i].choices.size()); ++j)
            if (viable(i, records[i].choices[j]))
              valid.push_back(j);
          if (valid.empty())
            return false;
          auto rank = std::tuple{int(valid.size()), -records[i].largest,
                                 records[i].load};
          if (rank < key) {
            key = rank;
            selected = i;
            choices = std::move(valid);
          }
        }
      auto &record = records[selected];
      auto &l = loads[record.load];
      std::stable_sort(choices.begin(), choices.end(), [&](int a, int b) {
        return priority(selected, record.choices[a]) <
               priority(selected, record.choices[b]);
      });
      for (int j : choices) {
        auto &fp = record.choices[j];
        int rack = cells[fp[0]].rack;
        chosen[selected] = j;
        for (int c : fp)
          used[c] = true;
        rackUse[rack] += int(fp.size());
        counts[{l.sku, rack}] += int(fp.size());
        if (dfs(remaining - 1))
          return true;
        chosen[selected] = -1;
        for (int c : fp)
          used[c] = false;
        rackUse[rack] -= int(fp.size());
        counts[{l.sku, rack}] -= int(fp.size());
      }
      return false;
    };
    // shortcut: bounded search matches the legacy reservation budget;
    // infeasible large maps use a deterministic partial reservation.
    completePlan = dfs(int(records.size()));
    if (!completePlan) {
      std::fill(used.begin(), used.end(), false);
      counts.clear();
      std::fill(rackUse.begin(), rackUse.end(), 0);
      std::fill(chosen.begin(), chosen.end(), -1);
      std::vector<int> order(records.size());
      std::iota(order.begin(), order.end(), 0);
      std::stable_sort(order.begin(), order.end(), [&](int a, int b) {
        return std::tuple{records[a].choices.size(), -records[a].largest,
                          records[a].load} <
               std::tuple{records[b].choices.size(), -records[b].largest,
                          records[b].load};
      });
      for (int i : order) {
        int best = -1;
        for (int j = 0; j < int(records[i].choices.size()); ++j)
          if (viable(i, records[i].choices[j]) &&
              (best < 0 || priority(i, records[i].choices[j]) <
                               priority(i, records[i].choices[best])))
            best = j;
        if (best < 0)
          continue;
        chosen[i] = best;
        auto &fp = records[i].choices[best];
        for (int c : fp)
          used[c] = true;
        int rack = cells[fp[0]].rack;
        rackUse[rack] += int(fp.size());
        counts[{loads[records[i].load].sku, rack}] += int(fp.size());
      }
    }
    for (int i = 0; i < int(records.size()); ++i)
      if (chosen[i] >= 0) {
        auto &l = loads[records[i].load];
        l.cells = records[i].choices[chosen[i]];
        l.reserved = true;
        for (int c : l.cells)
          reserved_owner[c] = records[i].load;
      }
  }

  std::vector<double> rank(int li, const std::vector<int> &fp) const {
    const auto &l = loads[li];
    const auto &c = cells[fp[0]];
    const auto &r = g.racks[c.rack];
    const auto &state = states[c.rack];
    const auto &p = l.profile;
    std::string bucket = p.enabled ? p.kind : "STANDARD";
    bool exception = bucket != "STANDARD",
         onlyExceptions =
             !state.buckets.empty() && !state.buckets.count("STANDARD");
    int physicalMix =
        state.buckets.count(bucket) || (exception && onlyExceptions) ? 0
        : state.buckets.empty()                                      ? 1
                                                                     : 2;
    bool isOversize = bucket == "OVERSIZE" ||
                      bucket == "OVERSIZE_AND_OVERWEIGHT" ||
                      bucket == "UNVERIFIED_OVERSIZE";
    int oversizePenalty =
        isOversize && !p.overweight() && state.buckets.count("STANDARD")
            ? int(c.level != std::min(3, g.levels))
        : bucket == "STANDARD" && state.oversize
            ? int(c.level == std::min(3, g.levels))
            : 0;
    double middle = 0;
    for (int ci : fp)
      middle += cells[ci].level;
    middle /= fp.size();
    double ergo = ergonomics && num(l.requirements, PHYSICAL[3]) > 0
                      ? std::abs(middle - (g.levels + 1) / 2.)
                      : 0;
    bool adjacent = false;
    if (l.count > 1 && skuPositions.contains(l.sku))
      for (int ci : fp)
        for (int prior : skuPositions.at(l.sku))
          if (cells[prior].rack == c.rack &&
              cells[prior].level == cells[ci].level &&
              std::abs(cells[prior].slot - cells[ci].slot) == 1)
            adjacent = true;
    double waste = 0;
    bool complete = p.status == "COMPLETE";
    for (auto &k : PHYSICAL)
      complete = complete && c.attrs.contains(k) && !empty(c.attrs[k]);
    if (complete) {
      std::array<double, 3> item, storage;
      for (int i = 0; i < 3; ++i) {
        item[i] = num(l.requirements, PHYSICAL[i]);
        storage[i] = num(c.attrs, PHYSICAL[i]);
      }
      std::sort(item.begin(), item.end());
      std::sort(storage.begin(), storage.end());
      for (int i = 0; i < 3; ++i)
        waste += std::max(0., storage[i] - item[i]) / std::max(storage[i], 1.);
      waste += std::max(0., num(c.attrs, PHYSICAL[3]) -
                                num(l.requirements, PHYSICAL[3])) /
               std::max(num(c.attrs, PHYSICAL[3]), 1.);
    }
    return {double(p.overweight() && !p.weight_disabled &&
                   c.level != std::min(2, g.levels)),
            double(physicalMix),
            double(oversizePenalty),
            double(state.classes.empty() ? 1 : 0),
            double(!state.classes.empty() && !state.classes.count(l.velocity)),
            0,
            0,
            double(p.exception() != flag(c.attrs, "oversize_capable")),
            ergo,
            -double(adjacent),
            r.distance,
            double(r.rank),
            waste,
            double(c.level),
            double(c.slot)};
  }

  void put(int li, const std::vector<int> &footprint) {
    auto &load = loads[li];
    load.cells = footprint;
    if (footprint.empty())
      return;
    load.reason.clear();
    int rack = cells[footprint.front()].rack;
    auto &state = states[rack];
    state.classes.insert(load.velocity);
    state.buckets.insert(load.profile.enabled ? load.profile.kind : "STANDARD");
    state.weight += load.weight;
    state.oversize = state.oversize || load.profile.kind == "OVERSIZE" ||
                     load.profile.kind == "OVERSIZE_AND_OVERWEIGHT";
    for (int cell : footprint)
      owner[cell] = li;
    skuPositions[load.sku].insert(skuPositions[load.sku].end(),
                                  footprint.begin(), footprint.end());
    skuCounts[{load.sku, rack}] += int(footprint.size());
    zoneDemand[g.racks[rack].zone] +=
        load.frequency * load.quantity /
        num(load.source, "total_slotted_ea", load.quantity);
  }
  void reset(bool keep_anchors = false) {
    std::fill(owner.begin(), owner.end(), -1);
    states.assign(g.racks.size(), State{});
    skuCounts.clear();
    skuPositions.clear();
    zoneDemand.clear();
    for (auto &load : loads)
      if (!keep_anchors || !load.reserved)
        load.cells.clear();
  }
  void allocate() {
    reserve();
    for (int li = 0; li < int(loads.size()); ++li) {
      auto &l = loads[li];
      std::vector<std::vector<int>> candidates;
      bool sameRack = false;
      for (int ci = 0; ci < int(cells.size()); ++ci) {
        auto fp = candidate(li, ci);
        if (!fp.empty()) {
          sameRack = sameRack || skuCounts[{l.sku, cells[ci].rack}] > 0;
          candidates.push_back(std::move(fp));
        }
      }
      if (sameRack && l.count > 1)
        std::erase_if(candidates, [&](auto &fp) {
          return skuCounts[{l.sku, cells[fp[0]].rack}] == 0;
        });
      double loadDemand = l.frequency * l.quantity /
                          num(l.source, "total_slotted_ea", l.quantity);
      if (balance && !sameRack && !candidates.empty()) {
        double minimum = INF;
        for (auto &fp : candidates) {
          auto zone = g.racks[cells[fp[0]].rack].zone;
          minimum = std::min(minimum, (zoneDemand[zone] + loadDemand) /
                                          zoneCapacity[zone]);
        }
        std::erase_if(candidates, [&](auto &fp) {
          auto zone = g.racks[cells[fp[0]].rack].zone;
          double score = (zoneDemand[zone] + loadDemand) / zoneCapacity[zone];
          return std::abs(score - minimum) >
                 std::max(1e-12,
                          1e-12 * std::max(std::abs(score), std::abs(minimum)));
        });
      }
      if (candidates.empty()) {
        l.cells.clear();
        bool free = false, temperature = false, oversize = false;
        for (int ci = 0; ci < int(cells.size()); ++ci)
          if (owner[ci] < 0) {
            free = true;
            temperature = temperature || !l.requirements.contains("chilled") ||
                          flag(cells[ci].attrs, "chilled") ==
                              flag(l.requirements, "chilled");
            oversize = oversize || flag(cells[ci].attrs, "oversize_capable");
          }
        if (!free) {
          l.failure_status = "UNASSIGNED_NO_CAPACITY";
          l.reason = "All configured physical cells are occupied.";
        } else if (!temperature) {
          l.failure_status = flag(l.requirements, "chilled")
                                 ? "UNASSIGNED_NO_CHILLED_LOCATION"
                                 : "UNASSIGNED_NO_AMBIENT_LOCATION";
          l.reason = "No available cells have the required chilled/ambient "
                     "storage profile.";
        } else if (l.profile.reserve() && !oversize)
          l.reason = "This inventory requires an explicitly oversize-capable "
                     "zone, but none has available cells.";
        else if (l.weight > rackMax)
          l.reason = "This inventory load exceeds the whole-rack weight limit. "
                     "Recalculate stock using smaller loads.";
        else
          l.reason =
              "No available contiguous footprint satisfies the configured "
              "attributes, same-SKU cell limit and remaining rack weight. "
              "Review zone capacity or the stock requirement.";
        continue;
      }
      int best = 0;
      auto bestRank = rank(li, candidates[0]);
      for (int i = 1; i < int(candidates.size()); ++i) {
        auto key = rank(li, candidates[i]);
        if (key < bestRank) {
          bestRank = std::move(key);
          best = i;
        }
      }
      put(li, candidates[best]);
    }
  }

  json assignments(const std::string &strategy) const {
    json result = json::array();
    auto reachable = operational_routes(g);
    std::vector<double> finalWeights(g.racks.size());
    for (auto &l : loads)
      if (!l.cells.empty())
        finalWeights[cells[l.cells[0]].rack] += l.weight;
    for (auto &l : loads) {
      json row = {{"sku", l.sku},
                  {"inventory_load_id", l.id},
                  {"sku_rank", l.abc_rank},
                  {"abc_frequency_rank", l.abc_rank},
                  {"quantity_load_index", l.index},
                  {"quantity_load_count", l.count},
                  {"quantity_ea", l.quantity},
                  {"total_required_ea", num(l.source, "total_required_ea")},
                  {"total_slotted_ea", num(l.source, "total_slotted_ea")},
                  {"required_slot_count", num(l.source, "required_slots")},
                  {"units_per_slot", num(l.source, "units_per_slot")},
                  {"slots_per_unit", num(l.source, "slots_per_unit", 1)},
                  {"required_racks", num(l.source, "required_racks")},
                  {"velocity_class", l.velocity},
                  {"pick_frequency", l.frequency},
                  {"total_quantity_ea", num(l.source, "total_quantity_ea")},
                  {"active_days", num(l.source, "active_days")},
                  {"strategy", strategy},
                  {"sku_requirements", l.requirements},
                  {"physical_data_status", l.profile.status},
                  {"physical_storage_class", l.profile.kind},
                  {"physical_missing_fields", l.profile.missing},
                  {"constrained_feasibility_reserved", l.reserved},
                  {"inventory_load_weight_kg", l.weight},
                  {"occupied_slot_count", l.cells.size()},
                  {"assignment_status",
                   l.cells.empty() ? l.failure_status : "ASSIGNED"},
                  {"compatibility_status", l.cells.empty() ? "NOT_EVALUATED"
                                           : l.profile.status != "COMPLETE"
                                               ? "UNVERIFIED"
                                               : "COMPATIBLE"},
                  {"compatibility_issues",
                   l.reason.empty() ? json::array() : json::array({l.reason})},
                  {"occupied_handling_units", json::array()},
                  {"occupied_storage_location_addresses", json::array()},
                  {"occupied_static_addresses", json::array()},
                  {"occupied_buffer_ids", json::array()},
                  {"occupied_level_span", 0},
                  {"occupied_horizontal_slot_span", 0},
                  {"auto_attribute_overrides", json::object()}};
      for (int ci : l.cells) {
        auto &c = cells[ci];
        auto &r = g.racks[c.rack];
        row["occupied_handling_units"].push_back(
            {{"rack_id", r.id},
             {"handling_unit_id", c.unit},
             {"storage_level", c.level},
             {"storage_slot", c.slot},
             {"buffer_id", c.buffer},
             {"static_address", c.static_address},
             {"storage_location_address", c.address}});
        row["occupied_storage_location_addresses"].push_back(c.address);
        row["occupied_static_addresses"].push_back(c.static_address);
        row["occupied_buffer_ids"].push_back(c.buffer);
      }
      if (!l.cells.empty()) {
        auto &c = cells[l.cells[0]];
        auto &r = g.racks[c.rack];
        std::string stationText;
        for (auto &s : r.stations) {
          if (!stationText.empty())
            stationText += '|';
          stationText += s;
        }
        int minl = c.level, maxl = c.level, mins = c.slot, maxs = c.slot;
        for (int ci : l.cells) {
          minl = std::min(minl, cells[ci].level);
          maxl = std::max(maxl, cells[ci].level);
          mins = std::min(mins, cells[ci].slot);
          maxs = std::max(maxs, cells[ci].slot);
        }
        row.update(
            {{"rack_id", r.id},
             {"rack_waypoint", r.id},
             {"pickup_dispenser_id", r.endpoint},
             {"rack_vertex_index", r.node},
             {"rack_rank", r.rank},
             {"zone_id", r.zone},
             {"aisle_id", r.aisle},
             {"static_bay_id", r.bay},
             {"storage_level", c.level},
             {"storage_slot", c.slot},
             {"static_address", c.static_address},
             {"storage_location_address", c.address},
             {"buffer_id", c.buffer},
             {"buffer_level", g.amr ? "grid" : "slot"},
             {"dynamic_address", c.dynamic},
             {"occupied_dynamic_address", c.dynamic},
             {"dynamic_address_level", g.amr ? "shelf_slot" : "handling_unit"},
             {"handling_unit_id", c.unit},
             {"handling_unit_type", g.handling},
             {"rmf_grid_address", g.level + "/" + r.id},
             {"rack_cumulative_weight_kg", finalWeights[c.rack]},
             {"effective_location_attributes", c.attrs},
             {"storage_area_type",
              flag(c.attrs, "oversize_capable") ? "OVERSIZE" : "STANDARD"},
             {"planned_storage_type",
              flag(c.attrs, "oversize_capable") ? "OVERSIZE" : "STANDARD"},
             {"zone_storage_type",
              flag(c.attrs, "oversize_capable") ? "OVERSIZE" : "STANDARD"},
             {"planned_zone_id", r.zone},
             {"routing_status",
              reachable[c.rack] ? "REACHABLE" : "UNREACHABLE"},
             {"average_workstation_distance_m",
              std::isfinite(r.distance) ? json(r.distance) : json(nullptr)},
             {"workstations_evaluated", stationText},
             {"occupied_level_span", maxl - minl + 1},
             {"occupied_horizontal_slot_span", maxs - mins + 1}});
      } else {
        row["rack_id"] = "";
        row["storage_location_address"] = "";
        row["routing_status"] = "NOT_EVALUATED";
      }
      result.push_back(row);
    }
    return result;
  }
};

using Moves = std::vector<std::pair<int, std::vector<int>>>;
struct QuantityPlan {
  const Engine &e;
  std::vector<std::vector<int>> placements;
  std::vector<int> owners, empty_order;
  std::vector<bool> placed;
  std::vector<std::string> profiles;
  std::map<std::pair<std::string, int>, int> counts;
  std::vector<double> weights;
  QuantityPlan(const Engine &engine, bool same_level = true)
      : e(engine), owners(e.cells.size(), -1), placed(e.loads.size()),
        weights(e.g.racks.size()) {
    for (auto &cell : e.cells)
      profiles.push_back(
          (same_level ? std::to_string(cell.level) + "|" : "") +
          nlohmann::json(cell.attrs).dump() + "|" +
          (flag(cell.attrs, "oversize_capable") ? "OVERSIZE" : "STANDARD"));
    for (int i = 0; i < int(e.cells.size()); ++i)
      empty_order.push_back(i);
    std::stable_sort(empty_order.begin(), empty_order.end(), [&](int a, int b) {
      auto &ca = e.cells[a];
      auto &cb = e.cells[b];
      auto &ra = e.g.racks[ca.rack];
      auto &rb = e.g.racks[cb.rack];
      return std::tuple{ra.distance, ra.rank, ra.id, ca.level, ca.slot} <
             std::tuple{rb.distance, rb.rank, rb.id, cb.level, cb.slot};
    });
    for (int i = 0; i < int(e.loads.size()); ++i) {
      auto &load = e.loads[i];
      placements.push_back(load.cells);
      for (int cell : load.cells) {
        if (owners[cell] >= 0)
          throw std::runtime_error("Feasibility seed overlaps");
        owners[cell] = i;
        counts[{load.sku, e.cells[cell].rack}]++;
      }
      if (!load.cells.empty())
        weights[e.cells[load.cells[0]].rack] += load.weight;
    }
  }
  bool valid(const Moves &moves) const {
    std::map<std::pair<std::string, int>, int> delta;
    std::map<int, double> weight;
    for (auto &[load, cells] : moves) {
      for (int cell : placements[load])
        delta[{e.loads[load].sku, e.cells[cell].rack}]--;
      for (int cell : cells)
        delta[{e.loads[load].sku, e.cells[cell].rack}]++;
      if (!placements[load].empty())
        weight[e.cells[placements[load][0]].rack] -= e.loads[load].weight;
      if (!cells.empty())
        weight[e.cells[cells[0]].rack] += e.loads[load].weight;
    }
    for (auto &[key, value] : delta) {
      auto it = counts.find(key);
      if ((it == counts.end() ? 0 : it->second) + value > e.sameLimit)
        return false;
    }
    for (auto &[rack, value] : weight)
      if (weights[rack] + value > e.rackMax + 1e-9)
        return false;
    return true;
  }
  std::optional<Moves> propose(int load,
                               const std::vector<int> &desired) const {
    auto old = placements[load];
    if (desired == old)
      return Moves{};
    std::set<int> displaced;
    for (int cell : desired)
      if (owners[cell] >= 0 && owners[cell] != load)
        displaced.insert(owners[cell]);
    if (displaced.empty()) {
      Moves moves{{load, desired}};
      if (valid(moves))
        return moves;
      return std::nullopt;
    }
    if (desired.size() != 1 || old.size() > 1 || displaced.size() != 1)
      return std::nullopt;
    int other = *displaced.begin();
    auto &row = e.loads[other];
    if (placed[other] || placements[other].size() != 1 || row.reserved ||
        (row.profile.kind != "STANDARD" && row.profile.kind != "NOT_EVALUATED"))
      return std::nullopt;
    auto destinations = old;
    destinations.insert(destinations.end(), empty_order.begin(),
                        empty_order.end());
    for (int destination : destinations) {
      if (destination == desired[0] ||
          (owners[destination] >= 0 && owners[destination] != load) ||
          profiles[destination] != profiles[placements[other][0]])
        continue;
      Moves moves{{load, desired}, {other, {destination}}};
      if (valid(moves))
        return moves;
    }
    return std::nullopt;
  }
  void commit(int load, const Moves &moves) {
    for (auto &[changed, _] : moves) {
      for (int cell : placements[changed]) {
        owners[cell] = -1;
        counts[{e.loads[changed].sku, e.cells[cell].rack}]--;
      }
      if (!placements[changed].empty())
        weights[e.cells[placements[changed][0]].rack] -=
            e.loads[changed].weight;
    }
    for (auto &[changed, cells] : moves) {
      placements[changed] = cells;
      for (int cell : cells) {
        owners[cell] = changed;
        counts[{e.loads[changed].sku, e.cells[cell].rack}]++;
      }
      if (!cells.empty())
        weights[e.cells[cells[0]].rack] += e.loads[changed].weight;
    }
    placed[load] = true;
  }
};

static json pack_affinity(Engine &e, const AffinityData &data) {
  QuantityPlan plan(e);
  e.reset(true);
  auto order = data.placement_order(e.loads, e.g.levels * e.g.slots, e.g.amr);
  int R = int(e.g.racks.size()), N = int(data.names.size());
  std::vector<std::set<int>> rackSkus(R), skuRacks(N);
  std::vector<std::vector<double>> nearest(N, std::vector<double>(R, INF)),
      dist(R, std::vector<double>(R));
  double minX = INF, maxX = -INF, minY = INF, maxY = -INF;
  for (auto &rack : e.g.racks) {
    minX = std::min(minX, rack.x);
    maxX = std::max(maxX, rack.x);
    minY = std::min(minY, rack.y);
    maxY = std::max(maxY, rack.y);
  }
  double span = std::max(1e-12, std::hypot(maxX - minX, maxY - minY));
  for (int a = 0; a < R; ++a)
    for (int b = 0; b < R; ++b)
      dist[a][b] = std::hypot(e.g.racks[a].x - e.g.racks[b].x,
                              e.g.racks[a].y - e.g.racks[b].y);
  for (int li : order) {
    auto &load = e.loads[li];
    int sku = data.indices.at(load.sku);
    struct Candidate {
      std::vector<int> fp;
      std::vector<double> key, base;
    };
    std::vector<Candidate> candidates;
    bool sameRack = false;
    double demand = load.frequency * load.quantity /
                    num(load.source, "total_slotted_ea", load.quantity);
    for (int ci = 0; ci < int(e.cells.size()); ++ci) {
      auto fp = e.candidate(li, ci);
      if (fp.empty())
        continue;
      auto key = e.rank(li, fp);
      if (load.profile.kind == "STANDARD")
        key[4] = 0;
      auto base = key;
      key.erase(key.begin() + 9);
      candidates.push_back({std::move(fp), std::move(key), std::move(base)});
      sameRack = sameRack || e.skuCounts[{load.sku, e.cells[ci].rack}] > 0;
    }
    if (!e.g.amr && sameRack && load.count > 1)
      std::erase_if(candidates, [&](auto &c) {
        return e.skuCounts[{load.sku, e.cells[c.fp[0]].rack}] == 0;
      });
    if (!e.g.amr && e.balance && !sameRack && !candidates.empty()) {
      double minimum = INF;
      for (auto &c : candidates) {
        auto zone = e.g.racks[e.cells[c.fp[0]].rack].zone;
        minimum = std::min(minimum, (e.zoneDemand[zone] + demand) /
                                        e.zoneCapacity[zone]);
      }
      std::erase_if(candidates, [&](auto &c) {
        auto zone = e.g.racks[e.cells[c.fp[0]].rack].zone;
        double score = (e.zoneDemand[zone] + demand) / e.zoneCapacity[zone];
        return std::abs(score - minimum) >
               std::max(1e-12, 1e-12 * std::abs(minimum));
      });
    }
    std::map<int, std::optional<Moves>> proposals;
    auto feasible = [&](int ci) {
      if (!e.g.amr)
        return true;
      int cell = candidates[ci].fp[0];
      if (!proposals.count(cell))
        proposals[cell] = plan.propose(li, candidates[ci].fp);
      return proposals[cell].has_value();
    };
    std::vector<int> sorted(candidates.size());
    std::iota(sorted.begin(), sorted.end(), 0);
    std::stable_sort(sorted.begin(), sorted.end(), [&](int a, int b) {
      return candidates[a].base < candidates[b].base;
    });
    int baseline = -1;
    for (int c : sorted)
      if (feasible(c)) {
        baseline = c;
        break;
      }
    if (baseline < 0) {
      if (e.g.amr && !plan.placements[li].empty())
        throw std::runtime_error(
            "Affinity feasibility seed lost a reserved load: " + load.id);
      load.cells.clear();
      load.reason = "No compatible capacity remains after affinity packing";
      continue;
    }
    int selected = baseline;
    if (load.profile.kind == "STANDARD" && mask_count(data.masks[sku])) {
      std::vector<Mask> others(R, Mask(data.masks[sku].size()));
      for (int r = 0; r < R; ++r)
        for (int s : rackSkus[r])
          if (!e.g.amr || s != sku)
            mask_or(others[r], data.masks[s]);
      Mask unsatisfied = data.masks[sku], satisfied(unsatisfied.size());
      if (e.g.amr)
        for (int rack : skuRacks[sku])
          mask_or(satisfied, others[rack]);
      for (size_t w = 0; w < unsatisfied.size(); ++w)
        unsatisfied[w] &= ~satisfied[w];
      int denominator = std::max(1, mask_count(unsatisfied));
      std::vector<double> cost(R), neighborDistance(R);
      double weight = 0;
      for (auto &n : data.neighbors[sku])
        if (!skuRacks[n.sku].empty()) {
          weight += n.weight;
          for (int r = 0; r < R; ++r)
            neighborDistance[r] += n.weight * nearest[n.sku][r];
        }
      for (int r = 0; r < R; ++r) {
        cost[r] =
            1 - shared_count(unsatisfied, others[r]) / double(denominator);
        neighborDistance[r] = weight ? neighborDistance[r] / weight / span : 0;
      }
      auto signature = [](auto &key) {
        return std::vector<double>{key[0], key[1], key[2], key[5],
                                   key[6], key[7], key[8]};
      };
      auto physical = signature(candidates[baseline].key);
      double baselineDistance =
          e.g.racks[e.cells[candidates[baseline].fp[0]].rack].distance;
      std::vector<int> eligible;
      for (int i = 0; i < int(candidates.size()); ++i)
        if (signature(candidates[i].key) == physical &&
            (!std::isfinite(baselineDistance) ||
             std::isfinite(
                 e.g.racks[e.cells[candidates[i].fp[0]].rack].distance)))
          eligible.push_back(i);
      auto score = [&](int i) {
        int rack = e.cells[candidates[i].fp[0]].rack;
        auto key = std::vector<double>{cost[rack], neighborDistance[rack],
                                       e.g.racks[rack].distance};
        key.insert(key.end(), candidates[i].key.begin(),
                   candidates[i].key.end());
        key.push_back(i);
        return key;
      };
      std::stable_sort(eligible.begin(), eligible.end(),
                       [&](int a, int b) { return score(a) < score(b); });
      for (int c : eligible)
        if (feasible(c)) {
          selected = c;
          break;
        }
    }
    if (e.g.amr)
      plan.commit(li, *proposals.at(candidates[selected].fp[0]));
    e.put(li, candidates[selected].fp);
    int rack = e.cells[load.cells[0]].rack;
    rackSkus[rack].insert(sku);
    if (skuRacks[sku].insert(rack).second)
      for (int r = 0; r < R; ++r)
        nearest[sku][r] = std::min(nearest[sku][r], dist[r][rack]);
  }
  return {{"method", "complete_order_affinity_packing"},
          {"relationship_thresholds", data.recommendation}};
}

struct Replay {
  Engine &e;
  std::map<std::string, int> skuIds;
  std::vector<std::string> skuNames;
  struct Group {
    std::string station;
    std::vector<int> skus;
  };
  std::vector<Group> groups;
  std::map<std::string, std::vector<int>> rackOrder;
  std::map<std::string, std::vector<double>> travel;
  std::vector<std::vector<int>> memberships;
  std::vector<int> loadSku;
  struct Selection {
    int rack;
    std::vector<int> covered;
  };
  mutable std::vector<std::vector<Selection>> selections;
  mutable std::map<std::string, std::vector<int>> visitsByStation;
  json mapping;
  Replay(Engine &engine, const json &orders, json map)
      : e(engine), mapping(map) {
    for (auto &l : e.loads)
      if (!skuIds.count(l.sku)) {
        skuIds[l.sku] = int(skuNames.size());
        skuNames.push_back(l.sku);
      }
    for (auto &l : e.loads)
      loadSku.push_back(skuIds[l.sku]);
    auto start = text(e.settings, "start_date"),
         end = text(e.settings, "end_date");
    for (auto &b : array(orders, "baskets")) {
      auto date = text(b, "date");
      if ((!start.empty() && date < start) || (!end.empty() && date > end))
        continue;
      auto store = text(b, "store");
      if (!mapping.contains(store))
        throw std::runtime_error(
            "Store-to-workstation mapping is missing store " + store);
      auto station = text(mapping, store);
      if (!e.g.stations.count(station))
        throw std::runtime_error("Mapping references an unknown workstation: " +
                                 station);
      Group group;
      group.station = station;
      for (auto &s : array(b, "skus")) {
        auto sku = str(s);
        if (!skuIds.count(sku)) {
          skuIds[sku] = int(skuNames.size());
          skuNames.push_back(sku);
        }
        group.skus.push_back(skuIds[sku]);
      }
      std::sort(group.skus.begin(), group.skus.end());
      group.skus.erase(std::unique(group.skus.begin(), group.skus.end()),
                       group.skus.end());
      if (!group.skus.empty())
        groups.push_back(std::move(group));
    }
    if (groups.empty())
      throw std::runtime_error(
          "Affinity requires store/day order groups in the selected range");
    memberships.resize(skuNames.size());
    for (int gi = 0; gi < int(groups.size()); ++gi)
      for (int s : groups[gi].skus)
        memberships[s].push_back(gi);
    for (auto &[station, node] : e.g.stations) {
      auto forward = distances(e.g, node, false, e.g.amr),
           backward = distances(e.g, node, true, e.g.amr);
      std::vector<double> paths;
      std::vector<int> order;
      for (int i = 0; i < int(e.g.racks.size()); ++i) {
        auto &r = e.g.racks[i];
        paths.push_back(forward[r.node] + backward[r.node]);
        order.push_back(i);
      }
      auto &n = e.g.nodes[node];
      std::stable_sort(order.begin(), order.end(), [&](int a, int b) {
        auto &ra = e.g.racks[a];
        auto &rb = e.g.racks[b];
        return std::pair{std::hypot(n.x - ra.x, n.y - ra.y), ra.id} <
               std::pair{std::hypot(n.x - rb.x, n.y - rb.y), rb.id};
      });
      rackOrder[station] = order;
      travel[station] = paths;
    }
  }
  json evaluate(bool record = false) const {
    if (record) {
      selections.assign(groups.size(), {});
      visitsByStation.clear();
      for (auto &[station, _] : e.g.stations)
        visitsByStation[station] = std::vector<int>(e.g.racks.size());
    }
    std::vector<std::set<int>> contents(e.g.racks.size());
    std::vector<std::vector<int>> sources(skuNames.size());
    for (int i = 0; i < int(e.loads.size()); ++i)
      if (!e.loads[i].cells.empty())
        contents[e.cells[e.loads[i].cells[0]].rack].insert(loadSku[i]);
    for (int r = 0; r < int(contents.size()); ++r)
      for (int s : contents[r])
        sources[s].push_back(r);
    int deliveries = 0, uncovered = 0;
    double metres = 0;
    std::map<std::string, double> zones, stationWork;
    std::map<std::string, int> visits, capacity;
    for (auto &r : e.g.racks)
      ++capacity[r.zone];
    for (size_t gi = 0; gi < groups.size(); ++gi) {
      const auto &group = groups[gi];
      auto present = [&](int best, const std::vector<int> &covered) {
        ++deliveries;
        auto &rack = e.g.racks[best];
        double path = travel.at(group.station)[best];
        metres += path;
        double work = path / 1.5 + 18;
        zones[rack.zone] += work;
        stationWork[group.station] += work;
        ++visits[rack.id];
        if (record) {
          selections[gi].push_back({best, covered});
          visitsByStation[group.station][best]++;
        }
      };
      if (!e.g.amr) {
        bool missing = false;
        for (int sku : group.skus) {
          int best = -1;
          for (int rack : rackOrder.at(group.station))
            if (contents[rack].count(sku) &&
                std::isfinite(travel.at(group.station)[rack])) {
              best = rack;
              break;
            }
          if (best < 0)
            missing = true;
          else
            present(best, {sku});
        }
        uncovered += missing;
        continue;
      }
      std::vector<int> counts(contents.size());
      std::vector<bool> pending(skuNames.size());
      int remaining = int(group.skus.size());
      for (int s : group.skus) {
        pending[s] = true;
        for (int r : sources[s])
          if (std::isfinite(travel.at(group.station)[r]))
            ++counts[r];
      }
      while (remaining) {
        int best = -1, count = 0;
        for (int r : rackOrder.at(group.station))
          if (counts[r] > count) {
            count = counts[r];
            best = r;
          }
        if (best < 0) {
          ++uncovered;
          break;
        }
        std::vector<int> covered;
        for (int sku : contents[best])
          if (pending[sku])
            covered.push_back(sku);
        present(best, covered);
        for (int s : contents[best])
          if (pending[s]) {
            pending[s] = false;
            --remaining;
            for (int r : sources[s])
              if (counts[r] > 0)
                --counts[r];
          }
      }
    }
    double maximum = 0, total = 0;
    json normalized = json::object(), work = json::object();
    for (auto &[zone, count] : capacity) {
      maximum = std::max(maximum, zones[zone] / count);
      total += zones[zone];
      normalized[zone] = zones[zone] / count;
      work[zone] = zones[zone];
    }
    double peakStation = 0;
    for (auto &[_, work] : stationWork)
      peakStation = std::max(peakStation, work);
    return {{"deliveries", deliveries},
            {"uncovered_groups", uncovered},
            {"round_trip_distance_m", metres},
            {"total_estimated_work_s", total},
            {"maximum_zone_work_s_per_rack", maximum},
            {"peak_station_work_s", peakStation},
            {"station_work_s", stationWork},
            {"zone_work_s", work},
            {"normalized_zone_work_s_per_rack", normalized},
            {"rack_visit_counts", visits},
            {"group_count", groups.size()},
            {"model", e.g.amr ? "maximum_distinct_sku_coverage_fixed_stations_"
                                "no_inventory_depletion"
                              : "single_sku_tote_presentations_fixed_stations_"
                                "no_inventory_depletion"}};
  }
};

#include "refinement.hpp"

static json validation(Engine &e, const json &rows) {
  json errors = json::array(), warnings = json::array();
  auto reachable = operational_routes(e.g);
  std::set<std::string> ids, occupied;
  std::map<std::string, double> quantity, weights;
  std::map<std::pair<std::string, std::string>, int> counts;
  int unassigned = 0, unverified = 0, unreachable = 0;
  std::map<std::string, int> loadLookup;
  for (int i = 0; i < int(e.loads.size()); ++i)
    loadLookup[e.loads[i].id] = i;
  for (auto &row : rows) {
    auto id = text(row, "inventory_load_id");
    if (id.empty() || !ids.insert(id).second)
      errors.push_back("Duplicate or empty inventory load ID: " + id);
    if (!loadLookup.count(id)) {
      errors.push_back("Unexpected inventory load: " + id);
      continue;
    }
    auto &l = e.loads[loadLookup[id]];
    if (text(row, "sku") != l.sku ||
        std::abs(num(row, "quantity_ea") - l.quantity) > 1e-9)
      errors.push_back("SKU or quantity changed for " + id);
    if (text(row, "assignment_status") != "ASSIGNED") {
      ++unassigned;
      continue;
    }
    auto rack = text(row, "rack_id");
    quantity[l.sku] += num(row, "quantity_ea");
    weights[rack] += l.weight;
    if (l.profile.status != "COMPLETE")
      ++unverified;
    auto units = array(row, "occupied_handling_units");
    if (units.empty())
      errors.push_back("Assigned load has no physical footprint: " + id);
    int rackIndex = -1;
    for (int i = 0; i < int(e.g.racks.size()); ++i)
      if (e.g.racks[i].id == rack)
        rackIndex = i;
    if (rackIndex >= 0 && !reachable[rackIndex])
      ++unreachable;
    std::vector<int> fp;
    for (auto &unit : units) {
      auto address = text(unit, "storage_location_address");
      if (!occupied.insert(address).second)
        errors.push_back("Overlapping storage cell: " + address);
      auto key = std::tuple{rackIndex, integer(unit, "storage_level", 0),
                            integer(unit, "storage_slot", 0)};
      if (!e.lookup.count(key)) {
        errors.push_back("Unknown storage cell in " + id);
        continue;
      }
      int ci = e.lookup[key];
      if (e.cells[ci].address != address || text(unit, "rack_id") != rack)
        errors.push_back("Cell address does not match its coordinates: " + id);
      fp.push_back(ci);
    }
    counts[{l.sku, rack}] += int(units.size());
    if (!fp.empty()) {
      if (text(row, "storage_location_address") != e.cells[fp.front()].address)
        errors.push_back(
            "Anchor address does not match the physical footprint: " + id);
      json addresses = json::array();
      for (int ci : fp)
        addresses.push_back(e.cells[ci].address);
      if (row.contains("occupied_storage_location_addresses") &&
          row["occupied_storage_location_addresses"] != addresses)
        errors.push_back(
            "Occupied address list does not match the physical footprint: " +
            id);
      auto originalCells = l.cells;
      bool originalReserved = l.reserved;
      l.reserved = false;
      l.cells.clear();
      auto expected = e.candidate(loadLookup[id], fp.front(), false, false);
      l.cells = originalCells;
      l.reserved = originalReserved;
      if (expected != fp)
        errors.push_back("Physical or attribute constraints failed for " + id);
    }
  }
  if (ids.size() != e.loads.size())
    errors.push_back("Inventory load count differs from the requested stock");
  for (auto &[key, count] : counts)
    if (count > e.sameLimit)
      errors.push_back("Same-SKU cell limit exceeded: " + key.first + " on " +
                       key.second);
  for (auto &[rack, weight] : weights)
    if (weight > e.rackMax + 1e-9)
      errors.push_back("Whole-rack weight exceeded: " + rack);
  if (unassigned)
    warnings.push_back(std::to_string(unassigned) +
                       " inventory loads remain unassigned; this layout does "
                       "not cover all stock");
  if (unverified)
    warnings.push_back(std::to_string(unverified) +
                       " assigned loads have incomplete physical data; their "
                       "physical fit is unverified");
  if (unreachable)
    warnings.push_back(std::to_string(unreachable) +
                       " assigned loads cannot reach every workstation");
  return {{"passed", errors.empty()},
          {"complete", unassigned == 0},
          {"physical_fit_verified", unverified == 0},
          {"error_count", errors.size()},
          {"errors", errors},
          {"warnings", warnings},
          {"unassigned_loads", unassigned},
          {"unverified_loads", unverified},
          {"unreachable_loads", unreachable},
          {"checked_loads", rows.size()},
          {"unique_occupied_cells", occupied.size()},
          {"checks",
           {"inventory_identity", "quantity_conservation",
            "unique_physical_cells", "attribute_compatibility",
            "contiguous_footprints", "same_sku_physical_cell_limit",
            "whole_rack_weight", "routing_reachability"}}};
}

json validate_layout(const json &request) {
  Engine e(request);
  return validation(e, array(request, "assignments"));
}

json score_layout(const json &request) {
  Engine e(request);
  auto rows = array(request, "assignments");
  auto report = validation(e, rows);
  if (!flag(report, "passed"))
    throw std::runtime_error("Cannot score an invalid layout: " +
                             report["errors"].dump());
  std::map<std::string, int> index;
  for (int i = 0; i < int(e.loads.size()); ++i)
    index[e.loads[i].id] = i;
  std::map<std::string, int> racks;
  for (int i = 0; i < int(e.g.racks.size()); ++i)
    racks[e.g.racks[i].id] = i;
  for (auto &row : rows)
    if (text(row, "assignment_status") == "ASSIGNED")
      for (auto &unit : array(row, "occupied_handling_units"))
        e.loads[index.at(text(row, "inventory_load_id"))].cells.push_back(
            e.lookup.at({racks.at(text(unit, "rack_id")),
                         integer(unit, "storage_level", 1),
                         integer(unit, "storage_slot", 1)}));
  auto mapping = object(e.settings, "store_workstation_mapping");
  if (mapping.empty() && e.g.stations.size() == 1)
    for (auto &basket : array(object(request, "orders"), "baskets"))
      mapping[text(basket, "store")] = e.g.stations.begin()->first;
  Replay replay(e, object(request, "orders"), mapping);
  return {{"validation", report}, {"score", replay.evaluate()}};
}

static json layout_result(Engine &e, const json &request,
                          const std::string &strategy, const json &affinity,
                          std::chrono::steady_clock::time_point start) {
  auto rows = e.assignments(strategy);
  for (auto &[zone, _] : e.zoneCapacity)
    e.zoneDemand[zone] = 0;
  for (auto &load : e.loads)
    if (!load.cells.empty())
      e.zoneDemand[e.g.racks[e.cells[load.cells[0]].rack].zone] +=
          load.frequency * load.quantity /
          num(load.source, "total_slotted_ea", load.quantity);
  std::map<std::tuple<std::string, int, int>, json> slotGeometry;
  for (auto &slot : array(object(e.g.project, "storage_layout"), "slots"))
    slotGeometry[{text(slot, "rack_id"), integer(slot, "level", 1),
                  integer(slot, "slot", 1)}] = slot;
  std::map<std::string, std::set<std::string>> occupiedBuffers;
  for (auto &row : rows)
    if (text(row, "assignment_status") == "ASSIGNED") {
      auto copyGeometry = [&](json &unit) {
        auto key =
            std::tuple{text(unit, "rack_id"), integer(unit, "storage_level", 1),
                       integer(unit, "storage_slot", 1)};
        if (slotGeometry.count(key))
          for (auto name : {"center_x", "center_y", "center_z", "length",
                            "width", "height"})
            if (slotGeometry[key].contains(name))
              unit[name] = slotGeometry[key][name];
      };
      copyGeometry(row);
      for (auto &unit : row["occupied_handling_units"]) {
        copyGeometry(unit);
        occupiedBuffers[text(unit, "buffer_id")].insert(
            text(unit, "handling_unit_id"));
      }
    }
  json bufferRecords = json::array();
  for (auto buffer : array(object(e.g.project, "storage_layout"), "buffers")) {
    auto id = text(buffer, "buffer_id");
    buffer["status"] = occupiedBuffers.count(id) ? "OCCUPIED" : "EMPTY";
    buffer["handling_unit_ids"] = occupiedBuffers[id];
    bufferRecords.push_back(buffer);
  }
  auto report = validation(e, rows);
  if (strategy != "basic") {
    int uncovered = int(num(object(affinity, "after"), "uncovered_groups"));
    report["uncovered_order_groups"] = uncovered;
    if (uncovered)
      report["warnings"].push_back(
          std::to_string(uncovered) +
          " store/day order groups cannot be fully served from their assigned "
          "workstation in the static route model");
  }
  if (!report["passed"].get<bool>())
    throw std::runtime_error("Generated layout failed internal validation: " +
                             report["errors"].dump());
  int assigned = 0, occupied = 0, unknown = 0;
  std::set<std::string> used;
  std::map<std::string, int> assignedSkus;
  double serviceDistance = 0, totalQuantity = 0;
  for (auto &row : rows)
    if (text(row, "assignment_status") == "ASSIGNED") {
      ++assigned;
      occupied += int(num(row, "occupied_slot_count"));
      used.insert(text(row, "rack_id"));
      assignedSkus[text(row, "sku")]++;
      unknown += text(row, "physical_data_status") == "MISSING";
      if (!empty(row["average_workstation_distance_m"])) {
        serviceDistance += num(row, "average_workstation_distance_m") *
                           num(row, "quantity_ea");
        totalQuantity += num(row, "quantity_ea");
      }
    }
  json zoneWork = json::object();
  for (auto &[zone, capacity] : e.zoneCapacity)
    zoneWork[zone] = {{"capacity", capacity},
                      {"demand", e.zoneDemand[zone]},
                      {"normalized_demand", e.zoneDemand[zone] / capacity}};
  double milliseconds = std::chrono::duration<double, std::milli>(
                            std::chrono::steady_clock::now() - start)
                            .count();
  json summary = {{"strategy", strategy},
                  {"engine", "C++20"},
                  {"algorithm_version",
                   strategy == "basic" ? "native_basic/v1"
                                       : "native_complete_order_affinity/v1"},
                  {"inventory_load_count", rows.size()},
                  {"assigned_count", assigned},
                  {"assigned_load_count", assigned},
                  {"unassigned_count", rows.size() - assigned},
                  {"unassigned_load_count", rows.size() - assigned},
                  {"occupied_slot_count", occupied},
                  {"capacity", e.cells.size()},
                  {"rack_count", e.g.racks.size()},
                  {"final_occupied_rack_count", used.size()},
                  {"workstation_count", e.g.stations.size()},
                  {"sku_count", array(request, "sku_rows").size()},
                  {"assigned_unverified_count", unknown},
                  {"maximum_same_sku_slots_per_rack", e.sameLimit},
                  {"zone_workload_enabled", e.balance},
                  {"zone_workload", zoneWork},
                  {"weighted_service_distance_m",
                   totalQuantity ? serviceDistance / totalQuantity : 0},
                  {"constrained_feasibility_plan_complete", e.completePlan},
                  {"constrained_feasibility_search_nodes", e.reservationNodes},
                  {"generation_ms", milliseconds},
                  {"affinity_pipeline", affinity}};
  return {{"schema", "inventory_slotting_layout/v2"},
          {"strategy", strategy},
          {"handling_unit_type", e.g.handling},
          {"rack_capacity",
           {{"levels", e.g.levels}, {"slots_per_level", e.g.slots}}},
          {"project", e.g.project},
          {"settings", e.settings},
          {"storage_layout", object(e.g.project, "storage_layout")},
          {"buffers", bufferRecords},
          {"sources", {{"application", "layout_generator"}}},
          {"zone_assignments", object(e.g.project, "zone_assignments")},
          {"attribute_catalog", e.g.catalog},
          {"location_attributes", e.g.local},
          {"building", e.g.building},
          {"storage_defaults", {{"standard", e.g.standard}}},
          {"affinity_configuration", affinity},
          {"assignments", rows},
          {"summary", summary},
          {"validation", report},
          {"operation_log", json::array()}};
}

json generate(const json &request) {
  auto start = std::chrono::steady_clock::now();
  Engine e(request);
  auto strategy = text(e.settings, "strategy", "basic");
  if (strategy != "basic" && strategy != "abc_affinity")
    throw std::runtime_error("Only Basic ABC and Affinity are supported; "
                             "traffic-aware slotting is deferred");
  e.allocate();
  json affinity = json::object();
  if (strategy == "abc_affinity") {
    auto orders = object(request, "orders"),
         mapping = object(e.settings, "store_workstation_mapping");
    if (mapping.empty() && e.g.stations.size() == 1)
      for (auto &basket : array(orders, "baskets"))
        mapping[text(basket, "store")] = e.g.stations.begin()->first;
    if (mapping.empty())
      throw std::runtime_error("Affinity with multiple workstations requires "
                               "explicit store-to-workstation assignments");
    Replay benchmark(e, orders, mapping);
    auto basicScore = benchmark.evaluate();
    std::vector<std::vector<int>> baseline;
    int assigned = 0;
    for (auto &load : e.loads) {
      baseline.push_back(load.cells);
      assigned += !load.cells.empty();
    }
    AffinityData data(orders, e.settings, e.loads);
    auto packing = pack_affinity(e, data);
    int after = 0;
    for (auto &load : e.loads)
      after += !load.cells.empty();
    if (after < assigned) {
      e.reset();
      for (int i = 0; i < int(e.loads.size()); ++i)
        e.put(i, baseline[i]);
      packing["status"] = "basic_retained_assignment_coverage";
    }
    json zoneBalance = json::object();
    if (e.g.amr && e.balance)
      zoneBalance = balance_groups(e, benchmark);
    auto packed = benchmark.evaluate();
    affinity = refine(e, orders, mapping);
    affinity["basic"] = basicScore;
    affinity["refinement_before"] = affinity["before"];
    affinity["before"] = basicScore;
    affinity["packing"] = packing;
    affinity["packing_score"] = packed;
    affinity["zone_balance"] = zoneBalance;
  }
  return layout_result(e, request, strategy, affinity, start);
}

json swap_layout(const json &request) {
  auto start = std::chrono::steady_clock::now();
  Engine e(request);
  auto source = object(request, "layout"), rows = array(source, "assignments");
  auto before = validation(e, rows);
  if (!flag(before, "passed"))
    throw std::runtime_error("The source layout is invalid: " +
                             before["errors"].dump());
  std::map<std::string, int> loads, racks;
  for (int i = 0; i < int(e.loads.size()); ++i)
    loads[e.loads[i].id] = i;
  for (int i = 0; i < int(e.g.racks.size()); ++i)
    racks[e.g.racks[i].id] = i;
  std::map<std::string, std::string> shelfAt;
  for (auto &row : rows) {
    auto &load = e.loads.at(loads.at(text(row, "inventory_load_id")));
    load.cells.clear();
    load.reserved = false;
    load.failure_status = text(row, "assignment_status");
    load.reason = array(row, "compatibility_issues").empty()
                      ? ""
                      : str(row["compatibility_issues"][0]);
    if (text(row, "assignment_status") != "ASSIGNED")
      continue;
    auto rack = text(row, "rack_id"), shelf = text(row, "handling_unit_id");
    if (e.g.amr && shelfAt.count(rack) && shelfAt[rack] != shelf)
      throw std::runtime_error("Rack contains multiple shelf identities: " +
                               rack);
    shelfAt[rack] = shelf;
    for (auto &unit : array(row, "occupied_handling_units"))
      load.cells.push_back(
          e.lookup.at({racks.at(rack), integer(unit, "storage_level", 1),
                       integer(unit, "storage_slot", 1)}));
  }
  const auto mode = text(request, "mode"), first = text(request, "source"),
             second = text(request, "target");
  if (first.empty() || second.empty() || first == second)
    throw std::runtime_error("Select two different positions to swap");
  if (mode == "load") {
    if (!loads.count(first) || !loads.count(second))
      throw std::runtime_error("Inventory load was not found");
    auto &a = e.loads[loads[first]], &b = e.loads[loads[second]];
    if (a.cells.size() != 1 || b.cells.size() != 1)
      throw std::runtime_error(
          "SKU swaps require two assigned single-cell loads. Use a whole-shelf "
          "swap for multi-cell inventory.");
    if (a.sku == b.sku)
      throw std::runtime_error("Select loads from two different SKUs");
    std::swap(a.cells, b.cells);
  } else if (mode == "shelf") {
    if (!e.g.amr)
      throw std::runtime_error(
          "Whole-shelf swaps are only available for AMR layouts");
    if (!racks.count(first) || !racks.count(second) || !shelfAt.count(first) ||
        !shelfAt.count(second))
      throw std::runtime_error("Select two occupied rack locations");
    int a = racks[first], b = racks[second];
    for (auto &load : e.loads) {
      if (load.cells.empty())
        continue;
      int rack = e.cells[load.cells.front()].rack;
      if (rack != a && rack != b)
        continue;
      for (int &ci : load.cells) {
        const auto &cell = e.cells[ci];
        auto key = std::tuple{rack == a ? b : a, cell.level, cell.slot};
        if (!e.lookup.count(key))
          throw std::runtime_error(
              "The target rack does not contain every required storage cell");
        ci = e.lookup.at(key);
      }
    }
    std::swap(shelfAt[first], shelfAt[second]);
  } else
    throw std::runtime_error("Unknown swap type");
  auto result = layout_result(e, request, text(source, "strategy", "basic"),
                              json::object(), start);
  // Shelf identity moves with its contents; static addresses belong to the
  // destination.
  if (e.g.amr) {
    std::set<std::string> identities;
    for (auto &[rack, shelf] : shelfAt)
      if (shelf.empty() || !identities.insert(shelf).second)
        throw std::runtime_error(
            "Shelf identities must be nonempty and unique");
    for (auto &row : result["assignments"]) {
      auto rack = text(row, "rack_id");
      if (text(row, "assignment_status") != "ASSIGNED")
        continue;
      auto shelf = shelfAt.at(rack);
      row["handling_unit_id"] = shelf;
      row["dynamic_address"] = shelf + "/L" +
                               padded(integer(row, "storage_level", 1)) + "/S" +
                               padded(integer(row, "storage_slot", 1));
      row["occupied_dynamic_address"] = row["dynamic_address"];
      for (auto &unit : row["occupied_handling_units"])
        unit["handling_unit_id"] = shelf;
    }
    for (auto &buffer : result["buffers"]) {
      auto rack = text(buffer, "grid_waypoint");
      if (shelfAt.count(rack))
        buffer["handling_unit_ids"] = json::array({shelfAt[rack]});
    }
  }
  result["summary"]["algorithm_version"] = "manual_inventory_scenario/v1";
  result["operation_log"] = array(source, "operation_log");
  result["operation_log"].push_back(
      {{"type", mode},
       {"source", first},
       {"target", second},
       {"message", (mode == "load" ? "Swapped inventory loads: "
                                   : "Swapped whole shelves at: ") +
                       first + " ↔ " + second}});
  result["generated_at"] = source.value("generated_at", json());
  result["scenario"] = true;
  result["sources"] = object(source, "sources");
  return result;
}
} // namespace layout
