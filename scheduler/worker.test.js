import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runScheduler } from "./worker.js";

const scheduledTime = Date.parse("2026-11-03T23:40:00Z");

function contentsResponse(state) {
  const content = Buffer.from(JSON.stringify(state)).toString("base64");
  return Response.json({ encoding: "base64", content });
}

test("uses the scheduled instant's Shanghai date across UTC midnight", async () => {
  let dispatches = 0;
  const logs = [];
  const result = await runScheduler({
    scheduledTime,
    token: "test-token",
    fetchImpl: async (input, init) => {
      if (init?.method === "POST") {
        dispatches += 1;
        assert.equal(new URL(input).pathname, "/repos/palmer67/github-daily-digest/actions/workflows/m5-daily.yml/dispatches");
        assert.deepEqual(JSON.parse(init.body), {
          ref: "main",
          inputs: { mode: "happy", columns: "", force: "no" },
        });
        assert.equal(init.headers["User-Agent"], "github-daily-digest-cloudflare-scheduler");
        return new Response(null, { status: 204 });
      }
      return contentsResponse({ runs: { "2026-11-04": { pushed: false } } });
    },
    wait: async () => {},
    log: (entry) => logs.push(entry),
  });

  assert.deepEqual(result, { day: "2026-11-04", dispatched: true, reason: "incomplete" });
  assert.equal(dispatches, 1);
  assert.deepEqual(logs, [{ event: "workflow_dispatch_accepted", day: "2026-11-04" }]);
});

test("skips dispatch when all enabled columns were completed", async () => {
  let dispatches = 0;
  const logs = [];
  const result = await runScheduler({
    scheduledTime,
    token: "",
    fetchImpl: async (_input, init) => {
      if (init?.method === "POST") {
        dispatches += 1;
        return new Response(null, { status: 204 });
      }
      return contentsResponse({ runs: { "2026-11-04": { pushed: true } } });
    },
    log: (entry) => logs.push(entry),
  });

  assert.equal(result.dispatched, false);
  assert.equal(result.reason, "already_complete");
  assert.equal(dispatches, 0);
  assert.deepEqual(logs, [{ event: "already_complete", day: "2026-11-04", action: "skip" }]);
});

test("fails open and dispatches if reading public state fails", async () => {
  let dispatches = 0;
  const logs = [];
  const result = await runScheduler({
    scheduledTime,
    token: "test-token",
    fetchImpl: async (_input, init) => {
      if (init?.method === "POST") {
        dispatches += 1;
        return new Response(null, { status: 204 });
      }
      return new Response(null, { status: 503 });
    },
    wait: async () => {},
    log: (entry) => logs.push(entry),
  });

  assert.deepEqual(result, { day: "2026-11-04", dispatched: true, reason: "state_unavailable" });
  assert.equal(dispatches, 1);
  assert.deepEqual(logs, [
    {
      event: "state_check_failed",
      day: "2026-11-04",
      reason: "state_http_503",
      action: "dispatch",
    },
    { event: "workflow_dispatch_accepted", day: "2026-11-04" },
  ]);
});

test("retries a rejected dispatch once and fails on a second non-204 response", async () => {
  let dispatches = 0;
  await assert.rejects(
    runScheduler({
      scheduledTime,
      token: "test-token",
      fetchImpl: async (_input, init) => {
        if (init?.method === "POST") {
          dispatches += 1;
          return new Response(null, { status: 401 });
        }
        return contentsResponse({ runs: {} });
      },
      wait: async () => {},
      log: () => {},
    }),
    /workflow_dispatch_failed_http_401/,
  );
  assert.equal(dispatches, 2);
});

test("fails clearly when a dispatch is needed but its secret is missing", async () => {
  await assert.rejects(
    runScheduler({
      scheduledTime,
      token: "",
      fetchImpl: async () => contentsResponse({ runs: {} }),
      log: () => {},
    }),
    /missing_GITHUB_WORKFLOW_TOKEN/,
  );
});

test("has the two expected UTC backup schedules", async () => {
  const config = await readFile(new URL("./wrangler.toml", import.meta.url), "utf8");
  assert.match(config, /crons = \["40 23 \* \* \*", "5 0 \* \* \*"\]/);
  assert.match(config, /workers_dev = false/);
});
