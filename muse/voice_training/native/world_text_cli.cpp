// Export the same dictionary, text normalization and phoneme IDs as the device.
// This host utility does not synthesize audio or infer phoneme timing.
#include <algorithm>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <vector>
#include "sumi_speech_text.h"
extern "C" {
#include "saan_kanji.h"
#include "token_table.h"
#include "openjtalk/text2mecab.h"
}
std::vector<char> read_file(const char* path) {
  std::ifstream stream(path, std::ios::binary);
  if (!stream) throw std::runtime_error("Cannot open input file");
  return {std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>()};
}
int main(int argc, char** argv) {
  try {
    if (argc != 3) throw std::runtime_error("usage: world_text_cli dict.bin text.txt");
    const auto dict = read_file(argv[1]);
    auto input = read_file(argv[2]);
    if (input.empty() || input.size() > 32768 ||
        std::find(input.begin(), input.end(), '\0') != input.end())
      throw std::runtime_error("Invalid text length or embedded NUL");
    input.push_back('\0');
    const auto size = sumi_speech_readings(input.data(), nullptr, 0);
    if (size == SIZE_MAX) throw std::runtime_error("Reading expansion failed");
    std::vector<char> reading(size + 1);
    if (sumi_speech_readings(input.data(), reading.data(), reading.size()) != size ||
        !sumi_speech_text(reading.data())) throw std::runtime_error("Text normalization failed");
    std::vector<char> normalized(std::strlen(reading.data()) * 3 + 1);
    if (text2mecab(normalized.data(), normalized.size(), reading.data()) != TEXT2MECAB_RESULT_SUCCESS)
      throw std::runtime_error("Open JTalk normalization failed");
    jdict_t dictionary{};
    if (jdict_open(&dictionary, reinterpret_cast<const uint8_t*>(dict.data()), dict.size()) != 0 ||
        !saan_kanji_init()) throw std::runtime_error("Dictionary initialization failed");
    std::vector<uint8_t> arena(saan_kanji_workbytes());
    std::vector<int32_t> ids(2048);
    int32_t count = 0; int tokens = 0;
    const auto result = saan_kanji_to_ids(&dictionary, normalized.data(), std::strlen(normalized.data()),
        arena.data(), arena.size(), ids.data(), ids.size(), &count, &tokens);
    if (result != SAAN_KANJI_OK) throw std::runtime_error(saan_kanji_strerror(result));
    for (int32_t i = 0; i < count; ++i) {
      if (ids[i] < 0 || ids[i] >= LABEL_IDS_N_TOKENS) throw std::runtime_error("Invalid phoneme ID");
      std::cout << ids[i] << '\t' << kSaanTokens[ids[i]].name << '\n';
    }
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
