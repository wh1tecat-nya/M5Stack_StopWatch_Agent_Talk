#include <M5Unified.h>
#include <atomic>
#include <cmath>

#include "VoiceLink.h"
#include "sumi_frames.h"

namespace {

enum class Mode { Badge, Conversation, Recording, Sending, Speaking };

constexpr int kFrameSize = 400;
constexpr size_t kCaptureChunk = VoiceLink::kRecordRate / 50;
constexpr uint32_t kIdleMs = 15000;
constexpr uint32_t kPollMs = 3000;
constexpr int kVoiceThreshold = 450;
constexpr uint8_t kVolume = 64;
#ifdef SUMI_AUDIO_BENCH
constexpr bool kAudioBench = true;
#else
constexpr bool kAudioBench = false;
#endif
constexpr size_t kSpeechRate = kAudioBench ? VoiceLink::kRecordRate : VoiceLink::kPlaybackRate;
constexpr const char* kRecordPrompt = kAudioBench ? "Audio test - A: Record" : "A: Record";
constexpr uint32_t kBenchReplyDelayMs = kIdleMs + 1000;

M5Canvas frame(&M5.Display);
Mode mode = Mode::Badge;
bool ready = false;
bool wifiConfigured = false;
bool networkBusy = false;
bool uploadPending = false;
bool replyPending = false;
bool ackPending = false;
uint8_t uploadAttempts = 0;
uint32_t nextNetworkMs = 0;
uint32_t modeStarted = 0;
uint32_t lastFrame = 0;
uint32_t benchQueuedAt = 0;
bool benchReplyPending = false;
int16_t* recording = nullptr;
int16_t* playback = nullptr;
size_t queued = 0;
size_t inspected = 0;
size_t lastVoiceSample = 0;
std::atomic<size_t> captured{0};
VoiceLink::Request upload{};
VoiceLink::Result reply{};
VoiceLink::Request ack{};

bool drawPose(bool eyesClosed) {
  frame.fillScreen(TFT_WHITE);
  return eyesClosed ? frame.drawPng(kSumiClosedEyes, sizeof(kSumiClosedEyes))
                    : frame.drawPng(kSumiNormal, sizeof(kSumiNormal));
}

void enter(Mode next, const char* status = "") {
  mode = next;
  modeStarted = millis();
  M5.Display.setBrightness(mode == Mode::Badge ? 20 : 64);
  M5.Display.fillScreen(TFT_WHITE);
  if (mode == Mode::Badge) {
    drawPose(false);
    frame.pushSprite((M5.Display.width() - kFrameSize) / 2,
                     (M5.Display.height() - kFrameSize) / 2);
  } else {
    M5.Display.setTextDatum(middle_center);
    M5.Display.setTextColor(TFT_DARKGREY, TFT_WHITE);
    M5.Display.setTextSize(2);
    M5.Display.drawString(status, M5.Display.width() / 2, M5.Display.height() - 43);
  }
}

int signalLevel(const int16_t* samples, size_t count) {
  int32_t sum = 0;
  int64_t squares = 0;
  for (size_t index = 0; index < count; ++index) {
    const int32_t value = samples[index];
    sum += value;
    squares += value * value;
  }
  const double mean = double(sum) / count;
  return std::sqrt(std::max(0.0, double(squares) / count - mean * mean));
}

void captureReleased(void*, void*, size_t samples) {
  captured.fetch_add(samples, std::memory_order_release);
}

void inspectRecording(size_t done) {
  while (inspected < done) {
    if (signalLevel(recording + inspected, kCaptureChunk) >= kVoiceThreshold) {
      lastVoiceSample = inspected + kCaptureChunk;
    }
    inspected += kCaptureChunk;
  }
}

void playSpeech(const int16_t* audio, size_t samples, const char* status) {
  enter(Mode::Speaking, status);
  M5.Speaker.setVolume(kVolume);
  if (!M5.Speaker.playRaw(audio, samples, kSpeechRate)) {
    M5.Speaker.end();
    enter(Mode::Conversation, "Speaker error");
  }
}

void finishRecording(bool failed = false) {
  M5.Mic.end();
  const size_t samples = captured.load(std::memory_order_acquire);
  inspectRecording(samples);
  if (failed || samples < VoiceLink::kRecordRate / 5 || !lastVoiceSample) {
    enter(Mode::Conversation, failed ? "Mic error" : "No speech");
    return;
  }
  if (kAudioBench) {
    reply.samples = samples;
    benchQueuedAt = millis();
    benchReplyPending = true;
    enter(Mode::Sending, "Replay in 16s");
    return;
  }
  upload = {VoiceLink::Job::Upload, samples, ""};
  VoiceLink::newId(upload.id);
  uploadPending = true;
  uploadAttempts = 0;
  enter(Mode::Sending, "Sending...");
}

void pumpRecording() {
  const size_t done = captured.load(std::memory_order_acquire);
  inspectRecording(done);
  const bool quiet = lastVoiceSample
      ? done - lastVoiceSample >= VoiceLink::kRecordRate * 9 / 10
      : done >= VoiceLink::kRecordRate * 4;
  if (quiet || done >= VoiceLink::kRecordSamples || millis() - modeStarted >= 17000) {
    finishRecording();
    return;
  }

  while (queued < VoiceLink::kRecordSamples && queued - done < 2 * kCaptureChunk) {
    if (!M5.Mic.record(recording + queued, kCaptureChunk, VoiceLink::kRecordRate)) {
      finishRecording(true);
      return;
    }
    queued += kCaptureChunk;
  }
}

void startRecording() {
  if (kAudioBench && benchReplyPending) {
    enter(Mode::Conversation, "Replay pending");
    return;
  }
  if (!recording) {
    enter(Mode::Conversation, "Audio memory error");
    return;
  }
  if (!kAudioBench && !VoiceLink::connected()) {
    enter(Mode::Conversation, wifiConfigured ? "Wi-Fi connecting" : "Wi-Fi setup needed");
    return;
  }
  if (uploadPending) {
    enter(Mode::Conversation, "Upload pending");
    return;
  }
  M5.Speaker.end();
  captured = 0;
  queued = inspected = lastVoiceSample = 0;
  M5.Mic.setBufferReleaseCallback(nullptr, captureReleased);
  enter(Mode::Recording, "Recording");
  pumpRecording();
}

void playReply() {
  replyPending = false;
  playSpeech(playback, reply.samples, "AI voice");
}

void acknowledgeReply(const VoiceLink::Request& request) {
  ack = request;
  ack.job = VoiceLink::Job::Acknowledge;
  ackPending = true;
  nextNetworkMs = millis();
}

void renderAvatar(uint32_t now) {
  if (mode == Mode::Badge || now - lastFrame < 80) {
    return;
  }
  const uint32_t elapsed = now - modeStarted;
  const bool eyesClosed = elapsed % 4000 >= 3850;
  const int y = (M5.Display.height() - kFrameSize) / 2
      + static_cast<int>(std::lround(4 * std::sin(6.2831853f * (elapsed % 2800) / 2800.0f)));
  const int x = (M5.Display.width() - kFrameSize) / 2;
  static bool previousEyes = false;
  static Mode previousMode = Mode::Badge;
  static uint32_t previousStart = 0;
  static int previousMouth = 0;
  static int previousY = 0;
  int mouth = 0;
  if (mode == Mode::Speaking) {
    const int16_t* audio = kAudioBench ? recording : playback;
    const size_t index = size_t(elapsed) * kSpeechRate / 1000;
    if (index < reply.samples) {
      const size_t count = std::min(reply.samples - index, kSpeechRate / 50);
      mouth = std::min(10, signalLevel(audio + index, count) / 350);
    }
  }
  const bool newMode = mode != previousMode || modeStarted != previousStart;
  if (!newMode && y == previousY && eyesClosed == previousEyes && mouth == previousMouth) {
    return;
  }
  if (newMode || eyesClosed != previousEyes || mouth != previousMouth) {
    drawPose(eyesClosed);
    if (mouth) {
      frame.fillRect(185, 213, 30, 15, TFT_WHITE);
      frame.fillEllipse(200, 219, 9, mouth, TFT_DARKGREY);
    }
  }
  M5.Display.startWrite();
  if (!newMode && y > previousY) {
    M5.Display.fillRect(x, previousY, kFrameSize, y - previousY, TFT_WHITE);
  } else if (!newMode && y < previousY) {
    M5.Display.fillRect(x, y + kFrameSize, kFrameSize, previousY - y, TFT_WHITE);
  }
  frame.pushSprite(x, y);
  M5.Display.endWrite();
  previousY = y;
  previousEyes = eyesClosed;
  previousMode = mode;
  previousMouth = mouth;
  previousStart = modeStarted;
  lastFrame = now;
}

void updateNetwork(uint32_t now) {
  VoiceLink::Result result{};
  if (VoiceLink::receive(result)) {
    networkBusy = false;
    switch (result.request.job) {
      case VoiceLink::Job::Upload:
        if (result.status == 202) {
          uploadPending = false;
          if (mode != Mode::Badge) {
            enter(Mode::Conversation, "Waiting for Sumi");
          }
        } else if ((result.status < 0 || result.status >= 500) && ++uploadAttempts < 3) {
          nextNetworkMs = now + 1000;
        } else {
          uploadPending = false;
          enter(Mode::Conversation, result.status == 409 ? "Sumi link unavailable" : "Send failed");
        }
        break;
      case VoiceLink::Job::Poll:
        if (result.status == 200) {
          reply = result;
          replyPending = true;
        } else if (result.status == 422) {
          acknowledgeReply(result.request);
          if (mode != Mode::Recording && mode != Mode::Sending) {
            enter(Mode::Conversation, "Sumi reply failed");
          }
        }
        break;
      case VoiceLink::Job::Acknowledge:
        ackPending = result.status != 204;
        break;
    }
  }
  if (mode == Mode::Recording || mode == Mode::Speaking) {
    return;
  }
  if (replyPending && !uploadPending) {
    playReply();
    return;
  }
  if (!VoiceLink::connected() || networkBusy || int32_t(now - nextNetworkMs) < 0) {
    return;
  }
  const VoiceLink::Request poll{VoiceLink::Job::Poll, 0, ""};
  networkBusy = VoiceLink::send(uploadPending ? upload : ackPending ? ack : poll);
  nextNetworkMs = now + (uploadPending ? 0 : kPollMs);
}

}  // namespace

