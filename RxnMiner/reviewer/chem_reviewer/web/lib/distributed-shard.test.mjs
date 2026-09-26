import assert from "node:assert/strict";
import { test } from "node:test";
import { shardProgress, shardStatusLabel, validateShardArchive, visibleShardCases } from "./distributed-shard.js";

test("distributed shard archive validation is fail closed", () => {
  assert.equal(validateShardArchive({ name: "shard-001.input.tar.gz", size: 12_000 }), "");
  assert.match(validateShardArchive({ name: "benchmark-with-gt.zip", size: 12_000 }), /coordinator-produced/);
  assert.match(validateShardArchive({ name: "shard.input.tar.gz", size: 81 * 1024 * 1024 }), /80 MB/);
});

test("distributed progress and filters stay derived from sealed cases", () => {
  assert.deepEqual(shardProgress({ total: 47, sealed: 12 }), { total: 47, sealed: 12, pending: 35, percent: 26 });
  const cases = [{ id: "a", status: "sealed" }, { id: "b", status: "pending" }];
  assert.deepEqual(visibleShardCases(cases, "sealed"), [cases[0]]);
  assert.equal(shardStatusLabel("complete"), "Validated");
});
