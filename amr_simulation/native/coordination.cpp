#include <algorithm>
#include <cstdint>
#include <unordered_map>
#include <unordered_set>
#include <vector>

struct Controller {
    explicit Controller(int count) : paths(count), active(count, false) {}
    std::vector<std::vector<int>> paths;
    std::vector<bool> active;
};

static int overlap_node(const std::vector<int>& first, int start,
                        const std::vector<int>& second) {
    const int limit = std::min<int>(first.size() - start, second.size());
    for (int length = 1; length < limit; ++length) {
        bool matches = true;
        for (int offset = 0; offset <= length; ++offset) {
            if (first[start + offset] != second[length - offset]) {
                matches = false;
                break;
            }
        }
        if (matches) return first[start + 1];
    }
    return -1;
}

extern "C" {

void* amr_coord_create(int agent_count) {
    if (agent_count < 1 || agent_count > 64) return nullptr;
    return new Controller(agent_count);
}

void amr_coord_destroy(void* pointer) {
    delete static_cast<Controller*>(pointer);
}

int amr_coord_set_path(void* pointer, int agent, const int* nodes, int count) {
    auto* controller = static_cast<Controller*>(pointer);
    if (!controller || agent < 0 || agent >= static_cast<int>(controller->paths.size()) || count < 1)
        return 0;
    controller->paths[agent].assign(nodes, nodes + count);
    controller->active[agent] = true;
    return 1;
}

void amr_coord_remove(void* pointer, int agent) {
    auto* controller = static_cast<Controller*>(pointer);
    if (!controller || agent < 0 || agent >= static_cast<int>(controller->paths.size())) return;
    controller->paths[agent].clear();
    controller->active[agent] = false;
}

int amr_coord_following(void* pointer, int follower, int leader, int blocked) {
    auto* controller = static_cast<Controller*>(pointer);
    if (!controller || follower < 0 || leader < 0 ||
        follower >= static_cast<int>(controller->paths.size()) ||
        leader >= static_cast<int>(controller->paths.size())) return 0;
    const auto& first = controller->paths[follower];
    const auto& second = controller->paths[leader];
    auto fi = std::find(first.begin(), first.end(), blocked);
    auto li = std::find(second.begin(), second.end(), blocked);
    return fi != first.end() && li != second.end() && fi + 1 != first.end() &&
           li + 1 != second.end() && *(fi + 1) == *(li + 1);
}

int amr_coord_reversed_passage(void* pointer, int first_agent, int second_agent,
                               int* output, int capacity) {
    auto* controller = static_cast<Controller*>(pointer);
    if (!controller || first_agent < 0 || second_agent < 0 ||
        first_agent >= static_cast<int>(controller->paths.size()) ||
        second_agent >= static_cast<int>(controller->paths.size())) return -1;
    const auto& first = controller->paths[first_agent];
    const auto& second = controller->paths[second_agent];
    std::vector<int> best;
    for (int i = 0; i + 1 < static_cast<int>(first.size()); ++i) {
        for (int j = 0; j + 1 < static_cast<int>(second.size()); ++j) {
            if (first[i] != second[j + 1] || first[i + 1] != second[j]) continue;
            int left = i, right = i + 1, other_left = j, other_right = j + 1;
            while (left > 0 && other_right + 1 < static_cast<int>(second.size()) &&
                   first[left - 1] == second[other_right + 1]) {
                --left;
                ++other_right;
            }
            while (right + 1 < static_cast<int>(first.size()) && other_left > 0 &&
                   first[right + 1] == second[other_left - 1]) {
                ++right;
                --other_left;
            }
            std::vector<int> candidate(first.begin() + left, first.begin() + right + 1);
            if (candidate.size() > best.size() ||
                (candidate.size() == best.size() && candidate < best)) best = std::move(candidate);
        }
    }
    if (static_cast<int>(best.size()) > capacity) return -1;
    std::copy(best.begin(), best.end(), output);
    return static_cast<int>(best.size());
}

// kind: 0=safe, 1=head-to-head, 2=cycle. Returns the number of safe prefix nodes.
int amr_coord_check_prefix(void* pointer, int selected, const int* candidates,
                           int candidate_count, const int* priority_rank,
                           int* kind, int* overlap, int* winner,
                           std::uint64_t* participants) {
    auto* controller = static_cast<Controller*>(pointer);
    if (!controller || selected < 0 || selected >= static_cast<int>(controller->paths.size()))
        return -1;
    const auto& original = controller->paths[selected];
    for (int candidate_index = 0; candidate_index < candidate_count; ++candidate_index) {
        const int candidate = candidates[candidate_index];
        auto found = std::find(original.begin(), original.end(), candidate);
        if (found == original.end()) return -1;
        const int start = static_cast<int>(found - original.begin());

        for (int other = 0; other < static_cast<int>(controller->paths.size()); ++other) {
            if (other == selected || !controller->active[other]) continue;
            const auto& path = controller->paths[other];
            const int node = overlap_node(original, start, path);
            if (node < 0) continue;
            const int selected_rank = priority_rank[selected];
            const int other_rank = priority_rank[other];
            if (selected_rank < other_rank) continue;
            *kind = 1;
            *overlap = node;
            *winner = other_rank < selected_rank ? other : selected;
            *participants = (std::uint64_t{1} << selected) | (std::uint64_t{1} << other);
            return candidate_index;
        }

        std::unordered_map<int, std::pair<int, int>> edges;
        for (int agent = 0; agent < static_cast<int>(controller->paths.size()); ++agent) {
            if (!controller->active[agent]) continue;
            if (agent == selected) {
                if (start + 1 < static_cast<int>(original.size()))
                    edges[candidate] = {original[start + 1], selected};
                continue;
            }
            const auto& path = controller->paths[agent];
            if (path.size() >= 2) edges[path[0]] = {path[1], agent};
        }

        std::unordered_map<int, int> visited;
        std::vector<std::pair<int, int>> order;
        int node = candidate;
        while (edges.find(node) != edges.end()) {
            auto prior = visited.find(node);
            if (prior != visited.end()) {
                std::uint64_t mask = 0;
                for (int i = prior->second; i < static_cast<int>(order.size()); ++i)
                    mask |= std::uint64_t{1} << order[i].second;
                *kind = 2;
                *overlap = node;
                *winner = -1;
                *participants = mask;
                return candidate_index;
            }
            visited[node] = static_cast<int>(order.size());
            const auto edge = edges[node];
            order.push_back({node, edge.second});
            node = edge.first;
        }
    }
    *kind = 0;
    *overlap = -1;
    *winner = -1;
    *participants = 0;
    return candidate_count;
}

}
