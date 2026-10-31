import {
  axisDuration,
  phasePosition,
  safeSum,
  type AsrsModel,
  type AsrsSettings,
  type AsrsPhase,
  type Vec3,
  type SimulationOrder,
} from "../shared/asrs.js";
import type { Row } from "../shared/model.js";

interface Group {
  key: string;
  store: string;
  date: string;
  load: string;
  lines: (SimulationOrder & { remaining: number })[];
  index: number;
  estimate: number;
}
interface Refill {
  load: string;
  ready: number;
  cause: SimulationOrder | null;
  external: boolean;
}
interface Cycle {
  id: string;
  load: string;
  kind: "pick" | "refill";
  phases: AsrsPhase[];
  index: number;
  group?: Group;
  refill?: Refill;
  completed: number[];
  start: number;
}
interface CraneState {
  id: string;
  position: Vec3;
  queue: Group[];
  work: number;
  refills: Refill[];
  cycle: Cycle | null;
  busy: number;
  cycles: number;
}
interface LoadState {
  quantity: number;
  location: string;
}
interface Event {
  at: number;
  seq: number;
  type: "phase" | "day" | "wake";
  id: string;
}
export interface SimulationSink {
  event(value: Row): void;
  line(
    sourceRow: number,
    fulfilled: number,
    status: string,
    shortage?: number,
  ): void;
  cycle(value: Row): void;
}

export class AsrsEngine {
  now = 0;
  readonly loads: Record<string, LoadState> = Object.create(null);
  readonly receiving: Record<string, number> = Object.create(null);
  readonly storage: Record<string, number> = Object.create(null);
  readonly cranes: CraneState[];
  readonly totals = {
    lines: 0,
    completed: 0,
    unfulfilled: 0,
    returned: 0,
    zero: 0,
    opening: 0,
    inbound: 0,
    outbound: 0,
    return_quantity: 0,
    cycles: 0,
    replenishments: 0,
  };
  private events: Event[] = [];
  private sequence = 0;
  private cycleId = 0;
  private eventId = 0;
  private readonly byLoad: Map<string, AsrsModel["loads"][number]>;
  private readonly bySku = new Map<string, AsrsModel["loads"]>();
  private readonly byCrane: Map<string, AsrsModel["cranes"][number]>;
  private readonly epoch: number;

