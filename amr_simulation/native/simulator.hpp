#pragma once

#include "day_abi.hpp"
#include "types.hpp"

#include <cstdint>
#include <queue>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace amr {

enum class EventKind : std::int32_t {
    task_release,
    node_crossing,
    movement_tail,
    reservation_timeout,
    coordination_timeout,
    pickup_arrival,
    jack_up_done,
    station_arrival,
    service_done,
    rack_home,
    jack_down_done,
};

enum class Stage : std::int32_t { pickup, station, return_rack };

struct Event {
    double time;
    std::uint64_t sequence;
    EventKind kind;
    std::int32_t subject;
    std::int32_t value;
    std::uint64_t token;
};

struct LaterEvent {
    bool operator()(const Event& first, const Event& second) const {
        if (first.time != second.time) return first.time > second.time;
        return first.sequence > second.sequence;
    }
};

struct TaskState {
    std::int32_t station = -1;
    std::unordered_map<std::int32_t, std::int32_t> outstanding;
    std::int32_t source_lines = 0;
    std::int32_t inflight = 0;
    std::int32_t completed_lines = 0;
    double completed_at = -1.0;
    bool released = false;
};

struct AmrState {
    std::int32_t position = -1;
    double heading = 0.0;
    double busy_seconds = 0.0;
    double travel_seconds = 0.0;
    double travel_distance = 0.0;
    bool free = true;
};

struct RackState {
    std::int32_t node = -1;
    std::vector<std::int32_t> skus;
    bool reserved = false;
};

struct StationState {
    std::int32_t node = -1;
    double service_seconds = 0.0;
    double queue_wait_seconds = 0.0;
    std::int32_t max_queue = 0;
    bool busy = false;
};

struct JobState {
    std::int32_t task = -1;
    std::int32_t rack = -1;
    std::int32_t robot = -1;
    std::int32_t station = -1;
    std::unordered_map<std::int32_t, std::int32_t> lines;
    Route route;
    std::vector<std::int32_t> traversed;
    std::unordered_set<std::int32_t> tabu_nodes;
    std::unordered_set<std::uint64_t> tabu_edges;
    std::uint64_t timeout_generation = 0;
    double dispatch_time = 0.0;
    double request_time = -1.0;
    double wait_since = -1.0;
    double completion_time = -1.0;
    double rack_departure = -1.0;
    double rack_return = -1.0;
    double station_queue_enter = -1.0;
    double station_admitted = -1.0;
    double station_arrival = -1.0;
    double service_start = -1.0;
    double travel_seconds = 0.0;
    double travel_distance = 0.0;
    double node_wait_seconds = 0.0;
    double movement_heading = 0.0;
    double following_wait_since = -1.0;
    double corridor_wait_since = -1.0;
    double dram_wait_since = -1.0;
    double dram_wait_seconds = 0.0;
    std::int32_t route_cursor = 0;
    std::int32_t goal = -1;
    std::int32_t blocked_node = -1;
    std::int32_t reservation_conflicts = 0;
    std::int32_t reroutes = 0;
    std::int32_t blocked_reason = 0;
    std::int32_t station_queue_position = -1;
    std::vector<std::int32_t> conflict_robots;
    std::vector<double> denial_times;
    std::vector<std::int32_t> denial_reasons;
    std::vector<std::int32_t> denial_nodes;
    Stage stage = Stage::pickup;
    EventKind arrival = EventKind::pickup_arrival;
    bool loaded = false;
    bool parking_backoff = false;
};

struct PassageState {
    std::vector<std::int32_t> nodes;
    std::int32_t owner_robot = -1;
    std::int32_t direction_start = -1;
    std::int32_t direction_end = -1;
    std::vector<std::int32_t> yielding;
};

struct MovementTailState { Route remaining; double heading = 0.0; };

