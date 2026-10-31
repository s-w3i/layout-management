#include "model.hpp"
#include <queue>

namespace layout {
using Point = std::pair<int, int>;
using Lane = std::pair<Point, Point>;
static Point point(const json &p) {
  return {integer(p, "column", 0), integer(p, "row", 0)};
}
static Lane normalized(Point a, Point b) {
  return a < b ? Lane{a, b} : Lane{b, a};
}

Geometry geometry(json project) {
  Geometry g;
  g.project = project;
  if (!project.contains("grid") || !project["grid"].is_object())
    throw std::runtime_error("Import or create a warehouse grid first");
  auto spec = project["grid"];
  double width = num(spec, "width_m", 20), height = num(spec, "length_m", 15),
         sx = num(spec, "spacing_m", 1), sy = num(spec, "spacing_y_m", sx);
  if (std::min({width, height, sx, sy}) <= 0)
    throw std::runtime_error(
        "Warehouse dimensions and grid spacing must be positive");
  double nc = std::ceil(width / sx - 1e-12),
         nr = std::ceil(height / sy - 1e-12);
  if (nc > 9999 || nr > 9999 || (nc + 1) * (nr + 1) > 10000)
    throw std::runtime_error("A grid may contain at most 10,000 points");
  int cols = int(nc), rows = int(nr);
  g.level = text(spec, "level_name", "L1");
  if (g.level.empty())
    throw std::runtime_error("Level name cannot be empty");
  g.local = object(project, "location_attributes");
  for (auto &[path, attrs] : g.local.items()) {
    if (!attrs.is_object())
      throw std::runtime_error("Location attributes must be objects: " + path);
    for (auto &key : PHYSICAL)
      if (attrs.contains(key) && !empty(attrs[key])) {
        double value = number(attrs[key]);
        attrs[key] = std::trunc(value) == value && std::abs(value) < 1e15
                         ? json(int64_t(value))
                         : json(value);
      }
  }
  g.catalog = array(project, "attribute_catalog");
  g.standard = defaults();
  g.standard.update(object(project, "warehouse_storage_defaults"));
  for (auto &key : PHYSICAL)
    if (num(g.standard, key) <= 0)
      throw std::runtime_error("Standard slot capacities must be positive");
  auto storage = object(project, "storage_layout");
  g.buffer_model = !storage.empty();
  auto system = text(storage, "system_type", "AMR");
  if (system != "AMR" && system != "Mini-load ASRS" && system != "Pallet ASRS")
    throw std::runtime_error("Unsupported storage system");
  g.amr = system == "AMR";
  g.handling = g.amr                     ? "AMR shelf"
               : system == "Pallet ASRS" ? "Pallet"
                                         : "Tote";
  if (storage.contains("handling_unit_type") &&
      text(storage, "handling_unit_type") != g.handling)
    throw std::runtime_error(
        "Handling unit type does not match the storage system");
  g.levels = integer(storage, "levels_per_rack", 3, 1, 64);
  g.slots = integer(storage, "slots_per_level", 4, 1, 64);
  if (g.levels * g.slots > 256)
    throw std::runtime_error("At most 256 cells per rack are supported");
  g.machine = object(storage, "machine_carrying_capacity");
  if (g.machine.empty())
    g.machine = object(project, "machine_carrying_capacity");
  for (auto &key : PHYSICAL)
    if (std::isfinite(cap(g.machine, key)) && cap(g.machine, key) <= 0)
      throw std::runtime_error(
          "Machine carrying capacities must be positive or unset");
  std::set<Point> removed;
  for (auto &p : array(project, "deleted_positions"))
    removed.insert(point(p));
  std::map<Point, json> markers, overrides;
  std::set<std::string> endpoints;
  for (auto &m : array(project, "markers")) {
    Point p = point(m);
    auto role = text(m, "role");
    auto endpoint = text(m, "endpoint_id");
    if (p.first > cols || p.second > rows || removed.count(p))
      throw std::runtime_error("Marker is outside the active grid");
    if (role != "rack" && role != "workstation")
      throw std::runtime_error("Marker role must be rack or workstation");
    if (endpoint.empty() || !endpoints.insert(endpoint).second ||
        markers.count(p))
      throw std::runtime_error(
          "Marker positions and endpoint IDs must be unique");
    markers[p] = m;
  }
  for (auto &p : array(project, "coordinate_overrides"))
    overrides[point(p)] = p;
  std::map<Point, int> indices;
  json vertices = json::array();
  for (int r = 0; r <= rows; ++r)
    for (int c = 0; c <= cols; ++c) {
      Point p{c, r};
      if (removed.count(p))
        continue;
      Node n;
      n.id = "G" + std::to_string(c) + "_" + std::to_string(r);
      n.column = c;
      n.row = r;
      n.x = rounded(std::min(width, c * sx), 9);
      n.y = rounded(std::min(height, r * sy), 9);
      if (overrides.count(p)) {
        n.x = num(overrides[p], "x");
        n.y = num(overrides[p], "y");
      }
      json vertex = {n.x, n.y, 0, n.id};
      if (markers.count(p)) {
        n.role = text(markers[p], "role");
        n.endpoint = text(markers[p], "endpoint_id");
        vertex.push_back(
            {{n.role == "rack" ? "pickup_dispenser" : "dropoff_ingestor",
              {1, n.endpoint}}});
      }
      indices[p] = int(g.nodes.size());
      if (n.role == "workstation")
        g.stations[n.endpoint] = int(g.nodes.size());
      g.nodes.push_back(n);
      vertices.push_back(vertex);
    }
  std::set<Lane> removed_lanes, added_lanes, oneway;
  for (auto &l : array(project, "deleted_lanes"))
    removed_lanes.insert(normalized(point(l.at("start")), point(l.at("end"))));
  for (auto &l : array(project, "added_lanes"))
    added_lanes.insert(normalized(point(l.at("start")), point(l.at("end"))));
  for (auto &l : array(project, "one_way_lanes"))
    oneway.insert({point(l.at("start")), point(l.at("end"))});
  std::vector<Lane> lanes;
  std::set<Lane> generated;
  auto add = [&](Point a, Point b) {
    auto l = normalized(a, b);
    generated.insert(l);
    if (!removed_lanes.count(l))
      lanes.push_back(l);
  };
  for (int r = 0; r <= rows; ++r) {
    std::optional<Point> prior;
    for (int c = 0; c <= cols; ++c)
      if (indices.count({c, r})) {
        if (prior)
          add(*prior, {c, r});
        prior = Point{c, r};
      }
  }
  for (int c = 0; c <= cols; ++c) {
    std::optional<Point> prior;
    for (int r = 0; r <= rows; ++r)
      if (indices.count({c, r})) {
        if (prior)
          add(*prior, {c, r});
        prior = Point{c, r};
      }
  }
  for (auto l : added_lanes)
    if (!generated.count(l))
      lanes.push_back(l);
  g.graph.resize(g.nodes.size());
  g.reverse.resize(g.nodes.size());
  json saved_lanes = json::array();
  for (auto l : lanes) {
    if (!indices.count(l.first) || !indices.count(l.second))
      throw std::runtime_error("Lane references an inactive grid point");
    bool bidirectional = true;
    if (oneway.count(l))
      bidirectional = false;
    else if (oneway.count({l.second, l.first})) {
      std::swap(l.first, l.second);
      bidirectional = false;
    }
    int a = indices[l.first], b = indices[l.second];
    double length =
        std::hypot(g.nodes[a].x - g.nodes[b].x, g.nodes[a].y - g.nodes[b].y);
    g.graph[a].push_back({b, length});
    g.reverse[b].push_back({a, length});
    if (bidirectional) {
      g.graph[b].push_back({a, length});
      g.reverse[a].push_back({b, length});
    }
    g.lanes.push_back({a, b, bidirectional});
    saved_lanes.push_back(
        {a, b, {{"bidirectional", {4, bidirectional}}, {"graph_idx", {2, 0}}}});
  }
  g.building = {{"coordinate_system", "cartesian_meters"},
                {"name", text(spec, "map_name", "warehouse_grid")},
                {"graphs", json::object()},
                {"lifts", json::object()},
                {"levels",
                 {{g.level,
                   {{"elevation", 0},
                    {"vertices", vertices},
                    {"lanes", saved_lanes},
                    {"measurements", json::array()},
                    {"models", json::array()},
                    {"walls", json::array()},
                    {"floors", json::array()}}}}}};
  std::map<std::string, std::vector<double>> station_distances;
  for (auto &[id, node] : g.stations)
    station_distances[id] = distances(g, node, true);
  auto zones = object(project, "zone_assignments");
  std::map<std::string, std::string> roots;
  std::set<std::string> bufferIds;
  std::set<std::tuple<std::string, int, int>> bufferCells;
  std::set<std::string> rackNames;
  for (auto &node : g.nodes)
    if (node.role == "rack")
      rackNames.insert(node.id);
  for (auto &b : array(storage, "buffers")) {
    auto root = text(b, "buffer_id");
    if (root.empty() || !bufferIds.insert(root).second)
      throw std::runtime_error(
          "Storage buffer IDs must be nonempty and unique");
    auto waypoint = text(b, "grid_waypoint");
    if (!rackNames.count(waypoint))
      throw std::runtime_error("Storage buffer references an unknown rack: " +
                               waypoint);
    if (text(b, "buffer_level") != (g.amr ? "grid" : "slot") ||
        text(b, "status", "EMPTY") != "EMPTY")
      throw std::runtime_error("Grid storage buffers must be EMPTY and match "
                               "the storage system level");
    if (!g.amr) {
      int level = integer(b, "level", 1, 1, g.levels),
          slot = integer(b, "slot", 1, 1, g.slots);
      if (!bufferCells.insert({waypoint, level, slot}).second)
        throw std::runtime_error("Duplicate physical storage buffer");
    }
    roots[text(b, "grid_waypoint")] = root.substr(0, root.find("/L"));
  }
  for (int i = 0; i < int(g.nodes.size()); ++i)
    if (g.nodes[i].role == "rack") {
      auto &n = g.nodes[i];
      Rack r;
      r.node = i;
      r.id = n.id;
      r.endpoint = n.endpoint;
      r.zone = text(zones, n.id, "Z01");
      r.x = n.x;
      r.y = n.y;
      r.bay = "B-" + n.id;
      if (r.zone.empty() || r.zone.find('/') != std::string::npos)
        throw std::runtime_error(
            "Zone IDs must be nonempty and cannot contain '/'");
      if (g.buffer_model) {
        if (!roots.count(n.id))
          throw std::runtime_error("Rack " + n.id +
                                   " has no generated storage buffer");
        r.bay = roots[n.id];
      }
      double total = 0;
      for (auto &[id, d] : station_distances) {
        if (std::isfinite(d[i])) {
          r.stations.push_back(id);
          total += d[i];
        }
      }
      if (!g.stations.empty() && r.stations.size() == g.stations.size())
        r.distance = total / g.stations.size();
      g.racks.push_back(r);
    }
  std::map<std::string, std::set<double>> columns;
  for (auto &r : g.racks)
    columns[r.zone].insert(rounded(r.x, 9));
  for (auto &r : g.racks) {
    auto &xs = columns[r.zone];
    r.aisle =
        "A" +
        padded(int(std::distance(xs.begin(), xs.find(rounded(r.x, 9)))) + 1);
  }
  std::stable_sort(
      g.racks.begin(), g.racks.end(), [](const Rack &a, const Rack &b) {
        return std::tie(a.distance, a.id) < std::tie(b.distance, b.id);
      });
  for (int i = 0; i < int(g.racks.size()); ++i)
    g.racks[i].rank = i + 1;
  return g;
}

std::vector<double> distances(const Geometry &g, int start, bool reverse,
                              bool block_racks) {
  const auto &graph = reverse ? g.reverse : g.graph;
  std::vector<double> d(g.nodes.size(), INF);
  d[start] = 0;
  using Entry = std::pair<double, int>;
  std::priority_queue<Entry, std::vector<Entry>, std::greater<Entry>> queue;
  queue.push({0, start});
  while (!queue.empty()) {
    auto [cost, node] = queue.top();
    queue.pop();
    if (cost > d[node] + 1e-9)
      continue;
    if (block_racks && g.nodes[node].role == "rack")
      continue;
    for (auto [next, w] : graph[node])
      if (cost + w + 1e-9 < d[next]) {
        d[next] = cost + w;
        queue.push({d[next], next});
      }
  }
  return d;
}

json geometry_result(const Geometry &g) {
  json racks = json::array(), nodes = json::array(), lanes = json::array();
  int unreachable = 0;
  for (auto &r : g.racks) {
    racks.push_back({{"id", r.id},
                     {"endpoint", r.endpoint},
                     {"zone", r.zone},
                     {"aisle", r.aisle},
                     {"x", r.x},
                     {"y", r.y},
                     {"distance", std::isfinite(r.distance) ? json(r.distance)
                                                            : json(nullptr)},
                     {"rank", r.rank}});
    if (!std::isfinite(r.distance))
      ++unreachable;
  }
  for (auto &n : g.nodes)
    nodes.push_back({{"id", n.id},
                     {"column", n.column},
                     {"row", n.row},
                     {"x", n.x},
                     {"y", n.y},
                     {"role", n.role},
                     {"endpoint", n.endpoint}});
  for (auto [a, b, bidi] : g.lanes)
    lanes.push_back({a, b, bidi});
  size_t capacity = g.racks.size() * g.levels * g.slots;
  if (g.buffer_model && !g.amr)
    capacity = array(object(g.project, "storage_layout"), "buffers").size();
  return {{"project", g.project},
          {"building", g.building},
          {"nodes", nodes},
          {"lanes", lanes},
          {"racks", racks},
          {"stations", g.stations},
          {"summary",
           {{"rack_count", g.racks.size()},
            {"workstation_count", g.stations.size()},
            {"vertex_count", g.nodes.size()},
            {"lane_count", g.lanes.size()},
            {"unreachable_rack_count", unreachable},
            {"capacity", capacity},
            {"levels", g.levels},
            {"slots", g.slots}}}};
}

Profile profile(const json &req, const Geometry &g) {
  Profile p;
  int dimensions = 0;
  bool all = true;
  std::set<std::string> catalog;
  for (auto &d : g.catalog)
    if (flag(d, "enabled", true))
      catalog.insert(text(d, "key"));
  for (auto &k : PHYSICAL)
    all = all && catalog.count(k);
  if (!all) {
    p.enabled = false;
    p.status = "NOT_EVALUATED";
    p.kind = "NOT_EVALUATED";
    return p;
  }
  for (int i = 0; i < 4; ++i) {
    double v = num(req, PHYSICAL[i]);
    if (v <= 0)
      p.missing.push_back(PHYSICAL[i]);
    else {
      p.values[PHYSICAL[i]] = v;
      if (i < 3)
        ++dimensions;
    }
  }
  p.weight_disabled = req.contains(PHYSICAL[3]) && num(req, PHYSICAL[3]) == 0;
  if (dimensions == 3) {
    std::array<double, 3> item, slot, machine;
    for (int i = 0; i < 3; ++i) {
      item[i] = num(req, PHYSICAL[i]);
      slot[i] = num(g.standard, PHYSICAL[i]);
      machine[i] = cap(g.machine, PHYSICAL[i]);
    }
    std::sort(item.begin(), item.end());
    std::sort(slot.begin(), slot.end());
    std::sort(machine.begin(), machine.end());
    for (int i = 0; i < 3; ++i)
      if (item[i] > slot[i] ||
          (!g.amr && std::isfinite(machine[2]) && item[i] > machine[i]))
        p.volumetric = true;
  }
  bool weight = p.values.contains(PHYSICAL[3]);
  bool heavy =
      weight && (num(req, PHYSICAL[3]) > num(g.standard, PHYSICAL[3]) ||
                 num(req, PHYSICAL[3]) > cap(g.machine, PHYSICAL[3]));
  if (!p.missing.empty()) {
    p.status = "MISSING";
    if (dimensions != 3 && !weight) {
      p.missing_type = "NON_VOLUMETRIC_DATA";
      p.kind = "NON_VOLUMETRIC_DATA";
    } else if (dimensions != 3) {
      p.missing_type = "UNKNOWN_SIZE";
      p.kind = num(req, PHYSICAL[3]) > num(g.standard, PHYSICAL[3])
                   ? "OVERSIZE_AND_OVERWEIGHT"
                   : "OVERSIZE";
    } else {
      p.missing_type = "UNKNOWN_WEIGHT";
      p.kind = p.volumetric ? "OVERSIZE" : "UNKNOWN_WEIGHT";
    }
  } else
    p.kind = p.volumetric ? (heavy ? "OVERSIZE_AND_OVERWEIGHT" : "OVERSIZE")
                          : (heavy ? "OVERWEIGHT" : "STANDARD");
  return p;
}

std::pair<int, int> footprint(const json &req, const json &attrs, int levels,
                              int slots) {
  std::array<double, 3> dims;
  for (int i = 0; i < 3; ++i) {
    dims[i] = num(req, PHYSICAL[i]);
    if (dims[i] <= 0)
      return {0, 0};
  }
  double depth = cap(attrs, PHYSICAL[0]), width = cap(attrs, PHYSICAL[1]),
         height = cap(attrs, PHYSICAL[2]);
  if (std::min({depth, width, height}) <= 0)
    return {0, 0};
  std::sort(dims.begin(), dims.end());
  std::tuple<int, int, int> best{1000000, 0, 0};
  do {
    if (dims[0] > depth + 1e-9)
      continue;
    double vertical = std::ceil(dims[2] / height - 1e-12),
           horizontal = std::ceil(dims[1] / width - 1e-12);
    if (vertical > levels || horizontal > slots)
      continue;
    int l = std::max(1, int(vertical)), s = std::max(1, int(horizontal));
    if (l <= levels && s <= slots)
      best = std::min(best, std::tuple{l * s, l, s});
  } while (std::next_permutation(dims.begin(), dims.end()));
  return {std::get<1>(best), std::get<2>(best)};
}
} // namespace layout
