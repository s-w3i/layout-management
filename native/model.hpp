#pragma once
#include "vendor/json.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <functional>
#include <limits>
#include <map>
#include <numeric>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_map>
#include <vector>

namespace layout {
using json = nlohmann::ordered_json;
constexpr double INF = std::numeric_limits<double>::infinity();
inline const std::array<std::string, 4> PHYSICAL = {
    "max_item_length", "max_item_width", "max_item_height", "max_item_weight"};
inline json defaults() {
  return {{PHYSICAL[0], 1.9},
          {PHYSICAL[1], 0.5},
          {PHYSICAL[2], 1.0},
          {PHYSICAL[3], 12.5}};
}
inline bool empty(const json &x) {
  return x.is_null() || (x.is_string() && x.get<std::string>().empty());
}
inline std::string str(const json &x) {
  return empty(x) ? "" : x.is_string() ? x.get<std::string>() : x.dump();
}
inline std::string text(const json &x, const std::string &k,
                        const std::string &fallback = "") {
  return x.contains(k) && !empty(x[k]) ? str(x[k]) : fallback;
}
inline double number(const json &v, double fallback = 0) {
  if (empty(v))
    return fallback;
  if (v.is_boolean())
    throw std::runtime_error("Expected a number, received a Boolean");
  if (v.is_number()) {
    double n = v.get<double>();
    if (!std::isfinite(n))
      throw std::runtime_error("Numbers must be finite");
    return n;
  }
  double n;
  try {
    size_t p = 0;
    auto s = str(v);
    n = std::stod(s, &p);
    if (p != s.size())
      throw std::runtime_error("number");
  } catch (...) {
    throw std::runtime_error("Invalid numeric value: " + str(v).substr(0, 60));
  }
  if (!std::isfinite(n))
    throw std::runtime_error("Numbers must be finite");
  return n;
}
inline double num(const json &x, const std::string &k, double fallback = 0) {
  return x.contains(k) ? number(x[k], fallback) : fallback;
}
inline int integer(const json &x, const std::string &k, int fallback,
                   int low = 0, int high = 1000000) {
  double v = num(x, k, fallback);
  if (v < low || v > high || std::floor(v) != v)
    throw std::runtime_error(k + " must be a whole number between " +
                             std::to_string(low) + " and " +
                             std::to_string(high));
  return int(v);
}
inline bool flag(const json &x, const std::string &k, bool fallback = false) {
  if (!x.contains(k))
    return fallback;
  if (!x[k].is_boolean())
    throw std::runtime_error(k + " must be a Boolean");
  return x[k].get<bool>();
}
inline double cap(const json &x, const std::string &k) {
  return !x.contains(k) || empty(x[k]) ? INF : number(x[k]);
}
inline double rounded(double x, int digits = 6) {
  auto f = std::pow(10., digits);
  return std::round(x * f) / f;
}
inline std::string padded(int n, int width = 2) {
  auto s = std::to_string(n);
  return std::string(std::max(0, width - int(s.size())), '0') + s;
}
inline json object(const json &x, const std::string &k) {
  return x.contains(k) && x[k].is_object() ? x[k] : json::object();
}
inline json array(const json &x, const std::string &k) {
  return x.contains(k) && x[k].is_array() ? x[k] : json::array();
}
inline json effective(const std::string &path, const json &local) {
  json result = json::object();
  size_t at = 0;
  while (true) {
    at = path.find('/', at);
    auto prefix = path.substr(0, at);
    if (local.contains(prefix)) {
      if (!local[prefix].is_object())
        throw std::runtime_error("Location attributes must be objects");
      result.update(local[prefix]);
    }
    if (at == std::string::npos)
      break;
    ++at;
  }
  return result;
}
struct Node {
  std::string id, role, endpoint;
  int column = 0, row = 0;
  double x = 0, y = 0;
};
struct Rack {
  int node = 0, rank = 0;
  std::string id, endpoint, zone, aisle, bay;
  double x = 0, y = 0, distance = INF;
  std::vector<std::string> stations;
};
struct Geometry {
  json project, building, local, catalog, standard, machine;
  std::vector<Node> nodes;
  std::vector<std::tuple<int, int, bool>> lanes;
  std::vector<std::vector<std::pair<int, double>>> graph, reverse;
  std::vector<Rack> racks;
  std::map<std::string, int> stations;
  int levels = 3, slots = 4;
  bool amr = true, buffer_model = true;
  std::string handling = "AMR shelf", level = "L1";
};
struct Profile {
  std::string status = "COMPLETE", kind = "STANDARD", missing_type;
  bool volumetric = false, weight_disabled = false, enabled = true;
  json missing = json::array(), values = json::object();
  bool exception() const {
    return kind != "STANDARD" && kind != "NOT_EVALUATED";
  }
  bool reserve() const {
    return kind == "OVERSIZE" || kind == "OVERWEIGHT" ||
           kind == "OVERSIZE_AND_OVERWEIGHT" || kind == "NON_VOLUMETRIC_DATA";
  }
  bool overweight() const {
    return kind == "OVERWEIGHT" || kind == "OVERSIZE_AND_OVERWEIGHT";
  }
};
struct Cell {
  int rack = 0, level = 0, slot = 0;
  std::string address, buffer, static_address, unit, dynamic;
  json attrs;
};
struct Load {
  json source, requirements;
  Profile profile;
  std::string sku, id, velocity;
  int index = 1, count = 1, abc_rank = 0;
  double quantity = 0, weight = 0, frequency = 0;
  std::vector<int> cells;
  bool reserved = false;
  std::string reason;
  std::string failure_status = "UNASSIGNED_NO_COMPATIBLE_LOCATION";
};
Geometry geometry(json project);
json geometry_result(const Geometry &g);
std::vector<double> distances(const Geometry &g, int start,
                              bool reverse = false, bool block_racks = false);
Profile profile(const json &req, const Geometry &g);
std::pair<int, int> footprint(const json &req, const json &attrs, int levels,
                              int slots);
json analyze(const json &request);
json calculate_stock(const json &request);
json generate(const json &request);
json validate_layout(const json &request);
json score_layout(const json &request);
json movement_metrics(const json &request);
json swap_layout(const json &request);
} // namespace layout
