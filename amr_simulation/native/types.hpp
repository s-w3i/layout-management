#pragma once

#include <cstdint>
#include <limits>
#include <vector>

namespace amr {

struct Node {
    double x;
    double y;
    std::int32_t grid_x;
    std::int32_t grid_y;
    std::uint8_t rack;
};

struct Edge {
    std::int32_t from;
    std::int32_t to;
    double distance;
    std::int32_t dx;
    std::int32_t dy;
};

struct Graph {
    std::vector<Node> nodes;
    std::vector<Edge> edges;
    std::vector<std::vector<std::int32_t>> outgoing;
};

struct Route {
    std::vector<std::int32_t> nodes;
    double distance = std::numeric_limits<double>::infinity();
};

struct MotionProfile {
    double linear_speed;
    double linear_acceleration;
    double angular_speed;
    double angular_acceleration;
};

struct MotionSegment {
    std::int32_t job = -1;
    std::int32_t robot = -1;
    std::int32_t kind = 0;
    double start_time = 0.0;
    double end_time = 0.0;
    double start_x = 0.0;
    double start_y = 0.0;
    double end_x = 0.0;
    double end_y = 0.0;
    double start_heading = 0.0;
    double end_heading = 0.0;
    double start_rate = 0.0;
    double acceleration = 0.0;
    bool loaded = false;
};

}  // namespace amr
