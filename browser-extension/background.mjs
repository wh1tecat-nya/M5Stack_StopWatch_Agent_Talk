import { allowedSender, dotPath, promptFor, validTurn } from "./shared.mjs";

export function createRelay(api) {
  let queue = Promise.resolve();
  async function handle(message, sender) {
    const state = await api.storage.session.get(["targetTab", "targetPath", "brokerTab", "attempts"]);
    if (!allowedSender(sender, state.brokerTab)) throw new Error("接続元を確認できません。");
    if (!["check", "send"].includes(message?.type)) throw new Error("Invalid request");
    const tab = await api.tabs.get(state.targetTab);
    if (!state.targetPath || dotPath(tab.url) !== state.targetPath) throw new Error("選択したすみの会話を開いて、拡張を接続し直してください。");
    const ready = await api.tabs.sendMessage(tab.id, { type: "sumi-stopwatch-probe", path: state.targetPath });
    if (!ready?.ready) return { ok: true, ready: false, reason: ready?.reason || "入力欄を確認してください。" };
    if (message.type === "check") return { ok: true, ready: true };
    if (!validTurn(message.turn)) throw new Error("Invalid turn");
    const attempts = state.attempts || {};
    const id = message.turn.request_id;
    if (attempts[id]) return { ok: false, ambiguous: true, error: "この発話は送信を試みています。二重送信を避けて停止しました。" };
    attempts[id] = "attempted";
    const retained = Object.fromEntries(Object.entries(attempts).slice(-64));
    await api.storage.session.set({ attempts: retained });
    const result = await api.tabs.sendMessage(tab.id, {
      type: "sumi-stopwatch-send", path: state.targetPath, id, prompt: promptFor(message.turn),
    });
    if (!result?.sent) return { ok: false, ambiguous: true, error: result?.reason || "送信結果を確認できません。自動再送しません。" };
    return { ok: true, sent: true };
  }
  return (message, sender) => {
    const next = queue.then(() => handle(message, sender));
    queue = next.catch(() => {});
    return next;
  };
}

if (globalThis.chrome?.runtime?.onMessageExternal) {
  const relay = createRelay(chrome);
  chrome.runtime.onMessageExternal.addListener((message, sender, respond) => {
    relay(message, sender).then(respond, error => respond({ ok: false, error: error.message }));
    return true;
  });
}
