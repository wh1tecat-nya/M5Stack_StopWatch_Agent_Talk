import { deliver, getSubscription, RpcError } from "./events.js";
import { equalSecret, InputError, json, readText } from "./http.js";

const TURN_TTL = 600_000;
const MAX_ATTEMPTS = 3;

export async function handleTurn(request, env, id) {
  const token = env.DEVICE_TOKEN;
  const auth = request.headers.get("Authorization") ?? "";
  if (typeof token !== "string" || token.length < 32 || auth.length > 256 ||
      !await equalSecret(auth, "Bearer " + token)) {
    return json({ error: "Unauthorized" }, 401);
  }

  const now = Date.now();
  if (request.method === "GET") {
    const turn = await env.DB.prepare("SELECT status, answer FROM turns WHERE id = ? AND expires > ?")
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
  const prior = await env.DB.prepare("SELECT * FROM turns WHERE id = ?").bind(id).first();
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

  const lease = Date.now() + 15_000;
  const turn = await env.DB.prepare(`
    UPDATE turns SET status = 'delivering', lease_until = ?, attempts = attempts + 1
    WHERE id = ? AND subscription_id = ? AND attempts < ? AND expires > ? AND
      (status = 'pending' OR (status = 'delivering' AND lease_until <= ?)) AND
      subscription_id = (SELECT id FROM subscription WHERE slot = 1 AND expires > ?)
    RETURNING id, created, attempts
  `).bind(lease, id, subscription.id, MAX_ATTEMPTS, now, now, now).first();
  if (!turn) {
    const exhausted = await env.DB.prepare(`
      UPDATE turns SET status = 'failed', lease_until = NULL WHERE id = ?
      AND status = 'delivering' AND lease_until <= ? AND attempts >= ?
    `).bind(id, Date.now(), MAX_ATTEMPTS).run();
    if (exhausted.meta.changes) return json({ error: "Delivery attempts exhausted" }, 409);
    return json({ id }, 202);
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
    UPDATE turns SET status = ?, lease_until = NULL
    WHERE id = ? AND status = 'delivering' AND lease_until = ?
  `).bind(accepted ? "waiting" : finalFailure ? "failed" : "pending", id, lease).run();

  if (accepted) return json({ id }, 202);
  return json({ error: finalFailure ? "Delivery failed" : "Retry the same request ID" }, finalFailure ? 409 : 503);
}

export async function submitReply(env, id, answer) {
  const now = Date.now();
  const updated = await env.DB.prepare(`
    UPDATE turns SET status = 'ready', answer = ?, lease_until = NULL
    WHERE id = ? AND expires > ? AND status IN ('delivering', 'waiting')
      AND subscription_id = (SELECT id FROM subscription WHERE slot = 1 AND expires > ?)
  `).bind(answer, id, now, now).run();
  if (updated.meta.changes) return;
  const existing = await env.DB.prepare(`
    SELECT id FROM turns WHERE id = ? AND expires > ? AND status = 'ready' AND answer = ?
      AND subscription_id = (SELECT id FROM subscription WHERE slot = 1 AND expires > ?)
  `).bind(id, now, answer, now).first();
  if (!existing) throw new RpcError(-32602, "Request expired, unknown, or already answered");
}
