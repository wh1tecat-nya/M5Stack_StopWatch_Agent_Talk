#include <M5Unified.h>
#include <esp_sleep.h>

#include <cmath>

namespace {

constexpr uint32_t kFrameMs = 33;
constexpr uint32_t kPowerPollMs = 12;
constexpr uint32_t kPowerDebounceMs = 24;
constexpr uint32_t kPowerHoldMs = 1500;
constexpr uint64_t kSleepPollUs = 80000;
constexpr uint8_t kPowerButtonStatus = 0x48;
constexpr uint8_t kPowerButtonConfig = 0x49;
constexpr uint8_t kPowerButtonConfig2 = 0x4A;
constexpr uint8_t kPowerButtonDown = 0x01;
constexpr uint8_t kPowerButtonLatched = 0x80;
constexpr uint8_t kDisableSingleReset = 0x01;
constexpr uint8_t kHardwareHold4s = 0x18;
constexpr uint8_t kDisableDoubleOff = 0x01;
constexpr uint8_t kAudioRailPin = 3;  // C152 M5IOE1 PYB_AU_EN.
constexpr uint32_t kImuSampleMs = 16;
constexpr uint32_t kShakeQuietMs = 200;
constexpr float kShakeStartG = 0.68f;
constexpr float kShakeQuietG = 0.26f;
constexpr float kPi = 3.14159265f;
constexpr uint32_t kSampleRate = 16000;
constexpr uint8_t kVolume = 255;  // M5Unified applies this setting quadratically.
constexpr uint8_t kSpeakerMagnification = 6;
constexpr float kEffectAmplitude = 17900.0f;
constexpr float kLimiterKnee = 20000.0f;
constexpr float kLimiterHeadroom = 2000.0f;

constexpr uint32_t kPopMs = 280;
constexpr uint32_t kBubbleGrowMs = 280;
constexpr uint32_t kSparkleMs = 750;
constexpr uint32_t kPink = 0xFF669F;
constexpr uint32_t kBlue = 0x8AEFFF;

enum class Mode { Sparkles, Bubbles };
enum class Sound { Chime, Twinkle, Pop, Spawn };
enum class PowerEvent { None, Click, Hold };

struct Bubble {
  int16_t offsetX, offsetY;
  int16_t radius;
  uint32_t color;
  int16_t x = 0, y = 0, drawnRadius = 0;
  uint32_t bornAt = 0, poppedAt = 0;
  bool visible = false;
  bool popping = false;
};

struct Sparkle {
  int16_t x, y;
  uint32_t bornAt;
  bool heart, visible;
};

M5Canvas frame(&M5.Display);
Mode mode = Mode::Sparkles;
Bubble bubbles[] = {
    {-5, -128, 39, kPink},
    {105, -77, 35, kBlue},
    {116, 46, 43, kPink},
    {9, 122, 41, kBlue},
    {-111, 63, 37, kPink},
    {-107, -57, 44, kBlue},
    {4, -1, 42, kPink},
};
Sparkle sparkles[12]{};
size_t nextSparkle = 0;
bool nextIsHeart = true;
bool ready = false, dirty = true;
bool sleeping = false;
bool powerKeyReady = false;
bool powerArmed = false, powerDown = false, powerCandidate = false, powerHoldHandled = false;
uint32_t powerPolledAt = 0, powerChangedAt = 0;
int centerX, centerY;
uint32_t lastFrame = 0;
uint32_t lastImuPoll = 0, lastAccelSample = 0, lastMotion = 0;
bool haveAccel = false, shakeArmed = true;
float gravityX = 0, gravityY = 0, gravityZ = 0;

int16_t chime[1920], twinkle[3200], pop[1440], spawn[4480];

bool setPowerConfigBits(uint8_t reg, uint8_t bits) {
  auto& pmic = M5.Power.M5pm1;
  uint8_t value = 0;
  if (!pmic.readRegister(reg, &value, 1)) return false;
  if ((value & bits) == bits) return true;
  return pmic.writeRegister8(reg, value | bits) &&
         pmic.readRegister(reg, &value, 1) && (value & bits) == bits;
}

PowerEvent pollPowerKey(uint32_t now) {
  if (!powerKeyReady || now - powerPolledAt < kPowerPollMs) return PowerEvent::None;
  const uint32_t gap = now - powerPolledAt;
  powerPolledAt = now;
  uint8_t status = 0;
  if (!M5.Power.M5pm1.readRegister(kPowerButtonStatus, &status, 1)) {
    powerChangedAt = now;
    return PowerEvent::None;
  }

  const bool pressed = status & kPowerButtonDown;
  if (!powerArmed) {
    // Consume the press that powered the board on.
    powerArmed = !pressed;
    return PowerEvent::None;
  }

  if (pressed != powerCandidate) {
    powerCandidate = pressed;
    powerChangedAt = now;
  }
  if (pressed != powerDown && now - powerChangedAt >= kPowerDebounceMs) {
    powerDown = pressed;
    if (pressed) {
      powerHoldHandled = false;
    } else if (!powerHoldHandled) {
      return PowerEvent::Click;
    }
  }
  if (pressed && powerDown && !powerHoldHandled && now - powerChangedAt >= kPowerHoldMs) {
    powerHoldHandled = true;
    return PowerEvent::Hold;
  }
  // The PMIC latches a press even if the whole tap fell between sleep polls.
  if (!pressed && !powerDown && (status & kPowerButtonLatched) && gap >= kPowerDebounceMs) {
    return PowerEvent::Click;
  }
  return PowerEvent::None;
}

void setSleeping(bool next) {
  if (sleeping == next) return;
  sleeping = next;
  if (sleeping) {
    M5.Display.sleep();
    if (M5.Speaker.isRunning()) M5.Speaker.end();
    // Speaker.begin() restores the ES8311 power rail on its next playback.
    M5.getIOExpander(0).digitalWrite(kAudioRailPin, false);
    return;
  }
  M5.Display.wakeup();
  haveAccel = false;
  dirty = true;
}

void waitWhileSleeping() {
  // USB Serial/JTAG needs its clock while connected.
  if (powerCandidate || powerDown || !powerArmed || M5.Power.getVBUSVoltage() > 4000) {
    delay(kPowerPollMs);
    return;
  }
  if (esp_sleep_enable_timer_wakeup(kSleepPollUs) != ESP_OK || esp_light_sleep_start() != ESP_OK) {
    delay(kPowerPollMs);
  }
}

template <size_t N>
void synthesize(int16_t (&samples)[N], Sound sound) {
  float phase = 0;
  uint32_t noise = 0x35AC91F3u;
  for (size_t i = 0; i < N; ++i) {
    const float t = float(i) / N;
    const float attack = fminf(1.0f, float(i) / 28);
    const float tail = 1.0f - t;
    float frequency = 0, wave = 0;
    switch (sound) {
      case Sound::Chime:
        frequency = 880 + 200 * t;
        wave = sinf(phase) + 0.24f * sinf(2 * phase);
        break;
      case Sound::Twinkle:
        frequency = t < 0.40f ? 660 : (t < 0.72f ? 880 : 1175);
        wave = sinf(phase) + 0.20f * sinf(2 * phase);
        break;
      case Sound::Pop:
        frequency = 740 - 490 * t;
        noise = noise * 1664525u + 1013904223u;
        wave = (float(noise >> 16) / 32768.0f - 1.0f) * tail +
               0.72f * sinf(phase);
        break;
      case Sound::Spawn:
        frequency = 330 + 520 * (1 - tail * tail);
        wave = sinf(phase) + 0.27f * sinf(2 * phase);
        break;
    }
    phase += 2 * kPi * frequency / kSampleRate;
    const float sample = kEffectAmplitude * attack * tail * tail * wave;
    const float excess = fabsf(sample) - kLimiterKnee;
    // Round off the loudest peaks before the StopWatch codec adds its own gain.
    const float limited = excess > 0
        ? copysignf(kLimiterKnee + excess / (1 + excess / kLimiterHeadroom), sample)
        : sample;
    samples[i] = int16_t(limited);
  }
}

template <size_t N>
void play(const int16_t (&samples)[N]) {
  M5.Speaker.playRaw(samples, N, kSampleRate, false, 1, 0, true);
}

bool detectShake(uint32_t now) {
  if (now - lastImuPoll < kImuSampleMs || !M5.Imu.isEnabled()) return false;
  lastImuPoll = now;
  if (!(M5.Imu.update() & m5::IMU_Class::sensor_mask_accel)) return false;
  const auto& accel = M5.Imu.getImuData().accel;
  const uint32_t elapsed = now - lastAccelSample;
  lastAccelSample = now;
  if (!haveAccel || elapsed > 250) {
    gravityX = accel.x;
    gravityY = accel.y;
    gravityZ = accel.z;
    lastMotion = now;
    haveAccel = true;
    return false;
  }

  // Low-pass the gravity vector so shakes along any axis can trigger.
  const float dx = accel.x - gravityX;
  const float dy = accel.y - gravityY;
  const float dz = accel.z - gravityZ;
  const float weight = float(elapsed) / (120 + elapsed);
  gravityX += weight * dx;
  gravityY += weight * dy;
  gravityZ += weight * dz;
  const float motion = dx * dx + dy * dy + dz * dz;
  if (motion > kShakeQuietG * kShakeQuietG) {
    lastMotion = now;
  } else if (now - lastMotion >= kShakeQuietMs) {
    shakeArmed = true;
  }
  if (!shakeArmed || motion < kShakeStartG * kShakeStartG) return false;
  shakeArmed = false;
  return true;
}

void addSparkle(int x, int y, uint32_t now) {
  sparkles[nextSparkle] = {int16_t(x), int16_t(y), now, nextIsHeart, true};
  nextSparkle = (nextSparkle + 1) % (sizeof(sparkles) / sizeof(sparkles[0]));
  nextIsHeart = !nextIsHeart;
  dirty = true;
}

void refillBubbles(uint32_t now) {
  for (auto& bubble : bubbles) {
    if (bubble.visible) continue;
    bubble.visible = true;
    bubble.popping = false;
    bubble.bornAt = now;
    bubble.drawnRadius = 0;
  }
}

void popBubble(int x, int y, uint32_t now) {
  for (auto& bubble : bubbles) {
    if (!bubble.visible || bubble.drawnRadius == 0) continue;
    const int32_t dx = x - bubble.x;
    const int32_t dy = y - bubble.y;
    if (dx * dx + dy * dy > bubble.drawnRadius * bubble.drawnRadius) continue;
    bubble.visible = false;
    bubble.popping = true;
    bubble.poppedAt = now;
    dirty = true;
    play(pop);
    return;
  }
}

void switchMode(uint32_t now) {
  if (mode == Mode::Sparkles) {
    mode = Mode::Bubbles;
    for (auto& bubble : bubbles) bubble.visible = false;
    refillBubbles(now);
  } else {
    mode = Mode::Sparkles;
    for (auto& sparkle : sparkles) sparkle.visible = false;
    addSparkle(centerX - 48, centerY + 12, now);
    addSparkle(centerX + 48, centerY - 12, now);
  }
  dirty = true;
}

void drawHeart(int x, int y, int radius, uint32_t color) {
  const int lobe = radius / 2;
  frame.fillTriangle(x - radius, y - radius / 4, x + radius, y - radius / 4,
                     x, y + radius, color);
  frame.fillCircle(x - lobe, y - radius / 4, lobe, color);
  frame.fillCircle(x + lobe, y - radius / 4, lobe, color);
}

void drawStar(int x, int y, int radius, uint32_t color) {
  constexpr float kX[] = {0, 0.294f, 0.951f, 0.476f, 0.588f, 0,
                          -0.588f, -0.476f, -0.951f, -0.294f};
  constexpr float kY[] = {-1, -0.405f, -0.309f, 0.155f, 0.809f, 0.5f,
                          0.809f, 0.155f, -0.309f, -0.405f};
  for (int i = 0; i < 10; ++i) {
    const int next = (i + 1) % 10;
    frame.fillTriangle(x, y, x + int(radius * kX[i]), y + int(radius * kY[i]),
                       x + int(radius * kX[next]), y + int(radius * kY[next]), color);
  }
}

bool drawSparkles(uint32_t now) {
  bool animating = false;
  for (auto& sparkle : sparkles) {
    if (!sparkle.visible) continue;
    const uint32_t age = now - sparkle.bornAt;
    if (age >= kSparkleMs) {
      sparkle.visible = false;
      continue;
    }
    animating = true;
    const float t = float(age) / kSparkleMs;
    const int x = sparkle.x;
    const int y = sparkle.y - int(40 * t);
    const int radius = 16 + int(22 * sinf(kPi * t));
    const uint32_t color = sparkle.heart ? kPink : kBlue;
    if (sparkle.heart) {
      drawHeart(x, y, radius, color);
    } else {
      drawStar(x, y, radius, color);
    }
    for (int i = 0; i < 5; ++i) {
      const float angle = i * (2 * kPi / 5) + t * 0.7f;
      const float spread = radius + 7 + 22 * t;
      frame.fillCircle(x + int(cosf(angle) * spread), y + int(sinf(angle) * spread),
                       2 + int(4 * (1 - t)), color);
    }
  }
  if (!animating) {
    drawHeart(centerX - 52, centerY + 5, 36, kPink);
    drawStar(centerX + 52, centerY - 5, 42, kBlue);
  }
  return animating;
}

bool drawBubbles(uint32_t now) {
  bool animating = false;
  for (auto& bubble : bubbles) {
    const uint32_t age = now - bubble.bornAt;
    if (bubble.visible) {
      animating = true;
      const float wobble = float(age % 2800) * (2 * kPi / 2800) + bubble.offsetX * 0.023f;
      const float growth = fminf(1.0f, float(age) / kBubbleGrowMs);
      bubble.x = centerX + bubble.offsetX + int(9 * sinf(wobble));
      bubble.y = centerY + bubble.offsetY + int(8 * cosf(wobble));
      const int r = bubble.drawnRadius = int(bubble.radius * (0.55f + 0.45f * growth));
      frame.drawCircle(bubble.x, bubble.y, r, bubble.color);
      frame.drawCircle(bubble.x, bubble.y, r - 1, bubble.color);
      frame.fillEllipse(bubble.x - r / 3, bubble.y - r / 2, r / 4, r / 8, bubble.color);
      frame.fillCircle(bubble.x - r / 2, bubble.y - r / 4, 2, bubble.color);
      frame.fillEllipse(bubble.x + r / 4, bubble.y + r / 2, r / 5, r / 12, bubble.color);
    } else if (bubble.popping) {
      const uint32_t popAge = now - bubble.poppedAt;
      if (popAge >= kPopMs) {
        bubble.popping = false;
        continue;
      }
      animating = true;
      const float t = float(popAge) / kPopMs;
      const int spread = bubble.drawnRadius + int(26 * t);
      if (t < 0.42f) frame.drawCircle(bubble.x, bubble.y, spread, bubble.color);
      for (int i = 0; i < 7; ++i) {
        const float angle = i * (2 * kPi / 7) + 0.2f;
        frame.fillCircle(bubble.x + int(cosf(angle) * spread),
                         bubble.y + int(sinf(angle) * spread + 12 * t * t),
                         1 + int(5 * (1 - t)), bubble.color);
      }
    }
  }
  return animating;
}

void render(uint32_t now) {
  if (!dirty || now - lastFrame < kFrameMs) return;
  lastFrame = now;
  const bool isBubbleMode = mode == Mode::Bubbles;
  frame.fillScreen(0x000000u);
  dirty = isBubbleMode ? drawBubbles(now) : drawSparkles(now);
  frame.pushSprite(0, 0);
}

}  // namespace

