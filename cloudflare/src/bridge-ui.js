export const BRIDGE_URI = "ui://sumi-stopwatch/bridge-v1.html";
export const bridgeHtml = String.raw`<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>すみ StopWatch 会話中継</title>
<style>
body{font:15px system-ui,sans-serif;margin:0;padding:20px;color:var(--color-text-primary,#222);background:var(--color-background-primary,#fff)}
h1{font-size:20px;margin:0 0 12px}p{line-height:1.6}button{font:inherit;padding:12px 18px;margin:6px 8px 6px 0;border:1px solid #888;border-radius:12px;background:transparent;color:inherit}
#status{font-weight:600}#last{white-space:pre-wrap;overflow-wrap:anywhere}small{color:var(--color-text-secondary,#666)}
</style>
<h1>すみとStopWatchをつなぐ</h1>
<p>既存のすみの会話で「開始」を押してください。StopWatchから話すと、この会話へ届けます。マイクとスピーカーは本体を使います。</p>
<p id="status" role="status">この画面が対応しているか確認中…</p>
<button id="start" disabled>開始</button><button id="stop" disabled>停止</button>
<p id="last"></p><small>使用中はこの画面を開いたままにしてください。閉じると、新しい発話は最大45秒後に従来の経路へ戻ります。</small>
<script>
(() => {
  const status = document.querySelector('#status'), last = document.querySelector('#last');
  const start = document.querySelector('#start'), stop = document.querySelector('#stop');
  const session = crypto.randomUUID().replaceAll('-', '');
  const pending = new Map(); let sequence = 0, running = false, timer;
  function notify(method, params = {}) { parent.postMessage({jsonrpc:'2.0',method,params}, '*'); }
  function request(method, params = {}) {
    return new Promise((resolve,reject) => {
      const id = ++sequence;
      const timeout = setTimeout(() => { pending.delete(id); reject(new Error('応答を確認できませんでした。二重送信を避けるため、自動再送しません。')); }, 25000);
      pending.set(id, {resolve,reject,timeout});
      parent.postMessage({jsonrpc:'2.0',id,method,params}, '*');
    });
  }
  addEventListener('message', (event) => {
    if (event.source !== parent || event.data?.jsonrpc !== '2.0') return;
    const message = event.data;
    if (pending.has(message.id)) {
      const item = pending.get(message.id); pending.delete(message.id); clearTimeout(item.timeout);
      if (message.error) item.reject(Object.assign(new Error(message.error.message), {code:message.error.code}));
      else item.resolve(message.result);
    } else if (message.method === 'ping') {
      parent.postMessage({jsonrpc:'2.0',id:message.id,result:{}}, '*');
    } else if (message.method === 'ui/resource-teardown') {
      running = false; clearTimeout(timer);
      parent.postMessage({jsonrpc:'2.0',id:message.id,result:{}}, '*');
    }
  });
  async function control(action, request_id) {
    const result = await request('tools/call', {name:'control_stopwatch_bridge',arguments:{session_id:session,action,...(request_id?{request_id}:{})}});
    if (result?.isError || !result?.structuredContent) throw new Error(result?.content?.find(x=>x.type==='text')?.text || '中継を確認できませんでした。');
    return result.structuredContent;
  }
  async function halt(error) {
    running = false; clearTimeout(timer); start.disabled = false; stop.disabled = true;
    status.textContent = error?.message || '停止しました。新しい発話は従来の経路へ戻ります。';
    try { await control('stop'); } catch {}
  }
  async function poll() {
    if (!running) return;
    try {
      const result = await control('poll');
      if (!running) return;
      if (result.request) {
        const turn = result.request;
        last.textContent = '届いたお話：' + turn.text;
        status.textContent = 'すみの会話へ送信中…';
        const text = 'StopWatchからの音声メッセージです。\n' + turn.text +
          '\n\nすみとして短く自然に返答し、同じ返答本文をsubmit_stopwatch_replyでStopWatchへ返してください。request_id: ' +
          turn.request_id + '。本文は日本語1〜2文、80文字程度。';
        const sent = await request('ui/message', {role:'user',content:[{type:'text',text}]});
        if (sent?.isError) {
          await control('reject', turn.request_id);
          throw new Error('この会話では送信できませんでした。');
        }
        await control('sent', turn.request_id);
        status.textContent = 'すみの会話へ送りました。返事はStopWatchから聞こえます。';
      }
      timer = setTimeout(poll, 1000);
    } catch (error) { await halt(error); }
  }
  start.onclick = async () => {
    start.disabled = true;
    try {
      await control('start'); running = true; stop.disabled = false;
      status.textContent = '接続中。StopWatchの左ボタンを押して話しかけてください。';
      void poll();
    } catch (error) { await halt(error); }
  };
  stop.onclick = () => { void halt(); };
  if (parent === window) { status.textContent = '既存のすみの会話から、この中継画面を開いてください。'; return; }
  request('ui/initialize', {protocolVersion:'2026-01-26',appInfo:{name:'Sumi StopWatch Bridge',version:'1.0.0'},appCapabilities:{}})
    .then((host) => {
      notify('ui/notifications/initialized');
      if (!host.hostCapabilities?.serverTools || !host.hostCapabilities?.message?.text) {
        throw new Error('この画面は会話への送信に対応していません。従来の経路は引き続き使えます。');
      }
      start.disabled = false; status.textContent = '準備できました。すみの会話で開いていることを確認して開始してください。';
      notify('ui/notifications/size-changed',{width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight});
    }).catch(error => { status.textContent = error.message; });
})();
</script></html>`;