class Simulator {
public:
    explicit Simulator(const AmrDayInput& input);
    bool valid() const { return error_.empty(); }
    const std::string& error() const { return error_; }
    bool calendar_order_self_test();
    std::int32_t dispatch_fixture(double now);
    bool run();
    const std::vector<JobState>& jobs() const { return jobs_; }
    const std::vector<TaskState>& tasks() const { return tasks_; }
    const std::vector<MotionSegment>& motion_segments() const { return motion_segments_; }
    std::string metrics_json() const;

private:
    void schedule(double time, EventKind kind, std::int32_t subject,
                  std::int32_t value = 0, std::uint64_t token = 0);
    bool import(const AmrDayInput& input);
    bool route_for(std::int32_t start, std::int32_t goal, Route& route) const;
    bool route_for(const JobState& job, Route& route) const;
    bool hard_station_wait(const JobState& job) const;
    bool dram_denied(std::int32_t selected, std::int32_t candidate,
                     std::vector<std::int32_t>& blockers,
                     std::int32_t& overlap, std::int32_t& kind) const;
    bool higher_priority(const JobState& first, const JobState& second) const;
    std::vector<std::int32_t> remaining_path(const JobState& job) const;
    std::int32_t active_job_for_robot(std::int32_t robot) const;
    bool same_direction_following(const JobState& follower, std::int32_t leader_job,
                                  std::int32_t blocked) const;
    void set_wait(const JobState& job, const std::vector<std::int32_t>& blockers);
    void clear_wait(JobState& job, double now);
    std::vector<std::int32_t> wait_cycle() const;
    bool coordination_reroute(std::int32_t job, double now);
    bool coordination_reroute(std::int32_t job, double now,
                              std::int32_t from, std::int32_t to);
    std::int32_t break_wait_cycle(double now);
    std::vector<std::int32_t> reversed_passage(const JobState& first,
                                               const JobState& second) const;
    std::vector<std::int32_t> following_queue(std::int32_t front) const;
    std::int32_t corridor_resolution(std::int32_t selected,
                                     std::int32_t other_robot, double now);
    void release_passages();
    bool parking_route(const JobState& job, Route& route) const;
    bool dispatch_one(double now);
    void start_stage(std::int32_t job, Route route, double now, Stage stage,
                     bool loaded, EventKind arrival);
    void request_reservation(std::int32_t job, double now);
    void resolve_reservations(double now);
    void move_reserved(std::int32_t job, const std::vector<std::int32_t>& nodes, double now);
    void handle(const Event& event);

    Graph graph_;
    AmrDayConfig config_{};
    std::vector<TaskState> tasks_;
    std::vector<AmrState> robots_;
    std::vector<RackState> racks_;
    std::vector<StationState> stations_;
    std::vector<JobState> jobs_;
    std::unordered_map<std::int32_t, std::int32_t> node_owner_;
    std::unordered_map<std::int32_t, std::vector<std::int32_t>> racks_by_sku_;
    std::priority_queue<Event, std::vector<Event>, LaterEvent> calendar_;
    std::unordered_set<std::int32_t> pending_;
    std::unordered_set<std::int32_t> pending_dirty_;
    std::vector<MotionSegment> motion_segments_;
    std::vector<MovementTailState> movement_tails_;
    std::uint64_t sequence_ = 0;
    bool dispatch_needed_ = false;
    std::string error_;
    mutable std::unordered_map<std::uint64_t, Route> static_route_cache_;
    mutable std::unordered_set<std::uint64_t> failed_static_routes_;
    std::int32_t day_year_ = 0, day_month_ = 0, day_day_ = 0;
    std::int64_t reservation_conflicts_ = 0;
    std::int64_t node_ownership_conflicts_ = 0;
    std::int64_t dram_conflicts_ = 0;
    std::int64_t dram_reroutes_ = 0;
    double dram_wait_seconds_ = 0.0;
    std::int64_t reroutes_ = 0;
    std::int32_t max_reserved_nodes_ = 0;
    std::int64_t loaded_priority_grants_ = 0;
    std::int64_t loaded_protected_waits_ = 0;
    std::unordered_map<std::int32_t, std::unordered_set<std::int32_t>> wait_for_;
    double following_wait_seconds_ = 0.0;
    std::int64_t following_avoided_reroutes_ = 0;
    std::int64_t wait_for_cycles_ = 0;
    std::int64_t cycle_breaking_reroutes_ = 0;
    std::vector<PassageState> passages_;
    std::int64_t corridor_conflicts_ = 0;
    std::int64_t corridor_ownership_changes_ = 0;
    std::unordered_set<std::int32_t> corridor_yielding_robots_;
    double corridor_wait_seconds_ = 0.0;
};

}  // namespace amr
