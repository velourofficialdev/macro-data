// Starts the GitHub "update" workflow in fast mode (news, Fed odds, options, prices).
// Cron Trigger only: no HTTP routes. The token comes from env.GITHUB_TOKEN (a Worker secret).
const DISPATCH_URL = "https://api.github.com/repos/velourofficialdev/macro-data/actions/workflows/update.yml/dispatches";

export async function dispatchFast(env) {
  if (!env || !env.GITHUB_TOKEN) {
    console.log("dispatch skipped: GITHUB_TOKEN secret is not set");
    return;
  }
  try {
    const res = await fetch(DISPATCH_URL, {
      method: "POST",
      headers: {
        Authorization: "Bearer " + env.GITHUB_TOKEN,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "macro-data-cron",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs: { mode: "fast" } }),
    });
    if (res.status === 204) {
      console.log("dispatch ok: 204");
      return;
    }
    let body = "";
    try { body = (await res.text()).slice(0, 200); } catch (e) { body = ""; }
    console.log("dispatch failed: HTTP " + res.status + " " + body);
  } catch (e) {
    console.log("dispatch error: network failure");
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatchFast(env));
  },
};
