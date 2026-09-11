# Current-heat slotting comparison

Comparison status: **complete**. Baseline: **regenerated_check_basic_zone_on**.

Primary metric: arithmetic mean daily completed order lines/hour. Lines complete after rack return and jack-down.
All layouts use the same 3 completed dates out of 3 requested dates.

Included dates: 2023-01-03, 2023-02-03, 2023-03-03

Excluded dates: none

| Layout | Strategy | Completed/requested | Mean lines/hour | Change vs baseline |
|---|---|---:|---:|---:|
| regenerated_check_basic_zone_on | basic | 3/3 | 951.11 | +0.00% |
| regenerated_check_basic_zone_off | basic | 3/3 | 804.43 | -15.42% |
| regenerated_check_affinity_zone_on | abc_affinity | 3/3 | 1017.07 | +6.94% |
| regenerated_check_affinity_zone_off | abc_affinity | 3/3 | 844.25 | -11.24% |
| regenerated_check_traffic_zone_off | ctbsa | 3/3 | 637.81 | -32.94% |
| regenerated_check_traffic_zone_on | ctbsa | 3/3 | 720.84 | -24.21% |
| regenerated_check_traffic_zone_off_minrack | ctbsa | 3/3 | 714.94 | -24.83% |
| regenerated_check_traffic_zone_on_minrack | ctbsa | 3/3 | 746.25 | -21.54% |

Completed order lines per rack presentation across common completed days:

| Layout | Minimum individual presentation | Daily mean | Maximum individual presentation |
|---|---:|---:|---:|
| regenerated_check_basic_zone_on | 1.000 | 1.839 | 11.000 |
| regenerated_check_basic_zone_off | 1.000 | 1.867 | 12.000 |
| regenerated_check_affinity_zone_on | 1.000 | 1.848 | 11.000 |
| regenerated_check_affinity_zone_off | 1.000 | 1.921 | 15.000 |
| regenerated_check_traffic_zone_off | 1.000 | 1.394 | 10.000 |
| regenerated_check_traffic_zone_on | 1.000 | 1.394 | 9.000 |
| regenerated_check_traffic_zone_off_minrack | 1.000 | 1.458 | 11.000 |
| regenerated_check_traffic_zone_on_minrack | 1.000 | 1.458 | 11.000 |

## Throughput comparison

![Throughput comparison](charts/throughput.png)

## Completed order lines per rack presentation

![Completed order lines per rack presentation](charts/completed_lines_per_rack_presentation.png)

## Minimum and maximum individual rack presentation

![Minimum and maximum individual rack presentation](charts/rack_presentation_min_max.png)

## Daily throughput

![Daily throughput](charts/daily_throughput.png)

## Travel and rack consolidation

![Travel and rack consolidation](charts/travel_and_trips.png)

## Robot activity and workstation balance

![Robot activity and workstation balance](charts/activity_and_stations.png)

## Blocking hotspots on common days

![Blocking hotspots on common days](charts/blocking_hotspots.png)

## Interpretation and definitions

- Throughput bars use sample standard deviation across days, not a confidence interval. One day has no measured between-day variation.
- Completed order lines per rack presentation divides returned/jacked-down order lines by rack arrivals at workstation service. Unfinished returns contribute a presentation but no completed lines. No presentations means unavailable.
- The mean presentation chart shows daily averages. The separate minimum/maximum chart shows the smallest/largest covered order-line count of any single completed rack job on the common dates. Repeated visits by the same rack count separately; unfinished jobs are excluded from these extrema.
- Weighted throughput is total completed lines divided by total simulation hours over the common dates.
- Travel is actual substep displacement, split by loaded/empty state. Normalized travel and waits use completed source order lines, not units or distinct SKUs.
- Robot time categories are mutually exclusive. Utilization includes active waiting; movement and waiting charts explain the difference.
- Reservation/path wait includes an active robot lacking a granted next step. Safety blocking is separately counted. Neither is an explicit workstation queue-time measurement.
- Hotspots accumulate blocked robot-seconds at the desired next node when known, otherwise at the current node. Multiple robots' waiting times add together.
- Workstation utilization is actual service time divided by daily simulation duration; completed lines are credited after rack return.
- Replica layout differences should be zero. Strategy names describe input metadata and do not establish the cause of an observed performance difference.
- Current-heat coordination and motion rules are retained. These results do not establish deadlock freedom or physical collision safety.

Detailed results: [layout comparison](layout_comparison.csv), [paired daily differences](paired_daily_differences.csv), and per-layout/day CSV files.