void setup() {
  auto config = M5.config();
  config.serial_baudrate = 115200;
  config.internal_mic = false;
  config.pmic_button = false;
  M5.begin(config);
  if (M5.getBoard() != m5::board_t::board_M5StopWatch) {
    Serial.println("StopWatchToy requires an M5Stack StopWatch (C152).");
    return;
  }

  for (int retry = 0; retry < 3 && !powerKeyReady; ++retry) {
    powerKeyReady = setPowerConfigBits(kPowerButtonConfig, kDisableSingleReset | kHardwareHold4s);
  }
  const bool doubleOffDisabled = powerKeyReady && setPowerConfigBits(kPowerButtonConfig2, kDisableDoubleOff);
  Serial.printf("StopWatchToy: power key %s, hardware double-off %s\n",
                powerKeyReady ? "ready" : "unavailable", doubleOffDisabled ? "disabled" : "available");

  centerX = M5.Display.width() / 2;
  centerY = M5.Display.height() / 2;
  frame.setColorDepth(16);
  frame.setPsram(true);
  if (!frame.createSprite(M5.Display.width(), M5.Display.height())) {
    Serial.println("StopWatchToy could not allocate its display buffer.");
    return;
  }

  M5.Display.setBrightness(128);
  auto speakerConfig = M5.Speaker.config();
  speakerConfig.magnification = kSpeakerMagnification;
  M5.Speaker.config(speakerConfig);
  M5.Speaker.setVolume(kVolume);
  synthesize(chime, Sound::Chime);
  synthesize(twinkle, Sound::Twinkle);
  synthesize(pop, Sound::Pop);
  synthesize(spawn, Sound::Spawn);
  if (!M5.Imu.isEnabled()) Serial.println("StopWatchToy: IMU unavailable.");
  ready = true;
}

