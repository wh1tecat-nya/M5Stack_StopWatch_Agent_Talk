#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>

static inline bool sumi_ascii_word(unsigned char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
        (c >= '0' && c <= '9') || c == '_';
}

/* Known product readings, before Open JTalk's full-width normalization.
 * out=NULL measures the required byte count, excluding the terminator.
 * Input/output must not overlap. SIZE_MAX means insufficient space/overflow. */
static inline size_t sumi_speech_readings(const char *text, char *out, size_t capacity) {
    static const struct { const char *word, *reading; } terms[] = {
        {"m5stack", "エムファイブスタック"},
        {"stopwatch", "ストップウォッチ"},
        {"muse", "ミューズ"},
    };
    size_t at = 0, written = 0;
    while (text[at]) {
        const char *piece = text + at;
        size_t consumed = 1, n = 1;
        if (!at || !sumi_ascii_word((unsigned char)text[at - 1])) {
            for (size_t i = 0; i < sizeof(terms) / sizeof(terms[0]); ++i) {
                size_t k = 0;
                while (terms[i].word[k]) {
                    unsigned char c = (unsigned char)text[at + k];
                    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
                    if (c != (unsigned char)terms[i].word[k]) break;
                    ++k;
                }
                if (!terms[i].word[k] && !sumi_ascii_word((unsigned char)text[at + k])) {
                    piece = terms[i].reading; n = strlen(piece); consumed = k; break;
                }
            }
        }
        if (written > SIZE_MAX - n - 1 || (out && written + n >= capacity)) return SIZE_MAX;
        if (out) memcpy(out + written, piece, n);
        written += n; at += consumed;
    }
    if (out) {
        if (written >= capacity) return SIZE_MAX;
        out[written] = '\0';
    }
    return written;
}

/* Prepare only the speech copy. Keep the chat/caption text untouched. */
static inline bool sumi_speech_text(char *text) {
    size_t read = 0, write = 0, size = strlen(text);
    while (read < size) {
        const unsigned char *p = (const unsigned char *)text + read;
        size_t n = p[0] < 0x80 ? 1 : (p[0] & 0xe0) == 0xc0 ? 2 :
            (p[0] & 0xf0) == 0xe0 ? 3 : (p[0] & 0xf8) == 0xf0 ? 4 : 0;
        if (!n || read + n > size) return false;
        uint32_t cp = p[0] & (n == 1 ? 0x7f : n == 2 ? 0x1f : n == 3 ? 0x0f : 7);
        for (size_t i = 1; i < n; ++i) {
            if ((p[i] & 0xc0) != 0x80) return false;
            cp = (cp << 6) | (p[i] & 0x3f);
        }
        if ((n == 2 && cp < 0x80) || (n == 3 && cp < 0x800) || (n == 4 && cp < 0x10000) ||
            (cp >= 0xd800 && cp <= 0xdfff) || cp > 0x10ffff) return false;
        bool decoration = (cp >= 0x1f000 && cp <= 0x1faff) ||
            (cp >= 0x2600 && cp <= 0x27bf) || (cp >= 0xfe00 && cp <= 0xfe0f) ||
            (cp >= 0xe0020 && cp <= 0xe007f) || cp == 0x2b1b || cp == 0x2b1c ||
            cp == 0x2b50 || cp == 0x2b55 || cp == 0x200b || cp == 0x200d || cp == 0x20e3;
        bool space = cp <= 0x20 || cp == 0x7f || cp == 0xa0 || cp == 0x3000 ||
            (cp < 0x80 && strchr("[]#_^$?*`", (int)cp));
        if (decoration || space) {
            if (write && text[write - 1] != ' ') text[write++] = ' ';
        } else {
            memmove(text + write, p, n); write += n;
        }
        read += n;
    }
    if (write && text[write - 1] == ' ') --write;
    text[write] = 0;
    return true;
}
