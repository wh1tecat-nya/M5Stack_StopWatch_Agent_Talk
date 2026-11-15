import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { test } from "node:test";
import { createRelay } from "../../browser-extension/background.mjs";
import { connectTab } from "../../browser-extension/popup.mjs";
import { allowedSender, dotPath, promptFor, RELAY_ORIGIN, validTurn } from "../../browser-extension/shared.mjs";

const path = "/dots/existing-sumi~conversation";
const turn = { request_id: "a".repeat(32), text: "すみ、聞こえる？" };
const sender = { url: RELAY_ORIGIN + "/bridge#extension=" + "b".repeat(32), tab: { id: 20 } };

test("extension grants access only after selecting a tab and restricts its broker", async () => {
  const manifest = JSON.parse(await readFile(new URL("../../browser-extension/manifest.json", import.meta.url), "utf8"));
  assert.deepEqual(manifest.permissions, ["activeTab", "scripting", "storage"]);
  assert.equal(manifest.host_permissions, undefined);
  assert.equal(manifest.content_scripts, undefined);
  assert.deepEqual(manifest.externally_connectable.matches, [RELAY_ORIGIN + "/bridge*"]);
  assert.equal(dotPath("https://chatgpt.com" + path), path);
  assert.equal(dotPath("https://evil.example" + path), null);
  assert.equal(dotPath("https://chatgpt.com/c/another-chat"), null);
  assert.equal(allowedSender(sender, 20), true);
  assert.equal(allowedSender(sender, 21), false);
  assert.equal(allowedSender({ ...sender, url: "https://evil.example/bridge" }, 20), false);
  assert.equal(allowedSender({ ...sender, url: RELAY_ORIGIN + "/history" }, 20), false);
  assert.equal(validTurn({ ...turn, request_id: "invalid" }), false);
  assert.equal(validTurn({ ...turn, text: "x".repeat(4001) }), false);
  assert.ok(promptFor(turn).includes(turn.request_id));
});

test("popup registers the broker before focusing it or loading its handshake", async () => {
  const state = {}, events = [];
  await connectTab({
    runtime: { id: "b".repeat(32) },
    storage: { session: { set: async value => { Object.assign(state, value); events.push("save"); } } },
    scripting: { executeScript: async options => { assert.equal(options.target.tabId, 10); events.push("inject"); } },
    tabs: {
      query: async () => [{ id: 10, url: "https://chatgpt.com" + path }],
      create: async options => { assert.equal(options.active, false); assert.equal(options.url, "about:blank"); return { id: 20 }; },
      update: async (id, options) => {
        assert.equal(id, 20); assert.equal(state.brokerTab, 20); assert.equal(state.targetTab, 10);
        assert.equal(options.active, true); assert.equal(options.url, RELAY_ORIGIN + "/bridge#extension=" + "b".repeat(32));
        events.push("focus");
      },
    },
  });
  assert.equal(events.at(-1), "focus");
});

function relayFixture({ draft = false, targetPath = path } = {}) {
  const state = { targetTab: 10, targetPath: path, brokerTab: 20 };
  const sends = [];
  const api = {
    storage: { session: { get: async () => structuredClone(state), set: async value => Object.assign(state, value) } },
    tabs: { get: async () => ({ id: 10, url: "https://chatgpt.com" + targetPath }),
      sendMessage: async (_id, message) => {
        if (message.type === "sumi-stopwatch-probe") return { ready: !draft, reason: "下書き" };
        sends.push(message); return { sent: true };
      } },
  };
  return { relay: createRelay(api), api, sends, state };
}
test("extension reserves each turn once, including concurrent requests and worker restart", async () => {
  const f = relayFixture();
  const result = await Promise.all([f.relay({ type: "send", turn }, sender), f.relay({ type: "send", turn }, sender)]);
  assert.equal(result.filter(item => item.sent).length, 1);
  assert.equal(f.sends.length, 1);
  assert.equal((await createRelay(f.api)({ type: "send", turn }, sender)).ambiguous, true);
  assert.equal(f.sends.length, 1);
});
test("extension refuses other tabs, origins, conversations and existing drafts", async () => {
  const f = relayFixture();
  await assert.rejects(f.relay({ type: "send", turn }, { ...sender, tab: { id: 21 } }));
  await assert.rejects(f.relay({ type: "send", turn }, { ...sender, url: "https://evil.example/bridge" }));
  const moved = relayFixture({ targetPath: "/dots/another-person" });
  await assert.rejects(moved.relay({ type: "send", turn }, sender));
  const draft = relayFixture({ draft: true });
  assert.equal((await draft.relay({ type: "send", turn }, sender)).ready, false);
  assert.equal(draft.state.attempts, undefined);
  assert.equal(f.sends.length + moved.sends.length + draft.sends.length, 0);
});

async function contentFixture({ draft = "", changed = false, attachment = false, multiline = false } = {}) {
  let listener, clicks = 0;
  const editor = { textContent: draft, getBoundingClientRect: () => ({ width: 300, height: 50 }),
    querySelector: () => null, focus: () => {} };
  editor.parentElement = { contains: () => true, querySelectorAll: () => attachment ? [editor] : [] };
  const send = { disabled: !draft, getBoundingClientRect: () => ({ width: 40, height: 40 }),
    click: () => { clicks++; editor.textContent = ""; editor.innerText = ""; } };
  const code = await readFile(new URL("../../browser-extension/content.js", import.meta.url), "utf8");
  vm.runInNewContext(code, {
    chrome: { runtime: { id: "extension", onMessage: { addListener: callback => { listener = callback; } } } },
    location: { origin: "https://chatgpt.com", pathname: path }, setTimeout,
    document: {
      querySelectorAll: selector => selector.includes("contenteditable") ? [editor] : [send],
      execCommand: (_command, _show, text) => {
        const value = text + (changed ? "ユーザーの追記" : "");
        editor.textContent = multiline ? value.replace(/\n/g, "") : value;
        if (multiline) editor.innerText = value.replace(/\n/g, "\n\n");
        send.disabled = false; return true;
      },
    },
  });
  const call = message => new Promise(resolve => listener(message, { id: "extension" }, resolve));
  return { call, editor, clicks: () => clicks };
}
test("content script sends only to the empty selected composer", async () => {
  const f = await contentFixture({ multiline: true });
  const result = await f.call({ type: "sumi-stopwatch-send", path, id: turn.request_id, prompt: promptFor(turn) });
  assert.equal(result.sent, true); assert.equal(f.clicks(), 1);
  assert.equal((await f.call({ type: "sumi-stopwatch-send", path, id: turn.request_id, prompt: promptFor(turn) })).sent, false);
});
test("content script preserves drafts, attachments and user edits before send", async () => {
  for (const options of [{ draft: "入力中のお話" }, { attachment: true }, { changed: true }]) {
    const f = await contentFixture(options);
    assert.equal((await f.call({ type: "sumi-stopwatch-send", path, id: turn.request_id, prompt: promptFor(turn) })).sent, false);
    assert.equal(f.clicks(), 0);
    if (options.draft) assert.equal(f.editor.textContent, options.draft);
  }
});
