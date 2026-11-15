import { RpcError, TURN_ID } from "./events.js";
import { bridgeHtml, BRIDGE_URI } from "./bridge-ui.js";

const LEASE_MS = 45_000;
export { BRIDGE_URI };
export const bridgeTools = [
  {
    name: "open_stopwatch_bridge",
    title: "StopWatchの会話中継を開く",
    description: "Open this panel in the existing Sumi dot conversation. After the owner starts it, forward StopWatch transcripts into this same conversation without MCP events. Reply to each forwarded request with submit_stopwatch_reply.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    outputSchema: {
      type: "object", properties: {
        bridge_active: { type: "boolean" }, start_requires_visible_page: { type: "boolean" }, fallback_url: { type: "string" },
      }, required: ["bridge_active", "start_requires_visible_page", "fallback_url"], additionalProperties: false,
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
    _meta: {
      ui: { resourceUri: BRIDGE_URI }, "openai/outputTemplate": BRIDGE_URI,
      "openai/ui": { entrypoints: [{ type: "thread" }] },
    },
  },
  {
    name: "control_stopwatch_bridge",
    description: "Control the owner's open StopWatch bridge panel and atomically claim one transcript. Only available to the app UI.",
    inputSchema: {
      type: "object",
      properties: {
        session_id: { type: "string", pattern: TURN_ID.source },
        action: { type: "string", enum: ["start", "poll", "stop", "sent", "reject"] },
        request_id: { type: "string", pattern: TURN_ID.source },
      },
      required: ["session_id", "action"], additionalProperties: false,
    },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false },
    _meta: { ui: { visibility: ["app"] }, "openai/visibility": "private", "openai/widgetAccessible": true },
  },
];

export function bridgeResource() {
  return {
    uri: BRIDGE_URI, mimeType: "text/html;profile=mcp-app", text: bridgeHtml,
    _meta: {
      ui: { csp: { connectDomains: [], resourceDomains: [] } },
      "openai/widgetDescription": "すみの既存の会話で開始するStopWatch中継。音声入出力はStopWatch本体。",
    },
  };
}

export async function activeBridge(env) {
  return env.DB.prepare("SELECT session_id FROM ui_bridge WHERE slot = 1 AND expires > ?")
    .bind(Date.now()).first();
}

export async function controlBridge(env, args) {
  const { session_id: session, action, request_id: id } = args ?? {};
  if (typeof session !== "string" || !TURN_ID.test(session) ||
      !["start", "poll", "stop", "sent", "reject"].includes(action)) {
    throw new RpcError(-32602, "Invalid bridge request");
  }
  const now = Date.now();
  const subscription = "ui:" + session;
  if (action === "start") {
    const started = await env.DB.prepare(`
      INSERT INTO ui_bridge (slot, session_id, expires) VALUES (1, ?, ?)
      ON CONFLICT(slot) DO UPDATE SET session_id = excluded.session_id, expires = excluded.expires
      WHERE ui_bridge.expires <= ? OR ui_bridge.session_id = excluded.session_id
      RETURNING session_id
    `).bind(session, now + LEASE_MS, now).first();
    if (!started) throw new RpcError(-32602, "別の中継画面が使用中です。閉じて45秒待ってください。");
    return { active: true, request: null };
  }
  if (action === "stop") {
    await env.DB.prepare("UPDATE ui_bridge SET expires = 0 WHERE slot = 1 AND session_id = ?")
      .bind(session).run();
    return { active: false, request: null };
  }
  if (action === "sent" || action === "reject") {
    if (typeof id !== "string" || !TURN_ID.test(id)) throw new RpcError(-32602, "Invalid request ID");
    const turn = await env.DB.prepare(`
      SELECT created FROM turns WHERE id = ? AND subscription_id = ?
        AND audio_phase IN ('bridge-turn', 'acknowledged') AND expires > ? AND status IN ('waiting', 'ready')
    `).bind(id, subscription, now).first();
    if (!turn) throw new RpcError(-32602, "Unknown bridge turn");
    if (action === "reject") {
      await env.DB.prepare("UPDATE turns SET status = 'failed' WHERE id = ? AND subscription_id = ? AND status = 'waiting'")
        .bind(id, subscription).run();
    }
    console.info(JSON.stringify({ event: "stopwatch_bridge_" + action, id, elapsed_ms: now - turn.created }));
    return { active: true, request: null };
  }
  const renewed = await env.DB.prepare(`
    UPDATE ui_bridge SET expires = ? WHERE slot = 1 AND session_id = ? AND expires > ?
    RETURNING session_id
  `).bind(now + LEASE_MS, session, now).first();
  if (!renewed) throw new RpcError(-32602, "中継が停止しました。開始し直してください。");
  const turn = await env.DB.prepare(`
    UPDATE turns SET status = 'waiting' WHERE id = (
      SELECT id FROM turns WHERE subscription_id = ? AND audio_phase = 'bridge-turn'
        AND status = 'pending' AND expires > ?
        AND NOT EXISTS (SELECT 1 FROM turns WHERE subscription_id = ? AND audio_phase = 'bridge-turn'
          AND status = 'waiting' AND expires > ?)
      ORDER BY created, id LIMIT 1
    ) AND status = 'pending' RETURNING id, transcript, created
  `).bind(subscription, now, subscription, now).first();
  if (!turn) return { active: true, request: null };
  console.info(JSON.stringify({ event: "stopwatch_bridge_claimed", id: turn.id, elapsed_ms: now - turn.created }));
  return { active: true, request: { request_id: turn.id, text: turn.transcript, elapsed_ms: now - turn.created } };
}
