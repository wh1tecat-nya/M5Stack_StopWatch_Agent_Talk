import { readText } from "./http.js";

export const MAX_RECORDING = 15 * 16_000 * 2;

function waveHeader(length) {
  const header = new Uint8Array(44);
  const view = new DataView(header.buffer);
  const label = (offset, text) => header.set(new TextEncoder().encode(text), offset);
  label(0, "RIFF");
  view.setUint32(4, length + 36, true);
  label(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 16_000, true);
  view.setUint32(28, 32_000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  label(36, "data");
  view.setUint32(40, length, true);
  return header;
}

export async function transcribe(env, pcm) {
  const form = new FormData();
  form.set("model", "gpt-transcribe");
  form.set("file", new Blob([waveHeader(pcm.length), pcm], { type: "audio/wav" }), "turn.wav");
  const response = await fetch("https://api.openai.com/v1/audio/transcriptions", {
    method: "POST",
    headers: { Authorization: "Bearer " + env.OPENAI_API_KEY },
    body: form,
    redirect: "manual",
    signal: AbortSignal.timeout(18_000),
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error(`Transcription API returned HTTP ${response.status}`);
  }
  const result = JSON.parse(await readText(response, 32_768));
  return typeof result.text === "string" ? result.text.trim().slice(0, 2000) : "";
}