  constructor(
    readonly model: AsrsModel,
    readonly dates: string[],
    readonly settings: AsrsSettings,
    private readonly orders: (date: string) => SimulationOrder[],
    private readonly sink: SimulationSink,
    checkpoint?: Row,
  ) {
    this.byLoad = new Map(model.loads.map((l) => [l.id, l]));
    this.byCrane = new Map(model.cranes.map((c) => [c.id, c]));
    this.epoch = Date.parse(dates[0] + "T00:00:00Z");
    for (const load of model.loads) {
      const list = this.bySku.get(load.sku) ?? [];
      list.push(load);
      this.bySku.set(load.sku, list);
    }
    this.cranes = model.cranes.map((c) => ({
      id: c.id,
      position: c.home,
      queue: [],
      work: 0,
      refills: [],
      cycle: null,
      busy: 0,
      cycles: 0,
    }));
    if (checkpoint) {
      if (![1, 2].includes(checkpoint.version))
        throw new Error("Unsupported simulation checkpoint");
      this.now = checkpoint.now;
      this.events = checkpoint.events;
      this.sequence = checkpoint.sequence;
      this.cycleId = checkpoint.cycleId;
      this.eventId = checkpoint.eventId;
      if (checkpoint.version === 1) Object.assign(this.loads, checkpoint.loads);
      else {
        if (checkpoint.loads.length !== model.loads.length)
          throw new Error(
            "Checkpoint tote count does not match the pinned model",
          );
        model.loads.forEach((load, index) => {
          const [quantity, location] = checkpoint.loads[index];
          this.loads[load.id] = {
            quantity,
            location: location || load.address,
          };
        });
      }
      Object.assign(this.storage, checkpoint.storage);
      Object.assign(this.receiving, checkpoint.receiving);
      Object.assign(this.totals, checkpoint.totals);
      if (checkpoint.version === 1) this.cranes = checkpoint.cranes;
      else {
        const days = new Map<string, Map<number, SimulationOrder>>();
        const hydrate = (group: Row): Group => {
          if (!days.has(group.date))
            days.set(
              group.date,
              new Map(
                this.orders(group.date).map((row) => [row.source_row, row]),
              ),
            );
          return {
            ...group,
            lines: group.lines.map(([sourceRow, remaining]: number[]) => {
              const original = days.get(group.date)!.get(sourceRow);
              if (!original)
                throw new Error(
                  `Checkpoint source row ${sourceRow} is missing`,
                );
              return { ...original, remaining };
            }),
          } as Group;
        };
        this.cranes = checkpoint.cranes.map((crane: Row) => ({
          ...crane,
          queue: crane.queue.map(hydrate),
          cycle: crane.cycle
            ? {
                ...crane.cycle,
                group: crane.cycle.group
                  ? hydrate(crane.cycle.group)
                  : undefined,
              }
            : null,
        }));
      }
    } else {
      for (const load of model.loads) {
        this.loads[load.id] = {
          quantity: load.initial,
          location: load.address,
        };
        this.storage[load.sku] = safeSum(
          this.storage[load.sku] ?? 0,
          load.initial,
        );
        this.totals.opening = safeSum(this.totals.opening, load.initial);
        this.log(
          "OPENING",
          load,
          load.initial,
          load.initial,
          load.initial,
          0,
          null,
          "EXTERNAL",
          load.address,
          "",
          "",
          "Published assigned quantity; simulation opening stock",
        );
      }
      for (const date of dates)
        this.push({
          type: "day",
          at: (Date.parse(date + "T00:00:00Z") - this.epoch) / 1000,
          id: date,
        });
    }
  }
  private push(value: Omit<Event, "seq">) {
    const event = { ...value, seq: ++this.sequence };
    this.events.push(event);
    let i = this.events.length - 1;
    while (i > 0) {
      const parent = (i - 1) >> 1;
      if (!this.before(event, this.events[parent])) break;
      this.events[i] = this.events[parent];
      i = parent;
    }
    this.events[i] = event;
  }
  private before(a: Event, b: Event) {
    return a.at < b.at || (a.at === b.at && a.seq < b.seq);
  }
  private pop(): Event {
    const first = this.events[0],
      last = this.events.pop()!;
    if (this.events.length) {
      let i = 0;
      while (i * 2 + 1 < this.events.length) {
        let child = i * 2 + 1;
        if (
          child + 1 < this.events.length &&
          this.before(this.events[child + 1], this.events[child])
        )
          child++;
        if (!this.before(this.events[child], last)) break;
        this.events[i] = this.events[child];
        i = child;
      }
      this.events[i] = last;
    }
    return first;
  }
  get nextTime() {
    return this.events[0]?.at ?? Infinity;
  }
  get finished() {
    return (
      !this.events.length &&
      this.cranes.every((c) => !c.cycle && !c.queue.length && !c.refills.length)
    );
  }
  get idle() {
    return this.cranes.every((c) => !c.cycle);
  }
  timestamp(at = this.now) {
    return new Date(this.epoch + Math.round(at * 1000))
      .toISOString()
      .slice(0, -1);
  }
  private log(
    kind: string,
    load: AsrsModel["loads"][number] | null,
    quantity: number,
    delta: number,
    loadDelta: number,
    receivingDelta: number,
    source: SimulationOrder | null,
    from: string,
    to: string,
    crane: string,
    cycle: string,
    reason = "",
    sku = load?.sku ?? source?.sku ?? "",
  ) {
    this.sink.event({
      sequence: ++this.eventId,
      sim_seconds: this.now,
      simulation_time_local: this.timestamp(),
      kind,
      sku,
      load_id: load?.id ?? "",
      quantity_micro: quantity,
      delta_micro: delta,
      load_delta_micro: loadDelta,
      receiving_delta_micro: receivingDelta,
      load_balance_micro: load ? this.loads[load.id].quantity : 0,
      load_location: load ? this.loads[load.id].location : "",
      sku_balance_micro: safeSum(
        this.storage[sku] ?? 0,
        this.receiving[sku] ?? 0,
      ),
      storage_balance_micro: this.storage[sku] ?? 0,
      source_row: source?.source_row ?? null,
      order_date: source?.date ?? "",
      source_time: source?.source_time ?? "",
      order_reference: source?.order_reference ?? "",
      store: source?.store ?? "",
      from_location: from,
      to_location: to,
      crane,
      cycle_id: cycle,
      reason,
    });
  }
  private travel(a: Vec3, b: Vec3) {
    return Math.max(
      ...this.settings.axes.map((axis, i) => axisDuration(b[i] - a[i], axis)),
    );
  }
  private point(load: AsrsModel["loads"][number]): Vec3 {
    return [
      load.position[0],
      this.byCrane.get(load.crane)!.home[1],
      load.position[2],
    ];
  }
  private delivery(origin: Vec3, load: AsrsModel["loads"][number]) {
    const point = this.point(load),
      home = this.byCrane.get(load.crane)!.home;
    return (
      this.travel(origin, point) +
      2 * this.travel(point, load.position) +
      this.settings.extraction_seconds +
      this.travel(point, home)
    );
  }
  private cycleTime(origin: Vec3, load: AsrsModel["loads"][number]) {
    const point = this.point(load),
      home = this.byCrane.get(load.crane)!.home;
    return (
      this.delivery(origin, load) +
      this.settings.picking_seconds +
      this.travel(home, point) +
      2 * this.travel(point, load.position) +
      this.settings.replacement_seconds
    );
  }
  private refill(sku: string, cause: SimulationOrder | null, external = true) {
    for (const load of this.bySku.get(sku) ?? []) {
      const crane = this.cranes.find((c) => c.id === load.crane)!;
      if (
        this.loads[load.id].quantity >= load.initial ||
        crane.refills.some((r) => r.load === load.id) ||
        (crane.cycle?.kind === "refill" && crane.cycle.load === load.id)
      )
        continue;
      const ready =
        this.now + (external ? this.settings.replenishment_lead_seconds : 0);
      crane.refills.push({ load: load.id, ready, cause, external });
      this.push({ type: "wake", at: ready, id: crane.id });
      this.log(
        "REPLENISH_REQUEST",
        load,
        load.initial - this.loads[load.id].quantity,
        0,
        0,
        0,
        cause,
        "RECEIVING",
        load.address,
        crane.id,
        "",
        external
          ? "Below SKU buffer or stockout; restore initial assigned quantity"
          : "Put away returned stock within initial assigned capacity",
      );
    }
  }
  private release(date: string) {
    const groups = new Map<string, SimulationOrder[]>();
    for (const line of this.orders(date)) {
      this.totals.lines++;
      if (line.quantity_micro < 0) {
        const q = -line.quantity_micro;
        this.receiving[line.sku] = safeSum(this.receiving[line.sku] ?? 0, q);
        this.totals.return_quantity = safeSum(this.totals.return_quantity, q);
        this.totals.returned++;
        this.log(
          "RETURN_IN",
          null,
          q,
          q,
          0,
          q,
          line,
          `STORE:${line.store}`,
          "RECEIVING",
          "",
          "",
          "Negative source quantity is returned to receiving",
        );
        this.sink.line(line.source_row, q, "returned");
        this.refill(line.sku, line, false);
        continue;
      }
      if (line.quantity_micro === 0) {
        this.totals.zero++;
        this.sink.line(line.source_row, 0, "no_movement");
        this.log(
          "NO_MOVEMENT",
          null,
          0,
          0,
          0,
          0,
          line,
          "",
          "",
          "",
          "",
          "Zero source quantity",
        );
        continue;
      }
      const candidates = (this.bySku.get(line.sku) ?? []).filter(
        (l) => l.initial > 0,
      );
      if (!candidates.length) {
        this.totals.unfulfilled++;
        this.sink.line(line.source_row, 0, "unfulfilled", line.quantity_micro);
        this.log(
          "UNFULFILLED",
          null,
          line.quantity_micro,
          0,
          0,
          0,
          line,
          "",
          `STORE:${line.store}`,
          "",
          "",
          "No accessible assigned tote with positive initial stock",
        );
        continue;
      }
      const key = JSON.stringify([line.store, line.sku]);
      const group = groups.get(key) ?? [];
      group.push(line);
      groups.set(key, group);
    }
    const ordered = [...groups.values()].sort(
      (a, b) =>
        a[0].store.localeCompare(b[0].store, "en") ||
        a[0].sku.localeCompare(b[0].sku, "en"),
    );
    for (const lines of ordered) {
      const first = lines[0],
        candidates = this.bySku.get(first.sku)!.filter((l) => l.initial > 0);
      const amount = lines.reduce((n, l) => safeSum(n, l.quantity_micro), 0);
      const score = (load: AsrsModel["loads"][number]) => {
        const crane = this.cranes.find((c) => c.id === load.crane)!;
        return (
          (crane.cycle ? crane.cycle.phases.at(-1)!.end - this.now : 0) +
          crane.work +
          this.cycleTime(crane.position, load) *
            Math.max(1, Math.ceil(amount / load.initial))
        );
      };
      const load = [...candidates].sort(
        (a, b) =>
          score(a) - score(b) ||
          a.crane.localeCompare(b.crane) ||
          a.address.localeCompare(b.address),
      )[0];
      const crane = this.cranes.find((c) => c.id === load.crane)!;
      const group = {
        key: `${date}/${first.store}`,
        store: first.store,
        date,
        load: load.id,
        lines: lines.map((l) => ({ ...l, remaining: l.quantity_micro })),
        index: 0,
        estimate:
          this.cycleTime(crane.position, load) *
          Math.ceil(amount / load.initial),
      };
      crane.queue.push(group);
      crane.work += group.estimate;
    }
  }
  private start(
    crane: CraneState,
    load: AsrsModel["loads"][number],
    group?: Group,
    refill?: Refill,
  ) {
    const phases: AsrsPhase[] = [],
      point = this.point(load),
      home = this.byCrane.get(crane.id)!.home;
    let now = this.now,
      position = crane.position;
    const add = (
      stage: string,
      target: Vec3,
      loaded: boolean,
      dwell?: number,
    ) => {
      const duration = dwell ?? this.travel(position, target);
      if (!Number.isFinite(now + duration))
        throw new Error("Motion settings produce a non-finite cycle duration");
      // Retain zero-duration boundaries: they can contain stock or completion events.
      phases.push({
        stage,
        start: now,
        end: now + duration,
        origin: position,
        target,
        loaded,
      });
      now += duration;
      position = target;
    };
    add("to_pickup", point, false);
    add("extract_extend", load.position, false);
    add(
      "extraction_dwell",
      load.position,
      false,
      this.settings.extraction_seconds,
    );
    add("extract_retract", point, true);
    add("to_workstation", home, true);
    add(
      refill ? "replenishing" : "picking",
      home,
      true,
      this.settings.picking_seconds,
    );
    add("to_return", point, true);
    add("replace_extend", load.position, true);
    add(
      "replacement_dwell",
      load.position,
      true,
      this.settings.replacement_seconds,
    );
    add("replace_retract", point, false);
    crane.cycle = {
      id: `C${++this.cycleId}`,
      load: load.id,
      kind: refill ? "refill" : "pick",
      phases,
      index: 0,
      group,
      refill,
      completed: [],
      start: this.now,
    };
    this.push({ type: "phase", at: phases[0].end, id: crane.id });
  }
  private dispatch() {
    for (const crane of this.cranes) {
      if (crane.cycle) continue;
      crane.refills = crane.refills.filter(
        (r) =>
          this.loads[r.load].quantity < this.byLoad.get(r.load)!.initial &&
          (r.external ||
            (this.receiving[this.byLoad.get(r.load)!.sku] ?? 0) > 0),
      );
      const refill = crane.refills
        .filter((r) => r.ready <= this.now)
        .sort((a, b) => a.ready - b.ready || a.load.localeCompare(b.load))[0];
      if (refill) {
        crane.refills.splice(crane.refills.indexOf(refill), 1);
        this.start(crane, this.byLoad.get(refill.load)!, undefined, refill);
        continue;
      }
      if (!crane.queue.length) continue;
      const key = crane.queue[0].key,
        batch = crane.queue.filter((g) => g.key === key);
      const eligible = batch.filter((g) => this.loads[g.load].quantity > 0);
      if (!eligible.length) {
        for (const group of batch) {
          const sku = this.byLoad.get(group.load)!.sku;
          const candidates = (this.bySku.get(sku) ?? []).filter((load) => {
            const owner = this.cranes.find((c) => c.id === load.crane)!,
              cycle = owner.cycle;
            const reserved =
              cycle?.kind === "pick" &&
              cycle.load === load.id &&
              cycle.index <= 5
                ? Math.min(
                    this.loads[load.id].quantity,
                    cycle
                      .group!.lines.slice(cycle.group!.index)
                      .reduce((n, l) => safeSum(n, l.remaining), 0),
                  )
                : 0;
            return this.loads[load.id].quantity - reserved > 0;
          });
          if (candidates.length) {
            const target = candidates.sort((a, b) => {
              const score = (l: typeof a) => {
                const owner = this.cranes.find((c) => c.id === l.crane)!;
                return (
                  owner.work +
                  (owner.cycle
                    ? owner.cycle.phases.at(-1)!.end - this.now
                    : 0) +
                  this.delivery(owner.position, l)
                );
              };
              return (
                score(a) - score(b) ||
                a.crane.localeCompare(b.crane) ||
                a.address.localeCompare(b.address)
              );
            })[0];
            crane.queue.splice(crane.queue.indexOf(group), 1);
            crane.work -= group.estimate;
            group.load = target.id;
            const owner = this.cranes.find((c) => c.id === target.crane)!;
            group.estimate =
              this.cycleTime(owner.position, target) *
              Math.ceil(
                group.lines
                  .slice(group.index)
                  .reduce((n, l) => safeSum(n, l.remaining), 0) /
                  target.initial,
              );
            owner.queue.push(group);
            owner.work += group.estimate;
            owner.queue.sort((a, b) => a.key.localeCompare(b.key, "en"));
            this.push({ type: "wake", at: this.now, id: owner.id });
          } else if (this.storage[sku] === 0)
            this.refill(sku, group.lines[group.index]);
        }
        continue;
      }
      const group = eligible.sort(
        (a, b) =>
          this.delivery(crane.position, this.byLoad.get(a.load)!) -
            this.delivery(crane.position, this.byLoad.get(b.load)!) ||
          this.byLoad
            .get(a.load)!
            .address.localeCompare(this.byLoad.get(b.load)!.address),
      )[0];
      crane.queue.splice(crane.queue.indexOf(group), 1);
      crane.work -= group.estimate;
      this.start(crane, this.byLoad.get(group.load)!, group);
    }
  }
  private move(crane: CraneState, destination: string, kind: string) {
    const cycle = crane.cycle!,
      load = this.byLoad.get(cycle.load)!,
      state = this.loads[load.id];
    const source =
      cycle.group?.lines[cycle.group.index] ?? cycle.refill?.cause ?? null;
    const from = state.location;
    state.location = destination;
    this.log(
      kind,
      load,
      state.quantity,
      0,
      0,
      0,
      source,
      from,
      destination,
      crane.id,
      cycle.id,
    );
  }
  private phase(crane: CraneState) {
    const cycle = crane.cycle!,
      phase = cycle.phases[cycle.index],
      load = this.byLoad.get(cycle.load)!,
      state = this.loads[load.id];
    crane.position = phase.target;
    if (phase.stage === "extract_retract")
      this.move(crane, `TRANSIT:${crane.id}`, "TOTE_PICKUP");
    if (phase.stage === "to_workstation")
      this.move(
        crane,
        this.byCrane.get(crane.id)!.workstation,
        "WORKSTATION_ARRIVAL",
      );
    if (phase.stage === "picking") {
      const group = cycle.group!;
      while (group.index < group.lines.length && state.quantity > 0) {
        const line = group.lines[group.index],
          q = Math.min(state.quantity, line.remaining);
        state.quantity -= q;
        this.storage[load.sku] -= q;
        line.remaining -= q;
        this.totals.outbound = safeSum(this.totals.outbound, q);
        this.log(
          "ORDER_OUT",
          load,
          q,
          -q,
          -q,
          0,
          line,
          state.location,
          `STORE:${line.store}`,
          crane.id,
          cycle.id,
        );
        this.sink.line(line.source_row, q, "picking");
        if (this.storage[load.sku] < this.model.buffers[load.sku])
          this.refill(load.sku, line);
        if (line.remaining === 0) {
          cycle.completed.push(line.source_row);
          group.index++;
        }
      }
      this.move(crane, `TRANSIT:${crane.id}`, "WORKSTATION_DEPARTURE");
    }
    if (phase.stage === "replenishing") {
      const refill = cycle.refill!,
        missing = load.initial - state.quantity;
      const fromReceiving = Math.min(missing, this.receiving[load.sku] ?? 0);
      if (fromReceiving) {
        this.receiving[load.sku] -= fromReceiving;
        state.quantity += fromReceiving;
        this.storage[load.sku] += fromReceiving;
        this.log(
          "RETURN_PUTAWAY",
          load,
          fromReceiving,
          0,
          fromReceiving,
          -fromReceiving,
          refill.cause,
          "RECEIVING",
          state.location,
          crane.id,
          cycle.id,
        );
      }
      const external = refill.external ? missing - fromReceiving : 0;
      if (external) {
        state.quantity += external;
        this.storage[load.sku] += external;
        this.totals.inbound = safeSum(this.totals.inbound, external);
        this.log(
          "REPLENISH_IN",
          load,
          external,
          external,
          external,
          0,
          refill.cause,
          "EXTERNAL",
          state.location,
          crane.id,
          cycle.id,
          "Refilled up to this tote's initial assigned quantity",
        );
      }
      if (fromReceiving || external) this.totals.replenishments++;
      this.move(crane, `TRANSIT:${crane.id}`, "WORKSTATION_DEPARTURE");
    }
    if (phase.stage === "replace_retract") {
      this.move(crane, load.address, "TOTE_RETURN");
      for (const sourceRow of cycle.completed) {
        this.sink.line(sourceRow, 0, "completed");
        this.totals.completed++;
      }
      if (cycle.group && cycle.group.index < cycle.group.lines.length) {
        cycle.group.estimate =
          this.cycleTime(crane.position, load) *
          Math.ceil(
            cycle.group.lines
              .slice(cycle.group.index)
              .reduce((n, l) => safeSum(n, l.remaining), 0) / load.initial,
          );
        crane.queue.unshift(cycle.group);
        crane.work += cycle.group.estimate;
        if (this.storage[load.sku] === 0)
          this.refill(load.sku, cycle.group.lines[cycle.group.index]);
      }
      crane.busy += this.now - cycle.start;
      crane.cycles++;
      this.totals.cycles++;
      this.sink.cycle({
        id: cycle.id,
        crane: crane.id,
        load_id: load.id,
        sku: load.sku,
        kind: cycle.kind,
        start: cycle.start,
        end: this.now,
        phases: cycle.phases,
      });
      crane.cycle = null;
      if (this.storage[load.sku] < this.model.buffers[load.sku])
        this.refill(
          load.sku,
          cycle.refill?.cause ?? cycle.group?.lines.at(-1) ?? null,
        );
    } else {
      cycle.index++;
      this.push({
        type: "phase",
        at: cycle.phases[cycle.index].end,
        id: crane.id,
      });
    }
  }
  step() {
    if (!this.events.length) {
      this.dispatch();
      if (!this.events.length && !this.finished)
        throw new Error(
          "ASRS scheduler has pending work but no executable event",
        );
      if (!this.events.length) return;
    }
    const event = this.pop();
    this.now = event.at;
    if (event.type === "day") this.release(event.id);
    if (event.type === "phase")
      this.phase(this.cranes.find((c) => c.id === event.id)!);
    this.dispatch();
    if (
      this.cranes.every(
        (c) => !c.cycle && !c.queue.length && !c.refills.length,
      ) &&
      this.events.every((e) => e.type === "wake")
    )
      this.events = [];
  }
  advanceClock(at: number) {
    if (!Number.isFinite(at) || at < this.now || at > this.nextTime)
      throw new Error("Simulation clock cannot skip unprocessed events");
    this.now = at;
  }
  checkpoint(): Row {
    const compact = (group: Group) => ({
      ...group,
      lines: group.lines.map((line) => [line.source_row, line.remaining]),
    });
    return {
      version: 2,
      now: this.now,
      events: this.events,
      sequence: this.sequence,
      eventId: this.eventId,
      cycleId: this.cycleId,
      loads: this.model.loads.map((load) => [
        this.loads[load.id].quantity,
        this.loads[load.id].location === load.address
          ? ""
          : this.loads[load.id].location,
      ]),
      storage: this.storage,
      receiving: this.receiving,
      cranes: this.cranes.map((crane) => ({
        ...crane,
        queue: crane.queue.map(compact),
        cycle: crane.cycle
          ? {
              ...crane.cycle,
              group: crane.cycle.group ? compact(crane.cycle.group) : undefined,
            }
          : null,
      })),
      totals: this.totals,
    };
  }
  snapshot(): Row {
    const onhand = safeSum(
      Object.values(this.storage).reduce(safeSum, 0),
      Object.values(this.receiving).reduce(safeSum, 0),
    );
    if (
      BigInt(onhand) !==
      BigInt(this.totals.opening) +
        BigInt(this.totals.inbound) +
        BigInt(this.totals.return_quantity) -
        BigInt(this.totals.outbound)
    )
      throw new Error("Stock conservation failed");
    return {
      now: this.now,
      simulation_time_local: this.timestamp(),
      totals: this.totals,
      onhand_micro: onhand,
      pending_events: this.events.length,
      cranes: this.cranes.map((c) => {
        const cycle = c.cycle,
          phase = cycle?.phases[cycle.index];
        return {
          id: c.id,
          workstation: this.byCrane.get(c.id)!.workstation,
          home: this.byCrane.get(c.id)!.home,
          position: phase
            ? phasePosition(phase, this.now, this.settings.axes)
            : c.position,
          phase,
          stage:
            phase?.stage ??
            (c.queue.length || c.refills.length
              ? "awaiting_replenishment"
              : "idle"),
          loaded: phase?.loaded ?? false,
          load_id: cycle?.load ?? "",
          sku: cycle ? this.byLoad.get(cycle.load)!.sku : "",
          kind: cycle?.kind ?? "",
          queue: c.queue.length,
          pending_refills: c.refills.length,
          cycles: c.cycles,
          busy_seconds: c.busy + (cycle ? this.now - cycle.start : 0),
        };
      }),
    };
  }
}
