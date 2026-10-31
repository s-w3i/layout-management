#pragma once
// Internal refinement implementation, included after Engine and Replay.
using Placements = std::vector<std::vector<int>>;
static Placements placements(const Engine &e) {
  Placements p;
  for (auto &load : e.loads)
    p.push_back(load.cells);
  return p;
}
static void set_placements(Engine &e, const Placements &p) {
  for (size_t i = 0; i < p.size(); ++i)
    e.loads[i].cells = p[i];
}
static void rebuild(Engine &e) {
  auto p = placements(e);
  e.reset();
  std::fill(e.reserved_owner.begin(), e.reserved_owner.end(), -1);
  for (int i = 0; i < int(p.size()); ++i) {
    e.put(i, p[i]);
    if (e.loads[i].reserved)
      for (int cell : p[i])
        e.reserved_owner[cell] = i;
  }
}
static std::vector<std::string> rack_profiles(const Engine &e) {
  std::vector<std::string> result(e.g.racks.size());
  for (auto &cell : e.cells)
    result[cell.rack] +=
        std::to_string(cell.level) + "," + std::to_string(cell.slot) + ":" +
        nlohmann::json(cell.attrs).dump() + ":" +
        (flag(cell.attrs, "oversize_capable") ? "OVERSIZE" : "STANDARD") + ";";
  return result;
}
static Placements relocated(const Engine &e, const Placements &p,
                            const std::map<int, int> &mapping) {
  auto result = p;
  for (size_t li = 0; li < p.size(); ++li)
    if (!p[li].empty()) {
      int source = e.cells[p[li][0]].rack;
      auto target = mapping.find(source);
      if (target == mapping.end())
        continue;
      for (size_t j = 0; j < p[li].size(); ++j) {
        auto &cell = e.cells[p[li][j]];
        result[li][j] = e.lookup.at({target->second, cell.level, cell.slot});
      }
    }
  return result;
}
struct MeanWork {
  int presentations = 0, uncovered = 0;
  std::vector<int> visits;
  std::map<std::string, int> capacity;
  std::map<std::string, double> zone;
  double maximum = 0, total = 0;
  json json_value() const {
    json zones = json::object();
    for (auto &[id, count] : capacity)
      zones[id] = {{"rack_capacity", count},
                   {"estimated_work_seconds", zone.at(id)},
                   {"normalized_work_seconds", zone.at(id) / count}};
    return {{"total_presentations", presentations},
            {"uncovered_group_count", uncovered},
            {"estimated_work_seconds", total},
            {"maximum_normalized_zone_work_seconds", maximum},
            {"zones", zones}};
  }
};
static MeanWork mean_work(const Engine &e, const Replay &model) {
  int R = int(e.g.racks.size());
  MeanWork work;
  work.visits.resize(R);
  std::vector<std::set<int>> contents(R);
  std::vector<std::vector<int>> sources(model.skuNames.size());
  for (int i = 0; i < int(e.loads.size()); ++i)
    if (!e.loads[i].cells.empty())
      contents[e.cells[e.loads[i].cells[0]].rack].insert(model.loadSku[i]);
  for (int r = 0; r < R; ++r)
    for (int sku : contents[r])
      sources[sku].push_back(r);
  std::vector<int> order(R);
  std::iota(order.begin(), order.end(), 0);
  std::sort(order.begin(), order.end(), [&](int a, int b) {
    return std::pair{e.g.racks[a].distance, e.g.racks[a].id} <
           std::pair{e.g.racks[b].distance, e.g.racks[b].id};
  });
  for (auto &group : model.groups) {
    std::vector<bool> pending(model.skuNames.size());
    std::vector<int> counts(R);
    for (int sku : group.skus) {
      pending[sku] = true;
      for (int r : sources[sku])
        counts[r]++;
    }
    int remaining = int(group.skus.size());
    while (remaining) {
      int best = -1, count = 0;
      for (int r : order)
        if (counts[r] > count) {
          best = r;
          count = counts[r];
        }
      if (best < 0) {
        work.uncovered++;
        break;
      }
      work.visits[best]++;
      for (int sku : contents[best])
        if (pending[sku]) {
          pending[sku] = false;
          remaining--;
          for (int r : sources[sku])
            counts[r]--;
        }
    }
  }
  for (int r = 0; r < R; ++r) {
    auto &rack = e.g.racks[r];
    double seconds =
        work.visits[r] ? work.visits[r] * (2 * rack.distance / 1.5 + 18) : 0;
    work.presentations += work.visits[r];
    work.total += seconds;
    work.capacity[rack.zone]++;
    work.zone[rack.zone] += seconds;
  }
  for (auto &[zone, count] : work.capacity)
    work.maximum = std::max(work.maximum, work.zone[zone] / count);
  return work;
}
static bool mean_improves(const MeanWork &after, const MeanWork &before) {
  return after.uncovered <= before.uncovered &&
         after.presentations <= before.presentations &&
         std::pair{after.maximum, after.total} <
             std::pair{before.maximum, before.total};
}

