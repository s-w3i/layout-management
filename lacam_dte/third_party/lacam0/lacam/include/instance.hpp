/*
 * instance definition
 */
#pragma once

#include "graph.hpp"
#include "utils.hpp"

struct Instance {
  Graph G;        // graph
  Config starts;  // initial configuration
  Config goals;   // goal configuration
  const uint N;   // number of agents
  std::vector<bool> terminals;
  std::vector<bool> locked;

  Instance(Graph *_G, const Config &_starts, const Config &_goals);
  Instance(Graph &&_G, const Config &_starts, const Config &_goals,
           const std::vector<int> &terminal_indexes = {},
           const std::vector<int> &locked_agents = {});
  Instance(const std::string &map_filename,
           const std::vector<int> &start_indexes,
           const std::vector<int> &goal_indexes);
  // for MAPF benchmark
  Instance(const std::string &scen_filename, const std::string &map_filename,
           const int _N = 1);
  // random instance generation
  Instance(const std::string &map_filename, const int _N = 1,
           const int seed = 0);
  ~Instance();

  // simple feasibility check of instance
  bool is_valid(const int verbose = 0) const;
  bool is_allowed(int agent, const Vertex *vertex) const;
};

// solution: a sequence of configurations
using Solution = std::vector<Config>;
