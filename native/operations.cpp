#include "affinity.hpp"

namespace layout {
json movement_metrics(const json &request) {
  auto rows = array(request, "assignments"),
       settings = object(request, "settings");
  const auto handling = text(request, "handling_unit_type", "AMR shelf");
  const bool amr = handling == "AMR shelf";
  const auto start = text(settings, "start_date"),
             end = text(settings, "end_date");
  if (!start.empty() && !end.empty() && start > end)
    throw std::runtime_error("Start date must be on or before end date");
  std::map<std::pair<std::string, std::string>, std::set<std::string>> groups;
  for (auto &basket : array(object(request, "orders"), "baskets")) {
    const auto date = text(basket, "date");
    if ((!start.empty() && date < start) || (!end.empty() && date > end))
      continue;
    auto &skus = groups[{date, text(basket, "store")}];
    for (auto &sku : array(basket, "skus"))
      skus.insert(str(sku));
  }
  if (groups.empty())
    throw std::runtime_error(
        "No store/day order groups in the selected date range");
  std::map<std::string, json> locations;
  std::map<std::string, int> visits;
  int unmatched = 0;
  std::set<std::string> known;
  for (auto &row : rows)
    if (text(row, "assignment_status") == "ASSIGNED")
      known.insert(text(row, "sku"));
  for (auto &[_, skus] : groups)
    if (std::any_of(skus.begin(), skus.end(),
                    [&](const auto &sku) { return !known.count(sku); }))
      ++unmatched;
  if (amr) {
    std::map<std::string, std::set<std::string>> contents;
    std::map<std::string, double> distance;
    for (auto &row : rows) {
      if (text(row, "assignment_status") != "ASSIGNED")
        continue;
      const auto rack = text(row, "rack_id");
      if (rack.empty())
        continue;
      contents[rack].insert(text(row, "sku"));
      distance[rack] =
          empty(row.value("average_workstation_distance_m", json()))
              ? INF
              : num(row, "average_workstation_distance_m");
      locations[rack] = {{"rack_id", rack},
                         {"handling_unit_id", text(row, "handling_unit_id")},
                         {"storage_level", row.value("storage_level", json())},
                         {"storage_slot", row.value("storage_slot", json())}};
    }
    std::vector<std::string> racks;
    for (auto &[rack, _] : contents)
      racks.push_back(rack);
    std::sort(racks.begin(), racks.end(), [&](auto &a, auto &b) {
      return std::pair{distance[a], a} < std::pair{distance[b], b};
    });
    std::map<std::string, int> indices;
    for (auto &sku : known)
      indices[sku] = int(indices.size());
    const size_t words = (indices.size() + 63) / 64;
    std::vector<std::vector<int>> rackSkus(racks.size()),
        sources(indices.size());
    for (int i = 0; i < int(racks.size()); ++i)
      for (auto &sku : contents[racks[i]]) {
        int id = indices.at(sku);
        rackSkus[i].push_back(id);
        sources[id].push_back(i);
      }
    std::map<Mask, std::vector<int>> cache;
    for (auto &[_, skus] : groups) {
      Mask required(words);
      std::vector<int> requested;
      for (auto &sku : skus)
        if (indices.count(sku)) {
          int id = indices.at(sku);
          required[id / 64] |= uint64_t(1) << (id % 64);
          requested.push_back(id);
        }
      if (!cache.count(required)) {
        auto pending = required;
        std::vector<int> selected, counts(racks.size());
        for (int id : requested)
          for (int rack : sources[id])
            ++counts[rack];
        int remaining = int(requested.size());
        while (remaining) {
          int maximum = 0, best = -1;
          for (int i = 0; i < int(racks.size()); ++i) {
            if (counts[i] > maximum) {
              maximum = counts[i];
              best = i;
            }
            if (maximum == remaining)
              break;
          }
          if (best < 0)
            break;
          selected.push_back(best);
          for (int id : rackSkus[best]) {
            auto bit = uint64_t(1) << (id % 64);
            if (!(pending[id / 64] & bit))
              continue;
            pending[id / 64] &= ~bit;
            --remaining;
            for (int rack : sources[id])
              --counts[rack];
          }
        }
        cache[required] = std::move(selected);
      }
      for (int rack : cache[required])
        ++visits[racks[rack]];
    }

  } else {
    struct Source {
      std::set<std::string> units;
      double weight = 0;
      int assigned = 0;
    };
    std::map<std::string, std::map<std::string, Source>> sources;
    for (auto &row : rows) {
      if (text(row, "assignment_status") != "ASSIGNED")
        continue;
      const auto primary = text(row, "handling_unit_id");
      if (primary.empty())
        continue;
      auto physical = array(row, "occupied_handling_units");
      if (physical.empty())
        physical.push_back(row);
      auto &source = sources[text(row, "sku")][primary];
      for (auto &unit : physical) {
        auto id = text(unit, "handling_unit_id");
        if (id.empty())
          continue;
        source.units.insert(id);
        if (!locations.count(id))
          locations[id] = unit;
      }
      const double quantity = num(row, "quantity_ea", 1);
      source.weight += std::max(0., quantity == 0 ? 1. : quantity);
    }
    std::map<std::string, std::set<int>> unitGroups;
    int groupIndex = 0;
    for (auto &[_, skus] : groups) {
      for (auto &sku : skus) {
        if (!sources.count(sku))
          continue;
        auto &choices = sources[sku];
        auto best = choices.end();
        double minimum = INF;
        for (auto it = choices.begin(); it != choices.end(); ++it) {
          if (it->second.units.empty())
            continue;
          double score = (it->second.assigned + 1.) /
                         (it->second.weight > 0 ? it->second.weight : 1.);
          if (score < minimum) {
            minimum = score;
            best = it;
          }
        }
        if (best == choices.end())
          continue;
        ++best->second.assigned;
        for (auto &unit : best->second.units)
          unitGroups[unit].insert(groupIndex);
      }
      ++groupIndex;
    }
    for (auto &[unit, membership] : unitGroups)
      visits[unit] = int(membership.size());
  }
  std::vector<std::string> ranked;
  int total = 0;
  for (auto &[unit, count] : visits)
    if (count) {
      ranked.push_back(unit);
      total += count;
    }
  std::sort(ranked.begin(), ranked.end(), [&](auto &a, auto &b) {
    return std::pair{-visits[a], a} < std::pair{-visits[b], b};
  });
  if (ranked.empty())
    throw std::runtime_error(
        "No assigned layout SKUs match the selected order history");
  json units = json::array(), rackVisits = json::object();
  int cumulative = 0, rank = 0;
  for (auto &unit : ranked) {
    double previous = double(cumulative) / total;
    cumulative += visits[unit];
    auto row = locations.at(unit);
    row.update({{"visit_rank", ++rank},
                {"visit_count", visits[unit]},
                {"visit_rate", double(visits[unit]) / groups.size()},
                {"visit_share", double(visits[unit]) / total},
                {"cumulative_visit_share", double(cumulative) / total},
                {"movement_class", previous < .8    ? "A"
                                   : previous < .95 ? "B"
                                                    : "C"}});
    auto rack = text(row, "rack_id");
    rackVisits[rack] = int(num(rackVisits, rack)) + visits[unit];
    units.push_back(row);
  }
  return {{"grouping", "Store ID + Date"},
          {"fulfillment_group_count", groups.size()},
          {"handling_unit_type", handling},
          {"ranking_level", amr ? "shelf" : "slot"},
          {"allocation_model", amr ? "amr_maximum_sku_coverage_all_racks_"
                                     "available_mean_station_distance"
                                   : "quantity_weighted_unit_visits"},
          {"total_handling_unit_visits", total},
          {"unit_count", units.size()},
          {"units", units},
          {"rack_visit_counts", rackVisits},
          {"groups_with_unassigned_skus", unmatched},
          {"start_date", start},
          {"end_date", end},
          {"estimate", true}};
}
} // namespace layout
