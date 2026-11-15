import { dotPath, RELAY_ORIGIN } from "./shared.mjs";

export async function connectTab(api) {
  const [tab] = await api.tabs.query({ active: true, currentWindow: true });
  const path = dotPath(tab?.url);
  if (!path) throw new Error("先に既存のすみの会話を開いてください。");
  await api.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  await api.storage.session.set({ targetTab: tab.id, targetPath: path, brokerTab: null });
  // Focusing a new tab destroys this popup before subsequent awaits can finish.
  const broker = await api.tabs.create({ url: "about:blank", active: false });
  await api.storage.session.set({ brokerTab: broker.id });
  await api.tabs.update(broker.id, { url: RELAY_ORIGIN + "/bridge#extension=" + api.runtime.id, active: true });
}

if (globalThis.document) {
  const button = document.querySelector("#connect"), status = document.querySelector("#status");
  button.onclick = async () => {
    button.disabled = true;
    try {
      await connectTab(chrome);
      status.textContent = "中継ページで「開始」を押してください。";
    } catch (error) { status.textContent = error.message; button.disabled = false; }
  };
}
