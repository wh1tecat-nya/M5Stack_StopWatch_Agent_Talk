import { equalSecret, InputError, readText, sha256 } from "./http.js";

const COOKIE = "__Host-sumi_history";
const SESSION_SECONDS = 7 * 24 * 60 * 60;
const PAGE_SIZE = 30;

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function cookie(token, seconds = SESSION_SECONDS) {
  return `${COOKIE}=${token}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${seconds}`;
}

function sessionToken(request) {
  return request.headers.get("Cookie")?.match(/(?:^|;\s*)__Host-sumi_history=([a-f0-9]{64})(?:;|$)/)?.[1];
}

function page(content, status = 200, extraHeaders = {}) {
  const nonce = crypto.randomUUID();
  return new Response(`<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>すみとの会話</title>
<style nonce="${nonce}">
*{box-sizing:border-box}body{margin:0;background:#f6f4f8;color:#292431;font:16px/1.7 system-ui,sans-serif}
main{max-width:740px;margin:auto;padding:28px 18px 70px}h1{font-size:25px;margin:0}header{margin-bottom:28px}
p{margin:8px 0}nav{display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-top:16px}
a{color:#674b8a}button{font:inherit;color:white;background:#674b8a;border:0;border-radius:12px;padding:10px 18px;cursor:pointer}
input{display:block;width:100%;font:inherit;border:1px solid #afa4bb;border-radius:10px;padding:12px;margin:10px 0 18px}
section{margin:26px 0 40px}time,.note{font-size:14px;color:#766d80}.bubble{border-radius:18px;padding:14px 18px;white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0}
.owner{background:#e6dff0;margin-left:30px}.sumi{background:white;border:1px solid #e6dfec;margin-right:30px}
.name{font-size:13px;color:#6b6077;margin:10px 2px 2px}.error{color:#9a2b3c}form{margin:0}
</style><main>${content}</main></html>`, {
    status,
    headers: {
      "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
      "Content-Security-Policy": `default-src 'none'; style-src 'nonce-${nonce}'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`,
      "Referrer-Policy": "strict-origin", "X-Content-Type-Options": "nosniff", ...extraHeaders,
    },
  });
}

function loginPage(message = "", status = 200) {
  return page(`<header><h1>すみとの会話</h1><p>StopWatchからのお話と、すみの返事を読み返せます。</p></header>
${message ? `<p class="error">${escapeHtml(message)}</p>` : ""}
<form method="post" action="/history/login"><label>オーナーキー
<input type="password" name="key" required maxlength="128" autocomplete="off"></label><button>履歴を開く</button></form>`, status);
}

export async function authorizedHistory(request, env) {
  if (typeof env.OWNER_KEY !== "string" || env.OWNER_KEY.length < 32) return false;
  const token = sessionToken(request);
  if (!token) return false;
  const session = await env.OAUTH_KV.get("history:" + await sha256(token), "json");
  return session?.expires > Date.now() && typeof session.owner === "string" &&
    await equalSecret(session.owner, await sha256(env.OWNER_KEY));
}

function redirect(headers = {}) {
  return new Response(null, { status: 303, headers: { Location: "/history", "Cache-Control": "no-store", ...headers } });
}

export async function history(request, env) {
  const url = new URL(request.url);
  if (typeof env.OWNER_KEY !== "string" || env.OWNER_KEY.length < 32) {
    return page("<h1>閲覧の設定が必要です</h1>", 503);
  }
  if (url.pathname === "/history/login" && request.method === "POST") {
    if (request.headers.get("Origin") !== url.origin ||
        request.headers.get("Content-Type")?.split(";")[0] !== "application/x-www-form-urlencoded") {
      return loginPage("このページからログインしてください。", 403);
    }
    let form;
    try {
      form = new URLSearchParams(await readText(request, 1024));
    } catch (error) {
      if (!(error instanceof InputError)) throw error;
      return loginPage("入力を確認してください。", 400);
    }
    if (!await equalSecret(form.get("key") ?? "", env.OWNER_KEY)) {
      return loginPage("オーナーキーを確認してください。", 403);
    }
    const token = Array.from(crypto.getRandomValues(new Uint8Array(32)), (byte) => byte.toString(16).padStart(2, "0")).join("");
    await env.OAUTH_KV.put("history:" + await sha256(token), JSON.stringify({
      owner: await sha256(env.OWNER_KEY), expires: Date.now() + SESSION_SECONDS * 1000,
    }), { expirationTtl: SESSION_SECONDS });
    return redirect({ "Set-Cookie": cookie(token) });
  }
  if (url.pathname === "/history/logout" && request.method === "POST") {
    if (request.headers.get("Origin") !== url.origin) return new Response(null, { status: 403 });
    const token = sessionToken(request);
    if (token) await env.OAUTH_KV.delete("history:" + await sha256(token));
    return redirect({ "Set-Cookie": cookie("", 0) });
  }
  if (url.pathname !== "/history") return new Response(null, { status: 404 });
  if (request.method !== "GET") return new Response(null, { status: 405 });
  if (!await authorizedHistory(request, env)) return loginPage();
  const cursor = url.searchParams.get("before");
  const parsed = cursor?.match(/^(\d{1,16})_([a-f0-9]{32})$/);
  if (cursor && !parsed) return page("<p>ページの指定を確認してください。</p>", 400);
  const db = env.DB.withSession("first-primary");
  const statement = parsed
    ? db.prepare(`SELECT id, created, transcript, answer, status FROM conversation_history
        WHERE (created, id) < (?, ?) ORDER BY created DESC, id DESC LIMIT ?`)
      .bind(Number(parsed[1]), parsed[2], PAGE_SIZE + 1)
    : db.prepare(`SELECT id, created, transcript, answer, status FROM conversation_history
        ORDER BY created DESC, id DESC LIMIT ?`).bind(PAGE_SIZE + 1);
  const { results } = await statement.all();
  const rows = results.slice(0, PAGE_SIZE);
  const heading = `<header><h1>すみとの会話</h1><p>StopWatchで交わした会話の記録</p>
<nav><a href="/history">最新の会話</a><form method="post" action="/history/logout"><button>ログアウト</button></form></nav>
<p class="note">履歴の保存開始後の会話を、期限を設けずに残しています。</p></header>`;
  const messages = rows.map((row) => {
    const date = new Date(row.created);
    const when = date.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" });
    const expired = row.created + 600000 <= Date.now();
    const pending = row.status === "failed" ? "お返事を届けられませんでした。"
      : expired ? "お返事が期限内に届きませんでした。" : "お返事を待っています。";
    return `<section><time datetime="${date.toISOString()}">${escapeHtml(when)}（日本時間）</time>
<p class="name">あなた · StopWatch</p><div class="bubble owner">${escapeHtml(row.transcript ?? "音声を文字にできませんでした。")}</div>
<p class="name">すみ</p><div class="bubble sumi">${escapeHtml(row.answer ?? pending)}</div></section>`;
  }).join("");
  const last = rows.at(-1);
  const more = results.length > PAGE_SIZE
    ? `<a href="/history?before=${last.created}_${last.id}">以前の会話を読む</a>` : "";
  return page(heading + (messages || "<p>保存された会話はまだありません。</p>") + more);
}
