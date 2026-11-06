import http.client
import io
import json
import uuid
import wave

MAX_INPUT = 15 * 16000 * 2
MAX_OUTPUT = 30 * 24000 * 2


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class Audio:
    def __init__(self, key, voice):
        self.key = key
        self.voice = voice

    def request(self, path, body, content_type, limit):
        connection = http.client.HTTPSConnection("api.openai.com", timeout=25)
        try:
            connection.request("POST", "/v1/audio/" + path, body, {
                "Authorization": "Bearer " + self.key,
                "Content-Type": content_type,
            })
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"Audio API returned HTTP {response.status}")
            data = response.read(limit + 1)
            if not data or len(data) > limit:
                raise RuntimeError("Unexpected Audio API response size")
            return data
        finally:
            connection.close()

    def transcribe(self, pcm):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(pcm)
        boundary = "sumi-" + uuid.uuid4().hex
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\ngpt-transcribe\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="turn.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode()
        body = head + buffer.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        result = self.request("transcriptions", body, "multipart/form-data; boundary=" + boundary, 32768)
        transcript = json.loads(result)["text"].strip()
        if not transcript or len(transcript) > 2000:
            raise RuntimeError("Transcript is empty or too long")
        return transcript

    def speak(self, text):
        return self.request("speech", encode_json({
            "model": "gpt-4o-mini-tts", "voice": self.voice, "input": text, "response_format": "pcm",
        }), "application/json", MAX_OUTPUT)
