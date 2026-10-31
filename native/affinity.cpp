#include "affinity.hpp"

namespace layout {
AffinityData::AffinityData(const json &orders, const json &settings,
                           const std::vector<Load> &loads) {
  std::set<std::string> known;
  for (auto &load : loads)
    known.insert(load.sku);
  names.assign(known.begin(), known.end());
  for (int i = 0; i < int(names.size()); ++i)
    indices[names[i]] = i;
  std::map<std::pair<std::string, std::string>, std::set<int>> baskets;
  auto start = text(settings, "start_date"), end = text(settings, "end_date");
  for (auto &basket : array(orders, "baskets")) {
    auto date = text(basket, "date");
    if ((!start.empty() && date < start) || (!end.empty() && date > end))
      continue;
    auto &group = baskets[{date, text(basket, "store")}];
    for (auto &sku : array(basket, "skus"))
      if (indices.count(str(sku)))
        group.insert(indices[str(sku)]);
  }
  groups = int(baskets.size());
  masks.assign(names.size(), Mask((groups + 63) / 64));
  std::vector<std::vector<int>> shared(names.size(),
                                       std::vector<int>(names.size()));
  int gi = 0;
  for (auto &[_, group] : baskets) {
    for (int sku : group)
      masks[sku][gi / 64] |= uint64_t(1) << (gi % 64);
    for (auto a = group.begin(); a != group.end(); ++a)
      for (auto b = std::next(a); b != group.end(); ++b)
        ++shared[*a][*b];
    ++gi;
  }
  std::vector<int> occurrences;
  int active = 0;
  for (auto &mask : masks) {
    occurrences.push_back(mask_count(mask));
    active += occurrences.back() > 0;
  }
  if (active < 2)
    throw std::runtime_error(
        "Affinity requires at least two stocked SKUs present in order history");
  struct Edge {
    int a, b, support;
    double score, mass;
    int si = 0, ci = 0;
  };
  std::vector<Edge> edges;
  std::vector<double> supports, scores;
  std::set<int> endpoints;
  double totalMass = 0;
  for (int a = 0; a < int(names.size()); ++a)
    for (int b = a + 1; b < int(names.size()); ++b)
      if (shared[a][b] > 0) {
        double score =
            shared[a][b] / std::sqrt(double(occurrences[a]) * occurrences[b]);
        double mass = shared[a][b] * score;
        edges.push_back({a, b, shared[a][b], score, mass});
        supports.push_back(shared[a][b]);
        scores.push_back(score);
        endpoints.insert(a);
        endpoints.insert(b);
        totalMass += mass;
      }
  if (edges.empty())
    throw std::runtime_error("Affinity order history has no shared store/day "
                             "relationships among stocked SKUs");
  int candidates = std::max(2, int(std::ceil(std::log2(edges.size()) + 1)));
  auto quantiles = [&](std::vector<double> values) {
    std::sort(values.begin(), values.end());
    std::vector<double> result;
    for (int i = 0; i < candidates; ++i)
      result.push_back(values[size_t(
          std::nearbyint(i / double(candidates - 1) * (values.size() - 1)))]);
    std::sort(result.begin(), result.end());
    result.erase(std::unique(result.begin(), result.end()), result.end());
    return result;
  };
  auto supportCandidates = quantiles(supports),
       scoreCandidates = quantiles(scores);
  int S = int(supportCandidates.size()), C = int(scoreCandidates.size());
  std::vector<std::vector<double>> counts(S, std::vector<double>(C)),
      mass = counts, coverage = counts;
  std::vector<std::vector<std::pair<int, int>>> incident(names.size());
  for (auto &edge : edges) {
    edge.si = int(std::upper_bound(supportCandidates.begin(),
                                   supportCandidates.end(), edge.support) -
                  supportCandidates.begin()) -
              1;
    edge.ci = int(std::upper_bound(scoreCandidates.begin(),
                                   scoreCandidates.end(), edge.score) -
                  scoreCandidates.begin()) -
              1;
    counts[edge.si][edge.ci]++;
    mass[edge.si][edge.ci] += edge.mass;
    incident[edge.a].push_back({edge.si, edge.ci});
    incident[edge.b].push_back({edge.si, edge.ci});
  }
  auto cumulative = [&](auto &grid) {
    for (int c = 0; c < C; ++c)
      for (int s = S - 2; s >= 0; --s)
        grid[s][c] += grid[s + 1][c];
    for (int s = 0; s < S; ++s)
      for (int c = C - 2; c >= 0; --c)
        grid[s][c] += grid[s][c + 1];
  };
  cumulative(counts);
  cumulative(mass);
  for (auto &bins : incident) {
    std::vector<std::vector<int>> grid(S, std::vector<int>(C));
    for (auto [s, c] : bins)
      grid[s][c] = 1;
    cumulative(grid);
    for (int s = 0; s < S; ++s)
      for (int c = 0; c < C; ++c)
        coverage[s][c] += grid[s][c] > 0;
  }
  std::vector<double> best{INF};
  int support = 1;
  double score = 0;
  for (int s = 0; s < S; ++s)
    for (int c = 0; c < C; ++c)
      if (counts[s][c]) {
        double edgeFraction = counts[s][c] / edges.size(),
               massFraction = mass[s][c] / totalMass,
               coverageFraction = coverage[s][c] / endpoints.size();
        double loss = std::sqrt(.25 * edgeFraction * edgeFraction +
                                .75 *
                                    (std::pow(1 - massFraction, 2) +
                                     std::pow(1 - coverageFraction, 2)) /
                                    2);
        std::vector<double> key{loss, -massFraction, edgeFraction,
                                -coverageFraction};
        if (key < best) {
          best = key;
          support = int(supportCandidates[s]);
          score = scoreCandidates[c];
          recommendation = {{"minimum_shared_store_days", support},
                            {"minimum_affinity_score", score},
                            {"retained_relationship_count", counts[s][c]},
                            {"relationship_count", edges.size()},
                            {"selection_loss", loss},
                            {"method", "empirical_relationship_pareto_knee"}};
        }
      }
  neighbors.resize(names.size());
  for (auto &edge : edges)
    if (edge.support >= support && edge.score >= score) {
      neighbors[edge.a].push_back({edge.b, edge.mass});
      neighbors[edge.b].push_back({edge.a, edge.mass});
    }
  for (auto &list : neighbors)
    std::sort(list.begin(), list.end(),
              [](auto &a, auto &b) { return a.sku < b.sku; });
}

std::vector<int> AffinityData::placement_order(const std::vector<Load> &loads,
                                               int capacity, bool amr) const {
  std::set<int> remaining;
  std::vector<std::vector<int>> bySku(names.size());
  int maxCopies = 0;
  for (int i = 0; i < int(loads.size()); ++i) {
    int s = indices.at(loads[i].sku);
    remaining.insert(s);
    bySku[s].push_back(i);
    maxCopies = std::max(maxCopies, loads[i].index);
  }
  Mask cluster(masks.empty() ? 0 : masks[0].size());
  std::vector<int> logical;
  int size = 0;
  while (!remaining.empty()) {
    int selected = -1, signal = -1;
    for (int sku : remaining) {
      int value =
          size ? shared_count(masks[sku], cluster) : mask_count(masks[sku]);
      if (value > signal) {
        signal = value;
        selected = sku;
      }
    }
    remaining.erase(selected);
    logical.push_back(selected);
    mask_or(cluster, masks[selected]);
    if (++size >= capacity) {
      std::fill(cluster.begin(), cluster.end(), 0);
      size = 0;
    }
  }
  std::vector<int> result;
  if (amr) {
    for (int copy = 1; copy <= maxCopies; ++copy)
      for (int sku : logical)
        for (int load : bySku[sku])
          if (loads[load].index == copy)
            result.push_back(load);
  } else
    for (int sku : logical)
      for (int load : bySku[sku])
        result.push_back(load);
  return result;
}
} // namespace layout
