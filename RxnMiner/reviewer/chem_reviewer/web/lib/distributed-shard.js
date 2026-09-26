export const DISTRIBUTED_TERMINAL = new Set(["complete", "failed", "interrupted"]);

export function validateShardArchive(file) {
  if (!file) return "";
  if (!/\.input\.tar\.gz$/i.test(file.name || "")) {
    return "Choose a coordinator-produced *.input.tar.gz shard.";
  }
  if (file.size > 80 * 1024 * 1024) return "Shard archive must be 80 MB or smaller.";
  return "";
}

export function shardProgress(batch) {
  const total = Number(batch?.total || 0);
  const sealed = Math.min(total, Math.max(0, Number(batch?.sealed || 0)));
  return { total, sealed, pending: Math.max(0, total - sealed), percent: total ? Math.round(sealed / total * 100) : 0 };
}

export function shardStatusLabel(status) {
  return ({
    staged: "Ready to run",
    running: "Extracting",
    complete: "Validated",
    failed: "Failed",
    interrupted: "Interrupted",
  })[status] || "Preparing";
}

export function visibleShardCases(cases, filter = "all") {
  const rows = Array.isArray(cases) ? cases : [];
  return filter === "all" ? rows : rows.filter((row) => row.status === filter);
}
