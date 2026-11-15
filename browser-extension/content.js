(() => {
  if (globalThis.__sumiStopwatchRelayInstalled) return;
  globalThis.__sumiStopwatchRelayInstalled = true;
  const attempted = new Set();
  const visible = element => Boolean(element?.getBoundingClientRect().width && element?.getBoundingClientRect().height);
  function input(path) {
    if (location.origin !== "https://chatgpt.com" || location.pathname.replace(/\/$/, "") !== path) {
      return { ready: false, reason: "選択したすみの会話ではありません。" };
    }
    const editor = [...document.querySelectorAll('[contenteditable="true"][role="textbox"][aria-label="メッセージ"]')].find(visible);
    const buttons = [...document.querySelectorAll('button[aria-label="送信"]')].filter(visible);
    const send = buttons.length === 1 ? buttons[0] : null;
    if (!editor || !send) return { ready: false, reason: "入力欄が見つかりません。画面の読み込みか、すみの応答が終わるのを待ってください。" };
    let composer = editor.parentElement;
    while (composer && !composer.contains(send)) composer = composer.parentElement;
    const attachments = composer ? [...composer.querySelectorAll('[data-testid*="attachment"],button[aria-label*="添付"][aria-label*="削除"],button[aria-label*="attachment"][aria-label*="Remove"]')].some(visible) : true;
    if (editor.textContent.trim() || editor.querySelector('img,[data-type="mention"]') || attachments) {
      return { ready: false, reason: "入力中の下書きがあります。送信または保存してから続けてください。" };
    }
    return { ready: true, editor, send };
  }
  async function deliver(message) {
    const state = input(message.path);
    if (message.type === "sumi-stopwatch-probe") return { ready: state.ready, reason: state.reason };
    if (!state.ready) return { sent: false, reason: state.reason };
    if (!/^[a-f0-9]{32}$/.test(message.id) || typeof message.prompt !== "string" || message.prompt.length > 5000 || attempted.has(message.id)) {
      return { sent: false, reason: "発話を確認できません。自動再送しません。" };
    }
    attempted.add(message.id);
    state.editor.focus();
    if (!document.execCommand("insertText", false, message.prompt)) return { sent: false, reason: "発話を入力できませんでした。" };
    for (let attempt = 0; attempt < 20 && state.send.disabled; attempt++) await new Promise(resolve => setTimeout(resolve, 50));
    const normalized = value => value.replace(/\s+/g, " ").trim();
    if (normalized(state.editor.innerText ?? state.editor.textContent) !== normalized(message.prompt) || state.send.disabled) {
      return { sent: false, reason: "入力欄に変更があったため送信を止めました。下書きを確認してください。" };
    }
    state.send.click();
    for (let attempt = 0; attempt < 30; attempt++) {
      if (!state.editor.textContent.trim()) return { sent: true };
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    return { sent: false, reason: "送信結果を確認できません。自動再送しません。" };
  }
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id || !["sumi-stopwatch-probe", "sumi-stopwatch-send"].includes(message?.type)) return;
    deliver(message).then(respond, error => respond({ ready: false, sent: false, reason: error.message }));
    return true;
  });
})();