static json balance_groups(Engine &e, const Replay &model) {
  auto original = placements(e);
  auto before = mean_work(e, model), after = before;
  auto signatures = rack_profiles(e);
  std::map<std::string, std::vector<int>> pools;
  std::set<int> occupied;
  for (auto &cells : original)
    if (!cells.empty())
      occupied.insert(e.cells[cells[0]].rack);
  for (int r = 0; r < int(e.g.racks.size()); ++r)
    pools[signatures[r]].push_back(r);
  auto compatible = pools;
  std::vector<int> sources(occupied.begin(), occupied.end());
  std::sort(sources.begin(), sources.end(), [&](int a, int b) {
    return std::tuple{pools[signatures[a]].size(), -before.visits[a],
                      e.g.racks[a].id} < std::tuple{pools[signatures[b]].size(),
                                                    -before.visits[b],
                                                    e.g.racks[b].id};
  });
  std::map<std::string, double> workloads;
  std::map<int, int> mapping;
  auto seconds = [&](int rack, int visits) {
    return visits ? visits * (2 * e.g.racks[rack].distance / 1.5 + 18) : 0.;
  };
  for (int source : sources) {
    auto &pool = pools[signatures[source]];
    auto key = [&](int rack) {
      auto zone = e.g.racks[rack].zone;
      double work = seconds(rack, before.visits[source]);
      return std::tuple{(workloads[zone] + work) / before.capacity[zone], work,
                        e.g.racks[rack].id};
    };
    auto best = std::min_element(pool.begin(), pool.end(),
                                 [&](int a, int b) { return key(a) < key(b); });
    int destination = *best;
    pool.erase(best);
    mapping[source] = destination;
    workloads[e.g.racks[destination].zone] +=
        seconds(destination, before.visits[source]);
  }
  auto proposed = relocated(e, original, mapping);
  set_placements(e, proposed);
  after = mean_work(e, model);
  bool accepted = mean_improves(after, before);
  std::string selected = accepted ? "whole_map" : "none";
  if (!accepted && !before.capacity.empty()) {
    std::string peak = before.capacity.begin()->first;
    for (auto &[zone, capacity] : before.capacity)
      if (before.zone[zone] / capacity >
          before.zone[peak] / before.capacity[peak])
        peak = zone;
    std::vector<int> peakSources;
    for (int source : occupied)
      if (e.g.racks[source].zone == peak)
        peakSources.push_back(source);
    std::sort(peakSources.begin(), peakSources.end(), [&](int a, int b) {
      return std::pair{-before.visits[a], e.g.racks[a].id} <
             std::pair{-before.visits[b], e.g.racks[b].id};
    });
    if (peakSources.size() > 8)
      peakSources.resize(8);
    using Score =
        std::tuple<double, double, double, std::string, std::string, int, int>;
    std::map<std::pair<int, std::string>, Score> bestPairs;
    for (int source : peakSources)
      for (int target : compatible[signatures[source]]) {
        auto a = e.g.racks[source].zone, b = e.g.racks[target].zone;
        if (a == b)
          continue;
        auto projected = before.zone;
        projected[a] += seconds(source, before.visits[target]) -
                        seconds(source, before.visits[source]);
        projected[b] += seconds(target, before.visits[source]) -
                        seconds(target, before.visits[target]);
        double maximum = 0, total = 0;
        for (auto &[zone, work] : projected) {
          maximum = std::max(maximum, work / before.capacity[zone]);
          total += work;
        }
        if (maximum >= before.maximum)
          continue;
        Score score{
            maximum,
            std::abs(e.g.racks[source].distance - e.g.racks[target].distance),
            total,
            e.g.racks[source].id,
            e.g.racks[target].id,
            source,
            target};
        auto pair = std::pair{source, b};
        if (!bestPairs.count(pair) || score < bestPairs[pair])
          bestPairs[pair] = score;
      }
    std::vector<Score> scores;
    for (auto &[_, score] : bestPairs)
      scores.push_back(score);
    std::sort(scores.begin(), scores.end());
    if (scores.size() > 8)
      scores.resize(8);
    for (auto &score : scores) {
      int source = std::get<5>(score), target = std::get<6>(score);
      std::map<int, int> swap{{source, target}};
      if (occupied.count(target))
        swap[target] = source;
      auto trial = relocated(e, original, swap);
      set_placements(e, trial);
      auto work = mean_work(e, model);
      if (mean_improves(work, before)) {
        accepted = true;
        selected = "verified_pair_swap";
        proposed = std::move(trial);
        after = work;
        mapping = swap;
        break;
      }
    }
  }
  set_placements(e, accepted ? proposed : original);
  rebuild(e);
  json moved = json::object();
  int count = 0;
  if (accepted)
    for (auto &[source, target] : mapping) {
      moved[e.g.racks[source].id] = e.g.racks[target].id;
      count += source != target;
    }
  return {{"method", "whole_rack_groups_equivalent_physical_profiles"},
          {"accepted", accepted},
          {"selected_proposal", selected},
          {"moved_rack_group_count", count},
          {"rack_mapping", moved},
          {"before", before.json_value()},
          {"after", (accepted ? after : before).json_value()}};
}
static std::vector<double> refinement_key(const json &score) {
  return {num(score, "deliveries"), num(score, "peak_station_work_s"),
          num(score, "round_trip_distance_m")};
}
static bool refinement_guard(const json &score, const json &baseline,
                             bool zone) {
  return num(score, "uncovered_groups") <= num(baseline, "uncovered_groups") &&
         num(score, "round_trip_distance_m") <=
             num(baseline, "round_trip_distance_m") + 1e-6 &&
         num(score, "peak_station_work_s") <=
             num(baseline, "peak_station_work_s") + 1e-6 &&
         (!zone || num(score, "maximum_zone_work_s_per_rack") <=
                       num(baseline, "maximum_zone_work_s_per_rack") + 1e-6);
}