void setup() {
  auto config = M5.config();
  M5.begin(config);
  if (M5.getBoard() != m5::board_t::board_M5StopWatch) {
    return;
  }

  M5.Mic.end();
  M5.Speaker.end();
  frame.setColorDepth(16);
  frame.setPsram(true);
  if (!frame.createSprite(kFrameSize, kFrameSize) || !drawPose(false)) {
    return;
  }

  recording = static_cast<int16_t*>(ps_malloc(VoiceLink::kRecordSamples * sizeof(int16_t)));
  if (!kAudioBench) {
    playback = static_cast<int16_t*>(ps_malloc(VoiceLink::kPlaybackSamples * sizeof(int16_t)));
    if (recording && playback) {
      wifiConfigured = VoiceLink::begin(recording, playback);
    }
  }
  enter(Mode::Badge);
  ready = true;
}

void loop() {
  delay(mode == Mode::Badge ? 20 : 5);
  if (!ready) {
    return;
  }

  M5.update();
  const uint32_t now = millis();
  const bool left = M5.BtnA.wasPressed();
  const bool right = M5.BtnB.wasPressed();
  switch (mode) {
    case Mode::Badge:
      if (left || right) {
        enter(Mode::Conversation, kRecordPrompt);
      }
      break;
    case Mode::Conversation:
      if (left) {
        startRecording();
      } else if (now - modeStarted >= kIdleMs) {
        enter(Mode::Badge);
      }
      break;
    case Mode::Recording:
      if (left) {
        finishRecording();
      } else {
        pumpRecording();
      }
      break;
    case Mode::Sending:
      if (now - modeStarted >= kIdleMs) {
        enter(Mode::Badge);
      }
      break;
    case Mode::Speaking:
      if (!M5.Speaker.isPlaying()) {
        M5.Speaker.end();
        if (!kAudioBench) {
          acknowledgeReply(reply.request);
        }
        enter(Mode::Conversation, kRecordPrompt);
      }
      break;
  }
  if (kAudioBench) {
    if (benchReplyPending && now - benchQueuedAt >= kBenchReplyDelayMs) {
      benchReplyPending = false;
      playSpeech(recording, reply.samples, "Local playback");
    }
  } else {
    updateNetwork(now);
  }
  renderAvatar(millis());
}
