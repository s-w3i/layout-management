#include "instance.hpp"
#include "planner.hpp"
#include "pibt.hpp"
#include <chrono>
#include <iostream>
#include <regex>
#include <sstream>

static std::string field(const std::string &s, const std::string &name)
{
  const auto key = "\"" + name + "\"";
  auto p = s.find(key);
  if (p == std::string::npos) throw std::runtime_error("missing " + name);
  p = s.find(':', p) + 1;
  while (p < s.size() && std::isspace(s[p])) ++p;
  if (s[p] != '[') {
    auto e = s.find_first_of(",}", p);
    return s.substr(p, e - p);
  }
  int depth = 0;
  for (auto e = p; e < s.size(); ++e) {
    if (s[e] == '[') ++depth;
    if (s[e] == ']' && --depth == 0) return s.substr(p, e - p + 1);
  }
  throw std::runtime_error("unterminated " + name);
}

static std::vector<int> ints(const std::string &s)
{
  std::vector<int> out;
  std::regex r("-?[0-9]+");
  for (std::sregex_iterator i(s.begin(), s.end(), r), e; i != e; ++i)
    out.push_back(std::stoi(i->str()));
  return out;
}

int main()
{
  try {
    std::ostringstream buf; buf << std::cin.rdbuf(); const auto raw = buf.str();
    const int width = std::stoi(field(raw, "width"));
    const int height = std::stoi(field(raw, "height"));
    const int timeout = std::stoi(field(raw, "timeout_ms"));
    const int seed = std::stoi(field(raw, "seed"));
    const auto nodes = ints(field(raw, "nodes"));
    const auto edges = ints(field(raw, "edges"));
    const auto start_ids = ints(field(raw, "starts"));
    const auto goal_ids = ints(field(raw, "goals"));
    const auto terminal_ids = ints(field(raw, "terminals"));
    const auto locked_agents = ints(field(raw, "locked_agents"));
    Graph graph(width, height);
    std::map<int, Vertex *> by_index;
    for (size_t i = 0; i < nodes.size(); i += 3) {
      auto *v = new Vertex(graph.V.size(), nodes[i], nodes[i + 1], nodes[i + 2]);
      graph.V.push_back(v); graph.U[nodes[i]] = v; by_index[nodes[i]] = v;
    }
    for (size_t i = 0; i < edges.size(); i += 2) {
      auto *a = by_index.at(edges[i]); auto *b = by_index.at(edges[i + 1]);
      a->neighbors.push_back(b); b->reverse_neighbors.push_back(a);
    }
    for (auto *v : graph.V) { v->actions = v->neighbors; v->actions.push_back(v); }
    Config starts, goals;
    for (auto id : start_ids) starts.push_back(by_index.at(id));
    for (auto id : goal_ids) goals.push_back(by_index.at(id));
    Instance ins(std::move(graph), starts, goals, terminal_ids, locked_agents);
    // Swap and hindrance heuristics assume symmetric adjacency. Warehouse
    // lanes are directed, so use LaCAM's base PIBT configuration generator.
    PIBT::SWAP = false;
    PIBT::HINDRANCE = false;
    Deadline deadline(timeout);
    const auto t0 = std::chrono::steady_clock::now();
    const auto sol = solve(ins, 0, &deadline, seed);
    const auto ms = std::chrono::duration<double, std::milli>(
      std::chrono::steady_clock::now() - t0).count();
    std::cout << "{\"solved\":" << (!sol.empty() ? "true" : "false")
              << ",\"runtime_ms\":" << ms << ",\"solution\":[";
    for (size_t t = 0; t < sol.size(); ++t) {
      if (t) std::cout << ',';
      std::cout << '[';
      for (size_t a = 0; a < sol[t].size(); ++a) {
        if (a) std::cout << ',';
        std::cout << sol[t][a]->index;
      }
      std::cout << ']';
    }
    std::cout << "]}\n";
    return sol.empty() ? 3 : 0;
  } catch (const std::exception &e) {
    std::cout << "{\"solved\":false,\"error\":\"" << e.what() << "\"}\n";
    return 2;
  }
}
