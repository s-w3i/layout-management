export const taskTypes = {
  delivery: { prefix: "DEL", label: "Delivery" },
  replenishment: { prefix: "RPL", label: "Replenishment" },
  relocation: { prefix: "MOV", label: "Relocation" },
} as const;
export type TaskType = keyof typeof taskTypes;

export function createTaskId(type: unknown, reference: string): string {
  if (typeof type !== "string" || !Object.hasOwn(taskTypes, type))
    throw new Error(
      "Choose delivery, replenishment or relocation as the task type",
    );
  return `${taskTypes[type as TaskType].prefix}-${reference}`;
}

export function taskTypeForId(id: string): TaskType {
  return (
    (Object.keys(taskTypes) as TaskType[]).find((type) =>
      id.startsWith(taskTypes[type].prefix + "-"),
    ) ?? "relocation"
  );
}
