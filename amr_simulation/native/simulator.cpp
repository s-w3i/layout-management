#include "simulator.hpp"
#include "routing_motion.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <functional>
#include <sstream>

namespace amr {

Simulator::Simulator(const AmrDayInput& input) { import(input); }

void Simulator::schedule(
    double time, EventKind kind, std::int32_t subject, std::int32_t value,
    std::uint64_t token
) {
    calendar_.push(Event{time, ++sequence_, kind, subject, value, token});
}

bool Simulator::import(const AmrDayInput& input) {
    if (!input.nodes || input.node_count < 1 || !input.edges || input.edge_count < 1
        || !input.racks || input.rack_count < 1 || !input.rack_skus
        || !input.tasks || input.task_count < 1 || !input.task_lines
        || !input.spawns || input.spawn_count < 1 || !input.stations
        || input.station_count < 1) {
        error_ = "invalid or empty native day input";
        return false;
    }
    config_ = input.config;
    day_year_ = input.day_year; day_month_ = input.day_month; day_day_ = input.day_day;
    graph_.nodes.reserve(input.node_count);
    graph_.outgoing.resize(input.node_count);
    for (std::int32_t index = 0; index < input.node_count; ++index) {
        const auto& node = input.nodes[index];
        if (!std::isfinite(node.x) || !std::isfinite(node.y)) {
            error_ = "node coordinates must be finite";
            return false;
        }
        graph_.nodes.push_back({node.x, node.y, node.grid_x, node.grid_y,
                                static_cast<std::uint8_t>(node.rack != 0)});
    }
    graph_.edges.reserve(input.edge_count);
    for (std::int32_t index = 0; index < input.edge_count; ++index) {
        const auto& edge = input.edges[index];
        if (edge.from < 0 || edge.from >= input.node_count || edge.to < 0
            || edge.to >= input.node_count || !std::isfinite(edge.distance)
            || edge.distance <= 0.0) {
            error_ = "invalid directed graph edge";
            return false;
        }
        graph_.edges.push_back({edge.from, edge.to, edge.distance, 0, 0});
        graph_.outgoing[edge.from].push_back(index);
    }
    for (auto& outgoing : graph_.outgoing) {
        std::sort(outgoing.begin(), outgoing.end(), [&](auto first, auto second) {
            return graph_.edges[first].to < graph_.edges[second].to;
        });
    }
    racks_.reserve(input.rack_count);
    for (std::int32_t index = 0; index < input.rack_count; ++index) {
        const auto& source = input.racks[index];
        if (source.node < 0 || source.node >= input.node_count || source.sku_offset < 0
            || source.sku_count < 1 || source.sku_offset + source.sku_count > input.rack_sku_count) {
            error_ = "invalid rack or rack SKU range";
            return false;
        }
        RackState rack;
        rack.node = source.node;
        rack.skus.assign(
            input.rack_skus + source.sku_offset,
            input.rack_skus + source.sku_offset + source.sku_count
        );
        std::sort(rack.skus.begin(), rack.skus.end());
        rack.skus.erase(std::unique(rack.skus.begin(), rack.skus.end()), rack.skus.end());
        racks_.push_back(std::move(rack));
        for (const auto sku : racks_.back().skus) racks_by_sku_[sku].push_back(index);
    }
    stations_.reserve(input.station_count);
    for (std::int32_t index = 0; index < input.station_count; ++index) {
        if (input.stations[index] < 0 || input.stations[index] >= input.node_count) {
            error_ = "invalid workstation node";
            return false;
        }
        StationState station;
        station.node = input.stations[index];
        stations_.push_back(station);
    }
    tasks_.reserve(input.task_count);
    for (std::int32_t index = 0; index < input.task_count; ++index) {
        const auto& source = input.tasks[index];
        if (source.station < 0 || source.station >= input.station_count
            || source.line_offset < 0 || source.line_count < 1
            || source.line_offset + source.line_count > input.task_line_count) {
            error_ = "invalid task or task-line range";
            return false;
        }
        TaskState task;
        task.station = source.station;
        for (std::int32_t line = source.line_offset; line < source.line_offset + source.line_count; ++line) {
            if (input.task_lines[line].count < 1) {
                error_ = "task line count must be positive";
                return false;
            }
            task.outstanding[input.task_lines[line].sku] += input.task_lines[line].count;
            task.source_lines += input.task_lines[line].count;
        }
        tasks_.push_back(std::move(task));
        schedule(0.0, EventKind::task_release, index);
    }
    robots_.reserve(input.spawn_count);
    for (std::int32_t index = 0; index < input.spawn_count; ++index) {
        const auto& source = input.spawns[index];
        if (source.node < 0 || source.node >= input.node_count
            || !std::isfinite(source.heading) || node_owner_.count(source.node)) {
            error_ = "invalid or duplicate AMR spawn";
            return false;
        }
        AmrState robot;
        robot.position = source.node;
        robot.heading = source.heading;
        robots_.push_back(robot);
        node_owner_[source.node] = index;
    }
    max_reserved_nodes_ = static_cast<std::int32_t>(node_owner_.size());
    return true;
}

bool Simulator::calendar_order_self_test() {
    std::priority_queue<Event, std::vector<Event>, LaterEvent> values;
    values.push({2.0, 1, EventKind::task_release, 0, 0, 0});
    values.push({1.0, 3, EventKind::task_release, 0, 0, 0});
    values.push({1.0, 2, EventKind::task_release, 0, 0, 0});
    const auto first = values.top(); values.pop();
    const auto second = values.top(); values.pop();
    const auto third = values.top();
    return first.time == 1.0 && first.sequence == 2
        && second.time == 1.0 && second.sequence == 3
        && third.time == 2.0 && third.sequence == 1;
}

bool Simulator::route_for(std::int32_t start, std::int32_t goal, Route& route) const {
    const auto key = (static_cast<std::uint64_t>(static_cast<std::uint32_t>(start)) << 32)
        | static_cast<std::uint32_t>(goal);
    const auto cached = static_route_cache_.find(key);
    if (cached != static_route_cache_.end()) { route = cached->second; return true; }
    if (failed_static_routes_.count(key)) return false;
    std::unordered_set<std::int32_t> blocked;
    for (std::int32_t node = 0; node < static_cast<std::int32_t>(graph_.nodes.size()); ++node) {
        if (graph_.nodes[node].rack && node != start && node != goal) blocked.insert(node);
    }
    if (!route_astar(graph_, start, goal, blocked, {}, route)) {
        failed_static_routes_.insert(key); return false;
    }
    static_route_cache_[key] = route;
    return true;
}

bool Simulator::route_for(const JobState& job, Route& route) const {
    std::unordered_set<std::int32_t> blocked = job.tabu_nodes;
    for (std::int32_t node = 0; node < static_cast<std::int32_t>(graph_.nodes.size()); ++node) {
        if (graph_.nodes[node].rack && node != robots_[job.robot].position && node != job.goal)
            blocked.insert(node);
    }
    return route_astar(
        graph_, robots_[job.robot].position, job.goal, blocked, job.tabu_edges, route
    );
}

bool Simulator::hard_station_wait(const JobState& job) const {
    if (job.stage != Stage::station) return false;
    const auto& path = job.route.nodes;
    std::vector<std::size_t> turns;
    for (std::size_t index = 1; index + 1 < path.size(); ++index) {
        const auto& a = graph_.nodes[path[index - 1]];
        const auto& b = graph_.nodes[path[index]];
        const auto& c = graph_.nodes[path[index + 1]];
        if (std::abs((b.x-a.x)*(c.y-b.y) - (b.y-a.y)*(c.x-b.x)) > 1e-9)
            turns.push_back(index);
    }
    return turns.empty() || (turns.size() == 1 && turns.front() == 1);
}

bool Simulator::higher_priority(const JobState& first, const JobState& second) const {
    if (config_.loaded_priority && first.loaded != second.loaded) return first.loaded;
    if (first.route.distance != second.route.distance) return first.route.distance < second.route.distance;
    if (first.request_time != second.request_time) return first.request_time < second.request_time;
    return first.robot < second.robot;
}

std::vector<std::int32_t> Simulator::remaining_path(const JobState& job) const {
    const auto position = robots_[job.robot].position;
    const auto found = std::find(job.route.nodes.begin(), job.route.nodes.end(), position);
    if (found != job.route.nodes.end()) return {found, job.route.nodes.end()};
    Route route;
    if (route_for(job, route)) return route.nodes;
    return {position};
}

std::int32_t Simulator::active_job_for_robot(std::int32_t robot) const {
    for (std::int32_t index = static_cast<std::int32_t>(jobs_.size()) - 1; index >= 0; --index)
        if (jobs_[index].robot == robot && jobs_[index].completion_time < 0.0) return index;
    return -1;
}

bool Simulator::same_direction_following(
    const JobState& follower, std::int32_t leader_index, std::int32_t blocked
) const {
    if (!config_.mutex_passage || leader_index < 0) return false;
    const auto& leader = jobs_[leader_index];
    const auto follower_path = remaining_path(follower);
    const auto leader_path = remaining_path(leader);
    const auto first = std::find(follower_path.begin(), follower_path.end(), blocked);
    const auto second = std::find(leader_path.begin(), leader_path.end(), blocked);
    return first != follower_path.end() && second != leader_path.end()
        && first + 1 != follower_path.end() && second + 1 != leader_path.end()
        && *(first + 1) == *(second + 1);
}

void Simulator::set_wait(const JobState& job, const std::vector<std::int32_t>& blockers) {
    if (!config_.mutex_passage || blockers.empty()) return;
    wait_for_[job.robot] = std::unordered_set<std::int32_t>(blockers.begin(), blockers.end());
}

void Simulator::clear_wait(JobState& job, double now) {
    wait_for_.erase(job.robot);
    if (job.following_wait_since >= 0.0) {
        following_wait_seconds_ += now - job.following_wait_since;
        job.following_wait_since = -1.0;
    }
    if (job.corridor_wait_since >= 0.0) {
        corridor_wait_seconds_ += now - job.corridor_wait_since;
        job.corridor_wait_since = -1.0;
    }
    if (job.dram_wait_since >= 0.0) {
        const auto waited = now - job.dram_wait_since;
        job.dram_wait_seconds += waited;
        dram_wait_seconds_ += waited;
        job.dram_wait_since = -1.0;
    }
}

std::vector<std::int32_t> Simulator::wait_cycle() const {
    std::vector<std::int32_t> roots;
    for (const auto& item : wait_for_) roots.push_back(item.first);
    std::sort(roots.begin(), roots.end());
    std::vector<std::int32_t> visiting;
    std::unordered_set<std::int32_t> visited;
    std::function<std::vector<std::int32_t>(std::int32_t)> visit = [&](std::int32_t node) {
        const auto current = std::find(visiting.begin(), visiting.end(), node);
        if (current != visiting.end()) return std::vector<std::int32_t>(current, visiting.end());
        if (visited.count(node)) return std::vector<std::int32_t>{};
        visiting.push_back(node);
        std::vector<std::int32_t> blockers;
        const auto found = wait_for_.find(node);
        if (found != wait_for_.end()) blockers.assign(found->second.begin(), found->second.end());
        std::sort(blockers.begin(), blockers.end());
        for (const auto blocker : blockers) {
            auto cycle = visit(blocker);
            if (!cycle.empty()) return cycle;
        }
        visiting.pop_back(); visited.insert(node);
        return std::vector<std::int32_t>{};
    };
    for (const auto root : roots) {
        auto cycle = visit(root);
        if (!cycle.empty()) return cycle;
    }
    return {};
}

bool Simulator::coordination_reroute(std::int32_t job_index, double now) {
    auto& job = jobs_[job_index];
    const auto path = remaining_path(job);
    if (hard_station_wait(job) || path.size() < 2) return false;
    if (coordination_reroute(job_index, now, path[0], path[1])) return true;
    Route route;
    if (!parking_route(job, route)) return false;
    clear_wait(job, now);
    job.parking_backoff = true;
    job.route = std::move(route);
    job.blocked_node = -1; job.blocked_reason = 0; job.conflict_robots.clear();
    ++job.timeout_generation; ++job.reroutes; ++reroutes_;
    pending_dirty_.insert(job_index);
    for (const auto pending : pending_) {
        const auto& waiting = jobs_[pending];
        if (waiting.blocked_reason == 2
            && std::find(waiting.conflict_robots.begin(), waiting.conflict_robots.end(), job.robot)
            != waiting.conflict_robots.end()) pending_dirty_.insert(pending);
    }
    return true;
}

bool Simulator::coordination_reroute(
    std::int32_t job_index, double now, std::int32_t from, std::int32_t to
) {
    auto& job = jobs_[job_index];
    if (hard_station_wait(job)) return false;
    const std::uint64_t edge = (static_cast<std::uint64_t>(static_cast<std::uint32_t>(from)) << 32)
        | static_cast<std::uint32_t>(to);
    job.tabu_edges.insert(edge);
    Route route;
    if (!route_for(job, route)) { job.tabu_edges.erase(edge); return false; }
    clear_wait(job, now);
    job.route = std::move(route);
    job.blocked_node = -1; job.blocked_reason = 0; job.conflict_robots.clear();
    ++job.timeout_generation; ++job.reroutes; ++reroutes_;
    pending_dirty_.insert(job_index);
    for (const auto pending : pending_) {
        const auto& waiting = jobs_[pending];
        if (waiting.blocked_reason == 2
            && std::find(waiting.conflict_robots.begin(), waiting.conflict_robots.end(), job.robot)
            != waiting.conflict_robots.end()) pending_dirty_.insert(pending);
    }
    return true;
}

std::vector<std::int32_t> Simulator::reversed_passage(
    const JobState& first, const JobState& second
) const {
    const auto a = remaining_path(first); const auto b = remaining_path(second);
    std::vector<std::int32_t> best;
    for (std::size_t i = 0; i + 1 < a.size(); ++i) {
        for (std::size_t j = 0; j + 1 < b.size(); ++j) {
            if (a[i] != b[j + 1] || a[i + 1] != b[j]) continue;
            std::size_t left = i, right = i + 1, other_left = j, other_right = j + 1;
            while (left > 0 && other_right + 1 < b.size() && a[left - 1] == b[other_right + 1]) {
                --left; ++other_right;
            }
            while (right + 1 < a.size() && other_left > 0 && a[right + 1] == b[other_left - 1]) {
                ++right; --other_left;
            }
            std::vector<std::int32_t> candidate(a.begin() + left, a.begin() + right + 1);
            if (candidate.size() > best.size() || (candidate.size() == best.size() && candidate < best))
                best = std::move(candidate);
        }
    }
    return best;
}

std::vector<std::int32_t> Simulator::following_queue(std::int32_t front) const {
    std::unordered_map<std::int32_t, std::vector<std::int32_t>> reverse;
    for (const auto& item : wait_for_)
        for (const auto blocker : item.second) reverse[blocker].push_back(item.first);
    for (auto& item : reverse) std::sort(item.second.begin(), item.second.end());
    std::vector<std::int32_t> result, pending{front};
    std::unordered_set<std::int32_t> seen;
    for (std::size_t cursor = 0; cursor < pending.size(); ++cursor) {
        const auto robot = pending[cursor];
        if (!seen.insert(robot).second) continue;
        result.push_back(robot);
        const auto found = reverse.find(robot);
        if (found != reverse.end()) pending.insert(pending.end(), found->second.begin(), found->second.end());
    }
    return result;
}

std::int32_t Simulator::corridor_resolution(
    std::int32_t selected_index, std::int32_t other_robot, double now
) {
    if (!config_.corridor_coordination || hard_station_wait(jobs_[selected_index])) return 0;
    const auto other_index = active_job_for_robot(other_robot);
    if (other_index < 0 || !pending_.count(selected_index) || !pending_.count(other_index)) return 0;
    const auto selected_robot = jobs_[selected_index].robot;
    if (!wait_for_.count(selected_robot) || !wait_for_.at(selected_robot).count(other_robot)
        || !wait_for_.count(other_robot) || !wait_for_.at(other_robot).count(selected_robot)) return 0;
    auto nodes = reversed_passage(jobs_[selected_index], jobs_[other_index]);
    if (nodes.size() < 2) return 0;
    PassageState* passage = nullptr;
    std::vector<std::int32_t> sorted_nodes = nodes;
    std::sort(sorted_nodes.begin(), sorted_nodes.end());
    for (auto& candidate : passages_) {
        auto existing = candidate.nodes; std::sort(existing.begin(), existing.end());
        if (existing == sorted_nodes) { passage = &candidate; break; }
    }
    if (!passage) {
        const auto first_queue = following_queue(selected_robot);
        const auto second_queue = following_queue(other_robot);
        auto has_unloaded = [&](const auto& queue) {
            for (const auto robot : queue) {
                const auto index = active_job_for_robot(robot);
                if (index >= 0 && !jobs_[index].loaded) return true;
            }
            return false;
        };
        std::int32_t yielding_front;
        if (has_unloaded(first_queue) != has_unloaded(second_queue))
            yielding_front = has_unloaded(first_queue) ? selected_robot : other_robot;
        else if (first_queue.size() != second_queue.size())
            yielding_front = first_queue.size() < second_queue.size() ? selected_robot : other_robot;
        else {
            const auto first_index = active_job_for_robot(selected_robot);
            const auto second_index = active_job_for_robot(other_robot);
            yielding_front = higher_priority(jobs_[first_index], jobs_[second_index])
                ? other_robot : selected_robot;
        }
        const auto holding_front = yielding_front == selected_robot ? other_robot : selected_robot;
        const auto holding_index = active_job_for_robot(holding_front);
        const auto yielding_queue = yielding_front == selected_robot ? first_queue : second_queue;
        const auto holding_path = remaining_path(jobs_[holding_index]);
        std::sort(nodes.begin(), nodes.end(), [&](auto a, auto b) {
            return std::find(holding_path.begin(), holding_path.end(), a)
                < std::find(holding_path.begin(), holding_path.end(), b);
        });
        passages_.push_back({nodes, holding_front, nodes.front(), nodes.back(), yielding_queue});
        passage = &passages_.back();
        ++corridor_conflicts_; ++corridor_ownership_changes_;
        for (auto iterator = yielding_queue.rbegin(); iterator != yielding_queue.rend(); ++iterator) {
            const auto victim = active_job_for_robot(*iterator);
            if (victim < 0 || jobs_[victim].loaded) continue;
            const auto path = remaining_path(jobs_[victim]);
            auto entrance = path.end();
            for (const auto node : passage->nodes) {
                const auto found = std::find(path.begin(), path.end(), node);
                if (found != path.end() && (entrance == path.end() || found < entrance)) entrance = found;
            }
            if (entrance == path.end()) continue;
            const auto offset = static_cast<std::size_t>(entrance - path.begin());
            if (offset == 0 && path.size() < 2) continue;
            const auto from = offset ? path[offset - 1] : path[0];
            const auto to = offset ? path[offset] : path[1];
            if (coordination_reroute(victim, now, from, to)) {
                corridor_yielding_robots_.insert(*iterator);
                if (victim == selected_index) return 2;
                break;
            }
        }
    }
    const auto path = remaining_path(jobs_[selected_index]);
    std::vector<std::size_t> indices;
    for (std::size_t index = 0; index < path.size(); ++index)
        if (std::find(passage->nodes.begin(), passage->nodes.end(), path[index]) != passage->nodes.end())
            indices.push_back(index);
    if (indices.size() >= 2 && path[indices.front()] == passage->direction_start
        && path[indices.back()] == passage->direction_end) return 1;
    return 0;
}

void Simulator::release_passages() {
    passages_.erase(std::remove_if(passages_.begin(), passages_.end(), [&](const auto& passage) {
        for (const auto node : passage.nodes) if (node_owner_.count(node)) return false;
        const auto owner = active_job_for_robot(passage.owner_robot);
        if (owner >= 0) {
            std::size_t count = 0;
            for (const auto node : remaining_path(jobs_[owner]))
                if (std::find(passage.nodes.begin(), passage.nodes.end(), node) != passage.nodes.end()) ++count;
            if (count >= 2) return false;
        }
        return true;
    }), passages_.end());
}

bool Simulator::parking_route(const JobState& job, Route& route) const {
    const auto current = robots_[job.robot].position;
    std::vector<std::pair<double, std::int32_t>> candidates;
    for (const auto edge_index : graph_.outgoing[current]) {
        const auto& edge = graph_.edges[edge_index];
        if (node_owner_.count(edge.to) || graph_.nodes[edge.to].rack) continue;
        std::unordered_set<std::int32_t> blocked{current};
        for (std::int32_t node = 0; node < static_cast<std::int32_t>(graph_.nodes.size()); ++node)
            if (graph_.nodes[node].rack && node != edge.to && node != job.goal) blocked.insert(node);
        Route continuation;
        if (route_astar(graph_, edge.to, job.goal, blocked, {}, continuation))
            candidates.push_back({edge.distance, edge.to});
    }
    if (candidates.empty()) return false;
    const auto selected = *std::min_element(candidates.begin(), candidates.end());
    route.nodes = {current, selected.second}; route.distance = selected.first;
    return true;
}

std::int32_t Simulator::break_wait_cycle(double now) {
    const auto cycle = wait_cycle();
    if (cycle.empty()) return -1;
    std::vector<std::int32_t> candidates;
    for (const auto robot : cycle) {
        const auto job = active_job_for_robot(robot);
        if (job >= 0 && !jobs_[job].loaded) candidates.push_back(job);
    }
    if (candidates.empty())
        for (const auto robot : cycle) { const auto job = active_job_for_robot(robot); if (job >= 0) candidates.push_back(job); }
    if (candidates.empty()) return -1;
    auto victim = candidates.front();
    for (const auto candidate : candidates)
        if (higher_priority(jobs_[victim], jobs_[candidate])) victim = candidate;
    ++wait_for_cycles_;
    if (coordination_reroute(victim, now)) { ++cycle_breaking_reroutes_; return victim; }
    return -1;
}

bool Simulator::dram_denied(
    std::int32_t selected_index, std::int32_t candidate,
    std::vector<std::int32_t>& blockers, std::int32_t& overlap,
    std::int32_t& kind
) const {
    const auto& selected = jobs_[selected_index];
    const auto selected_remaining = remaining_path(selected);
    const auto found = std::find(selected_remaining.begin(), selected_remaining.end(), candidate);
    if (found == selected_remaining.end()) return false;
    const std::vector<std::int32_t> selected_path(found, selected_remaining.end());
    for (std::int32_t index = 0; index < static_cast<std::int32_t>(jobs_.size()); ++index) {
        if (index == selected_index) continue;
        const auto& other = jobs_[index];
        if (other.completion_time >= 0.0 || other.route.nodes.size() < 2) continue;
        const auto path = remaining_path(other);
        bool conflict = false;
        for (std::size_t length = 1; length < std::min(selected_path.size(), path.size()); ++length) {
            bool match = true;
            for (std::size_t offset = 0; offset <= length; ++offset) {
                if (selected_path[offset] != path[length - offset]) { match = false; break; }
            }
            if (match) { conflict = true; overlap = selected_path[1]; break; }
        }
        if (conflict && higher_priority(other, selected)) {
            kind = 1;
            blockers = {other.robot};
            return true;
        }
    }
    std::unordered_map<std::int32_t, std::pair<std::int32_t, std::int32_t>> edges;
    for (std::int32_t index = 0; index < static_cast<std::int32_t>(jobs_.size()); ++index) {
        const auto& job = jobs_[index];
        if (job.completion_time >= 0.0 || job.route.nodes.size() < 2) continue;
        if (index == selected_index) {
            if (found + 1 != selected_remaining.end()) edges[candidate] = {*(found + 1), index};
        } else {
            const auto path = remaining_path(job);
            if (path.size() >= 2) edges[path[0]] = {path[1], index};
        }
    }
    std::unordered_map<std::int32_t, std::size_t> visited;
    std::vector<std::pair<std::int32_t, std::int32_t>> order;
    auto node = candidate;
    while (edges.count(node)) {
        const auto prior = visited.find(node);
        if (prior != visited.end()) {
            std::unordered_set<std::int32_t> participants;
            for (std::size_t index = prior->second; index < order.size(); ++index)
                participants.insert(jobs_[order[index].second].robot);
            blockers.assign(participants.begin(), participants.end());
            std::sort(blockers.begin(), blockers.end());
            blockers.erase(std::remove(blockers.begin(), blockers.end(), selected.robot), blockers.end());
            overlap = node;
            kind = 2;
            return true;
        }
        visited[node] = order.size();
        const auto edge = edges[node];
        order.push_back({node, edge.second});
        node = edge.first;
    }
    return false;
}

bool Simulator::dispatch_one(double now) {
    std::int32_t chosen_task = -1;
    std::int32_t chosen_rack = -1;
    std::int32_t chosen_robot = -1;
    std::vector<std::int32_t> chosen_skus;
    Route chosen_route;
    for (std::int32_t task_index = 0; task_index < static_cast<std::int32_t>(tasks_.size()); ++task_index) {
        auto& task = tasks_[task_index];
        if (!task.released || task.outstanding.empty()) continue;
        struct RackChoice {
            std::int32_t negative_coverage;
            double station_distance;
            std::int32_t rack;
            std::vector<std::int32_t> skus;
        };
        std::vector<RackChoice> rack_choices;
        const auto& station_node = graph_.nodes[stations_[task.station].node];
        for (std::int32_t rack_index = 0; rack_index < static_cast<std::int32_t>(racks_.size()); ++rack_index) {
            const auto& rack = racks_[rack_index];
            if (rack.reserved) continue;
            std::vector<std::int32_t> covered;
            for (const auto sku : rack.skus) {
                if (task.outstanding.count(sku)) covered.push_back(sku);
            }
            if (covered.empty()) continue;
            const auto& rack_node = graph_.nodes[rack.node];
            rack_choices.push_back({
                -static_cast<std::int32_t>(covered.size()),
                std::hypot(station_node.x - rack_node.x, station_node.y - rack_node.y),
                rack_index,
                std::move(covered),
            });
        }
        std::sort(rack_choices.begin(), rack_choices.end(), [](const auto& first, const auto& second) {
            if (first.negative_coverage != second.negative_coverage)
                return first.negative_coverage < second.negative_coverage;
            if (first.station_distance != second.station_distance)
                return first.station_distance < second.station_distance;
            return first.rack < second.rack;
        });
        for (const auto& rack_choice : rack_choices) {
            const auto& rack = racks_[rack_choice.rack];
            const auto owner = node_owner_.find(rack.node);
            double best_time = std::numeric_limits<double>::infinity();
            std::int32_t best_robot = -1;
            Route best_route;
            for (std::int32_t robot = 0; robot < static_cast<std::int32_t>(robots_.size()); ++robot) {
                if (!robots_[robot].free || (owner != node_owner_.end() && owner->second != robot)) continue;
                Route pickup;
                if (!route_for(robots_[robot].position, rack.node, pickup)) continue;
                double final_heading = 0.0;
                const double predicted = predicted_motion_seconds(
                    graph_, pickup, robots_[robot].heading,
                    {config_.linear_speed, config_.linear_acceleration,
                     config_.angular_speed, config_.angular_acceleration},
                    &final_heading
                );
                if (predicted < best_time || (predicted == best_time && robot < best_robot)) {
                    best_time = predicted;
                    best_robot = robot;
                    best_route = std::move(pickup);
                }
            }
            if (best_robot >= 0) {
                chosen_task = task_index;
                chosen_rack = rack_choice.rack;
                chosen_robot = best_robot;
                chosen_skus = rack_choice.skus;
                chosen_route = std::move(best_route);
                break;
            }
        }
        if (chosen_task >= 0) break;
    }
    if (chosen_task < 0) return false;
    JobState job;
    job.task = chosen_task;
    job.rack = chosen_rack;
    job.robot = chosen_robot;
    job.station = tasks_[chosen_task].station;
    job.route = std::move(chosen_route);
    job.traversed.push_back(robots_[chosen_robot].position);
    job.dispatch_time = now;
    for (const auto sku : chosen_skus) {
        job.lines[sku] = tasks_[chosen_task].outstanding[sku];
        tasks_[chosen_task].outstanding.erase(sku);
    }
    ++tasks_[chosen_task].inflight;
    racks_[chosen_rack].reserved = true;
    robots_[chosen_robot].free = false;
    jobs_.push_back(std::move(job));
    start_stage(
        static_cast<std::int32_t>(jobs_.size() - 1),
        std::move(jobs_.back().route), now, Stage::pickup, false,
        EventKind::pickup_arrival
    );
    return true;
}

std::int32_t Simulator::dispatch_fixture(double now) {
    for (auto& task : tasks_) task.released = true;
    while (dispatch_one(now)) {}
    return static_cast<std::int32_t>(jobs_.size());
}

void Simulator::start_stage(
    std::int32_t job_index, Route route, double now, Stage stage,
    bool loaded, EventKind arrival
) {
    auto& job = jobs_[job_index];
    job.stage = stage;
    job.loaded = loaded;
    job.arrival = arrival;
    job.goal = route.nodes.back();
    job.route = std::move(route);
    job.route_cursor = 0;
    job.tabu_nodes.clear();
    job.tabu_edges.clear();
    job.request_time = -1.0;
    job.wait_since = -1.0;
    job.blocked_node = -1;
    ++job.timeout_generation;
    if (job.route.nodes.size() == 1) schedule(now, arrival, job_index);
    else request_reservation(job_index, now);
}

void Simulator::request_reservation(std::int32_t job_index, double now) {
    auto& job = jobs_[job_index];
    if (job.request_time < 0.0) job.request_time = now;
    if (job.wait_since < 0.0) job.wait_since = now;
    pending_.insert(job_index);
    pending_dirty_.insert(job_index);
}

void Simulator::move_reserved(
    std::int32_t job_index, const std::vector<std::int32_t>& nodes, double now
) {
    auto& job = jobs_[job_index];
    auto& robot = robots_[job.robot];
    Route route;
    route.nodes.reserve(nodes.size() + 1);
    route.nodes.push_back(robot.position);
    route.nodes.insert(route.nodes.end(), nodes.begin(), nodes.end());
    route.distance = 0.0;
    for (std::size_t index = 1; index < route.nodes.size(); ++index) {
        for (const auto edge_index : graph_.outgoing[route.nodes[index - 1]]) {
            const auto& edge = graph_.edges[edge_index];
            if (edge.to == route.nodes[index]) { route.distance += edge.distance; break; }
        }
    }
    const auto first_segment = motion_segments_.size();
    double final_heading = robot.heading;
    const double end = build_motion(
        graph_, route, job_index, job.robot, now, robot.heading,
        {config_.linear_speed, config_.linear_acceleration,
         config_.angular_speed, config_.angular_acceleration},
        job.loaded, motion_segments_, &final_heading
    );
    const double duration = end - now;
    job.travel_seconds += duration;
    job.travel_distance += route.distance;
    robot.travel_seconds += duration;
    robot.travel_distance += route.distance;
    for (const auto& crossing : crossing_times(graph_, route, motion_segments_, first_segment)) {
        schedule(crossing.second, EventKind::node_crossing, job_index, crossing.first);
    }
    job.movement_heading = final_heading;
    Route remaining;
    const auto consumed = nodes.size();
    remaining.nodes.assign(job.route.nodes.begin() + consumed, job.route.nodes.end());
    remaining.distance = 0.0;
    for (std::size_t index = 1; index < remaining.nodes.size(); ++index) {
        for (const auto edge_index : graph_.outgoing[remaining.nodes[index - 1]]) {
            const auto& edge = graph_.edges[edge_index];
            if (edge.to == remaining.nodes[index]) { remaining.distance += edge.distance; break; }
        }
    }
    movement_tails_.push_back({std::move(remaining), final_heading});
    schedule(end, EventKind::movement_tail, job_index,
             static_cast<std::int32_t>(movement_tails_.size() - 1));
}

void Simulator::resolve_reservations(double now) {
    if (pending_dirty_.empty()) return;
    std::vector<std::int32_t> ordered(pending_dirty_.begin(), pending_dirty_.end());
    pending_dirty_.clear();
    std::sort(ordered.begin(), ordered.end(), [&](auto first, auto second) {
        return higher_priority(jobs_[first], jobs_[second]);
    });
    for (const auto job_index : ordered) {
        if (!pending_.count(job_index)) continue;
        auto& job = jobs_[job_index];
        const auto& path = job.route.nodes;
        std::size_t limit = path.size();
        bool atomic = false;
        for (std::size_t index = 1; index + 1 < path.size(); ++index) {
            const auto& before = graph_.nodes[path[index - 1]];
            const auto& current = graph_.nodes[path[index]];
            const auto& after = graph_.nodes[path[index + 1]];
            const double ax = current.x - before.x, ay = current.y - before.y;
            const double bx = after.x - current.x, by = after.y - current.y;
            if (std::abs(ax * by - ay * bx) > 1e-9) {
                if (index > 1) limit = index;
                else { limit = std::min<std::size_t>(3, path.size()); atomic = true; }
                break;
            }
        }
        std::vector<std::int32_t> available;
        std::int32_t blocker = -1;
        std::int32_t blocking_robot = -1;
        for (std::size_t index = 1; index < limit; ++index) {
            const auto owner = node_owner_.find(path[index]);
            if (owner != node_owner_.end() && owner->second != job.robot) {
                blocker = path[index]; blocking_robot = owner->second; break;
            }
            available.push_back(path[index]);
        }
        std::vector<std::int32_t> dram_blockers;
        bool corridor_rerouted = false;
        bool corridor_blocked = false;
        if (blocker < 0) {
            std::vector<std::int32_t> safe;
            for (const auto node : available) {
                std::int32_t dram_overlap = -1;
                std::int32_t dram_kind = 0;
                std::vector<std::int32_t> candidate_blockers;
                if (dram_denied(job_index, node, candidate_blockers, dram_overlap, dram_kind)) {
                    if (dram_kind == 1) {
                        const auto corridor = corridor_resolution(
                            job_index, candidate_blockers.front(), now
                        );
                        if (corridor == 1) { safe.push_back(node); continue; }
                        if (corridor == 2) { corridor_rerouted = true; break; }
                        if (config_.corridor_coordination) corridor_blocked = true;
                    }
                    blocker = dram_overlap;
                    dram_blockers = std::move(candidate_blockers);
                    break;
                }
                safe.push_back(node);
            }
            available = std::move(safe);
        }
        if (corridor_rerouted) continue;
        if (atomic && blocker >= 0) available.clear();
        if (available.empty()) {
            const auto leader_job = active_job_for_robot(blocking_robot);
            const bool following = blocking_robot >= 0
                && same_direction_following(job, leader_job, blocker);
            const auto reason = following ? 3 : (corridor_blocked ? 4 : (dram_blockers.empty() ? 1 : 2));
            std::vector<std::int32_t> blockers = dram_blockers;
            if (blockers.empty() && blocking_robot >= 0) blockers.push_back(blocking_robot);
            const bool changed = job.blocked_reason != reason || job.blocked_node != blocker
                || job.conflict_robots != blockers;
            job.blocked_node = blocker;
            job.blocked_reason = reason;
            job.conflict_robots = blockers;
            ++job.reservation_conflicts;
            job.denial_times.push_back(now);
            job.denial_reasons.push_back(reason);
            job.denial_nodes.push_back(blocker);
            ++reservation_conflicts_;
            if (reason == 2 || reason == 4) ++dram_conflicts_; else ++node_ownership_conflicts_;
            if (job.wait_since < 0.0) job.wait_since = now;
            if (hard_station_wait(job)) {
                if (job.station_queue_enter < 0.0) {
                    job.station_queue_enter = now;
                    job.station_queue_position = robots_[job.robot].position;
                }
                std::int32_t waiting = 0;
                for (const auto pending : pending_)
                    if (hard_station_wait(jobs_[pending])) ++waiting;
                stations_[job.station].max_queue = std::max(stations_[job.station].max_queue, waiting);
            }
            if (changed) ++job.timeout_generation;
            if (changed && reason == 2 && job.dram_wait_since < 0.0)
                job.dram_wait_since = now;
            else if (changed && reason != 2 && job.dram_wait_since >= 0.0) {
                const auto waited = now - job.dram_wait_since;
                job.dram_wait_seconds += waited; dram_wait_seconds_ += waited;
                job.dram_wait_since = -1.0;
            }
            if (changed && config_.loaded_priority && job.loaded)
                ++loaded_protected_waits_;
            set_wait(job, blockers);
            if (following && job.following_wait_since < 0.0) {
                job.following_wait_since = now;
                ++following_avoided_reroutes_;
            }
            if (corridor_blocked && job.corridor_wait_since < 0.0)
                job.corridor_wait_since = now;
            if (changed && !(config_.loaded_priority && job.loaded) && !hard_station_wait(job)
                && blocker != job.goal && !following && !corridor_blocked) {
                schedule(now + (reason == 2 ? config_.dram_wait_seconds : config_.reservation_wait_seconds),
                         EventKind::reservation_timeout, job_index, 0,
                         job.timeout_generation);
            } else if (changed && (following || corridor_blocked
                                   || (config_.loaded_priority && job.loaded))) {
                schedule(now + config_.dram_wait_seconds, EventKind::coordination_timeout,
                         job_index, 0, job.timeout_generation);
            }
            continue;
        }
        pending_.erase(job_index);
        if (config_.loaded_priority && job.loaded) ++loaded_priority_grants_;
        for (const auto node : available) node_owner_[node] = job.robot;
        max_reserved_nodes_ = std::max(max_reserved_nodes_, static_cast<std::int32_t>(node_owner_.size()));
        if (job.wait_since >= 0.0 && now - job.wait_since > 0.0)
            job.node_wait_seconds += now - job.wait_since;
        job.request_time = job.wait_since = -1.0;
        job.blocked_node = -1;
        job.blocked_reason = 0;
        job.conflict_robots.clear();
        clear_wait(job, now);
        if (job.stage == Stage::station &&
            std::find(available.begin(), available.end(), job.goal) != available.end()) {
            auto& station = stations_[job.station];
            if (job.station_queue_enter < 0.0) {
                job.station_queue_enter = now;
                job.station_queue_position = robots_[job.robot].position;
            }
            job.station_admitted = now;
            station.queue_wait_seconds += now - job.station_queue_enter;
        }
        ++job.timeout_generation;
        move_reserved(job_index, available, now);
    }
}

void Simulator::handle(const Event& event) {
    if (event.kind == EventKind::task_release) {
        tasks_[event.subject].released = true;
        dispatch_needed_ = true;
        return;
    }
    auto& job = jobs_[event.subject];
    auto& robot = robots_[job.robot];
    if (event.kind == EventKind::coordination_timeout) {
        if (!pending_.count(event.subject) || event.token != job.timeout_generation) return;
        if (break_wait_cycle(event.time) < 0) {
            if (job.loaded) {
                std::vector<std::int32_t> blockers;
                for (const auto robot : job.conflict_robots) {
                    const auto index = active_job_for_robot(robot);
                    if (index >= 0) blockers.push_back(index);
                }
                std::vector<std::int32_t> unloaded;
                for (const auto index : blockers) if (!jobs_[index].loaded) unloaded.push_back(index);
                if (!unloaded.empty()) {
                    auto victim = unloaded.front();
                    for (const auto candidate : unloaded)
                        if (higher_priority(jobs_[victim], jobs_[candidate])) victim = candidate;
                    const auto path = remaining_path(jobs_[victim]);
                    if (path.size() >= 2
                        && coordination_reroute(victim, event.time, path[0], path[1])) return;
                }
                blockers.push_back(event.subject);
                auto victim = blockers.front();
                for (const auto candidate : blockers)
                    if (higher_priority(jobs_[victim], jobs_[candidate])) victim = candidate;
                const auto path = remaining_path(jobs_[victim]);
                if (victim != event.subject && path.size() >= 2
                    && coordination_reroute(victim, event.time, path[0], path[1])) return;
            }
            coordination_reroute(event.subject, event.time);
        }
        return;
    }
    if (event.kind == EventKind::reservation_timeout) {
        if (!pending_.count(event.subject) || event.token != job.timeout_generation
            || job.blocked_node < 0 || hard_station_wait(job)
            || job.blocked_node == job.goal || (config_.loaded_priority && job.loaded)) return;
        job.tabu_nodes.insert(job.blocked_node);
        const bool dram_timeout = job.blocked_reason == 2;
        Route route;
        if (!route_for(job, route)) {
            job.tabu_nodes.clear();
            if (parking_route(job, route)) {
                job.parking_backoff = true;
                if (robots_[job.robot].position != job.goal)
                    job.tabu_nodes.insert(robots_[job.robot].position);
            } else if (!route_for(robots_[job.robot].position, job.goal, route)) {
                error_ = "validated route became unavailable"; return;
            }
        }
        job.route = std::move(route);
        job.blocked_node = -1;
        job.blocked_reason = 0;
        job.wait_since = event.time;
        ++job.reroutes;
        ++reroutes_;
        if (dram_timeout) ++dram_reroutes_;
        ++job.timeout_generation;
        pending_dirty_.insert(event.subject);
        return;
    }
    if (event.kind == EventKind::node_crossing) {
        const auto previous = robot.position;
        if (previous != event.value) {
            const auto owner = node_owner_.find(previous);
            if (owner != node_owner_.end() && owner->second == job.robot) {
                node_owner_.erase(owner);
                for (const auto pending : pending_)
                    if (jobs_[pending].blocked_node == previous) pending_dirty_.insert(pending);
            }
        }
        robot.position = event.value;
        clear_wait(job, event.time);
        release_passages();
        if (job.traversed.empty() || job.traversed.back() != event.value)
            job.traversed.push_back(event.value);
        for (const auto pending : pending_) {
            const auto& waiting = jobs_[pending];
            const auto& conflicts = waiting.conflict_robots;
            if (waiting.blocked_reason == 2
                && std::find(conflicts.begin(), conflicts.end(), job.robot) != conflicts.end())
                pending_dirty_.insert(pending);
        }
        return;
    }
    if (event.kind == EventKind::movement_tail) {
        const auto& tail = movement_tails_[event.value];
        robot.heading = tail.heading;
        job.route = tail.remaining;
        if (job.parking_backoff) {
            job.parking_backoff = false;
            Route route;
            if (!route_for(job, route) && !route_for(robot.position, job.goal, route)) {
                error_ = "validated route became unavailable"; return;
            }
            job.route = std::move(route);
            request_reservation(event.subject, event.time);
            return;
        }
        if (job.route.nodes.size() == 1) schedule(event.time, job.arrival, event.subject);
        else request_reservation(event.subject, event.time);
        return;
    }
    if (event.kind == EventKind::pickup_arrival) {
        schedule(event.time + config_.jack_up_seconds, EventKind::jack_up_done, event.subject);
    } else if (event.kind == EventKind::jack_up_done) {
        job.rack_departure = event.time;
        Route route;
        if (!route_for(racks_[job.rack].node, stations_[job.station].node, route)) {
            error_ = "validated route became unavailable"; return;
        }
        start_stage(event.subject, std::move(route), event.time, Stage::station, true, EventKind::station_arrival);
    } else if (event.kind == EventKind::station_arrival) {
        auto& station = stations_[job.station];
        if (station.busy) { error_ = "reserved workstation is already busy"; return; }
        station.busy = true;
        job.station_arrival = job.service_start = event.time;
        schedule(event.time + config_.service_seconds, EventKind::service_done, event.subject);
    } else if (event.kind == EventKind::service_done) {
        auto& station = stations_[job.station];
        station.busy = false;
        station.service_seconds += config_.service_seconds;
        Route route;
        if (!route_for(station.node, racks_[job.rack].node, route)) {
            error_ = "validated route became unavailable"; return;
        }
        start_stage(event.subject, std::move(route), event.time, Stage::return_rack, true, EventKind::rack_home);
    } else if (event.kind == EventKind::rack_home) {
        job.rack_return = event.time;
        schedule(event.time + config_.jack_down_seconds, EventKind::jack_down_done, event.subject);
    } else if (event.kind == EventKind::jack_down_done) {
        racks_[job.rack].reserved = false;
        robot.free = true;
        robot.busy_seconds += event.time - job.dispatch_time;
        job.completion_time = event.time;
        clear_wait(job, event.time);
        for (const auto pending : pending_) {
            const auto& waiting = jobs_[pending];
            const auto& conflicts = waiting.conflict_robots;
            if (waiting.blocked_reason == 2
                && std::find(conflicts.begin(), conflicts.end(), job.robot) != conflicts.end())
                pending_dirty_.insert(pending);
        }
        auto& task = tasks_[job.task];
        --task.inflight;
        for (const auto& line : job.lines) task.completed_lines += line.second;
        if (task.outstanding.empty() && task.inflight == 0) task.completed_at = event.time;
        dispatch_needed_ = true;
    }
}

bool Simulator::run() {
    std::uint64_t processed = 0;
    double previous_time = -1.0;
    std::uint64_t same_time = 0;
    while (!calendar_.empty() && error_.empty()) {
        const double now = calendar_.top().time;
        if (previous_time >= 0.0 && std::abs(now - previous_time) <= 1e-9) ++same_time;
        else { previous_time = now; same_time = 0; }
        if (same_time > 10000 || processed > 200000 || reroutes_ > 1000) {
            std::ostringstream message;
            message << "native coordination livelock at " << now << " after "
                    << processed << " events; pending=" << pending_.size()
                    << ", reroutes=" << reroutes_;
            for (const auto index : pending_) {
                const auto& job = jobs_[index];
                message << " [job=" << index << ",robot=" << job.robot
                        << ",pos=" << robots_[job.robot].position
                        << ",goal=" << job.goal << ",blocked=" << job.blocked_node
                        << ",reason=" << job.blocked_reason
                        << ",tabu_edges=" << job.tabu_edges.size()
                        << ",job_reroutes=" << job.reroutes
                        << ",parking=" << job.parking_backoff << ']';
            }
            error_ = message.str();
            return false;
        }
        while (!calendar_.empty() && std::abs(calendar_.top().time - now) <= 1e-9) {
            const auto event = calendar_.top(); calendar_.pop(); ++processed; handle(event);
            if (!error_.empty()) return false;
        }
        if (dispatch_needed_) {
            dispatch_needed_ = false;
            while (dispatch_one(now)) {}
        }
        resolve_reservations(now);
    }
    for (const auto& task : tasks_) {
        if (task.completed_at < 0.0) { error_ = "simulation deadlocked with incomplete tasks"; return false; }
    }
    return true;
}

std::string Simulator::metrics_json() const {
    double completion = 0.0;
    std::int32_t lines = 0;
    for (const auto& task : tasks_) {
        completion = std::max(completion, task.completed_at);
        lines += task.completed_lines;
    }
    double distance = 0.0, travel = 0.0, busy = 0.0;
    for (const auto& robot : robots_) {
        distance += robot.travel_distance;
        travel += robot.travel_seconds;
        busy += robot.busy_seconds;
    }
    std::ostringstream output;
    output.precision(17);
    output << "{\"date\":\"" << day_year_ << '-';
    if (day_month_ < 10) output << '0'; output << day_month_ << '-';
    if (day_day_ < 10) output << '0'; output << day_day_ << "\""
           << ",\"first_release_seconds\":0"
           << ",\"completed_lines\":" << lines
           << ",\"completed_tasks\":" << tasks_.size()
           << ",\"final_completion_seconds\":" << completion
           << ",\"makespan_seconds\":" << completion
           << ",\"makespan_hours\":" << completion / 3600.0
           << ",\"rack_presentations\":" << jobs_.size()
           << ",\"line_throughput_per_hour\":" << (completion > 0.0 ? lines * 3600.0 / completion : 0.0)
           << ",\"travel_distance_m\":" << distance
           << ",\"travel_time_seconds\":" << travel
           << ",\"station_queue_time_seconds\":";
    double station_wait = 0.0, node_wait = 0.0;
    for (const auto& station : stations_) station_wait += station.queue_wait_seconds;
    for (const auto& job : jobs_) node_wait += job.node_wait_seconds;
    output << station_wait
           << ",\"node_reservation_wait_seconds\":" << node_wait
           << ",\"reservation_conflicts\":" << reservation_conflicts_
           << ",\"node_ownership_conflicts\":" << node_ownership_conflicts_
           << ",\"dram_solver_conflicts\":" << dram_conflicts_
           << ",\"reservation_reroutes\":" << reroutes_
           << ",\"dram_solver_reroutes\":" << dram_reroutes_
           << ",\"dram_conflict_wait_seconds\":" << dram_wait_seconds_
           << ",\"max_reserved_nodes\":" << max_reserved_nodes_
           << ",\"deadlock_count\":0,\"loaded_priority_grants\":" << loaded_priority_grants_
           << ",\"loaded_protected_waits\":" << loaded_protected_waits_
           << ",\"following_wait_seconds\":" << following_wait_seconds_
           << ",\"following_avoided_reroutes\":" << following_avoided_reroutes_
           << ",\"wait_for_cycles\":" << wait_for_cycles_
           << ",\"cycle_breaking_reroutes\":" << cycle_breaking_reroutes_
           << ",\"corridor_conflicts\":" << corridor_conflicts_
           << ",\"corridor_ownership_changes\":" << corridor_ownership_changes_
           << ",\"corridor_yielding_amrs\":" << corridor_yielding_robots_.size()
           << ",\"corridor_wait_seconds\":" << corridor_wait_seconds_
           << ",\"amr_utilization\":" << (completion > 0.0 ? busy / (completion * robots_.size()) : 0.0)
           << ",\"workstations\":{";
    for (std::size_t index = 0; index < stations_.size(); ++index) {
        if (index) output << ',';
        const auto& station = stations_[index];
        output << '\"' << index << "\":{\"utilization\":"
               << (completion > 0.0 ? station.service_seconds / completion : 0.0)
               << ",\"service_seconds\":" << station.service_seconds
               << ",\"queue_wait_seconds\":" << station.queue_wait_seconds
               << ",\"max_queue\":" << station.max_queue << '}';
    }
    output << "},\"amrs\":{";
    for (std::size_t index = 0; index < robots_.size(); ++index) {
        if (index) output << ',';
        const auto& robot = robots_[index];
        output << '\"' << index << "\":{\"utilization\":"
               << (completion > 0.0 ? robot.busy_seconds / completion : 0.0)
               << ",\"busy_seconds\":" << robot.busy_seconds
               << ",\"travel_seconds\":" << robot.travel_seconds
               << ",\"travel_distance_m\":" << robot.travel_distance << '}';
    }
    output << "}}";
    return output.str();
}

}  // namespace amr
