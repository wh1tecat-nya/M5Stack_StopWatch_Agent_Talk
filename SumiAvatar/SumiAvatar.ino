#include <M5Unified.h>
#include <atomic>
#include <cmath>
#include <utility/M5IOE1_Class.hpp>

#include "VoiceLink.h"
#include "sumi_frames.h"

namespace {

enum class Mode { Badge, Conversation, Recording, Sending, Waiting, Speaking };

constexpr int kFrameSize = 400;
constexpr int kPortraitSize = 360;
constexpr int kPortraitInset = (kFrameSize - kPortraitSize) / 2;
constexpr size_t kCaptureChunk = VoiceLink::kRecordRate / 50;
constexpr uint32_t kIdleMs = 15000;
constexpr uint32_t kBadgeDimMs = 30000;
constexpr uint8_t kBright = 64;
constexpr uint8_t kDim = 20;
constexpr uint32_t kPollMs = 3000;
constexpr uint32_t kActivePollMs = 750;
constexpr uint32_t kFastWaitMs = 120000;
constexpr uint32_t kReplyTimeoutMs = 600000;  // Worker turn lifetime.
constexpr uint32_t kAudioIdleMs = 750;
constexpr uint8_t kVolume = 190;
#ifdef SUMI_AUDIO_BENCH
constexpr bool kAudioBench = true;
#else
constexpr bool kAudioBench = false;
#endif
constexpr const char* kRecordPrompt = "Hold A to talk";
constexpr uint32_t kBenchReplyDelayMs = kIdleMs + 1000;

M5Canvas frame(&M5.Display);
M5Canvas portrait(&M5.Display);
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
bool badgeDimmed = false;
bool captureStopped = false;
bool audioRailOn = false;
uint32_t audioIdleSince = 0;
uint32_t lastSpeakerActiveMs = 0;
uint32_t speakerDrainMs = 0;
const char* modeStatus = "";
int16_t* recording = nullptr;
int16_t* playback = nullptr;
size_t queued = 0;
std::atomic<size_t> captured{0};
VoiceLink::Request upload{};
VoiceLink::Result reply{};
VoiceLink::Request ack{};

void drawPose(bool eyesClosed, int mouth, int offsetX) {
  frame.fillScreen(TFT_WHITE);
  portrait.pushSprite(&frame, kPortraitInset + offsetX, kPortraitInset);
  if (eyesClosed) {
    const auto skin = frame.color565(250, 224, 216);
    const auto lashes = frame.color565(94, 63, 64);
    frame.fillEllipse(183 + offsetX, 169, 16, 12, skin);
    frame.fillEllipse(235 + offsetX, 171, 16, 12, skin);
    frame.fillEllipse(183 + offsetX, 169, 12, 1, lashes);
    frame.fillEllipse(235 + offsetX, 171, 12, 1, lashes);
  }
  if (mouth > 0) {
    frame.fillEllipse(207 + offsetX, 200, 10, 7, frame.color565(253, 237, 230));
    frame.fillEllipse(207 + offsetX, 200, 7, mouth, frame.color565(138, 76, 79));
  }
}

void enter(Mode next, const char* status = "") {
  mode = next;
  modeStatus = status;
  modeStarted = millis();
  M5.Display.setBrightness(kBright);
  M5.Display.fillScreen(TFT_WHITE);
  if (mode == Mode::Badge) {
    badgeDimmed = false;
    frame.fillScreen(TFT_WHITE);
    frame.drawPng(kSumiBadge, sizeof(kSumiBadge));
    frame.pushSprite((M5.Display.width() - kFrameSize) / 2,
                     (M5.Display.height() - kFrameSize) / 2);
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

void audioStopped() {
  audioIdleSince = millis();
}

void muteIdleAudio(uint32_t now) {
  if (audioRailOn && (mode != Mode::Recording || captureStopped) && mode != Mode::Speaking &&
      now - audioIdleSince >= kAudioIdleMs) {
    // C152 codec and mic supply. M5Unified enables it on the next audio start.
    audioRailOn = !M5.getIOExpander(0).digitalWrite(m5::M5IOE1_Class::gpio3, false);
    audioIdleSince = now;
  }
}

void playSpeech(const int16_t* audio, size_t samples, const char* status) {
  enter(Mode::Speaking, status);
  audioRailOn = true;
  M5.Speaker.setVolume(kVolume);
  if (!M5.Speaker.playRaw(audio, samples, reply.sampleRate)) {
    M5.Speaker.end();
    audioStopped();
    enter(Mode::Conversation, "Speaker error");
  } else {
    const auto config = M5.Speaker.config();
    // The software queue can empty while I2S still has samples to play.
    speakerDrainMs = 1 + 1000 * config.dma_buf_len * config.dma_buf_count / config.sample_rate;
    lastSpeakerActiveMs = millis();
  }
}

void finishRecording(bool failed = false) {
  if (!captureStopped) {
    M5.Mic.end();
    audioStopped();
  }
  const size_t samples = captured.load(std::memory_order_acquire);
  if (failed || samples < VoiceLink::kRecordRate / 5) {
    enter(Mode::Conversation, failed ? "Mic error" : kRecordPrompt);
    return;
  }
  if (kAudioBench) {
    reply.samples = samples;
    reply.sampleRate = VoiceLink::kRecordRate;
    benchQueuedAt = millis();
    benchReplyPending = true;
    enter(Mode::Waiting, "Replay in 16s");
    return;
  }
  upload = {VoiceLink::Job::Upload, samples, ""};
  VoiceLink::newId(upload.id);
  uploadPending = true;
  uploadAttempts = 0;
  nextNetworkMs = millis();
  enter(Mode::Sending, "Sending...");
}

void pumpRecording() {
  if (captureStopped) return;
  const size_t done = captured.load(std::memory_order_acquire);
  if (done >= VoiceLink::kRecordSamples || millis() - modeStarted >= 17000) {
    M5.Mic.end();
    audioStopped();
    captureStopped = true;
    enter(Mode::Recording, "Limit - release to send");
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
  audioRailOn = true;
  captureStopped = false;
  captured = 0;
  queued = 0;
  M5.Mic.setBufferReleaseCallback(nullptr, captureReleased);
  enter(Mode::Recording, "REC - release to send");
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
  const double sway = std::sin(6.2831853 * (elapsed % 2800) / 2800.0);
  const bool thinking = mode == Mode::Waiting;
  const int offsetX = thinking ? std::lround(14 * sway) : 0;
  const int y = (M5.Display.height() - kFrameSize) / 2 + (thinking ? 0 : std::lround(4 * sway));
  const int x = (M5.Display.width() - kFrameSize) / 2;
  static bool previousEyes = false;
  static Mode previousMode = Mode::Badge;
  static uint32_t previousStart = 0;
  static int previousMouth = 0;
  static int previousLevel = 0;
  static int previousY = 0;
  static int previousOffsetX = 0;
  int mouth = 0;
  int level = 0;
  if (mode == Mode::Recording && !captureStopped) {
    const size_t done = captured.load(std::memory_order_acquire);
    if (done >= kCaptureChunk) {
      level = std::min(5, signalLevel(recording + done - kCaptureChunk, kCaptureChunk) / 400);
    }
  }
  if (mode == Mode::Speaking) {
    const int16_t* audio = kAudioBench ? recording : playback;
    const size_t index = size_t(elapsed) * reply.sampleRate / 1000;
    if (index < reply.samples) {
      const size_t count = std::min(reply.samples - index, size_t(reply.sampleRate / 50));
      mouth = std::min(6, signalLevel(audio + index, count) / 350);
    }
  }
  const bool newMode = mode != previousMode || modeStarted != previousStart;
  const bool redraw = newMode || offsetX != previousOffsetX || eyesClosed != previousEyes ||
      mouth != previousMouth || level != previousLevel;
  if (!redraw && y == previousY) {
    return;
  }
  if (redraw) {
    drawPose(eyesClosed, mouth, offsetX);
    frame.fillRoundRect(72, 347, 256, 40, 12, mode == Mode::Recording ? TFT_MAROON : TFT_WHITE);
    frame.setTextDatum(middle_center);
    frame.setTextSize(2);
    frame.setTextColor(mode == Mode::Recording ? TFT_WHITE : TFT_DARKGREY);
    frame.drawString(modeStatus, kFrameSize / 2, mode == Mode::Recording ? 359 : 367);
    if (mode == Mode::Recording && !captureStopped) {
      for (int index = 0; index < 5; ++index) {
        frame.fillRect(151 + index * 20, 372, 17, 5, index < level ? TFT_GREEN : TFT_LIGHTGREY);
      }
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
  previousOffsetX = offsetX;
  previousEyes = eyesClosed;
  previousMode = mode;
  previousMouth = mouth;
  previousLevel = level;
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
          nextNetworkMs = now;
          enter(Mode::Waiting, "Waiting for Sumi...");
        } else if ((result.status < 0 || result.status >= 500) && ++uploadAttempts < 3) {
          nextNetworkMs = now + 1000;
        } else {
          uploadPending = false;
          const char* error = result.status == 409 ? "Sumi link unavailable"
              : result.status == 424 ? "Audio service not set" : "Send failed";
          enter(Mode::Conversation, error);
        }
        break;
      case VoiceLink::Job::Poll:
        if (result.status == 200) {
          reply = result;
          replyPending = true;
        } else if (result.status == 422 || result.status == 415) {
          acknowledgeReply(result.request);
          if (mode != Mode::Recording && mode != Mode::Sending) {
            enter(Mode::Conversation, result.status == 415 ? "Local speech failed" : "Sumi reply failed");
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
  const bool fastPoll = mode == Mode::Waiting && millis() - modeStarted < kFastWaitMs;
  nextNetworkMs = now + (uploadPending ? 0 : fastPoll ? kActivePollMs : kPollMs);
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
  audioRailOn = true;
  audioStopped();
  frame.setColorDepth(16);
  frame.setPsram(true);
  portrait.setColorDepth(16);
  portrait.setPsram(true);
  if (!frame.createSprite(kFrameSize, kFrameSize) ||
      !portrait.createSprite(kPortraitSize, kPortraitSize) ||
      !portrait.drawPng(kSumiAvatar, sizeof(kSumiAvatar))) {
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
      if (left) {
        startRecording();
      } else if (right) {
        enter(Mode::Conversation, kRecordPrompt);
      } else if (M5.Touch.getCount() && M5.Touch.getDetail().isPressed()) {
        modeStarted = now;
        if (badgeDimmed) {
          M5.Display.setBrightness(kBright);
          badgeDimmed = false;
        }
      } else if (!badgeDimmed && now - modeStarted >= kBadgeDimMs) {
        M5.Display.setBrightness(kDim);
        badgeDimmed = true;
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
      if (!M5.BtnA.isPressed()) {
        finishRecording();
      } else {
        pumpRecording();
      }
      break;
    case Mode::Sending:
      break;
    case Mode::Waiting:
      if (!networkBusy && now - modeStarted >= kReplyTimeoutMs) {
        enter(Mode::Conversation, "Sumi reply timed out");
      }
      break;
    case Mode::Speaking:
      if (M5.Speaker.isPlaying()) {
        lastSpeakerActiveMs = now;
      } else if (now - lastSpeakerActiveMs >= speakerDrainMs) {
        M5.Speaker.end();
        audioStopped();
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
  muteIdleAudio(millis());
  renderAvatar(millis());
}
