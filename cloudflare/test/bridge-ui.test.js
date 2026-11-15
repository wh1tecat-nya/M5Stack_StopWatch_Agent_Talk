import assert from "node:assert/strict";
import { test } from "node:test";
import vm from "node:vm";
import { bridgeHtml } from "../src/bridge-ui.js";

const settle = () => new Promise(resolve => setImmediate(resolve));
function fixture({ supported = true, reject = false, ambiguous = false } = {}) {
  const nodes = Object.fromEntries(["#start", "#stop", "#status", "#last"].map(id => [id, { disabled: true, textContent: "" }]));
  const timers = new Map(), calls = [], messages = [];
  let listener, sequence = 0, claimed = false;
  const parent = { postMessage(message) {
    if (!message.id) return;
    calls.push(message);
    let result;
    if (message.method === "ui/initialize") {
      result = { hostCapabilities: supported ? { serverTools: {}, message: { text: {} } } : {} };
    } else if (message.method === "tools/call") {
      const { action } = message.params.arguments;
      let turn = null;
      if (action === "poll" && !claimed) { claimed = true; turn = { request_id: "a".repeat(32), text: "すみ、聞こえる？" }; }
      result = { structuredContent: { active: action !== "stop", request: turn } };
    } else if (message.method === "ui/message") {
      messages.push(message.params);
      if (ambiguous) return;
      result = { isError: reject };
    }
    queueMicrotask(() => listener({ source: parent, data: { jsonrpc: "2.0", id: message.id, result } }));
  } };
  vm.runInNewContext(bridgeHtml.match(/<script>([\s\S]*)<\/script>/)[1], {
    parent, window: {}, crypto: { randomUUID: () => "b".repeat(32) },
    document: { querySelector: id => nodes[id], documentElement: { scrollWidth: 380, scrollHeight: 240 } },
    addEventListener: (_name, handler) => { listener = handler; },
    setTimeout: (callback, delay) => { const id = ++sequence; timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => timers.delete(id),
  });
  return { nodes, timers, calls, messages };
}

test("unsupported host never enables or claims the bridge", async () => {
  const f = fixture({ supported: false }); await settle();
  assert.equal(f.nodes["#start"].disabled, true);
  assert.ok(f.nodes["#status"].textContent.includes("対応していません"));
  assert.equal(f.calls.filter(call => call.method === "tools/call").length, 0);
});
test("panel forwards a claimed transcript once and records host acceptance", async () => {
  const f = fixture(); await settle();
  assert.equal(f.nodes["#start"].disabled, false);
  await f.nodes["#start"].onclick(); await settle();
  assert.equal(f.messages.length, 1);
  assert.equal(f.messages[0].role, "user");
  assert.ok(f.messages[0].content[0].text.includes("すみ、聞こえる？"));
  assert.ok(f.messages[0].content[0].text.includes("a".repeat(32)));
  assert.ok(f.calls.some(call => call.params?.arguments?.action === "sent"));
  const poll = [...f.timers.values()].find(timer => timer.delay === 1000);
  await poll.callback(); await settle();
  assert.equal(f.messages.length, 1);
  f.nodes["#stop"].onclick(); await settle();
  assert.equal(f.nodes["#stop"].disabled, true);
});
test("host rejection fails only the claimed turn and stops the bridge", async () => {
  const f = fixture({ reject: true }); await settle();
  await f.nodes["#start"].onclick(); await settle();
  const actions = f.calls.map(call => call.params?.arguments?.action);
  assert.ok(actions.includes("reject")); assert.ok(actions.includes("stop"));
  assert.ok(!actions.includes("sent")); assert.equal(f.messages.length, 1);
});
test("ambiguous send timeout stops new claims without resending or rejecting the turn", async () => {
  const f = fixture({ ambiguous: true }); await settle();
  await f.nodes["#start"].onclick(); await settle();
  [...f.timers.values()].find(timer => timer.delay === 25000).callback(); await settle();
  const actions = f.calls.map(call => call.params?.arguments?.action);
  assert.ok(actions.includes("stop")); assert.ok(!actions.includes("reject"));
  assert.equal(f.messages.length, 1);
  assert.ok(f.nodes["#status"].textContent.includes("自動再送しません"));
});
