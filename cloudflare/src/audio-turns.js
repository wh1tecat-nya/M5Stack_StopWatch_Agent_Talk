import { MAX_RECORDING, transcribe } from "./audio.js";
import { getSubscription } from "./events.js";
import { InputError, json, readBytes } from "./http.js";
import { authorizedDevice, deliverTurn, TURN_TTL } from "./turns.js";
import { activeBridge } from "./bridge.js";

const AUDIO_ATTEMPTS = 3;
const LEASE_MS = 28_000;
const PCM_TYPE = "application/octet-stream";

export async function handleAudio(request, env, ctx, operation, id) {
  if (!await authorizedDevice(request, env)) return json({ error: "Unauthorized" }, 401);
  if (operation === "upload" && request.method === "PUT") return upload(request, env, ctx, id);
  if (operation === "next" && request.method === "GET") return nextReply(env, ctx);
  if (operation === "ack" && request.method === "DELETE") {
    await env.DB.prepare(`
      UPDATE turns SET audio_phase = 'acknowledged', recording = NULL, transcript = NULL, answer = NULL
      WHERE id = ? AND audio_phase IS NOT NULL AND status IN ('ready', 'failed')
    `).bind(id).run();
    return new Response(null, { status: 204 });
  }
  return new Response(null, { status: 405 });
}

async function upload(request, env, ctx, id) {
  if (!env.OPENAI_API_KEY) return json({ error: "Audio service is not configured" }, 424);
  if (request.headers.get("Content-Type") !== PCM_TYPE) {
    return json({ error: "Expected 16 kHz mono PCM" }, 415);
  }
  let bytes;
  try {
    bytes = await readBytes(request, MAX_RECORDING);
    if (bytes.length < 6400 || bytes.length % 2) throw new InputError("Invalid PCM");
  } catch (error) {
    if (!(error instanceof InputError)) throw error;
    return json({ error: "Invalid PCM size" }, 400);
  }

  const now = Date.now();
  await env.DB.prepare("DELETE FROM turns WHERE id = ? AND expires <= ?").bind(id, now).run();
  const existing = await env.DB.prepare("SELECT audio_phase FROM turns WHERE id = ?").bind(id).first();
  if (existing) {
    return existing.audio_phase ? json({ id }, 202) : json({ error: "Request ID already used" }, 409);
  }
  const bridge = await activeBridge(env);
  const subscription = bridge ? { id: "ui:" + bridge.session_id } : await getSubscription(env);
  if (!subscription) return json({ error: "No matching Sumi subscription" }, 409);

  const inserted = await env.DB.prepare(`
    INSERT INTO turns (id, subscription_id, created, expires, status, audio_phase, recording)
    VALUES (?, ?, ?, ?, 'pending', ?, ?) ON CONFLICT(id) DO NOTHING
  `).bind(id, subscription.id, now, now + TURN_TTL, bridge ? "bridge-transcribe" : "transcribe", bytes.buffer).run();
  if (inserted.meta.changes) schedule(env, ctx, id);
  else {
    const turn = await env.DB.prepare("SELECT audio_phase FROM turns WHERE id = ?").bind(id).first();
    if (!turn?.audio_phase) return json({ error: "Request ID already used" }, 409);
  }
  return json({ id }, 202);
}

async function nextReply(env, ctx) {
  const now = Date.now();
  const reply = await env.DB.prepare(`
    SELECT id, status, answer FROM turns WHERE expires > ?
      AND audio_phase IN ('transcribe', 'turn', 'bridge-transcribe', 'bridge-turn') AND status IN ('ready', 'failed')
    ORDER BY created, id LIMIT 1
  `).bind(now).first();
  if (reply) {
    const headers = { "Cache-Control": "no-store", "X-Reply-Id": reply.id };
    if (reply.status === "failed") return new Response(null, { status: 422, headers });
    const text = new TextEncoder().encode(reply.answer);
    headers["Content-Type"] = "text/plain; charset=utf-8";
    headers["Content-Length"] = String(text.length);
    return new Response(text, { headers });
  }
  if (env.OPENAI_API_KEY) {
    const candidate = await env.DB.prepare(`
      SELECT id FROM turns WHERE expires > ? AND audio_lease <= ? AND
        ((audio_phase IN ('transcribe', 'bridge-transcribe') AND status = 'pending') OR
         (audio_phase = 'turn' AND (status = 'pending' OR
          (status = 'delivering' AND lease_until <= ?))))
      ORDER BY created, id LIMIT 1
    `).bind(now, now, now).first();
    if (candidate) schedule(env, ctx, candidate.id);
  }
  return new Response(null, { status: 204, headers: { "Cache-Control": "no-store" } });
}

