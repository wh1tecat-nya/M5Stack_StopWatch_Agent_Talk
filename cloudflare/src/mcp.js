import { DEVICE, EVENT, eventDefinition, getSubscription, replyTool, RpcError, subscribe, TURN_ID } from "./events.js";
import { InputError, json, readText } from "./http.js";
import { submitReply } from "./turns.js";

export const SCOPE = "mcp:stopwatch";
const securitySchemes = [{ type: "oauth2", scopes: [SCOPE] }];
const serverInfo = { name: "sumi-stopwatch-text", version: "0.1.0" };
const capabilities = { events: {}, tools: {} };
const versions = ["2025-03-26", "2025-06-18", "2025-11-25"];
const statusTool = {
  name: "get_stopwatch_status",
  title: "Check StopWatch connection",
  description: "Check the fixed-text relay connection and whether the StopWatch event has an active subscription.",
  inputSchema: { type: "object", properties: {}, additionalProperties: false },
  outputSchema: {
    type: "object",
    properties: {
      device_id: { type: "string" },
      event: { type: "string" },
      subscription_active: { type: "boolean" },
    },
    required: ["device_id", "event", "subscription_active"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, openWorldHint: false },
};
const tools = [statusTool, replyTool].map((tool) => ({
  ...tool,
  securitySchemes,
  _meta: { securitySchemes },
}));

async function rpc(env, method, params) {
  switch (method) {
    case "server/discover":
      return { resultType: "complete", supportedVersions: ["2026-07-28"], capabilities };
    case "initialize":
      return {
        protocolVersion: versions.includes(params.protocolVersion) ? params.protocolVersion : versions.at(-1),
        serverInfo, capabilities,
      };
    case "ping":
      return {};
    case "events/list":
      return { events: [eventDefinition] };
    case "events/subscribe":
      console.info(JSON.stringify({ event: "stopwatch_subscription_requested" }));
      return subscribe(env, params, false);
    case "events/unsubscribe":
      return subscribe(env, params, true);
    case "tools/list":
      return { tools };
    case "tools/call": {
      if (params.name === statusTool.name) {
        const status = {
          device_id: DEVICE,
          event: EVENT,
          subscription_active: Boolean(await getSubscription(env)),
        };
        console.info(JSON.stringify({ event: "stopwatch_status_checked" }));
        return { content: [{ type: "text", text: JSON.stringify(status) }], structuredContent: status };
      }
      if (params.name !== replyTool.name) throw new RpcError(-32602, "Unknown tool");
      const { request_id: id, text } = params.arguments ?? {};
      const answer = typeof text === "string" ? text.trim() : "";
      if (typeof id !== "string" || !TURN_ID.test(id) || !answer || answer.length > 300) {
        throw new RpcError(-32602, "Invalid reply");
      }
      await submitReply(env, id, answer);
      return { content: [{ type: "text", text: "Reply recorded for the fixed-text probe." }] };
    }
    default:
      throw new RpcError(-32601, "Unknown method");
  }
}

export const mcp = {
  async fetch(request, env, ctx) {
    if (new URL(request.url).pathname !== "/mcp") return new Response(null, { status: 404 });
    if (!ctx.auth?.scope.includes(SCOPE) || ctx.props?.owner !== true) {
      return json({ error: "Insufficient scope" }, 403);
    }
    if (request.method !== "POST") return new Response(null, { status: 405, headers: { Allow: "POST" } });
    if (request.headers.get("Content-Type")?.split(";")[0] !== "application/json") {
      return json({ error: "Expected application/json" }, 415);
    }

    let id = null;
    try {
      const body = JSON.parse(await readText(request, 16_384));
      if (body?.jsonrpc !== "2.0" || typeof body.method !== "string") {
        throw new RpcError(-32600, "Invalid JSON-RPC request");
      }
      if (body.id === undefined && body.method.startsWith("notifications/")) {
        return new Response(null, { status: 202 });
      }
      if (typeof body.id !== "string" && typeof body.id !== "number") {
        throw new RpcError(-32600, "Invalid JSON-RPC id");
      }
      id = body.id;
      const result = await rpc(env, body.method, body.params ?? {});
      return json({ jsonrpc: "2.0", id, result });
    } catch (error) {
      if (error instanceof SyntaxError || error instanceof InputError) {
        return json({ jsonrpc: "2.0", id, error: { code: -32700, message: "Invalid JSON" } });
      }
      if (error instanceof RpcError) {
        return json({ jsonrpc: "2.0", id, error: { code: error.code, message: error.message, data: error.data } });
      }
      console.error(JSON.stringify({ event: "stopwatch_mcp_error", type: error.name }));
      return json({ jsonrpc: "2.0", id, error: { code: -32603, message: "Internal error" } });
    }
  },
};
