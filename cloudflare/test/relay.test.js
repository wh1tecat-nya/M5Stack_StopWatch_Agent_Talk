import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { test } from "node:test";
import { convertV4MiniflareOptions, Miniflare } from "miniflare";

const ORIGIN = "https://relay.example.com";
const CALLBACK = "https://callbacks.openai.com/test";
const REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect";
const OWNER_KEY = "test-owner-key".repeat(3);
const DEVICE_TOKEN = "test-device-token".repeat(3);
const KEY = Buffer.alloc(32, 11);

test("OAuth and a signed fixed-text round trip", async (t) => {
  const work = await mkdtemp(join(tmpdir(), "sumi-worker-test-"));
  t.after(() => rm(work, { recursive: true, force: true }));
  execFileSync("node", ["node_modules/wrangler/bin/wrangler.js", "deploy", "--dry-run", "--outdir", work], {
    env: { ...process.env, WRANGLER_LOG_PATH: join(work, "wrangler.log"), WRANGLER_SEND_METRICS: "false" },
    stdio: "pipe",
  });
  const events = [];
  let deliveryStatus = 503;
  const worker = new Miniflare(convertV4MiniflareOptions({
    modules: true,
    script: await readFile(join(work, "index.js"), "utf8"),
    compatibilityDate: "2026-11-06",
    compatibilityFlags: ["nodejs_compat", "global_fetch_strictly_public"],
    kvNamespaces: ["OAUTH_KV"],
    d1Databases: ["DB"],
    bindings: {
      RELAY_ORIGIN: ORIGIN,
      CALLBACK_HOST: "callbacks.openai.com",
      OWNER_KEY,
      DEVICE_TOKEN,
    },
    outboundService: async (request) => {
      assert.equal(request.url, CALLBACK);
      const body = await request.text();
      const headers = request.headers;
      const signed = `${headers.get("webhook-id")}.${headers.get("webhook-timestamp")}.${body}`;
      const expected = createHmac("sha256", KEY).update(signed).digest("base64");
      assert.equal(headers.get("webhook-signature"), "v1," + expected);
      assert.match(headers.get("x-mcp-subscription-id"), /^sub_/);
      const value = JSON.parse(body);
      if (value.type === "verification") return Response.json({ challenge: value.challenge });
      events.push(value);
      return new Response(null, { status: deliveryStatus });
    },
  }));
  t.after(() => worker.dispose());

  const db = await worker.getD1Database("DB");
  const sql = await readFile(new URL("../migrations/0001_text_probe.sql", import.meta.url), "utf8");
  await db.batch(sql.split(";").map((statement) => statement.trim()).filter(Boolean)
    .map((statement) => db.prepare(statement)));
  const fetch = (path, init) => worker.dispatchFetch(ORIGIN + path, init);
  const jsonPost = (path, body, headers = {}) => fetch(path, {
    method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json", ...headers },
  });

  const rejected = await fetch("/mcp");
  assert.equal(rejected.status, 401);
  assert.match(rejected.headers.get("www-authenticate"), /oauth-protected-resource/);
  const metadataUrl = rejected.headers.get("www-authenticate").match(/resource_metadata="([^"]+)"/)[1];
  const resourceMetadata = await (await fetch(new URL(metadataUrl).pathname)).json();
  assert.equal(resourceMetadata.resource, ORIGIN + "/mcp");
  assert.deepEqual(resourceMetadata.authorization_servers, [ORIGIN]);
  const authMetadata = await (await fetch("/.well-known/oauth-authorization-server")).json();
  assert.equal(authMetadata.issuer, ORIGIN);
  assert.equal(authMetadata.authorization_response_iss_parameter_supported, true);
  assert.ok(authMetadata.code_challenge_methods_supported.includes("S256"));
  assert.equal((await jsonPost("/oauth/register", { redirect_uris: ["https://example.com/callback"] })).status, 400);
  const registration = await jsonPost("/oauth/register", {
    client_name: "ChatGPT fixture", redirect_uris: [REDIRECT], token_endpoint_auth_method: "none",
  });
  assert.equal(registration.status, 201);
  const client = await registration.json();
  const verifier = "verifier".repeat(8);
  const query = new URLSearchParams({
    client_id: client.client_id, response_type: "code", redirect_uri: REDIRECT,
    resource: ORIGIN + "/mcp", scope: "mcp:stopwatch", state: "test-state",
    code_challenge: createHash("sha256").update(verifier).digest("base64url"), code_challenge_method: "S256",
  });
  const consent = await fetch("/authorize?" + query);
  assert.equal(consent.status, 200);
  assert.equal(consent.headers.get("referrer-policy"), "strict-origin");
  assert.ok(consent.headers.get("content-security-policy").includes(`form-action 'self' ${REDIRECT};`));
  const handle = (await consent.text()).match(/name="handle" value="([^"]+)"/)[1];
  const cookie = consent.headers.get("set-cookie").split(";")[0];
  const approve = (key, origin = ORIGIN) => fetch("/authorize", {
    method: "POST", redirect: "manual",
    headers: { Origin: origin, Cookie: cookie, "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ handle, key }),
  });
  assert.equal((await approve(OWNER_KEY, "https://example.com")).status, 403);
  assert.equal((await approve(OWNER_KEY, "null")).status, 403);
  assert.equal((await approve("wrong")).status, 403);
  const approved = await approve(OWNER_KEY);
  assert.equal(approved.status, 303);
  const redirect = new URL(approved.headers.get("location"));
  assert.equal(redirect.origin, "https://chatgpt.com");
  assert.equal(redirect.searchParams.get("state"), "test-state");
  assert.equal(redirect.searchParams.get("iss"), ORIGIN);
  const issued = await fetch("/oauth/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code", client_id: client.client_id, code_verifier: verifier,
      redirect_uri: REDIRECT, code: redirect.searchParams.get("code"), resource: ORIGIN + "/mcp",
    }),
  });
  assert.equal(issued.status, 200);
  const tokens = await issued.json();
  assert.ok(tokens.access_token);
  const rpc = async (method, params = {}) => {
    const response = await jsonPost("/mcp", { jsonrpc: "2.0", id: 1, method, params }, {
      Authorization: "Bearer " + tokens.access_token,
      Accept: "application/json, text/event-stream",
    });
    assert.equal(response.status, 200);
    return response.json();
  };
  assert.ok((await rpc("initialize", { protocolVersion: "2025-03-26" })).result.capabilities.events);
  assert.deepEqual((await rpc("server/discover")).result.supportedVersions, ["2026-07-28"]);
  assert.equal((await rpc("events/list")).result.events[0].name, "stopwatch.message");
  const tools = (await rpc("tools/list")).result.tools;
  for (const tool of tools) {
    assert.deepEqual(tool.securitySchemes, [{ type: "oauth2", scopes: ["mcp:stopwatch"] }]);
    assert.deepEqual(tool._meta.securitySchemes, tool.securitySchemes);
  }
  const statusTool = tools.find((tool) => tool.name === "get_stopwatch_status");
  assert.equal(statusTool.annotations.readOnlyHint, true);
  const status = async () => (await rpc("tools/call", { name: statusTool.name })).result.structuredContent;
  assert.deepEqual(await status(), { device_id: "stopwatch", event: "stopwatch.message", subscription_active: false });

  const id = "ab".repeat(16);
  const path = "/device/text-turns/" + id;
  const device = (method) => fetch(path, { method, headers: { Authorization: "Bearer " + DEVICE_TOKEN } });
  assert.equal((await fetch(path, { method: "PUT" })).status, 401);
  assert.equal((await device("PUT")).status, 409);
  const parameters = {
    name: "stopwatch.message", arguments: { device_id: "stopwatch" },
    delivery: { mode: "webhook", url: CALLBACK, secret: "whsec_" + KEY.toString("base64") },
  };
  const lookalike = await rpc("events/subscribe", {
    ...parameters, delivery: { ...parameters.delivery, url: "https://callbacks.openaixcom/test" },
  });
  assert.equal(lookalike.error?.code, -32602);
  const subscribed = await rpc("events/subscribe", parameters);
  assert.ok(subscribed.result?.id, JSON.stringify(subscribed.error));
  assert.equal((await status()).subscription_active, true);
  assert.equal((await device("PUT")).status, 503);
  deliveryStatus = 202;
  assert.equal((await device("PUT")).status, 202);
  assert.equal((await device("PUT")).status, 202);
  assert.equal(events.length, 2);
  assert.deepEqual(events[0], events[1]);
  assert.equal(events[1].data.request_id, id);
  assert.equal((await (await device("GET")).json()).status, "waiting");

  const reply = { name: "submit_stopwatch_reply", arguments: { request_id: id, text: "届いたよ" } };
  assert.ok((await rpc("tools/call", reply)).result);
  assert.ok((await rpc("tools/call", reply)).result);
  assert.deepEqual(await (await device("GET")).json(), { status: "ready", answer: "届いたよ" });
  assert.ok((await rpc("events/unsubscribe", parameters)).result);
  const next = await fetch("/device/text-turns/" + "c".repeat(32), {
    method: "PUT", headers: { Authorization: "Bearer " + DEVICE_TOKEN },
  });
  assert.equal(next.status, 409);
});
