import { deliver, getSubscription, RpcError } from "./events.js";
import { equalSecret, InputError, json, readText } from "./http.js";

export const TURN_TTL = 600_000;
const MAX_ATTEMPTS = 3;

export async function authorizedDevice(request, env) {
  const token = env.DEVICE_TOKEN;
  const auth = request.headers.get("Authorization") ?? "";
  return typeof token === "string" && token.length >= 32 && auth.length <= 256 &&
    await equalSecret(auth, "Bearer " + token);
}

export async function handleTurn(request, env, id) {
  if (!await authorizedDevice(request, env)) {
    return json({ error: "Unauthorized" }, 401);
  }

  const now = Date.now();
  if (request.method === "GET") {
    const turn = await env.DB.prepare("SELECT status, answer FROM turns WHERE id = ? AND expires > ? AND audio_phase IS NULL")
      .bind(id, now).first();
    return turn ? json(turn) : json({ error: "Unknown or expired request" }, 404);
  }
  if (request.method !== "PUT") return new Response(null, { status: 405 });
  try {
    await readText(request, 0);
  } catch (error) {
    if (!(error instanceof InputError)) throw error;
    return json({ error: "This probe only sends the fixed message" }, 400);
  }

  await env.DB.prepare("DELETE FROM turns WHERE expires <= ?").bind(now).run();
  const prior = await env.DB.prepare("SELECT status, subscription_id, audio_phase FROM turns WHERE id = ?").bind(id).first();
  if (prior?.audio_phase) return json({ error: "Request ID already used" }, 409);
  if (prior?.status === "ready" || prior?.status === "waiting") return json({ id }, 202);
  if (prior?.status === "failed") return json({ error: "Delivery failed. Use a new request ID." }, 409);

  const subscription = await getSubscription(env);
  if (!subscription || (prior && prior.subscription_id !== subscription.id)) {
    return json({ error: "No matching Sumi subscription" }, 409);
  }
  if (!prior) {
    await env.DB.prepare(`
      INSERT INTO turns (id, subscription_id, created, expires, status)
      VALUES (?, ?, ?, ?, 'pending') ON CONFLICT(id) DO NOTHING
    `).bind(id, subscription.id, now, now + TURN_TTL).run();
  }

  const status = await deliverTurn(env, subscription, id, false);
  if (status === 202) return json({ id }, 202);
  return json({ error: status === 409 ? "Delivery failed" : "Retry the same request ID" }, status);
}

export async function deliverTurn(env, subscription, id, audio) {
  const now = Date.now();
  const lease = now + 15_000;
  const turn = await env.DB.prepare(`
    UPDATE turns SET status = 'delivering', lease_until = ?, attempts = attempts + 1
    WHERE id = ? AND subscription_id = ? AND attempts < ? AND expires > ? AND
      (status = 'pending' OR (status = 'delivering' AND lease_until <= ?)) AND
      CASE WHEN ? THEN audio_phase = 'turn' ELSE audio_phase IS NULL END AND
      subscription_id = (SELECT id FROM subscription WHERE slot = 1 AND expires > ?)
    RETURNING id, created, attempts, transcript
  `).bind(lease, id, subscription.id, MAX_ATTEMPTS, now, now, audio ? 1 : 0, now).first();
  if (!turn) {
    const exhausted = await env.DB.prepare(`
      UPDATE turns SET status = 'failed', lease_until = NULL WHERE id = ?
      AND status = 'delivering' AND lease_until <= ? AND attempts >= ?
      AND CASE WHEN ? THEN audio_phase = 'turn' ELSE audio_phase IS NULL END
    `).bind(id, Date.now(), MAX_ATTEMPTS, audio ? 1 : 0).run();
    return exhausted.meta.changes ? 409 : 202;
  }

  let status = 0;
  try {
    status = await deliver(env, subscription, turn);
  } catch {
    console.warn(JSON.stringify({ event: "stopwatch_delivery_unavailable", id }));
  }
  const accepted = status >= 200 && status < 300;
  const finalFailure = status === 410 || status === 413 || turn.attempts >= MAX_ATTEMPTS;
  await env.DB.prepare(`
    UPDATE turns SET status = ?, lease_until = NULL, audio_lease = ?
    WHERE id = ? AND status = 'delivering' AND lease_until = ?
  `).bind(accepted ? "waiting" : finalFailure ? "failed" : "pending", audio ? Date.now() + 2000 : 0, id, lease).run();

  return accepted ? 202 : finalFailure ? 409 : 503;
}

export async function submitReply(env, id, answer) {
  const now = Date.now();
  const updated = await env.DB.prepare(`
    UPDATE turns SET status = 'ready', answer = ?, lease_until = NULL
    WHERE id = ? AND expires > ? AND status IN ('delivering', 'waiting')
      AND (subscription_id = (SELECT id FROM subscription WHERE slot = 1 AND expires > ?)
        OR (subscription_id LIKE 'ui:%' AND audio_phase = 'bridge-turn'))
    RETURNING created
  `).bind(answer, id, now, now).first();
  if (updated) {
    console.info(JSON.stringify({ event: "stopwatch_reply_received", id, elapsed_ms: now - updated.created }));
    return;
  }
  const existing = await env.DB.prepare(`
    SELECT id FROM turns WHERE id = ? AND expires > ? AND status = 'ready' AND answer = ?
      AND (subscription_id = (SELECT id FROM subscription WHERE slot = 1 AND expires > ?)
        OR (subscription_id LIKE 'ui:%' AND audio_phase = 'bridge-turn'))
  `).bind(id, now, answer, now).first();
  if (!existing) throw new RpcError(-32602, "Request expired, unknown, or already answered");
}
