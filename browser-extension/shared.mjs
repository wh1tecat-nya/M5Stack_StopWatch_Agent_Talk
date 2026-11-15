export const RELAY_ORIGIN = "https://sumi-stopwatch-text.wh1tecat-sec.workers.dev";
export const REQUEST_ID = /^[a-f0-9]{32}$/;

export function dotPath(value) {
  try {
    const url = new URL(value);
    return url.origin === "https://chatgpt.com" && /^\/dots\/[a-zA-Z0-9_.~-]+\/?$/.test(url.pathname)
      ? url.pathname.replace(/\/$/, "") : null;
  } catch { return null; }
}
export function allowedSender(sender, brokerTab) {
  try {
    const url = new URL(sender.url);
    return !sender.id && sender.tab?.id === brokerTab && url.origin === RELAY_ORIGIN && url.pathname === "/bridge";
  } catch { return false; }
}
export function validTurn(turn) {
  return turn && typeof turn.request_id === "string" && REQUEST_ID.test(turn.request_id) &&
    typeof turn.text === "string" && turn.text.trim().length > 0 && turn.text.length <= 4000;
}
export function promptFor(turn) {
  if (!validTurn(turn)) throw new Error("Invalid turn");
  return "StopWatchからの音声メッセージです。\n" + turn.text +
    "\n\nすみとして自然に短く返答し、同じ返答本文をSumi StopWatchのsubmit_stopwatch_replyへ渡してください。" +
    "request_id: " + turn.request_id + "。日本語1〜2文、80文字程度。使うデータはこの発話とrequest_idだけです。";
}
