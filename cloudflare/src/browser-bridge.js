import { controlBridge } from "./bridge.js";
import { authorizedHistory } from "./history.js";
import { InputError, json, readText } from "./http.js";
import { RpcError } from "./events.js";

export async function browserBridge(request, env) {
  const url = new URL(request.url);
  if (url.pathname === "/bridge" && request.method === "GET") return page();
  if (url.pathname !== "/bridge/control") return new Response(null, { status: 404 });
  if (request.method !== "POST") return new Response(null, { status: 405 });
  if (request.headers.get("Origin") !== url.origin ||
      request.headers.get("Content-Type")?.split(";")[0] !== "application/json") {
    return json({ error: "この中継ページから操作してください。" }, 403);
  }
  if (!await authorizedHistory(request, env)) {
    return json({ error: "別タブで履歴にログインしてから、この画面で開始してください。" }, 401);
  }
  try {
    const args = JSON.parse(await readText(request, 1024));
    return json(await controlBridge(env, args));
  } catch (error) {
    if (error instanceof RpcError || error instanceof InputError || error instanceof SyntaxError) {
      return json({ error: error instanceof RpcError ? error.message : "入力を確認してください。" }, 400);
    }
    throw error;
  }
}

function page() {
  const nonce = crypto.randomUUID();
  return new Response(`<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>すみとStopWatchをつなぐ</title>
<style nonce="${nonce}">
*{box-sizing:border-box}body{font:16px/1.7 system-ui;margin:0;background:#f6f4f8;color:#292431}main{max-width:680px;margin:40px auto;padding:24px}h1{font-size:27px}section{background:white;border:1px solid #e6dfec;border-radius:20px;padding:24px}button{font:inherit;border:0;border-radius:12px;padding:12px 24px;margin:8px 10px 8px 0;background:#674b8a;color:white}button:disabled{opacity:.5}a{color:#674b8a}#status{font-weight:600}#last{white-space:pre-wrap;overflow-wrap:anywhere}.note{font-size:14px;color:#766d80}
</style><main><h1>すみとStopWatchをつなぐ</h1><section>
<p>既存のすみのタブでChrome拡張を接続してから、この画面で開始してください。音声の録音と読み上げはStopWatch本体で行います。</p>
<p><a href="/history" target="_blank" rel="noopener">履歴と同じオーナーキーでログイン</a> · <a href="/history" target="_blank" rel="noopener">会話履歴</a></p>
<p id="status" role="status">拡張から開いた中継ページか確認しています…</p>
<button id="start" disabled>開始</button><button id="stop" disabled>停止</button><p id="last"></p>
<p class="note">使用中はこのページとすみの会話を開いたままにしてください。画面の「接続中」が実際の開始状態です。タブを再読み込みした場合は拡張から接続し直してください。</p>
</section></main><script nonce="${nonce}">
(() => {
  const extension = new URLSearchParams(location.hash.slice(1)).get('extension');
  const start = document.querySelector('#start'), stop = document.querySelector('#stop');
  const status = document.querySelector('#status'), last = document.querySelector('#last');
  const session = crypto.randomUUID().replaceAll('-','');
  let running = false, timer;
  const message = (type,turn) => new Promise((resolve,reject) => {
    if (!/^[a-p]{32}$/.test(extension || '') || !globalThis.chrome?.runtime?.sendMessage) {
      reject(new Error('既存すみのタブでChrome拡張を開き、「このすみに接続」を押してください。')); return;
    }
    chrome.runtime.sendMessage(extension,{type,...(turn?{turn}:{})},result => {
      const error = chrome.runtime.lastError;
      if (error || !result?.ok) reject(new Error(error?.message || result?.error || '拡張へ接続できません。'));
      else resolve(result);
    });
  });
  async function control(action,request_id) {
    const response = await fetch('/bridge/control',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:session,action,...(request_id?{request_id}:{})})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '中継を確認できません。');
    return result;
  }
  async function halt(error) {
    running=false; clearTimeout(timer); start.disabled=false; stop.disabled=true;
    status.textContent=error?.message || '停止しました。新しい発話は従来の経路へ戻ります。';
    try { await control('stop'); } catch {}
  }
  async function poll() {
    if (!running) return;
    try {
      const ready = await message('check');
      if (!running) return;
      if (!ready.ready) {
        await control('start'); status.textContent='待機中：'+ready.reason;
      } else {
        const result = await control('poll');
        if (!running) return;
        if (result.request) {
          last.textContent='届いたお話：'+result.request.text;
          const sent = await message('send',result.request);
          if (!sent.sent) { await control('reject',result.request.request_id); throw new Error(sent.reason || '入力欄を確認してください。'); }
          await control('sent',result.request.request_id);
          status.textContent='接続中。すみの会話へ送りました。返事はStopWatchから聞こえます。';
        } else if (!last.textContent) status.textContent='接続中。左ボタンを押して話しかけてください。';
      }
      timer=setTimeout(poll,1000);
    } catch(error) { await halt(error); }
  }
  start.onclick=async()=>{
    start.disabled=true;
    try {
      const ready=await message('check'); if (!ready.ready) throw new Error(ready.reason);
      await control('start'); running=true; stop.disabled=false;
      status.textContent='接続中。左ボタンを押して話しかけてください。'; void poll();
    } catch(error) { await halt(error); }
  };
  stop.onclick=()=>{void halt();};
  message('check').then(result=>{
    start.disabled=false; status.textContent=result.ready?'準備できました。開始を押してください。':result.reason;
  }).catch(error=>{status.textContent=error.message;});
})();
</script></html>`, {
    headers: {
      "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Referrer-Policy": "strict-origin",
      "X-Content-Type-Options": "nosniff",
      "Content-Security-Policy": `default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`,
    },
  });
}
