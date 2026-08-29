#pragma once

#include <cstdint>

extern "C" {

struct AmrNode { double x; double y; std::int32_t grid_x; std::int32_t grid_y; std::int32_t rack; };
struct AmrEdge { std::int32_t from; std::int32_t to; double distance; };
struct AmrRack { std::int32_t node; std::int32_t sku_offset; std::int32_t sku_count; };
struct AmrTask { std::int32_t station; std::int32_t line_offset; std::int32_t line_count; };
struct AmrTaskLine { std::int32_t sku; std::int32_t count; };
struct AmrSpawn { std::int32_t node; double heading; };

struct AmrDayConfig {
    double jack_up_seconds;
    double jack_down_seconds;
    double service_seconds;
    double reservation_wait_seconds;
    double dram_wait_seconds;
    double linear_speed;
    double linear_acceleration;
    double angular_speed;
    double angular_acceleration;
    std::int32_t loaded_priority;
    std::int32_t mutex_passage;
    std::int32_t corridor_coordination;
    std::int32_t trace;
};

struct AmrDayInput {
    const AmrNode* nodes; std::int32_t node_count;
    const AmrEdge* edges; std::int32_t edge_count;
    const AmrRack* racks; std::int32_t rack_count;
    const std::int32_t* rack_skus; std::int32_t rack_sku_count;
    const AmrTask* tasks; std::int32_t task_count;
    const AmrTaskLine* task_lines; std::int32_t task_line_count;
    const AmrSpawn* spawns; std::int32_t spawn_count;
    const std::int32_t* stations; std::int32_t station_count;
    AmrDayConfig config;
    std::int32_t day_year;
    std::int32_t day_month;
    std::int32_t day_day;
};

struct AmrDayResult;

struct AmrJobResult {
    std::int32_t task;
    std::int32_t rack;
    std::int32_t robot;
    std::int32_t station;
    std::int32_t covered_lines;
    std::int32_t sku_offset;
    std::int32_t sku_count;
    std::int32_t path_offset;
    std::int32_t path_count;
    std::int32_t station_queue_position;
    std::int32_t reservation_conflicts;
    std::int32_t reroutes;
    std::int32_t denial_offset;
    std::int32_t denial_count;
    std::int32_t last_denial_reason;
    std::int32_t last_denial_node;
    double dispatch_time;
    double rack_departure;
    double rack_return;
    double station_queue_enter;
    double station_admitted;
    double station_arrival;
    double service_start;
    double completion_time;
    double travel_distance;
    double travel_seconds;
    double node_wait_seconds;
    double dram_wait_seconds;
};

const char* amr_day_abi_version();
std::int32_t amr_day_engine_ready();
AmrDayResult* amr_day_simulate(const AmrDayInput* input);
const char* amr_day_result_error(const AmrDayResult* result);
const char* amr_day_result_metrics_json(const AmrDayResult* result);
std::int32_t amr_day_result_job_count(const AmrDayResult* result);
const AmrJobResult* amr_day_result_jobs(const AmrDayResult* result);
const std::int32_t* amr_day_result_job_skus(const AmrDayResult* result);
const std::int32_t* amr_day_result_path_nodes(const AmrDayResult* result);
const double* amr_day_result_denial_times(const AmrDayResult* result);
const std::int32_t* amr_day_result_denial_reasons(const AmrDayResult* result);
const std::int32_t* amr_day_result_denial_nodes(const AmrDayResult* result);
void amr_day_result_destroy(AmrDayResult* result);
std::int32_t amr_day_dispatch_probe(const AmrDayInput* input);

}
