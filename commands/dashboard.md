---
description: Set the InferenceIQ dashboard (console) URL + optional token this machine reports to
argument-hint: "<dashboard-url> [token]"
allowed-tools: Write, Read, Bash
disable-model-invocation: true
---

# Configure the InferenceIQ dashboard target

Point this machine's InferenceIQ reporting (the `UserPromptSubmit` hook and the `optimize`/`recommend` CLIs) at a specific dashboard collector — e.g. one running on AWS.

**Dashboard URL:** `$1`
**Token (optional):** `$2`

Do exactly this:

1. If `$1` is empty, tell the user the usage `/inferenceiq:dashboard <dashboard-url> [token]` and stop.

2. Write the file `~/.inferenceiq.json` (expand `~` to the user's home directory) with **exactly these keys** — the reporters read `dashboard` and `token`, so do not rename them:
   - If a token (`$2`) was given:
     ```json
     {
       "dashboard": "$1",
       "token": "$2"
     }
     ```
   - If no token was given, omit the token key:
     ```json
     {
       "dashboard": "$1"
     }
     ```
   Strip any trailing slash from the URL. If the file already exists, preserve any other keys it contains and only update `dashboard`/`token`.

3. Best-effort verify (do **not** block on it — write the file first): run
   `curl -s -o /dev/null -w "%{http_code}" "$1/api/stats"` and report the result. `200` = the collector is reachable; anything else just means it's not up yet or is behind auth — the config is still saved.

4. Confirm to the user: show the path written and the effective dashboard URL, and note that it takes effect on the **next** prompt (the hook reads this file each run). Remind them `INFERENCEIQ_DASHBOARD=off` (env) disables reporting, and that env vars override this file.