static json refine(Engine &e, const json &orders, json mapping) {
  Replay model(e, orders, mapping);
  auto initial = placements(e);
  json baseline = model.evaluate(true), current = baseline;
  int rackMoves = 0, bundleMoves = 0, evaluated = 0;
  std::set<int> frozen;
  std::vector<bool> protectedLoads(e.loads.size());
  if (!e.g.amr)
    return {{"method", "complete_orders_fixed_stations_bundles/v1"},
            {"status", "slot_affinity_packing"},
            {"before", baseline},
            {"after", baseline},
            {"accepted_moves", 0},
            {"store_workstation_mapping", mapping}};
  for (auto &load : e.loads)
    if (!load.cells.empty() &&
        (load.profile.status != "COMPLETE" || load.profile.kind != "STANDARD" ||
         load.reserved || load.cells.size() != 1))
      frozen.insert(e.cells[load.cells[0]].rack);
  auto rackFrozen = frozen;
  for (int li = 0; li < int(e.loads.size()); ++li)
    if (!e.loads[li].cells.empty()) {
      int rack = e.cells[e.loads[li].cells[0]].rack;
      if (frozen.count(rack) || model.memberships[model.loadSku[li]].empty()) {
        protectedLoads[li] = true;
        rackFrozen.insert(rack);
      }
    }
  auto signatures = rack_profiles(e);
  std::map<std::string, std::vector<int>> pools;
  for (int rack = 0; rack < int(e.g.racks.size()); ++rack)
    if (!rackFrozen.count(rack))
      pools[signatures[rack]].push_back(rack);
  std::vector<std::pair<int, int>> pairs;
  for (auto &[_, pool] : pools) {
    std::sort(pool.begin(), pool.end(),
              [&](int a, int b) { return e.g.racks[a].id < e.g.racks[b].id; });
    for (size_t i = 0; i < pool.size(); ++i)
      for (size_t j = i + 1; j < pool.size(); ++j) {
        int a = pool[i], b = pool[j];
        bool reachable = true;
        for (auto &[station, travel] : model.travel)
          reachable =
              reachable && std::isfinite(travel[a]) && std::isfinite(travel[b]);
        if (reachable)
          pairs.push_back({a, b});
      }
  }
  if (num(baseline, "uncovered_groups") > 0)
    return {{"method", "complete_orders_fixed_stations_bundles/v1"},
            {"status", "baseline_retained_uncovered_orders"},
            {"before", baseline},
            {"after", baseline},
            {"accepted_moves", 0},
            {"store_workstation_mapping", mapping}};
  for (int iteration = 0; iteration < 8; ++iteration) {
    auto original = placements(e);
    auto score = [&](auto pair) {
      auto [a, b] = pair;
      double delta = 0;
      for (auto &[station, visits] : model.visitsByStation)
        delta += (visits[a] - visits[b]) *
                 (model.travel[station][b] - model.travel[station][a]);
      return std::tuple{delta, e.g.racks[a].id, e.g.racks[b].id};
    };
    auto ranked = pairs;
    std::sort(ranked.begin(), ranked.end(),
              [&](auto a, auto b) { return score(a) < score(b); });
    if (ranked.size() > 32)
      ranked.resize(32);
    std::optional<Placements> chosen;
    json best = current;
    for (auto [a, b] : ranked) {
      auto trial = relocated(e, original, {{a, b}, {b, a}});
      set_placements(e, trial);
      auto result = model.evaluate();
      evaluated++;
      if (refinement_guard(result, baseline, e.balance) &&
          refinement_key(result) < refinement_key(current) &&
          (!chosen || refinement_key(result) < refinement_key(best))) {
        chosen = std::move(trial);
        best = result;
      }
    }
    if (!chosen) {
      set_placements(e, original);
      break;
    }
    set_placements(e, *chosen);
    current = model.evaluate(true);
    rackMoves++;
  }
  for (int iteration = 0; iteration < 8; ++iteration) {
    auto original = placements(e);
    QuantityPlan plan(e, false);
    plan.placed = protectedLoads;
    std::erase_if(plan.empty_order,
                  [&](int cell) { return frozen.count(e.cells[cell].rack); });
    int R = int(e.g.racks.size());
    std::vector<std::set<int>> contents(R), locations(model.skuNames.size()),
        privateSkus(R);
    std::map<std::pair<int, int>, std::vector<int>> loads;
    for (int li = 0; li < int(e.loads.size()); ++li)
      if (!plan.placements[li].empty()) {
        int rack = e.cells[plan.placements[li][0]].rack,
            sku = model.loadSku[li];
        contents[rack].insert(sku);
        locations[sku].insert(rack);
        loads[{sku, rack}].push_back(li);
      }
    for (int sku = 0; sku < int(locations.size()); ++sku)
      if (locations[sku].size() == 1)
        privateSkus[*locations[sku].begin()].insert(sku);
    struct Event {
      int source, target, anchor, count = 0;
      std::vector<int> bundle;
    };
    std::vector<Event> events;
    std::map<std::tuple<int, int, std::vector<int>>, int> eventIndex;
    for (size_t gi = 0; gi < model.groups.size(); ++gi) {
      std::map<int, std::set<int>> requiredRacks;
      std::set<int> required(model.groups[gi].skus.begin(),
                             model.groups[gi].skus.end());
      for (int rack = 0; rack < R; ++rack)
        if (!frozen.count(rack))
          for (int sku : privateSkus[rack])
            if (required.count(sku))
              requiredRacks[rack].insert(sku);
      for (auto &selected : model.selections[gi]) {
        int source = selected.rack;
        if (requiredRacks.count(source) || frozen.count(source) ||
            selected.covered.size() < 2 || selected.covered.size() > 8)
          continue;
        auto bundle = selected.covered;
        std::sort(bundle.begin(), bundle.end(), [&](int a, int b) {
          return model.skuNames[a] < model.skuNames[b];
        });
        std::vector<int> targets;
        for (auto &[rack, _] : requiredRacks)
          targets.push_back(rack);
        auto overlap = [&](int rack) {
          int n = 0;
          for (int s : bundle)
            n += contents[rack].count(s);
          return n;
        };
        std::sort(targets.begin(), targets.end(), [&](int a, int b) {
          return std::pair{-overlap(a), e.g.racks[a].id} <
                 std::pair{-overlap(b), e.g.racks[b].id};
        });
        if (targets.size() > 4)
          targets.resize(4);
        for (int target : targets) {
          int missing = int(bundle.size()) - overlap(target);
          if (missing < 1 || missing > 4)
            continue;
          auto key = std::tuple{source, target, bundle};
          if (!eventIndex.count(key)) {
            int anchor = *std::min_element(
                requiredRacks[target].begin(), requiredRacks[target].end(),
                [&](int a, int b) {
                  return model.skuNames[a] < model.skuNames[b];
                });
            eventIndex[key] = int(events.size());
            events.push_back({source, target, anchor, 0, bundle});
          }
          events[eventIndex[key]].count++;
        }
      }
    }
    std::vector<int> ranked(events.size());
    std::iota(ranked.begin(), ranked.end(), 0);
    std::stable_sort(ranked.begin(), ranked.end(), [&](int a, int b) {
      return events[a].count > events[b].count;
    });
    if (ranked.size() > 32)
      ranked.resize(32);
    std::optional<Placements> chosen;
    json best = current;
    for (int ei : ranked) {
      auto &event = events[ei];
      auto trial = plan;
      for (int load : loads[{event.anchor, event.target}])
        trial.placed[load] = true;
      bool failed = false;
      for (int sku : event.bundle)
        if (!contents[event.target].count(sku)) {
          struct Option {
            bool occupied;
            int cell, load;
          };
          std::vector<Option> options;
          auto sources = loads[{sku, event.source}];
          std::sort(sources.begin(), sources.end(), [&](int a, int b) {
            return e.loads[a].id < e.loads[b].id;
          });
          for (int load : sources)
            if (!trial.placed[load])
              for (int cell = 0; cell < int(e.cells.size()); ++cell)
                if (e.cells[cell].rack == event.target) {
                  int owner = trial.owners[cell];
                  if ((owner >= 0 && trial.placed[owner]) ||
                      plan.profiles[cell] !=
                          plan.profiles[trial.placements[load][0]])
                    continue;
                  options.push_back({owner >= 0, cell, load});
                }
          std::sort(options.begin(), options.end(), [&](auto a, auto b) {
            return std::tuple{a.occupied, e.cells[a.cell].level,
                              e.cells[a.cell].slot, e.loads[a.load].id} <
                   std::tuple{b.occupied, e.cells[b.cell].level,
                              e.cells[b.cell].slot, e.loads[b.load].id};
          });
          bool moved = false;
          for (auto option : options) {
            auto proposal = trial.propose(option.load, {option.cell});
            if (proposal) {
              trial.commit(option.load, *proposal);
              moved = true;
              break;
            }
          }
          if (!moved) {
            failed = true;
            break;
          }
        }
      if (failed)
        continue;
      bool changed = false, protectedChange = false;
      for (size_t load = 0; load < original.size(); ++load)
        if (trial.placements[load] != plan.placements[load]) {
          changed = true;
          protectedChange = protectedChange || protectedLoads[load];
        }
      if (!changed || protectedChange)
        continue;
      set_placements(e, trial.placements);
      auto result = model.evaluate();
      evaluated++;
      if (num(result, "deliveries") < num(current, "deliveries") &&
          refinement_guard(result, baseline, e.balance) &&
          (!chosen || refinement_key(result) < refinement_key(best))) {
        chosen = trial.placements;
        best = result;
      }
    }
    if (!chosen) {
      set_placements(e, original);
      break;
    }
    set_placements(e, *chosen);
    current = model.evaluate(true);
    bundleMoves++;
  }
  auto final = placements(e);
  auto meanAfter = e.balance ? mean_work(e, model).maximum : 0;
  set_placements(e, initial);
  auto meanBefore = e.balance ? mean_work(e, model).maximum : 0;
  bool accepted = num(current, "deliveries") < num(baseline, "deliveries") &&
                  refinement_guard(current, baseline, e.balance) &&
                  (!e.balance || meanAfter <= meanBefore + 1e-6);
  set_placements(e, accepted ? final : initial);
  if (!accepted) {
    current = baseline;
    rackMoves = bundleMoves = 0;
  }
  rebuild(e);
  return {{"method", "complete_orders_fixed_stations_bundles/v1"},
          {"status", accepted ? "improved" : "baseline_retained"},
          {"before", baseline},
          {"after", current},
          {"rack_moves", rackMoves},
          {"bundle_moves", bundleMoves},
          {"accepted_moves", rackMoves + bundleMoves},
          {"evaluated_candidates", evaluated},
          {"store_workstation_mapping", mapping},
          {"search_limits",
           {{"rack_moves", 8}, {"bundle_moves", 8}, {"candidates", 32}}}};
}
