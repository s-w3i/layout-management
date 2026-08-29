#include "routing_motion.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <numeric>
#include <queue>
#include <tuple>
#include <unordered_map>
#include <utility>

namespace amr {
namespace {

constexpr double epsilon = 1e-9;

double normalize_angle(double value) {
    value = std::fmod(value + M_PI, 2.0 * M_PI);
    if (value < 0.0) value += 2.0 * M_PI;
    return value - M_PI;
}

double phase_seconds(double amount, double maximum_rate, double acceleration) {
    if (amount <= 1e-12) return 0.0;
    const double threshold = maximum_rate * maximum_rate / acceleration;
    if (amount <= threshold + 1e-12) {
        return 2.0 * std::sqrt(amount * acceleration) / acceleration;
    }
    return 2.0 * maximum_rate / acceleration
        + (amount - threshold) / maximum_rate;
}

struct Phase { std::int32_t kind; double duration; double amount; double start_rate; double end_rate; };

std::vector<Phase> phases(double amount, double maximum_rate, double acceleration) {
    if (amount <= 1e-12) return {};
    const double threshold = maximum_rate * maximum_rate / acceleration;
    if (amount <= threshold + 1e-12) {
        const double peak = std::sqrt(amount * acceleration);
        const double duration = peak / acceleration;
        return {{0, duration, amount / 2.0, 0.0, peak},
                {2, duration, amount / 2.0, peak, 0.0}};
    }
    const double acceleration_time = maximum_rate / acceleration;
    const double acceleration_amount = 0.5 * maximum_rate * maximum_rate / acceleration;
    const double cruise_amount = amount - 2.0 * acceleration_amount;
    return {{0, acceleration_time, acceleration_amount, 0.0, maximum_rate},
            {1, cruise_amount / maximum_rate, cruise_amount, maximum_rate, maximum_rate},
            {2, acceleration_time, acceleration_amount, maximum_rate, 0.0}};
}

std::uint64_t edge_key(std::int32_t from, std::int32_t to) {
    return (static_cast<std::uint64_t>(static_cast<std::uint32_t>(from)) << 32)
        | static_cast<std::uint32_t>(to);
}

struct Direction { std::int32_t x; std::int32_t y; bool operator==(const Direction& other) const { return x == other.x && y == other.y; } };

struct State {
    std::int32_t node;
    Direction direction;
    bool operator==(const State& other) const {
        return node == other.node && direction == other.direction;
    }
};

struct StateHash {
    std::size_t operator()(const State& value) const {
        const auto direction = (static_cast<std::uint64_t>(static_cast<std::uint32_t>(value.direction.x)) << 32)
            | static_cast<std::uint32_t>(value.direction.y);
        return static_cast<std::size_t>(edge_key(value.node, static_cast<std::int32_t>(direction ^ (direction >> 32))));
    }
};

struct QueueItem {
    double estimate;
    std::int32_t turns;
    double travelled;
    std::int32_t node;
    Direction direction;
};

struct Later {
    bool operator()(const QueueItem& a, const QueueItem& b) const {
        return std::tie(a.estimate, a.turns, a.travelled, a.node, a.direction.x, a.direction.y)
            > std::tie(b.estimate, b.turns, b.travelled, b.node, b.direction.x, b.direction.y);
    }
};

}  // namespace

std::int32_t reduced_direction(double dx, double dy) {
    const double angle = std::atan2(dy, dx);
    return static_cast<std::int32_t>(std::llround(angle * 1e9));
}

bool route_astar(
    const Graph& graph,
    std::int32_t start,
    std::int32_t goal,
    const std::unordered_set<std::int32_t>& tabu_nodes,
    const std::unordered_set<std::uint64_t>& tabu_edges,
    Route& output
) {
    if (start < 0 || goal < 0 || start >= static_cast<std::int32_t>(graph.nodes.size())
        || goal >= static_cast<std::int32_t>(graph.nodes.size()) || tabu_nodes.count(goal)) {
        return false;
    }
    const auto& target = graph.nodes[goal];
    auto heuristic = [&](std::int32_t node) {
        const auto& point = graph.nodes[node];
        return std::hypot(point.x - target.x, point.y - target.y);
    };
    using Cost = std::pair<double, std::int32_t>;
    const State initial{start, {0, 0}};
    std::unordered_map<State, Cost, StateHash> costs;
    std::unordered_map<State, State, StateHash> previous;
    costs.emplace(initial, Cost{0.0, 0});
    std::priority_queue<QueueItem, std::vector<QueueItem>, Later> queue;
    queue.push({heuristic(start), 0, 0.0, start, {0, 0}});
    while (!queue.empty()) {
        const QueueItem item = queue.top();
        queue.pop();
        const State state{item.node, item.direction};
        const auto known = costs.find(state);
        if (known == costs.end()) continue;
        if (item.travelled > known->second.first + epsilon
            || (std::abs(item.travelled - known->second.first) <= epsilon
                && item.turns > known->second.second)) continue;
        if (item.node == goal) {
            output.nodes.clear();
            State cursor = state;
            for (;;) {
                output.nodes.push_back(cursor.node);
                const auto parent = previous.find(cursor);
                if (parent == previous.end()) break;
                cursor = parent->second;
            }
            std::reverse(output.nodes.begin(), output.nodes.end());
            output.distance = item.travelled;
            return true;
        }
        for (const std::int32_t edge_index : graph.outgoing[item.node]) {
            const Edge& edge = graph.edges[edge_index];
            if (tabu_nodes.count(edge.to) || tabu_edges.count(edge_key(edge.from, edge.to))) continue;
            const double candidate = item.travelled + edge.distance;
            auto dx = graph.nodes[edge.to].grid_x - graph.nodes[edge.from].grid_x;
            auto dy = graph.nodes[edge.to].grid_y - graph.nodes[edge.from].grid_y;
            const auto divisor = std::gcd(std::abs(dx), std::abs(dy));
            const Direction direction{dx / divisor, dy / divisor};
            const std::int32_t turns = item.turns
                + ((item.direction.x != 0 || item.direction.y != 0) && !(item.direction == direction));
            const State next{edge.to, direction};
            const auto old = costs.find(next);
            if (old == costs.end() || candidate + epsilon < old->second.first
                || (std::abs(candidate - old->second.first) <= epsilon && turns < old->second.second)) {
                costs[next] = {candidate, turns};
                previous[next] = state;
                queue.push({candidate + heuristic(edge.to), turns, candidate, edge.to, direction});
            }
        }
    }
    return false;
}

double predicted_motion_seconds(
    const Graph& graph,
    const Route& route,
    double heading,
    const MotionProfile& profile,
    double* final_heading
) {
    double seconds = 0.0;
    std::size_t index = 0;
    while (index + 1 < route.nodes.size()) {
        const auto& start = graph.nodes[route.nodes[index]];
        const auto& next = graph.nodes[route.nodes[index + 1]];
        const double run_heading = std::atan2(next.y - start.y, next.x - start.x);
        double distance = std::hypot(next.x - start.x, next.y - start.y);
        std::size_t tail = index + 1;
        while (tail + 1 < route.nodes.size()) {
            const auto& before = graph.nodes[route.nodes[tail]];
            const auto& after = graph.nodes[route.nodes[tail + 1]];
            const double dx = after.x - before.x;
            const double dy = after.y - before.y;
            const double candidate = std::atan2(dy, dx);
            if (std::abs(normalize_angle(candidate - run_heading)) > epsilon) break;
            distance += std::hypot(dx, dy);
            ++tail;
        }
        seconds += phase_seconds(
            std::abs(normalize_angle(run_heading - heading)),
            profile.angular_speed,
            profile.angular_acceleration
        );
        seconds += phase_seconds(distance, profile.linear_speed, profile.linear_acceleration);
        heading = run_heading;
        index = tail;
    }
    if (final_heading) *final_heading = normalize_angle(heading);
    return seconds;
}

double build_motion(
    const Graph& graph, const Route& route, std::int32_t job, std::int32_t robot,
    double now, double heading, const MotionProfile& profile, bool loaded,
    std::vector<MotionSegment>& output, double* final_heading
) {
    std::size_t index = 0;
    while (index + 1 < route.nodes.size()) {
        const auto& start = graph.nodes[route.nodes[index]];
        const auto& next = graph.nodes[route.nodes[index + 1]];
        const double run_heading = std::atan2(next.y - start.y, next.x - start.x);
        double distance = std::hypot(next.x - start.x, next.y - start.y);
        std::size_t tail = index + 1;
        while (tail + 1 < route.nodes.size()) {
            const auto& before = graph.nodes[route.nodes[tail]];
            const auto& after = graph.nodes[route.nodes[tail + 1]];
            const double dx = after.x - before.x, dy = after.y - before.y;
            if (std::abs(normalize_angle(std::atan2(dy, dx) - run_heading)) > epsilon) break;
            distance += std::hypot(dx, dy);
            ++tail;
        }
        const double turn = normalize_angle(run_heading - heading);
        const double sign = turn >= 0.0 ? 1.0 : -1.0;
        for (const auto& phase : phases(std::abs(turn), profile.angular_speed, profile.angular_acceleration)) {
            const double end_heading = normalize_angle(heading + sign * phase.amount);
            output.push_back({job, robot, 10 + phase.kind, now, now + phase.duration,
                start.x, start.y, start.x, start.y, heading, end_heading,
                sign * phase.start_rate,
                sign * (phase.end_rate - phase.start_rate) / phase.duration, loaded});
            now += phase.duration;
            heading = end_heading;
        }
        heading = run_heading;
        double x = start.x, y = start.y;
        for (const auto& phase : phases(distance, profile.linear_speed, profile.linear_acceleration)) {
            const double end_x = x + std::cos(heading) * phase.amount;
            const double end_y = y + std::sin(heading) * phase.amount;
            output.push_back({job, robot, phase.kind, now, now + phase.duration,
                x, y, end_x, end_y, heading, heading, phase.start_rate,
                (phase.end_rate - phase.start_rate) / phase.duration, loaded});
            now += phase.duration;
            x = end_x; y = end_y;
        }
        index = tail;
    }
    if (final_heading) *final_heading = normalize_angle(heading);
    return now;
}

std::vector<std::pair<std::int32_t, double>> crossing_times(
    const Graph& graph, const Route& route, const std::vector<MotionSegment>& segments,
    std::size_t first_segment
) {
    std::vector<std::pair<std::int32_t, double>> result;
    double previous_time = first_segment < segments.size() ? segments[first_segment].start_time : 0.0;
    for (std::size_t node_index = 1; node_index < route.nodes.size(); ++node_index) {
        const auto& target = graph.nodes[route.nodes[node_index]];
        bool found = false;
        for (std::size_t index = first_segment; index < segments.size(); ++index) {
            const auto& segment = segments[index];
            if (segment.kind >= 10 || segment.end_time + 1e-9 < previous_time) continue;
            const double dx = segment.end_x - segment.start_x;
            const double dy = segment.end_y - segment.start_y;
            const double length = std::hypot(dx, dy);
            if (length <= 1e-12) continue;
            const double projection = ((target.x - segment.start_x) * dx
                + (target.y - segment.start_y) * dy) / length;
            const double cross = dx * (target.y - segment.start_y) - dy * (target.x - segment.start_x);
            if (std::abs(cross) > 1e-7 || projection < -1e-9 || projection > length + 1e-9) continue;
            const double distance = std::min(length, std::max(0.0, projection));
            double elapsed = 0.0;
            if (std::abs(segment.acceleration) <= 1e-12) {
                elapsed = distance / segment.start_rate;
            } else {
                const double discriminant = std::max(0.0, segment.start_rate * segment.start_rate
                    + 2.0 * segment.acceleration * distance);
                const double root = std::sqrt(discriminant);
                const double first = (-segment.start_rate + root) / segment.acceleration;
                const double second = (-segment.start_rate - root) / segment.acceleration;
                const double duration = segment.end_time - segment.start_time;
                elapsed = (first >= -1e-9 && first <= duration + 1e-9) ? first : second;
            }
            const double reached = segment.start_time + std::max(0.0, elapsed);
            if (reached + 1e-9 >= previous_time) {
                result.push_back({route.nodes[node_index], reached});
                previous_time = reached;
                found = true;
                break;
            }
        }
        if (!found) return {};
    }
    return result;
}

}  // namespace amr