void loop() {
  delay(2);
  if (!ready) {
    delay(100);
    return;
  }

  const uint32_t now = millis();
  const PowerEvent powerEvent = pollPowerKey(now);
  if (powerEvent == PowerEvent::Hold) {
    const bool wasSleeping = sleeping;
    setSleeping(true);
    Serial.println("StopWatchToy: power off");
    if (M5.Power.M5pm1.powerOff()) {
      delay(250);
    }
    Serial.println("StopWatchToy: power-off did not complete");
    setSleeping(wasSleeping);
    return;
  }
  if (powerEvent == PowerEvent::Click) {
    setSleeping(!sleeping);
    Serial.println(sleeping ? "StopWatchToy: sleeping" : "StopWatchToy: awake");
  }
  if (sleeping) {
    waitWhileSleeping();
    return;
  }

  M5.update();
  if (powerEvent == PowerEvent::Click) {
    render(now);
    return;
  }
  const bool shaken = detectShake(now);
  if (M5.BtnA.wasPressed() || M5.BtnB.wasPressed()) {
    switchMode(now);
  } else {
    const auto& touch = M5.Touch.getDetail();
    const bool tapped = M5.Touch.getCount() && touch.wasPressed();
    if (mode == Mode::Sparkles) {
      if (tapped) {
        addSparkle(touch.x, touch.y, now);
        play(chime);
      }
      if (shaken) {
        for (int i = 0; i < 6; ++i) {
          const float angle = i * (2 * kPi / 6);
          addSparkle(centerX + int(109 * cosf(angle)), centerY + int(109 * sinf(angle)), now);
        }
        play(twinkle);
      }
    } else {
      if (shaken) {
        refillBubbles(now);
        dirty = true;
        play(spawn);
      }
      if (tapped) popBubble(touch.x, touch.y, now);
    }
  }
  render(now);
}
