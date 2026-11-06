import { AuthorizationError, OAuthProvider } from "@cloudflare/workers-oauth-provider";
import { TURN_ID } from "./events.js";
import { equalSecret, InputError, json, readText } from "./http.js";
import { mcp, SCOPE } from "./mcp.js";
import { handleTurn } from "./turns.js";

const DEFAULT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect";

function allowedRedirect(env) {
  const url = new URL(env.CHATGPT_REDIRECT_URI || DEFAULT_REDIRECT);
  if (url.origin !== "https://chatgpt.com" || url.username || url.password || url.search || url.hash ||
      !/^\/(connector_platform_oauth_redirect|connector\/oauth\/[a-zA-Z0-9_-]+)$/.test(url.pathname)) {
    throw new Error("Invalid ChatGPT redirect configuration");
  }
  return url.href;
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

async function authorize(request, env, origin, redirectUri) {
  const ownerKey = env.OWNER_KEY;
  if (typeof ownerKey !== "string" || ownerKey.length < 32) {
    return new Response("Owner access is not configured", { status: 503 });
  }
  const oauth = env.OAUTH_PROVIDER;
  try {
    if (request.method === "GET") {
      const auth = await oauth.parseAuthRequest(request);
      if (auth.redirectUri !== redirectUri || auth.codeChallengeMethod !== "S256") {
        return new Response("Unapproved client", { status: 403 });
      }
      const consent = await oauth.describeConsent(auth);
      const { handle, headers } = await oauth.beginConsent(auth);
      headers.set("Content-Type", "text/html; charset=utf-8");
      headers.set("Cache-Control", "no-store");
      headers.set("Referrer-Policy", "no-referrer");
      headers.set("Content-Security-Policy", "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
      return new Response(`<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>すみ StopWatch 接続</title><h1>すみ StopWatch 接続</h1>
<p>${escapeHtml(consent.clientName || "ChatGPT")} に、固定文の受信と返答の記録を許可します。</p>
<p>戻り先 ${escapeHtml(consent.redirectHost)}</p>
<form method="post" action="/authorize">
<input type="hidden" name="handle" value="${escapeHtml(handle)}">
<label>オーナーキー <input name="key" type="password" required maxlength="128" autocomplete="off"></label>
<button type="submit">接続を許可</button></form></html>`, { headers });
    }
    if (request.method !== "POST") return new Response(null, { status: 405 });
    if (request.headers.get("Origin") !== origin ||
        request.headers.get("Content-Type")?.split(";")[0] !== "application/x-www-form-urlencoded") {
      return new Response("Invalid consent request", { status: 403 });
    }
    const form = new URLSearchParams(await readText(request, 4096));
    if (!form.get("handle") || !await equalSecret(form.get("key") ?? "", ownerKey)) {
      return new Response("Authorization failed", { status: 403, headers: { "Cache-Control": "no-store" } });
    }
    const approval = await oauth.approveConsent(request, form.get("handle"), { scope: [SCOPE] });
    if (approval.request.redirectUri !== redirectUri) {
      return new Response("Unapproved client", { status: 403 });
    }
    const { redirectTo } = await oauth.completeAuthorization({
      request: approval.request, userId: "owner", metadata: {}, scope: [SCOPE], props: { owner: true },
    });
    approval.headers.set("Location", redirectTo);
    return new Response(null, { status: 303, headers: approval.headers });
  } catch (error) {
    if (error instanceof AuthorizationError) {
      return error.redirectTo && new URL(error.redirectTo).origin === "https://chatgpt.com"
        ? Response.redirect(error.redirectTo, 302)
        : new Response("Invalid authorization request", { status: 400 });
    }
    if (error instanceof InputError) return new Response("Invalid consent form", { status: 400 });
    throw error;
  }
}

export default {
  async fetch(request, env, ctx) {
    try {
      const url = new URL(request.url);
      const origin = env.RELAY_ORIGIN;
      if (typeof origin !== "string" || !origin.startsWith("https://") || url.origin !== origin) {
        return json({ error: "Relay origin is not configured for this URL" }, 503);
      }
      const redirectUri = allowedRedirect(env);
      const provider = new OAuthProvider({
        apiRoute: "/mcp",
        apiHandler: mcp,
        defaultHandler: {
          async fetch(request, providerEnv) {
            const path = new URL(request.url).pathname;
            if (path === "/authorize") return authorize(request, providerEnv, origin, redirectUri);
            const id = path.slice("/device/text-turns/".length);
            if (path.startsWith("/device/text-turns/") && TURN_ID.test(id)) {
              return handleTurn(request, providerEnv, id);
            }
            return new Response(null, { status: 404 });
          },
        },
        authorizeEndpoint: "/authorize",
        tokenEndpoint: "/oauth/token",
        clientRegistrationEndpoint: "/oauth/register",
        clientRegistrationCallback: ({ clientMetadata }) => {
          const redirects = clientMetadata.redirect_uris;
          if (!Array.isArray(redirects) || redirects.length !== 1 || redirects[0] !== redirectUri) {
            return { description: "Unapproved redirect URI" };
          }
        },
        scopesSupported: [SCOPE],
        requiredScopes: [SCOPE],
        resourceMetadata: { resource: origin + "/mcp", authorization_servers: [origin] },
        refreshTokenTTL: 86_400,
      });
      return await provider.fetch(request, env, ctx);
    } catch (error) {
      console.error(JSON.stringify({ event: "stopwatch_relay_error", type: error.name }));
      return json({ error: "Relay unavailable" }, 500);
    }
  },
  async scheduled(_event, env) {
    const now = Date.now();
    await env.DB.batch([
      env.DB.prepare("DELETE FROM turns WHERE expires <= ?").bind(now),
      env.DB.prepare("DELETE FROM subscription WHERE expires <= ?").bind(now),
    ]);
  },
};
