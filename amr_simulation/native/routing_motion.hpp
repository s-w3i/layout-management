#pragma once

#include "types.hpp"

#include <cstdint>
#include <unordered_set>

namespace amr {

std::int32_t reduced_direction(double dx, double dy);

bool route_astar(
    const Graph& graph,
    std::int32_t start,
    std::int32_t goal,
    const std::unordered_set<std::int32_t>& tabu_nodes,
    const std::unordered_set<std::uint64_t>& tabu_edges,
    Route& output
);

double predicted_motion_seconds(
    const Graph& graph,
    const Route& route,
    double initial_heading,
    const MotionProfile& profile,
    double* final_heading
);

double build_motion(
    const Graph& graph, const Route& route, std::int32_t job, std::int32_t robot,
    double start_time, double initial_heading, const MotionProfile& profile,
    bool loaded, std::vector<MotionSegment>& output, double* final_heading
);

std::vector<std::pair<std::int32_t, double>> crossing_times(
    const Graph& graph, const Route& route,
    const std::vector<MotionSegment>& segments, std::size_t first_segment
);

}  // namespace amr
