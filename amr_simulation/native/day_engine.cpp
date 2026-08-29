#include "routing_motion.hpp"
#include "day_abi.hpp"
#include "simulator.hpp"

#include <cstdint>
#include <algorithm>
#include <cstring>
#include <new>
#include <string>
#include <unordered_set>
#include <vector>

extern "C" {

struct AmrDayResult {
    std::string error;
    std::string metrics;
    std::vector<AmrJobResult> jobs;
    std::vector<std::int32_t> job_skus;
    std::vector<std::int32_t> path_nodes;
    std::vector<double> denial_times;
    std::vector<std::int32_t> denial_reasons;
    std::vector<std::int32_t> denial_nodes;
};

const char* amr_day_abi_version() { return "amr-day/v1"; }

// Full-day execution is enabled only after all parity stages are present.
std::int32_t amr_day_engine_ready() { return 0; }

AmrDayResult* amr_day_simulate(const AmrDayInput* input) {
    auto* result = new (std::nothrow) AmrDayResult;
    if (!result) return nullptr;
    if (!input) {
        result->error = "invalid or empty native day input";
        return result;
    }
    amr::Simulator simulator(*input);
    if (!simulator.valid()) {
        result->error = simulator.error();
        return result;
    }
    if (!simulator.run()) {
        result->error = simulator.error();
        return result;
    }
    result->metrics = simulator.metrics_json();
    for (const auto& source : simulator.jobs()) {
        AmrJobResult job{};
        job.task = source.task; job.rack = source.rack; job.robot = source.robot;
        job.station = source.station;
        job.sku_offset = static_cast<std::int32_t>(result->job_skus.size());
        for (const auto& line : source.lines) {
            result->job_skus.push_back(line.first);
            job.covered_lines += line.second;
        }
        std::sort(result->job_skus.begin() + job.sku_offset, result->job_skus.end());
        job.sku_count = static_cast<std::int32_t>(source.lines.size());
        job.path_offset = static_cast<std::int32_t>(result->path_nodes.size());
        result->path_nodes.insert(result->path_nodes.end(), source.traversed.begin(), source.traversed.end());
        job.path_count = static_cast<std::int32_t>(source.traversed.size());
        job.station_queue_position = source.station_queue_position;
        job.reservation_conflicts = source.reservation_conflicts; job.reroutes = source.reroutes;
        job.denial_offset = static_cast<std::int32_t>(result->denial_times.size());
        result->denial_times.insert(result->denial_times.end(), source.denial_times.begin(), source.denial_times.end());
        result->denial_reasons.insert(result->denial_reasons.end(), source.denial_reasons.begin(), source.denial_reasons.end());
        result->denial_nodes.insert(result->denial_nodes.end(), source.denial_nodes.begin(), source.denial_nodes.end());
        job.denial_count = static_cast<std::int32_t>(source.denial_times.size());
        job.last_denial_reason = source.denial_reasons.empty() ? 0 : source.denial_reasons.back();
        job.last_denial_node = source.denial_nodes.empty() ? -1 : source.denial_nodes.back();
        job.dispatch_time = source.dispatch_time; job.rack_departure = source.rack_departure;
        job.rack_return = source.rack_return; job.station_queue_enter = source.station_queue_enter;
        job.station_admitted = source.station_admitted; job.station_arrival = source.station_arrival;
        job.service_start = source.service_start; job.completion_time = source.completion_time;
        job.travel_distance = source.travel_distance; job.travel_seconds = source.travel_seconds;
        job.node_wait_seconds = source.node_wait_seconds;
        job.dram_wait_seconds = source.dram_wait_seconds;
        result->jobs.push_back(job);
    }
    return result;
}

const char* amr_day_result_error(const AmrDayResult* result) {
    return result ? result->error.c_str() : "native allocation failed";
}

const char* amr_day_result_metrics_json(const AmrDayResult* result) {
    return result ? result->metrics.c_str() : "";
}

std::int32_t amr_day_result_job_count(const AmrDayResult* result) {
    return result ? static_cast<std::int32_t>(result->jobs.size()) : 0;
}
const AmrJobResult* amr_day_result_jobs(const AmrDayResult* result) {
    return result && !result->jobs.empty() ? result->jobs.data() : nullptr;
}
const std::int32_t* amr_day_result_job_skus(const AmrDayResult* result) {
    return result && !result->job_skus.empty() ? result->job_skus.data() : nullptr;
}
const std::int32_t* amr_day_result_path_nodes(const AmrDayResult* result) {
    return result && !result->path_nodes.empty() ? result->path_nodes.data() : nullptr;
}
const double* amr_day_result_denial_times(const AmrDayResult* result) {
    return result && !result->denial_times.empty() ? result->denial_times.data() : nullptr;
}
const std::int32_t* amr_day_result_denial_reasons(const AmrDayResult* result) {
    return result && !result->denial_reasons.empty() ? result->denial_reasons.data() : nullptr;
}
const std::int32_t* amr_day_result_denial_nodes(const AmrDayResult* result) {
    return result && !result->denial_nodes.empty() ? result->denial_nodes.data() : nullptr;
}

void amr_day_result_destroy(AmrDayResult* result) { delete result; }

std::int32_t amr_day_dispatch_probe(const AmrDayInput* input) {
    if (!input) return -1;
    amr::Simulator simulator(*input);
    return simulator.valid() ? simulator.dispatch_fixture(0.0) : -1;
}

std::int32_t amr_day_calendar_self_test() {
    AmrDayInput empty{};
    // The ordering check is independent of imported simulation state.
    return amr::Simulator(empty).calendar_order_self_test() ? 1 : 0;
}

std::int32_t amr_day_route(
    const AmrNode* nodes, std::int32_t node_count,
    const AmrEdge* edges, std::int32_t edge_count,
    std::int32_t start, std::int32_t goal,
    const std::int32_t* tabu_nodes, std::int32_t tabu_node_count,
    const std::uint64_t* tabu_edges, std::int32_t tabu_edge_count,
    std::int32_t* output, std::int32_t capacity, double* distance
) {
    if (!nodes || !edges || !output || !distance || node_count < 1 || edge_count < 0) return -1;
    amr::Graph graph;
    graph.nodes.reserve(node_count);
    graph.outgoing.resize(node_count);
    for (std::int32_t index = 0; index < node_count; ++index) {
        graph.nodes.push_back({nodes[index].x, nodes[index].y, nodes[index].grid_x,
                               nodes[index].grid_y, static_cast<std::uint8_t>(nodes[index].rack != 0)});
    }
    graph.edges.reserve(edge_count);
    for (std::int32_t index = 0; index < edge_count; ++index) {
        if (edges[index].from < 0 || edges[index].from >= node_count
            || edges[index].to < 0 || edges[index].to >= node_count) return -1;
        graph.edges.push_back({edges[index].from, edges[index].to, edges[index].distance, 0, 0});
        graph.outgoing[edges[index].from].push_back(index);
    }
    std::unordered_set<std::int32_t> blocked_nodes;
    std::unordered_set<std::uint64_t> blocked_edges;
    for (std::int32_t index = 0; index < tabu_node_count; ++index) blocked_nodes.insert(tabu_nodes[index]);
    for (std::int32_t index = 0; index < tabu_edge_count; ++index) blocked_edges.insert(tabu_edges[index]);
    amr::Route route;
    if (!amr::route_astar(graph, start, goal, blocked_nodes, blocked_edges, route)) return 0;
    if (static_cast<std::int32_t>(route.nodes.size()) > capacity) return -2;
    std::memcpy(output, route.nodes.data(), route.nodes.size() * sizeof(std::int32_t));
    *distance = route.distance;
    return static_cast<std::int32_t>(route.nodes.size());
}

double amr_day_predicted_motion(
    const AmrNode* nodes, std::int32_t node_count,
    const std::int32_t* route_nodes, std::int32_t route_count,
    double initial_heading,
    double linear_speed, double linear_acceleration,
    double angular_speed, double angular_acceleration,
    double* final_heading
) {
    if (!nodes || !route_nodes || !final_heading || node_count < 1 || route_count < 1) return -1.0;
    amr::Graph graph;
    graph.nodes.reserve(node_count);
    for (std::int32_t index = 0; index < node_count; ++index) {
        graph.nodes.push_back({nodes[index].x, nodes[index].y, nodes[index].grid_x,
                               nodes[index].grid_y, static_cast<std::uint8_t>(nodes[index].rack != 0)});
    }
    amr::Route route;
    route.nodes.assign(route_nodes, route_nodes + route_count);
    for (const auto node : route.nodes) {
        if (node < 0 || node >= node_count) return -1.0;
    }
    return amr::predicted_motion_seconds(
        graph, route, initial_heading,
        {linear_speed, linear_acceleration, angular_speed, angular_acceleration},
        final_heading
    );
}

}
