# Cloudflare Independent Scheduler

The GitHub `schedule` event is retained as the primary trigger. After the
Cloudflare Worker is deployed and its secret is set, Cron Triggers independently
check whether all enabled columns have completed for the current
`Asia/Shanghai` date and dispatch the existing M5 workflow only when they have
not.

## Schedule

Cloudflare Cron Triggers use UTC:

| UTC cron | Beijing time | Purpose |
| --- | --- | --- |
| `40 23 * * *` | 07:40 | Primary independent check |
| `5 0 * * *` | 08:05 | Retry window, before the 08:10 watchdog |

The Worker reads the public `state/sent.json`. It treats
`runs[YYYY-MM-DD].pushed === true` as the only full-day completion signal.
State read errors fail open and cause a dispatch attempt. The M5 workflow's
existing concurrency group and per-column completion state prevent duplicate
messages if GitHub or Cloudflare triggers overlap.

## Credential

Create a GitHub fine-grained personal access token with:

- Resource owner: `palmer67`
- Repository access: only `github-daily-digest`
- Repository permission: `Actions: write`

Add it to the Cloudflare Worker as the secret `GITHUB_WORKFLOW_TOKEN`. Do not
put the token in this repository, Wrangler configuration, or logs. The token
is used only to call the fixed M5 `workflow_dispatch` endpoint on `main`.
The configured fine-grained token expires on 2027-02-02; renew it before then.

`HEARTBEAT_URL` remains a separate GitHub Actions secret. The M5 workflow pings
Healthchecks.io only after every enabled column is successfully delivered. It
detects a missed delivery; it does not start the workflow.

## Cloudflare Routing

Cloudflare requires an account-level `workers.dev` subdomain before its Cron
Trigger API can be configured. The account subdomain is initialized, but this
Worker has both its `workers.dev` route and preview URLs disabled; it has no
active public route.

## Deploy And Verify

Deploy the Worker with `workers_dev` disabled. Upload the script first, add
`GITHUB_WORKFLOW_TOKEN` as a Cloudflare Worker secret, then attach the two Cron
Triggers. This order avoids activating the schedule before its dispatch
credential is present. If using Wrangler instead of the connected Cloudflare
API, authenticate Wrangler locally and follow the same order.

The repository's `npm test` exercises the date boundary, full-day skip,
fail-open state check, fixed workflow dispatch payload, retry behavior, and
schedule configuration without sending a real workflow dispatch.

Verify the deployed Worker lists both Cron Triggers in Cloudflare. On the next
scheduled cycle, confirm the Worker log reports either `already_complete` or
`workflow_dispatch_accepted`, then confirm M5 succeeds and Healthchecks records
its heartbeat. Do not use a live dispatch as a test: it sends real messages and
uses the DeepSeek API.
