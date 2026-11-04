const OWNER = "palmer67";
const REPOSITORY = "github-daily-digest";
const WORKFLOW = "m5-daily.yml";
const STATE_URL = `https://api.github.com/repos/${OWNER}/${REPOSITORY}/contents/state/sent.json`;
const DISPATCH_URL = `https://api.github.com/repos/${OWNER}/${REPOSITORY}/actions/workflows/${WORKFLOW}/dispatches`;
const API_VERSION = "2022-11-28";

function logEvent(log, event, fields = {}) {
  log({ event, ...fields });
}

function dateInShanghai(timestamp) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(timestamp));
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function isDayComplete(state, day) {
  return state?.runs?.[day]?.pushed === true;
}

function decodeState(content) {
  const binary = atob(content.replace(/\s/g, ""));
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

async function readState(fetchImpl) {
  const url = new URL(STATE_URL);
  url.searchParams.set("ref", "main");
  const response = await fetchImpl(url, {
    headers: {
      Accept: "application/vnd.github+json",
      "Cache-Control": "no-cache",
      "X-GitHub-Api-Version": API_VERSION,
      "User-Agent": "github-daily-digest-cloudflare-scheduler",
    },
  });
  if (!response.ok) {
    throw new Error(`state_http_${response.status}`);
  }

  const file = await response.json();
  if (file.encoding !== "base64" || typeof file.content !== "string") {
    throw new Error("state_payload_invalid");
  }
  return decodeState(file.content);
}

async function dispatchWorkflow(token, fetchImpl, wait) {
  if (!token) {
    throw new Error("missing_GITHUB_WORKFLOW_TOKEN");
  }

  let lastFailure = "unknown";
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      const response = await fetchImpl(DISPATCH_URL, {
        method: "POST",
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "X-GitHub-Api-Version": API_VERSION,
          "User-Agent": "github-daily-digest-cloudflare-scheduler",
        },
        body: JSON.stringify({
          ref: "main",
          inputs: {
            mode: "happy",
            columns: "",
            force: "no",
          },
        }),
      });
      if (response.status === 204) {
        return;
      }
      lastFailure = `http_${response.status}`;
    } catch (error) {
      lastFailure = `network_${error?.name || "Error"}`;
    }

    if (attempt < 2) {
      await wait(1000);
    }
  }

  throw new Error(`workflow_dispatch_failed_${lastFailure}`);
}

export async function runScheduler({
  scheduledTime,
  token,
  fetchImpl = fetch,
  wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  log = console.log,
}) {
  const day = dateInShanghai(scheduledTime);
  let state;

  try {
    state = await readState(fetchImpl);
  } catch (error) {
    logEvent(log, "state_check_failed", { day, reason: error.message, action: "dispatch" });
  }

  if (state && isDayComplete(state, day)) {
    logEvent(log, "already_complete", { day, action: "skip" });
    return { day, dispatched: false, reason: "already_complete" };
  }

  await dispatchWorkflow(token, fetchImpl, wait);
  logEvent(log, "workflow_dispatch_accepted", { day });
  return { day, dispatched: true, reason: state ? "incomplete" : "state_unavailable" };
}

export default {
  async fetch() {
    return new Response("Not found", { status: 404 });
  },

  async scheduled(controller, env) {
    const day = dateInShanghai(controller.scheduledTime);
    try {
      await runScheduler({
        scheduledTime: controller.scheduledTime,
        token: env.GITHUB_WORKFLOW_TOKEN,
      });
    } catch (error) {
      console.error({
        event: "scheduler_failed",
        day,
        reason: error instanceof Error ? error.message : "unknown_error",
      });
      throw error;
    }
  },
};
