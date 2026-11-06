import { equalSecret, readText, sha256 } from "./http.js";

export const EVENT = "stopwatch.message";
export const DEVICE = "stopwatch";
export const TURN_ID = /^[a-f0-9]{32}$/;
export const FIXTURE = "StopWatch連携の接続テストです。すみ、届いたら短く返答してください。";

export class RpcError extends Error {
  constructor(code, message, data) {
    super(message);
    this.code = code;
    this.data = data;
  }
}

function schema(properties) {
  return { type: "object", properties, required: Object.keys(properties), additionalProperties: false };
}

export const eventDefinition = {
  name: EVENT,
  description: "A fixed-text connection test from the owner's C152 project.",
  delivery: ["webhook"],
  inputSchema: schema({ device_id: { type: "string", enum: [DEVICE] } }),
  payloadSchema: schema({
    device_id: { type: "string" },
    request_id: { type: "string" },
    text: { type: "string" },
  }),
};

export const replyTool = {
  name: "submit_stopwatch_reply",
  description: "Return a short text reply to the owner for the exact request_id in stopwatch.message.",
  inputSchema: schema({
    request_id: { type: "string", pattern: TURN_ID.source },
    text: { type: "string", minLength: 1, maxLength: 300 },
  }),
};

function callbackUrl(value, allowedHost) {
  if (typeof value !== "string" || value.length > 2048) throw new Error("Invalid callback URL");
  const url = new URL(value);
  if (url.protocol !== "https:" || url.port || url.username || url.password || url.hash ||
      !/^(?:[a-z0-9-]+\.)*(?:openai|chatgpt)\.com$/.test(url.hostname)) {
    throw new Error("Invalid callback URL");
  }
  if (url.hostname !== allowedHost) {
    throw new RpcError(-32015, "OpenAI callback host needs operator approval", {
      reason: "unapproved_callback_host", hostname: url.hostname,
    });
  }
  return url;
}

function signingBytes(secret) {
  if (typeof secret !== "string" || !/^whsec_[A-Za-z0-9+/]{32,88}={0,2}$/.test(secret)) {
    throw new Error("Invalid signing secret");
  }
  const key = Uint8Array.from(atob(secret.slice(6)), (character) => character.charCodeAt(0));
  if (key.length < 24 || key.length > 64) throw new Error("Invalid signing secret");
  return key;
}

async function postCallback(env, subscription, id, data) {
  const url = callbackUrl(subscription.url, env.CALLBACK_HOST);
  const body = JSON.stringify(data);
  const timestamp = String(Math.floor(Date.now() / 1000));
  const input = new TextEncoder().encode(`${id}.${timestamp}.${body}`);
  const secrets = [subscription.secret];
  if (subscription.old_secret && subscription.rotate_until > Date.now()) {
    secrets.push(subscription.old_secret);
  }
  const signatures = await Promise.all(secrets.map(async (secret) => {
    const key = await crypto.subtle.importKey(
      "raw", signingBytes(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
    );
    const signature = new Uint8Array(await crypto.subtle.sign("HMAC", key, input));
    return "v1," + btoa(String.fromCharCode(...signature));
  }));
  return fetch(url, {
    method: "POST",
    redirect: "manual",
    signal: AbortSignal.timeout(8000),
    headers: {
      "Content-Type": "application/json",
      "webhook-id": id,
      "webhook-timestamp": timestamp,
      "webhook-signature": signatures.join(" "),
      "X-MCP-Subscription-Id": subscription.id,
    },
    body,
  });
}

export async function subscribe(env, params, remove) {
  const delivery = params?.delivery;
  if (params?.name !== EVENT || params.arguments?.device_id !== DEVICE ||
      Object.keys(params.arguments).length !== 1 || delivery?.mode !== "webhook" ||
      params.cursor != null) {
    throw new RpcError(-32602, "Invalid subscription");
  }
  let url;
  try {
    url = callbackUrl(delivery.url, env.CALLBACK_HOST).href;
    if (!remove) signingBytes(delivery.secret);
  } catch (error) {
    if (error instanceof RpcError) throw error;
    throw new RpcError(-32602, "Callback URL or signing secret is invalid");
  }
  const id = "sub_" + (await sha256(JSON.stringify([DEVICE, EVENT, url]))).slice(0, 32);
  if (remove) {
    await env.DB.prepare("DELETE FROM subscription WHERE slot = 1 AND id = ?").bind(id).run();
    return {};
  }
  const ttl = params.ttlMs;
  if (ttl != null && (!Number.isSafeInteger(ttl) || ttl <= 0)) {
    throw new RpcError(-32602, "Invalid ttlMs");
  }
  const now = Date.now();
  const current = await getSubscription(env);
  if (current && current.id !== id) throw new RpcError(-32602, "Another subscription is active");

  let verified = current?.verified ?? 0;
  if (current?.secret !== delivery.secret || now - verified > 300_000) {
    const challenge = crypto.randomUUID();
    try {
      const response = await postCallback(env, { id, url, secret: delivery.secret },
        "verify_" + crypto.randomUUID(), { type: "verification", challenge });
      if (!response.ok) {
        await response.body?.cancel();
        throw new Error("Callback rejected verification");
      }
      const received = JSON.parse(await readText(response, 8192)).challenge;
      if (typeof received !== "string" || !await equalSecret(received, challenge)) {
        throw new Error("Callback verification mismatch");
      }
    } catch (error) {
      const reason = error.name === "TimeoutError" ? "timeout" : "challenge_failed";
      throw new RpcError(-32015, "Callback verification failed", { reason });
    }
    verified = Date.now();
  }

  const expires = Date.now() + Math.min(ttl ?? 86_400_000, 86_400_000);
  const updated = await env.DB.prepare(`
    INSERT INTO subscription (slot, id, url, secret, expires, verified)
    VALUES (1, ?, ?, ?, ?, ?)
    ON CONFLICT(slot) DO UPDATE SET id = excluded.id, url = excluded.url,
      old_secret = CASE
        WHEN subscription.id != excluded.id THEN NULL
        WHEN subscription.secret != excluded.secret THEN subscription.secret
        ELSE subscription.old_secret END,
      rotate_until = CASE
        WHEN subscription.id != excluded.id THEN NULL
        WHEN subscription.secret != excluded.secret THEN ? ELSE subscription.rotate_until END,
      secret = excluded.secret, expires = excluded.expires, verified = excluded.verified
    WHERE subscription.id = excluded.id OR subscription.expires <= ?
  `).bind(id, url, delivery.secret, expires, verified, Date.now() + 300_000, Date.now()).run();
  if (!updated.meta.changes) throw new RpcError(-32602, "Another subscription is active");
  return { id, refreshBefore: new Date(expires).toISOString(), cursor: null, truncated: false };
}

export function getSubscription(env) {
  return env.DB.prepare("SELECT * FROM subscription WHERE slot = 1 AND expires > ?")
    .bind(Date.now()).first();
}

export async function deliver(env, subscription, turn) {
  const eventId = "evt_" + turn.id;
  const response = await postCallback(env, subscription, eventId, {
    eventId,
    name: EVENT,
    timestamp: new Date(turn.created).toISOString(),
    data: { device_id: DEVICE, request_id: turn.id, text: FIXTURE },
    cursor: null,
  });
  await response.body?.cancel();
  return response.status;
}
