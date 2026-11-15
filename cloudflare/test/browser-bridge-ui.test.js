import assert from "node:assert/strict";
import vm from "node:vm";
import { test } from "node:test";
import { browserBridge } from "../src/browser-bridge.js";

const settle = () => new Promise(resolve => setImmediate(resolve));
async function fixture(installed) {
  const response = await browserBridge(new Request("https://relay.example.com/bridge"), {});
  const html = await response.text();
  const nodes = Object.fromEntries(["#start", "#stop", "#status", "#last"].map(id => [id, { disabled: true, textContent: "" }]));
  const actions = [];
  const chrome = installed ? { runtime: { sendMessage: (_id, _message, respond) => queueMicrotask(() => respond({ ok: true, ready: true })) } } : undefined;
  vm.runInNewContext(html.match(/<script nonce="[^"]+">([\s\S]*)<\/script>/)[1], {
    document: { querySelector: selector => nodes[selector] },
    location: { hash: "#extension=" + "a".repeat(32) }, URLSearchParams,
    crypto: { randomUUID: () => "b".repeat(32) }, chrome,
    fetch: async (_url, options) => {
      actions.push(JSON.parse(options.body).action);
      return { ok: true, json: async () => ({ active: true, request: null }) };
    },
    setTimeout: () => 1, clearTimeout: () => {},
  });
  await settle();
  return { nodes, actions };
}

test("standalone bridge never activates when the extension is unavailable", async () => {
  const f = await fixture(false);
  assert.equal(f.nodes["#start"].disabled, true);
  assert.ok(f.nodes["#status"].textContent.includes("Chrome拡張"));
  assert.deepEqual(f.actions, []);
});
test("standalone bridge checks the selected tab and activates only after clicking start", async () => {
  const f = await fixture(true);
  assert.equal(f.nodes["#start"].disabled, false);
  assert.deepEqual(f.actions, []);
  await f.nodes["#start"].onclick(); await settle();
  assert.ok(f.actions.includes("start"));
  assert.ok(f.actions.includes("poll"));
  assert.equal(f.nodes["#stop"].disabled, false);
  f.nodes["#stop"].onclick(); await settle();
  assert.equal(f.actions.at(-1), "stop");
});
