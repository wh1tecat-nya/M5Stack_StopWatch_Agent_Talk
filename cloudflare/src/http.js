const encoder = new TextEncoder();

export class InputError extends Error {}

export function json(value, status = 200) {
  return Response.json(value, { status, headers: { "Cache-Control": "no-store" } });
}

export async function readText(message, limit) {
  if (!message.body) return "";
  const reader = message.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let size = 0;
  let result = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return result + decoder.decode();
      size += value.byteLength;
      if (size > limit) throw new InputError("Body too large");
      result += decoder.decode(value, { stream: true });
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    if (error instanceof TypeError) throw new InputError("Invalid body", { cause: error });
    throw error;
  } finally {
    reader.releaseLock();
  }
}

export async function readBytes(message, limit) {
  if (!message.body) return new Uint8Array();
  const reader = message.body.getReader();
  const chunks = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > limit) throw new InputError("Body too large");
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

export async function equalSecret(left, right) {
  const [leftDigest, rightDigest] = await Promise.all(
    [left, right].map((value) => crypto.subtle.digest("SHA-256", encoder.encode(value))),
  );
  return crypto.subtle.timingSafeEqual(leftDigest, rightDigest);
}

export async function sha256(value) {
  const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value)));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
