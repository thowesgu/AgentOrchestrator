export function formatUsage(metric, unit) {
  if (!metric || metric.status === "unavailable" || metric.value == null) {
    return `Unavailable ${unit}`;
  }
  const value = Number(metric.value).toLocaleString(undefined, {
    maximumFractionDigits: unit === "credits" ? 6 : 0,
  });
  return `${value} ${unit}${metric.status === "partial" ? " (partial)" : ""}`;
}