function schedule(env, ctx, id) {
  ctx.waitUntil(processAudio(env, id).catch((error) => {
    console.error(JSON.stringify({ event: "stopwatch_audio_error", type: error.name }));
  }));
}

async function processAudio(env, id) {
  const now = Date.now();
  const turn = await env.DB.prepare(`
    SELECT status, audio_phase, subscription_id FROM turns
    WHERE id = ? AND expires > ? AND audio_lease <= ?
  `).bind(id, now, now).first();
  if (!turn || turn.status === "failed") return;
  if (turn.audio_phase === "transcribe" || turn.audio_phase === "bridge-transcribe") {
    if (!await transcribeTurn(env, id)) return;
  } else if (turn.audio_phase !== "turn" || !["pending", "delivering"].includes(turn.status)) {
    return;
  }

  // UI turns are claimed by the open panel; never also deliver an MCP event.
  if (turn.subscription_id.startsWith("ui:")) return;

  const subscription = await getSubscription(env);
  if (subscription?.id === turn.subscription_id) {
    await deliverTurn(env, subscription, id, true);
  } else {
    await env.DB.prepare(`
      UPDATE turns SET status = 'failed', transcript = NULL
      WHERE id = ? AND audio_phase = 'turn' AND
        (status = 'pending' OR (status = 'delivering' AND lease_until <= ?))
    `).bind(id, Date.now()).run();
  }
}

async function transcribeTurn(env, id) {
  const now = Date.now();
  const lease = now + LEASE_MS;
  const turn = await env.DB.prepare(`
    UPDATE turns SET audio_lease = ?, audio_attempts = audio_attempts + 1
    WHERE id = ? AND audio_phase IN ('transcribe', 'bridge-transcribe') AND status = 'pending' AND expires > ? AND audio_lease <= ?
      AND audio_attempts < ?
    RETURNING audio_attempts, recording, audio_phase, created
  `).bind(lease, id, now, now, AUDIO_ATTEMPTS).first();
  if (!turn) {
    await env.DB.prepare(`
      UPDATE turns SET status = 'failed', recording = NULL, transcript = NULL, answer = NULL
      WHERE id = ? AND audio_phase IN ('transcribe', 'bridge-transcribe') AND status = 'pending' AND audio_lease <= ? AND audio_attempts >= ?
    `).bind(id, now, AUDIO_ATTEMPTS).run();
    return false;
  }

  try {
    const transcript = await transcribe(env, new Uint8Array(turn.recording));
    if (!transcript) {
      await env.DB.prepare(`
        UPDATE turns SET status = 'failed', recording = NULL WHERE id = ? AND audio_lease = ?
      `).bind(id, lease).run();
      return false;
    }
    const updated = await env.DB.prepare(`
      UPDATE turns SET audio_phase = ?, transcript = ?, recording = NULL, audio_lease = 0, audio_attempts = 0
      WHERE id = ? AND audio_lease = ?
    `).bind(turn.audio_phase === "bridge-transcribe" ? "bridge-turn" : "turn", transcript, id, lease).run();
    console.info(JSON.stringify({ event: "stopwatch_transcribed", id, elapsed_ms: Date.now() - turn.created }));
    return Boolean(updated.meta.changes);
  } catch {
    console.warn(JSON.stringify({ event: "stopwatch_transcription_failed" }));
    if (turn.audio_attempts >= AUDIO_ATTEMPTS) {
      await env.DB.prepare(`
        UPDATE turns SET status = 'failed', recording = NULL, transcript = NULL, answer = NULL
        WHERE id = ? AND audio_lease = ?
      `).bind(id, lease).run();
    } else {
      await env.DB.prepare("UPDATE turns SET audio_lease = ? WHERE id = ? AND audio_lease = ?")
        .bind(Date.now() + turn.audio_attempts * 3000, id, lease).run();
    }
    return false;
  }
}
