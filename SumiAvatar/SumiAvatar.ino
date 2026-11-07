#include <M5Unified.h>
#include <Preferences.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <utility/M5IOE1_Class.hpp>

#include "VoiceLink.h"
#include "StopwatchSettings.h"
#include "WifiSetup.h"
#include "sumi_frames.h"

namespace {

enum class Mode { Conversation, Recording, Sending, Waiting, Speaking };
enum class Screen { Badge, Avatar, Settings, Off };

constexpr int kFrameSize = 400;
constexpr int kPortraitSize = 360;
constexpr int kPortraitInset = (kFrameSize - kPortraitSize) / 2;
constexpr size_t kCaptureChunk = VoiceLink::kRecordRate / 50;
constexpr uint8_t kDim = 20;
constexpr uint32_t kBadgeInfoMs = 6000;
constexpr uint32_t kPollMs = 3000;
constexpr uint32_t kActivePollMs = 750;
constexpr uint32_t kFastWaitMs = 120000;
constexpr uint32_t kReplyTimeoutMs = 600000;  // Worker turn lifetime.
constexpr uint32_t kAudioIdleMs = 750;
constexpr uint32_t kAvatarFrameMs = 33;
constexpr int kSwayPixels = 14;
constexpr int kSwayMargin = kSwayPixels + 1;
#ifdef SUMI_AUDIO_BENCH
constexpr bool kAudioBench = true;
#else
constexpr bool kAudioBench = false;
#endif
constexpr const char* kRecordPrompt = "左ボタンを押して話しかけてね";
constexpr const char* kSendingStatus = "お話を聞いてるよ";
constexpr const char* kWaitingStatus = "お返事を考えてるよ";
constexpr uint32_t kBenchReplyDelayMs = 16000;

M5Canvas frame(&M5.Display);
M5Canvas portrait(&M5.Display);
M5Canvas scene(&M5.Display);
Mode mode = Mode::Conversation;
Screen screen = Screen::Badge;
Screen beforeSettings = Screen::Badge;
const auto& settings = StopwatchSettings::values;
bool screenDirty = true;
bool ready = false;
bool wifiConfigured = false;
bool networkBusy = false;
bool uploadPending = false;
bool replyPending = false;
bool ackPending = false;
uint8_t uploadAttempts = 0;
uint32_t nextNetworkMs = 0;
uint32_t modeStarted = 0;
uint32_t screenStarted = 0;
uint32_t badgeInfoStarted = 0;
uint32_t lastBatteryCheck = 0;
int batteryLevel = -1;
bool wifiOnline = false;
bool badgeInfo = false;
uint32_t lastFrame = 0;
uint16_t swayWeights[kPortraitSize]{};
uint16_t swayInput[kPortraitSize]{};
uint16_t swayOutput[kPortraitSize + 1]{};
// One half-pixel phase costs 259,920 bytes in PSRAM and avoids per-frame blending.
uint16_t* swayHalf = nullptr;
uint32_t animationFrames = 0;
uint32_t animationWindow = 0;
uint32_t animationFps10 = 0;
uint32_t composeUs = 0;
uint32_t pushUs = 0;
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
bool waitingForTurn = false;
uint32_t waitingSince = 0;
uint32_t sentAt = 0;
uint32_t lastEmptyPoll = 0;
uint16_t pollErrors = 0;
uint16_t speechLineStarts[513]{};
uint16_t speechLineCount = 0;

struct TurnTiming {
  char id[33];
  uint32_t uploadMs, lastEmptyMs, replyMs, synthMs, playbackMs;
  uint16_t pollErrors;
  int16_t status;
};

TurnTiming lastTiming{};
struct TimingHistory {
  uint32_t count;
  TurnTiming turns[4];
};
TimingHistory timingHistory{};
bool powerKeyConfigured = false;
uint32_t powerKeyClicks = 0;
uint32_t lastPowerRead = 0;
uint8_t powerKeyState = 0;

bool updatePowerKey(uint32_t now) {
  if (now - lastPowerRead < 20) return false;
  lastPowerRead = now;
  uint8_t state = 0;
  if (M5.Power.M5pm1.readRegister(0x48, &state, 1)) {
    if ((state & 1) != (powerKeyState & 1)) {
      Serial.printf("Sumi power raw=%u uptime=%lu\n", unsigned(state & 1), (unsigned long)now);
    }
    powerKeyState = state;
    // BTN_FLAG retains a tap that began and ended between two screen updates.
    if ((state & 0x80) && !(state & 1) && !M5.BtnPWR.isPressed()) {
      M5.BtnPWR.setState(now, m5::Button_Class::state_clicked);
      return true;
    }
    M5.BtnPWR.setRawState(now, state & 1);
    return M5.BtnPWR.wasClicked();
  }
  return false;
}

void printTiming(const TurnTiming& timing) {
  if (!timing.id[0]) return;
  Serial.printf("Sumi turn %s upload_ack=%lu last_empty=%lu reply_headers=%lu synth=%lu "
                "playback_queued=%lu poll_errors=%u status=%d (ms since release, synth is duration)\n",
                timing.id, (unsigned long)timing.uploadMs, (unsigned long)timing.lastEmptyMs,
                (unsigned long)timing.replyMs, (unsigned long)timing.synthMs,
                (unsigned long)timing.playbackMs, unsigned(timing.pollErrors), timing.status);
}

void printTimings() {
  for (uint32_t index = 0; index < timingHistory.count; ++index) printTiming(timingHistory.turns[index]);
}

void configurePowerKey() {
  constexpr uint8_t kButtonConfig = 0x49;
  constexpr uint8_t kSingleResetDisable = 0x01;
  // Same SINGLE_RST_DIS setting as the official StopWatch factory firmware.
  uint8_t previous = 0;
  uint8_t current = 0;
  if (M5.Power.M5pm1.readRegister(kButtonConfig, &previous, 1) &&
      M5.Power.M5pm1.writeRegister8(kButtonConfig, previous | kSingleResetDisable) &&
      M5.Power.M5pm1.readRegister(kButtonConfig, &current, 1)) {
    powerKeyConfigured = current == (previous | kSingleResetDisable);
  }
  Serial.printf("Sumi power key configured=%u config=0x%02x\n", powerKeyConfigured, current);
}

void saveTiming(int status, uint32_t playbackAt = 0) {
  if (kAudioBench || strcmp(upload.id, reply.request.id) || !waitingForTurn) return;
  lastTiming = {};
  strlcpy(lastTiming.id, reply.request.id, sizeof(lastTiming.id));
  lastTiming.uploadMs = waitingSince - sentAt;
  lastTiming.lastEmptyMs = lastEmptyPoll;
  lastTiming.replyMs = reply.receivedMs - sentAt;
  lastTiming.synthMs = reply.synthesisMs;
  lastTiming.playbackMs = playbackAt ? playbackAt - sentAt : 0;
  lastTiming.pollErrors = pollErrors;
  lastTiming.status = status;
  for (int index = 3; index > 0; --index) timingHistory.turns[index] = timingHistory.turns[index - 1];
  timingHistory.turns[0] = lastTiming;
  timingHistory.count = std::min(timingHistory.count + 1, uint32_t(4));
  Preferences preferences;
  if (preferences.begin("sumi-display", false)) {
    preferences.putBytes("turn-history", &timingHistory, sizeof(timingHistory));
  }
  printTiming(lastTiming);
  waitingForTurn = false;
}

uint16_t blendPixel(uint16_t current, uint16_t previous, uint32_t fraction) {
  if (current == previous || fraction < 8) return current;
  const uint32_t a = __builtin_bswap16(current);
  const uint32_t b = __builtin_bswap16(previous);
  // Five-bit weights keep the packed red and blue channels from overlapping.
  const uint32_t weight = fraction >> 3;
  const uint32_t rb = (((a & 0xf81f) * (32 - weight) + (b & 0xf81f) * weight) >> 5) & 0xf81f;
  const uint32_t g = (((a & 0x07e0) * (32 - weight) + (b & 0x07e0) * weight) >> 5) & 0x07e0;
  return __builtin_bswap16(rb | g);
}

void prepareSway() {
  swayHalf = static_cast<uint16_t*>(ps_malloc(kPortraitSize * sizeof(swayOutput)));
  if (!swayHalf) return;  // Integer-pixel movement remains available without the cache.
  const auto* source = static_cast<const uint16_t*>(portrait.getBuffer());
  for (int row = 0; row < kPortraitSize; ++row) {
    memcpy(swayInput, source + row * portrait.width(), sizeof(swayInput));
    uint16_t previous = 0xffff;
    for (int column = 0; column <= kPortraitSize; ++column) {
      const uint16_t current = column < kPortraitSize ? swayInput[column] : 0xffff;
      swayOutput[column] = blendPixel(current, previous, 128);
      previous = current;
    }
    memcpy(swayHalf + row * (kPortraitSize + 1), swayOutput, sizeof(swayOutput));
  }
}

void drawSway(int portraitX, int portraitY, int swayQ8, bool eraseEdges = false) {
  const auto* source = static_cast<const uint16_t*>(portrait.getBuffer());
  auto* destination = static_cast<uint16_t*>(scene.getBuffer());
  for (int row = 0; row < kPortraitSize; ++row) {
    const int offsetQ8 = swayQ8 * swayWeights[row] / 65535;
    int shift = offsetQ8 / 256;
    int fraction = offsetQ8 - shift * 256;
    if (fraction < 0) {
      --shift;
      fraction += 256;
    }
    const bool half = swayHalf && fraction >= 64 && fraction < 192;
    if (fraction >= (swayHalf ? 192 : 128)) ++shift;
    const auto* input = half ? swayHalf + row * (kPortraitSize + 1) : source + row * portrait.width();
    auto* rowStart = destination + (portraitY + row) * scene.width() + portraitX - kSwayMargin;
    auto* output = rowStart + kSwayMargin + shift;
    const size_t pixels = kPortraitSize + (half ? 1 : 0);
    if (eraseEdges) {
      std::fill(rowStart, output, uint16_t(0xffff));
      std::fill(output + pixels, rowStart + kPortraitSize + 2 * kSwayMargin, uint16_t(0xffff));
    }
    // Stage through internal RAM to avoid alternating PSRAM reads and writes.
    memcpy(swayOutput, input, pixels * sizeof(uint16_t));
    memcpy(output, swayOutput, pixels * sizeof(uint16_t));
  }
}

void drawPose(bool eyesClosed, int mouth, int swayQ8) {
  scene.fillScreen(TFT_WHITE);
  const int portraitX = (scene.width() - kPortraitSize) / 2;
  const int portraitY = scene.height() - kPortraitSize;
  if (swayQ8 != 0) {
    drawSway(portraitX, portraitY, swayQ8);
  } else {
    portrait.pushSprite(&scene, portraitX, portraitY);
  }
  const int dx = portraitX - kPortraitInset;
  const int dy = portraitY - kPortraitInset;
  if (eyesClosed) {
    const auto skin = scene.color565(250, 224, 216);
    const auto lashes = scene.color565(94, 63, 64);
    scene.fillEllipse(183 + dx, 169 + dy, 16, 12, skin);
    scene.fillEllipse(235 + dx, 171 + dy, 16, 12, skin);
    scene.fillEllipse(183 + dx, 169 + dy, 12, 1, lashes);
    scene.fillEllipse(235 + dx, 171 + dy, 12, 1, lashes);
  }
  if (mouth > 0) {
    scene.fillEllipse(207 + dx, 200 + dy, 10, 7, scene.color565(253, 237, 230));
    scene.fillEllipse(207 + dx, 200 + dy, 7, mouth, scene.color565(138, 76, 79));
  }
}

void setScreen(Screen next) {
  if (screen == Screen::Settings && next != Screen::Settings) StopwatchSettings::close();
  const bool wasOff = screen == Screen::Off;
  screen = next;
  screenStarted = millis();
  if (screen == Screen::Off) {
    M5.Display.sleep();
    return;
  }
  if (wasOff) M5.Display.wakeup();
  if (screen == Screen::Badge) {
    badgeDimmed = false;
    badgeInfo = false;
  }
  M5.Display.setBrightness(screen == Screen::Badge ? settings.badgeLight : settings.avatarLight);
  M5.Display.fillScreen(TFT_WHITE);
  screenDirty = true;
}

void enter(Mode next, const char* status = "") {
  mode = next;
  modeStatus = status;
  modeStarted = millis();
  if ((screen == Screen::Badge && (next == Mode::Recording || next == Mode::Sending)) ||
      (next == Mode::Speaking && (screen == Screen::Off || screen == Screen::Badge))) {
    setScreen(Screen::Avatar);
  }
  screenDirty = true;
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
  M5.Speaker.setVolume(settings.volume);
  if (!M5.Speaker.playRaw(audio, samples, reply.sampleRate)) {
    saveTiming(500);
    M5.Speaker.end();
    audioStopped();
    enter(Mode::Conversation, "音が出せなかったよ");
  } else {
    const auto config = M5.Speaker.config();
    // The software queue can empty while I2S still has samples to play.
    speakerDrainMs = 1 + 1000 * config.dma_buf_len * config.dma_buf_count / config.sample_rate;
    lastSpeakerActiveMs = millis();
    saveTiming(200, lastSpeakerActiveMs);
  }
}

void finishRecording(bool failed = false) {
  const uint32_t releasedAt = millis();
  if (!captureStopped) {
    M5.Mic.end();
    audioStopped();
  }
  const size_t samples = captured.load(std::memory_order_acquire);
  if (failed || samples < VoiceLink::kRecordRate / 5) {
    enter(Mode::Conversation, failed ? "マイクを確認してね" : kRecordPrompt);
    return;
  }
  if (kAudioBench) {
    reply.samples = samples;
    reply.sampleRate = VoiceLink::kRecordRate;
    benchQueuedAt = millis();
    benchReplyPending = true;
    enter(Mode::Waiting, "少し待ってね");
    return;
  }
  upload = {VoiceLink::Job::Upload, samples, ""};
  VoiceLink::newId(upload.id);
  sentAt = releasedAt;
  lastEmptyPoll = 0;
  pollErrors = 0;
  uploadPending = true;
  uploadAttempts = 0;
  nextNetworkMs = millis();
  enter(Mode::Sending, kSendingStatus);
}

void pumpRecording() {
  if (captureStopped) return;
  const size_t done = captured.load(std::memory_order_acquire);
  if (done >= VoiceLink::kRecordSamples || millis() - modeStarted >= 17000) {
    M5.Mic.end();
    audioStopped();
    captureStopped = true;
    enter(Mode::Recording, "いったん指を離してね");
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
    enter(Mode::Conversation, "もう少し待ってね");
    return;
  }
  if (!recording) {
    enter(Mode::Conversation, "録音の準備ができなかったよ");
    return;
  }
  if (!kAudioBench && !VoiceLink::connected()) {
    enter(Mode::Conversation, wifiConfigured ? "Wi-Fiにつないでるよ" : "Wi-Fiを設定してね");
    return;
  }
  if (uploadPending) {
    enter(Mode::Conversation, kSendingStatus);
    return;
  }
  audioRailOn = true;
  captureStopped = false;
  captured = 0;
  queued = 0;
  M5.Mic.setBufferReleaseCallback(nullptr, captureReleased);
  enter(Mode::Recording, "いま聞いてるよ");
  pumpRecording();
}

void prepareSpeechLines() {
  speechLineCount = 1;
  speechLineStarts[0] = 0;
  scene.setFont(&fonts::lgfxJapanGothic_20);
  scene.setTextSize(1);
  String line;
  const size_t length = strlen(reply.text);
  for (size_t offset = 0; offset < length;) {
    if (reply.text[offset] == '\n') {
      speechLineStarts[speechLineCount++] = ++offset;
      line = "";
      continue;
    }
    const uint8_t first = reply.text[offset];
    const size_t bytes = first < 0x80 ? 1 : first < 0xe0 ? 2 : first < 0xf0 ? 3 : 4;
    char character[5]{};
    memcpy(character, reply.text + offset, std::min(bytes, length - offset));
    if (line.length() && scene.textWidth(line + character) > 296) {
      speechLineStarts[speechLineCount++] = offset;
      line = "";
    }
    line += character;
    offset += bytes;
  }
}

void playReply() {
  replyPending = false;
  prepareSpeechLines();
  playSpeech(playback, reply.samples, "すみからのお返事だよ");
}

void acknowledgeReply(const VoiceLink::Request& request) {
  ack = request;
  ack.job = VoiceLink::Job::Acknowledge;
  ackPending = true;
  nextNetworkMs = millis();
}

void drawIndicators(M5Canvas& canvas, int insetX = 0) {
  const int x = 146 + insetX;
  constexpr int y = 20;
  canvas.fillRoundRect(x - 11, 13, 148, 31, 10, TFT_WHITE);
  canvas.drawRoundRect(x, y, 23, 14, 3, TFT_DARKGREY);
  canvas.fillRect(x + 23, y + 4, 3, 6, TFT_DARKGREY);
  if (batteryLevel > 0) canvas.fillRect(x + 3, y + 3, (17 * batteryLevel + 99) / 100, 8, TFT_DARKGREEN);
  char text[6];
  if (batteryLevel < 0) {
    strlcpy(text, "--", sizeof(text));
  } else {
    snprintf(text, sizeof(text), "%d%%", batteryLevel);
  }
  canvas.setFont(&fonts::Font0);
  canvas.setTextSize(2);
  canvas.setTextDatum(middle_left);
  canvas.setTextColor(TFT_DARKGREY);
  canvas.drawString(text, x + 33, y + 8);
  const int wifiX = 263 + insetX;
  const auto color = wifiOnline ? TFT_DARKGREEN : TFT_LIGHTGREY;
  canvas.drawArc(wifiX, y + 15, 14, 12, 215, 325, color);
  canvas.drawArc(wifiX, y + 15, 9, 7, 215, 325, color);
  canvas.fillCircle(wifiX, y + 13, 2, color);
  if (!wifiOnline) canvas.drawLine(wifiX - 10, y + 2, wifiX + 8, y + 15, TFT_DARKGREY);
}

void updateIndicators(uint32_t now) {
  if (now - lastBatteryCheck >= 5000) {
    lastBatteryCheck = now;
    const int nextLevel = std::clamp(int(M5.Power.getBatteryLevel()), -1, 100);
    if (nextLevel != batteryLevel) {
      batteryLevel = nextLevel;
      screenDirty = true;
    }
  }
  const bool connected = WifiSetup::connected();
  if (connected != wifiOnline) {
    wifiOnline = connected;
    screenDirty = true;
  }
}

void pushFrame(int y) {
  frame.pushSprite((M5.Display.width() - kFrameSize) / 2, y);
}

int speechScroll(uint32_t elapsed) {
  if (mode != Mode::Speaking || speechLineCount <= 2 || !reply.samples || !reply.sampleRate) return 0;
  const uint32_t duration = (uint64_t(reply.samples) * 1000 + reply.sampleRate - 1) / reply.sampleRate;
  const int target = std::min(uint64_t(elapsed) * speechLineCount / duration, uint64_t(speechLineCount - 2));
  if (target == 0) return 0;
  const uint32_t transition = uint64_t(target) * duration / speechLineCount;
  return (target - 1) * 25 + std::min(uint32_t(180), elapsed - transition) * 25 / 180;
}

void drawSpeechBubble(int level, int scroll, bool frameOnly = false) {
  constexpr int width = 324;
  constexpr int top = 60;
  constexpr int height = 66;
  const int centerX = scene.width() / 2;
  const int left = centerX - width / 2;
  const auto outline = mode == Mode::Recording ? TFT_MAROON : scene.color565(144, 136, 156);
  scene.fillRoundRect(left, top, width, height, 18, TFT_WHITE);
  scene.drawRoundRect(left, top, width, height, 18, outline);
  scene.fillTriangle(centerX - 12, top + height - 2, centerX + 12, top + height - 2,
                     centerX, top + height + 12, TFT_WHITE);
  scene.drawLine(centerX - 12, top + height - 1, centerX, top + height + 12, outline);
  scene.drawLine(centerX + 12, top + height - 1, centerX, top + height + 12, outline);
  if (frameOnly) return;
  scene.setFont(&fonts::lgfxJapanGothic_20);
  scene.setTextSize(1);
  scene.setTextColor(TFT_DARKGREY);
  scene.setTextDatum(middle_center);
  if (mode == Mode::Speaking && reply.text[0] && speechLineCount) {
    scene.setClipRect(left + 10, top + 8, width - 20, height - 16);
    const size_t length = strlen(reply.text);
    const int firstLine = scroll / 25;
    for (int row = 0; row < 3 && firstLine + row < speechLineCount; ++row) {
      const int index = firstLine + row;
      const size_t begin = speechLineStarts[index];
      size_t end = index + 1 < speechLineCount ? speechLineStarts[index + 1] : length;
      while (end > begin && (reply.text[end - 1] == '\n' || reply.text[end - 1] == '\r')) --end;
      char line[513]{};
      memcpy(line, reply.text + begin, end - begin);
      const int y = speechLineCount == 1 ? top + height / 2 : top + 21 + row * 25 - scroll % 25;
      scene.drawString(line, centerX, y);
    }
    scene.clearClipRect();
  } else {
    scene.drawString(modeStatus, centerX, top + (mode == Mode::Recording ? 24 : height / 2));
  }
  if (mode == Mode::Recording) {
    scene.fillCircle(centerX - 64, top + 50, 4, TFT_RED);
    scene.setFont(&fonts::Font0);
    scene.setTextSize(1);
    scene.setTextDatum(middle_left);
    scene.drawString("REC", centerX - 53, top + 50);
    if (!captureStopped) {
      for (int index = 0; index < 5; ++index) {
        scene.fillRect(centerX - 19 + index * 16, top + 47, 12, 6,
                       index < level ? TFT_DARKGREEN : TFT_LIGHTGREY);
      }
    }
  }
}

void renderAvatar(uint32_t now) {
  if (!screenDirty && now - lastFrame < kAvatarFrameMs) {
    return;
  }
  const uint32_t elapsed = now - modeStarted;
  const double sway = std::sin(6.2831853 * (elapsed % 2800) / 2800.0);
  const bool thinking = mode == Mode::Waiting;
  const bool eyesClosed = !thinking && elapsed % 4000 >= 3850;
  const int swayQ8 = thinking ? std::lround(kSwayPixels * 256 * sway) : 0;
  static bool previousEyes = false;
  static int previousMouth = 0;
  static int previousLevel = 0;
  static int previousSway = 0;
  static int previousScroll = 0;
  const int scroll = speechScroll(elapsed);
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
  const bool redraw = screenDirty || scroll != previousScroll || swayQ8 != previousSway || eyesClosed != previousEyes ||
      mouth != previousMouth || level != previousLevel;
  if (!redraw) return;
  const bool partial = thinking && !screenDirty;
  const uint32_t composeStarted = micros();
  const int portraitX = (scene.width() - kPortraitSize) / 2;
  const int portraitY = scene.height() - kPortraitSize;
  if (partial) {
    int32_t x = 0, y = 0, w = 0, h = 0;
    scene.getClipRect(&x, &y, &w, &h);
    scene.setClipRect(portraitX - kSwayMargin, portraitY, kPortraitSize + 2 * kSwayMargin, kPortraitSize);
    drawSway(portraitX, portraitY, swayQ8, true);
    // The portrait overlaps the bubble's lower border, but its text is unchanged.
    drawSpeechBubble(level, scroll, true);
    scene.setClipRect(x, y, w, h);
  } else {
    drawPose(eyesClosed, mouth, swayQ8);
    drawSpeechBubble(level, scroll);
    drawIndicators(scene, (scene.width() - kFrameSize) / 2);
  }
  composeUs = micros() - composeStarted;
  const uint32_t pushStarted = micros();
  int32_t clipX = 0, clipY = 0, clipW = 0, clipH = 0;
  if (partial) {
    M5.Display.getClipRect(&clipX, &clipY, &clipW, &clipH);
    M5.Display.setClipRect(portraitX - kSwayMargin, portraitY, kPortraitSize + 2 * kSwayMargin, kPortraitSize);
  }
  scene.pushSprite(0, 0);
  if (partial) M5.Display.setClipRect(clipX, clipY, clipW, clipH);
  pushUs = micros() - pushStarted;
  ++animationFrames;
  const uint32_t measured = millis();
  if (measured - animationWindow >= 1000) {
    animationFps10 = animationFrames * 10000 / (measured - animationWindow);
    animationFrames = 0;
    animationWindow = measured;
  }
  previousSway = swayQ8;
  previousScroll = scroll;
  previousEyes = eyesClosed;
  previousMouth = mouth;
  previousLevel = level;
  lastFrame = now;
  screenDirty = false;
}

void renderScreen(uint32_t now) {
  if (screen == Screen::Off) return;
  if (screen == Screen::Avatar || (screen == Screen::Badge && badgeInfo)) updateIndicators(now);
  if (screen == Screen::Avatar) {
    renderAvatar(now);
    return;
  }
  if (!screenDirty) return;
  if (screen == Screen::Settings) {
    StopwatchSettings::draw(frame, !kAudioBench);
    scene.fillScreen(TFT_WHITE);
    frame.pushSprite(&scene, (scene.width() - kFrameSize) / 2, (scene.height() - kFrameSize) / 2);
    StopwatchSettings::drawBack(scene);
    scene.pushSprite(0, 0);
  } else {
    frame.fillScreen(TFT_WHITE);
    frame.drawPng(kSumiBadge, sizeof(kSumiBadge));
    if (badgeInfo) drawIndicators(frame);
    pushFrame((M5.Display.height() - kFrameSize) / 2);
  }
  screenDirty = false;
}

void updateNetwork(uint32_t now) {
  VoiceLink::Result result{};
  if (VoiceLink::receive(result)) {
    networkBusy = false;
    switch (result.request.job) {
      case VoiceLink::Job::Upload:
        if (result.status == 202) {
          uploadPending = false;
          waitingForTurn = true;
          waitingSince = result.receivedMs;
          nextNetworkMs = now;
          enter(Mode::Waiting, kWaitingStatus);
        } else if ((result.status < 0 || result.status >= 500) && ++uploadAttempts < 3) {
          nextNetworkMs = now + 1000;
        } else {
          uploadPending = false;
          const char* error = result.status == 409 ? "すみとの接続を確認してね"
              : result.status == 424 ? "音声認識の設定を確認してね" : "うまく送れなかったよ";
          enter(Mode::Conversation, error);
        }
        break;
      case VoiceLink::Job::Poll:
        if (result.status == 200) {
          reply = result;
          replyPending = true;
        } else if (result.status == 422 || result.status == 415) {
          reply = result;
          saveTiming(result.status);
          acknowledgeReply(result.request);
          if (!waitingForTurn && mode != Mode::Recording && mode != Mode::Sending) {
            enter(Mode::Conversation, result.status == 415 ? "声を作れなかったよ" : "お返事が届かなかったよ");
          }
        } else if (waitingForTurn) {
          if (result.status == 204) {
            lastEmptyPoll = result.receivedMs - sentAt;
          } else {
            ++pollErrors;
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
  if (WifiSetup::active() || !VoiceLink::connected() || networkBusy || int32_t(now - nextNetworkMs) < 0) {
    return;
  }
  const VoiceLink::Request poll{VoiceLink::Job::Poll, 0, ""};
  networkBusy = VoiceLink::send(uploadPending ? upload : ackPending ? ack : poll);
  const bool fastPoll = waitingForTurn && millis() - waitingSince < kFastWaitMs;
  nextNetworkMs = now + (uploadPending ? 0 : fastPoll ? kActivePollMs : kPollMs);
}

void updateScreen(uint32_t now) {
  const bool left = M5.BtnA.wasPressed();
  const bool right = M5.BtnB.wasPressed();
  const bool power = updatePowerKey(now);
  if (power) {
    ++powerKeyClicks;
    Serial.printf("Sumi power click=%lu screen=%u\n", (unsigned long)powerKeyClicks, unsigned(screen));
  }
  if (screen == Screen::Off) {
    if (left || right || power) setScreen(Screen::Badge);
    return;
  }
  if (power) {
    setScreen(Screen::Off);
    return;
  }
  if (right) {
    if (screen == Screen::Settings) {
      setScreen(mode == Mode::Conversation ? beforeSettings : Screen::Avatar);
    } else {
      beforeSettings = screen;
      StopwatchSettings::open();
      setScreen(Screen::Settings);
    }
    return;
  }

  const auto& touch = M5.Touch.getDetail();
  const bool touched = M5.Touch.getCount() && touch.isPressed();
  if (screen == Screen::Settings) {
    const int x = touch.x - (M5.Display.width() - kFrameSize) / 2;
    const int y = touch.y - (M5.Display.height() - kFrameSize) / 2;
    const auto update = StopwatchSettings::update(x, y, touched, touched && touch.wasPressed(),
                                                !kAudioBench && !networkBusy && mode == Mode::Conversation);
    if (update == StopwatchSettings::Update::Close) {
      setScreen(mode == Mode::Conversation ? beforeSettings : Screen::Avatar);
    } else if (update == StopwatchSettings::Update::Changed) {
      M5.Speaker.setVolume(settings.volume);
      M5.Display.setBrightness(settings.avatarLight);
      screenDirty = true;
    }
    return;
  }
  if (left && mode == Mode::Conversation) {
    setScreen(Screen::Avatar);
    startRecording();
    return;
  }
  const uint32_t screenElapsed = now - screenStarted;
  if (screen == Screen::Avatar) {
    const uint32_t timeout = uint32_t(settings.avatarSeconds) * 1000;
    if (mode == Mode::Conversation && screenElapsed >= timeout && now - modeStarted >= timeout) {
      setScreen(Screen::Badge);
    }
    return;
  }

  if (touched) {
    screenStarted = now;
    if (badgeDimmed) {
      M5.Display.setBrightness(settings.badgeLight);
      badgeDimmed = false;
    }
    if (touch.wasPressed() && touch.y <= M5.Display.height() / 4) {
      badgeInfo = true;
      badgeInfoStarted = now;
      screenDirty = true;
    }
  } else if (!badgeDimmed && screenElapsed >= uint32_t(settings.badgeSeconds) * 1000) {
    M5.Display.setBrightness(std::min(kDim, uint8_t(settings.badgeLight / 2)));
    badgeDimmed = true;
    screenStarted = now;
    badgeInfo = false;
    screenDirty = true;
  } else if (badgeDimmed && screenElapsed >= uint32_t(settings.dimSeconds) * 1000) {
    setScreen(Screen::Off);
  }
  if (badgeInfo && now - badgeInfoStarted >= kBadgeInfoMs) {
    badgeInfo = false;
    screenDirty = true;
  }
}

void updateActivity(uint32_t now) {
  switch (mode) {
    case Mode::Conversation:
    case Mode::Sending:
      break;
    case Mode::Recording:
      if (!M5.BtnA.isPressed()) {
        finishRecording();
      } else {
        pumpRecording();
      }
      break;
    case Mode::Waiting:
      if (waitingForTurn && !networkBusy && now - waitingSince >= kReplyTimeoutMs) {
        waitingForTurn = false;
        enter(Mode::Conversation, "お返事がまだ届かないよ");
      }
      break;
    case Mode::Speaking:
      if (M5.Speaker.isPlaying()) {
        lastSpeakerActiveMs = now;
      } else if (now - lastSpeakerActiveMs >= speakerDrainMs) {
        M5.Speaker.end();
        audioStopped();
        if (!kAudioBench) acknowledgeReply(reply.request);
        if (waitingForTurn) {
          enter(Mode::Waiting, kWaitingStatus);
        } else {
          enter(Mode::Conversation, kRecordPrompt);
        }
      }
      break;
  }
}

}  // namespace

void setup() {
  auto config = M5.config();
  config.serial_baudrate = 115200;
  config.pmic_button = false;  // Read BTN_STATUS directly rather than button IRQ flags.
  M5.begin(config);
  if (M5.getBoard() != m5::board_t::board_M5StopWatch) {
    return;
  }
  configurePowerKey();
  M5.BtnPWR.setHoldThresh(1000);

  M5.Mic.end();
  M5.Speaker.end();
  audioRailOn = true;
  audioStopped();
  frame.setColorDepth(16);
  frame.setPsram(true);
  portrait.setColorDepth(16);
  portrait.setPsram(true);
  scene.setColorDepth(16);
  scene.setPsram(true);
  if (!frame.createSprite(kFrameSize, kFrameSize) ||
      !scene.createSprite(M5.Display.width(), M5.Display.height()) ||
      !portrait.createSprite(kPortraitSize, kPortraitSize) ||
      !portrait.drawPng(kSumiAvatar, sizeof(kSumiAvatar))) {
    return;
  }
  for (int row = 0; row < kPortraitSize; ++row) {
    const float position = 1.0f - float(row) / (kPortraitSize - 1);
    swayWeights[row] = std::lround(65535 * position * position * (3 - 2 * position));
  }
  prepareSway();
  StopwatchSettings::load();
  {
    Preferences preferences;
    if (preferences.begin("sumi-display", true)) {
      if (preferences.getBytesLength("turn-history") == sizeof(timingHistory) &&
          preferences.getBytes("turn-history", &timingHistory, sizeof(timingHistory)) == sizeof(timingHistory) &&
          timingHistory.count <= 4) {
        for (auto& timing : timingHistory.turns) timing.id[32] = '\0';
      } else {
        timingHistory = {};
        if (preferences.getBytesLength("last-turn") == sizeof(lastTiming) &&
            preferences.getBytes("last-turn", &lastTiming, sizeof(lastTiming)) == sizeof(lastTiming)) {
          lastTiming.id[32] = '\0';
          timingHistory.turns[0] = lastTiming;
          timingHistory.count = 1;
        }
      }
    }
  }

  recording = static_cast<int16_t*>(ps_malloc(VoiceLink::kRecordSamples * sizeof(int16_t)));
  if (!kAudioBench) {
    playback = static_cast<int16_t*>(ps_malloc(VoiceLink::kPlaybackSamples * sizeof(int16_t)));
    if (recording && playback) {
      wifiConfigured = VoiceLink::begin(recording, playback);
    }
  }
  lastBatteryCheck = millis() - 5000;
  setScreen(Screen::Badge);
  ready = true;
}

void loop() {
  delay(screen == Screen::Avatar || mode == Mode::Recording ? 5 : 20);
  if (!ready) {
    return;
  }

  M5.update();
  updateScreen(millis());
  const uint32_t now = millis();
  updateActivity(now);
  if (Serial.available()) {
    const char command = Serial.read();
    if (command == '?') printTimings();
    if (command == 'p') {
      uint8_t config = 0;
      const bool read = M5.Power.M5pm1.readRegister(0x49, &config, 1);
      Serial.printf("Sumi power read=%u config=0x%02x raw=0x%02x clicks=%lu screen=%u uptime=%lu\n",
                    read, config, powerKeyState, (unsigned long)powerKeyClicks, unsigned(screen), (unsigned long)millis());
      Serial.printf("Sumi animation fps10=%lu compose_us=%lu push_us=%lu\n",
                    (unsigned long)animationFps10, (unsigned long)composeUs, (unsigned long)pushUs);
    }
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
  renderScreen(millis());
}
