#pragma once
#include "model.hpp"
#include <bit>

namespace layout {
using Mask = std::vector<uint64_t>;
inline int mask_count(const Mask &a) {
  int total = 0;
  for (auto word : a)
    total += std::popcount(word);
  return total;
}
inline int shared_count(const Mask &a, const Mask &b) {
  int total = 0;
  for (size_t i = 0; i < a.size(); ++i)
    total += std::popcount(a[i] & b[i]);
  return total;
}
inline void mask_or(Mask &a, const Mask &b) {
  for (size_t i = 0; i < a.size(); ++i)
    a[i] |= b[i];
}
struct AffinityData {
  struct Neighbor {
    int sku;
    double weight;
  };
  std::vector<std::string> names;
  std::map<std::string, int> indices;
  std::vector<Mask> masks;
  std::vector<std::vector<Neighbor>> neighbors;
  int groups = 0;
  json recommendation;
  AffinityData(const json &orders, const json &settings,
               const std::vector<Load> &loads);
  std::vector<int> placement_order(const std::vector<Load> &loads, int capacity,
                                   bool amr) const;
};
} // namespace layout
