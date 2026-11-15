#include "sumi_ui.h"
#include "sumi_tts.h"
#include "sumi_chat.h"
#include "sumi_charge.h"
#include "sumi_noise_test.h"
#include "sumi_asr.h"
#include "sumi_panel_test.h"
#include "sumi_voice_options.h"
#include "sumi_phone.h"
#include "sumi_phone_audio.h"
#include "sumi_wifi.h"
#include "sumi_core.h"
#include "sumi_clock.h"
#include "sumi_assets.h"
#include "muse_state.h"
#include "muse_settings.h"
#include "muse_voice.h"
#include "muse_link.h"
#include "muse_wifi.h"
#include "muse_ble.h"
#include "lvgl.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "nvs.h"
#include <stdatomic.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

LV_FONT_DECLARE(muse_font_cjk_16);
LV_FONT_DECLARE(muse_font_latin_16);
LV_FONT_DECLARE(sumi_font_24);
LV_FONT_DECLARE(sumi_clock_64);
static lv_font_t font, settings_font;
static lv_obj_t *root, *canvas, *caption, *status, *meter, *rec, *rec_dot, *panel, *back, *value;
static uint16_t *pixels;
static lv_image_dsc_t badge;
static lv_obj_t *badge_image, *clock_label;
static nvs_handle_t settings;
static bool have_settings, paired, shown, menu, badge_clock;
static atomic_bool toggle, wake, menu_open;
static sumi_screen_t screen = SUMI_BADGE;
static uint32_t started, last_draw, caption_version;
static muse_mode_t previous_mode = MUSE_MODE_BOOT;
static char text[MUSE_CAPTION_MAX];
static int times[3] = {30, 60, 60}, avatar_light = 25, badge_light = 25;
static int page = -1;
static uint32_t charge_deadline;
static bool charge_restore_pending;
enum { WIFI_NETWORKS = 7, WIFI_SETUP = 9, WIFI_NETWORK = 22, WIFI_DELETE = 23, WIFI_RESULT = 24 };
static muse_wifi_saved_t wifi_networks[MUSE_WIFI_SAVED_MAX];
static char wifi_selection[MUSE_SSID_MAX + 1];
static const char *titles[] = {"音量", "アバターの明るさ", "バッジの明るさ",
    "アバターの表示時間", "バッジ明の表示時間", "バッジ暗の表示時間",
    "返答の声", "Wi-Fiを変更", "通話を終了", "Wi-Fi設定",
    "読み上げ速度", "マイク感度", "会話先", "録音を聞き返す", "充電速度", "充電音を確認", "音声回路を確認", "音声認識", "表示回路を確認", "送信設定", "接続モード", "スマホから設定",
    "保存済みWi-Fi", "Wi-Fiの削除", "Wi-Fi設定"};
static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }
static bool save(const char *key, int v) {
    if (!have_settings) return false;
    esp_err_t err = nvs_set_i32(settings, key, v);
    if (err == ESP_OK) err = nvs_commit(settings);
    if (err != ESP_OK) ESP_LOGE("sumi_ui", "settings save failed: %s", esp_err_to_name(err));
    return err == ESP_OK;
}
static int load(const char *key, int initial, int low, int high) {
    int32_t v = initial;
    if (have_settings) nvs_get_i32(settings, key, &v);
    return v >= low && v <= high ? v : initial;
}
static void set_screen(sumi_screen_t next) { screen = next; started = now_ms(); }
void sumi_ui_wake(void) { atomic_store(&wake, true); }
void sumi_ui_toggle_settings(void) { atomic_store(&toggle, true); }
bool sumi_ui_settings_open(void) { return atomic_load(&menu_open); }
static void plain(lv_obj_t *o) {
    lv_obj_remove_style_all(o);
    lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_text_font(o, panel && (o == panel || lv_obj_get_parent(o) != root) ? &settings_font : &font, 0);
    lv_obj_set_style_text_color(o, lv_color_hex(0x443847), 0);
}
static lv_obj_t *label(lv_obj_t *parent, const char *str, int y, int width) {
    lv_obj_t *o = lv_label_create(parent);
    plain(o); lv_obj_set_width(o, width);
    lv_obj_set_style_text_align(o, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_text(o, str);
    lv_obj_align(o, LV_ALIGN_TOP_MID, 0, y);
    return o;
}
static lv_obj_t *button(lv_obj_t *parent, const char *str, int y, int width,
                         lv_event_cb_t cb, intptr_t data) {
    lv_obj_t *o = lv_button_create(parent);
    plain(o); lv_obj_set_size(o, width, 64);
    lv_obj_align(o, LV_ALIGN_TOP_MID, 0, y);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(o, lv_color_hex(0xf0e9f3), 0);
    lv_obj_set_style_radius(o, 18, 0);
    lv_obj_add_event_cb(o, cb, LV_EVENT_CLICKED, (void *)data);
    lv_obj_t *l = label(o, str, 0, width - 12); lv_obj_center(l);
    return o;
}
static void show_page(int which);
static const char *noise_message(sumi_noise_state_t s) {
    const char *messages[] = {"USBをつないだまま\n音の変化を確認", "停止の準備中…",
        "音声回路 停止中（20秒）\n終了後、自動で再開", "音声回路を再開中…",
        "音声回路を再開しました", "停止・復帰を確認できません\nログを確認してください",
        "音声の復帰を再試行中\n戻らない場合は再起動"};
    return messages[s];
}
static void restore_charging(void) {
    if (!charge_restore_pending || (int32_t)(now_ms() - charge_deadline) < 0) return;
    if (sumi_charge_enable(true)) {
        charge_restore_pending = false;
        if (page == 15 && value) lv_label_set_text(value, "充電を再開しました");
    } else {
        charge_deadline = now_ms() + 1000;
        if (page == 15 && value) lv_label_set_text(value, "充電再開を確認できません\n自動で再試行します");
    }
}
static bool voice_busy(void) {
    muse_mode_t m = muse_state_mode(NULL);
    return sumi_panel_test_active() || sumi_noise_test_active() || muse_voice_in_call() || m == MUSE_MODE_LISTENING || m == MUSE_MODE_THINKING || m == MUSE_MODE_SPEAKING;
}

int sumi_ui_setting(unsigned key) {
    switch (key) {
    case SUMI_SETTING_MODE: return sumi_phone_mode();
    case SUMI_SETTING_CHAT: return sumi_chat_selected();
    case SUMI_SETTING_WEARABLE: return sumi_chat_wearable();
    case SUMI_SETTING_VOLUME: return muse_settings_volume();
    case SUMI_SETTING_MIC_GAIN: return muse_settings_mic_gain();
    case SUMI_SETTING_TTS_RATE: return sumi_tts_rate();
    case SUMI_SETTING_WIFI_ASR: return sumi_asr_enabled();
    case SUMI_SETTING_AVATAR_LIGHT: return avatar_light;
    case SUMI_SETTING_BADGE_LIGHT: return badge_light;
    case SUMI_SETTING_AVATAR_TIME:
    case SUMI_SETTING_BADGE_TIME:
    case SUMI_SETTING_DIM_TIME: return times[key - SUMI_SETTING_AVATAR_TIME];
    case SUMI_SETTING_CHARGE: return sumi_charge_current();
    case SUMI_SETTING_WIFI_SETUP: return sumi_wifi_active();
    default: return 0;
    }
}

int sumi_ui_apply_setting(unsigned key, int32_t value) {
    if (key == SUMI_SETTING_WIFI_SETUP && value == 0) { sumi_wifi_cancel(); return 0; }
    if (voice_busy() || muse_voice_notes_waiting() || sumi_wifi_active() || charge_restore_pending) return 2;
    switch (key) {
    case SUMI_SETTING_MODE:
        if (value != 0 && value != 1) return 3;
        return sumi_phone_set_mode(value != 0) ? 0 : 4;
    case SUMI_SETTING_CHAT:
    case SUMI_SETTING_WEARABLE:
        if (value != 0 && value != 1) return 3;
        if (!save(key == SUMI_SETTING_CHAT ? "chat" : "wearable", value)) return 4;
        if (key == SUMI_SETTING_CHAT) sumi_chat_select(value);
        else sumi_chat_set_wearable(value);
        return 0;
    case SUMI_SETTING_VOLUME:
        if (value < 0 || value > 100) return 3;
        muse_settings_set_volume(value);
        return 0;
    case SUMI_SETTING_MIC_GAIN:
        if (value < 0 || value > 36 || value % 3) return 3;
        muse_settings_set_mic_gain(value);
        return 0;
    case SUMI_SETTING_TTS_RATE:
        if (value < 60 || value > 120 || value % 10) return 3;
        if (!save("tts_rate", value)) return 4;
        sumi_tts_set_rate(value);
        return 0;
    case SUMI_SETTING_WIFI_ASR:
        if (value != 0 && value != 1) return 3;
        return sumi_asr_select(value != 0) ? 0 : 4;
    case SUMI_SETTING_AVATAR_LIGHT:
    case SUMI_SETTING_BADGE_LIGHT:
        if (value < 1 || value > 100) return 3;
        if (!save(key == SUMI_SETTING_AVATAR_LIGHT ? "avatar_light" : "badge_light", value)) return 4;
        if (key == SUMI_SETTING_AVATAR_LIGHT) avatar_light = value;
        else badge_light = value;
        return 0;
    case SUMI_SETTING_AVATAR_TIME:
    case SUMI_SETTING_BADGE_TIME:
    case SUMI_SETTING_DIM_TIME: {
        static const char *const keys[] = {"avatar_s", "badge_s", "dim_s"};
        if (value < 15 || value > 60 || value % 15) return 3;
        unsigned index = key - SUMI_SETTING_AVATAR_TIME;
        if (!save(keys[index], value)) return 4;
        times[index] = value;
        return 0;
    }
    case SUMI_SETTING_CHARGE: {
        if (value != 185 && value != 425) return 3;
        int previous = sumi_charge_current();
        if (!sumi_charge_set_current(value)) return 4;
        if (save("charge_ma", value)) return 0;
        if (previous == 185 || previous == 425) sumi_charge_set_current(previous);
        return 4;
    }
    case SUMI_SETTING_WIFI_SETUP:
        if (value != 1 || sumi_phone_mode()) return 3;
        if (!sumi_wifi_request()) return 2;
        menu = true;
        atomic_store(&menu_open, true);
        show_page(WIFI_SETUP);
        return 0;
    default: return 3;
    }
}
static void choose(lv_event_t *e) {
    int v = (int)(intptr_t)lv_event_get_user_data(e);
    muse_state_poke();
    if (page >= 3 && page <= 5) {
        times[page - 3] = v;
        const char *keys[] = {"avatar_s", "badge_s", "dim_s"};
        save(keys[page - 3], v); show_page(-1);
    } else if (page == 6) {
        if (voice_busy()) { lv_label_set_text(value, "会話が終わってから変更してね"); return; }
        if (!v) sumi_native_voice_retry();
        muse_settings_set_voice_notes(v != 0);
        muse_settings_set_speaker_on(true);
        show_page(-1);
    } else if (page == 12) {
        if (voice_busy() || muse_voice_notes_waiting()) { lv_label_set_text(value, "会話・未送信の音声が\nなくなってから変更できます"); return; }
        sumi_chat_select(v); save("chat", v); show_page(-1);
    } else if (page == 13) {
        if (voice_busy() || muse_voice_notes_waiting()) return;
        sumi_mic_test_request();
    } else if (page == 14) {
        if (!sumi_charge_set_current(v)) {
            lv_label_set_text(value, "充電設定を確認できません\nもう一度選んでください"); return;
        }
        save("charge_ma", v); show_page(14);
    } else if (page == 15) {
        if (!v) { charge_deadline = now_ms(); restore_charging(); return; }
        if (charge_restore_pending) return;
        if (!muse_state_power().usb) { lv_label_set_text(value, "USBを接続してから\n開始してください"); return; }
        charge_restore_pending = true;
        charge_deadline = now_ms() + 20000;
        if (sumi_charge_enable(false)) lv_label_set_text(value, "充電停止中（20秒）\n終了後、自動で再開");
        else { charge_deadline = now_ms(); restore_charging(); }
    } else if (page == 16) {
        if (!v) { sumi_noise_test_cancel(); return; }
        if (!muse_state_power().usb) { lv_label_set_text(value, "USBを接続してから\n開始してください"); return; }
        if (voice_busy() || muse_voice_notes_waiting()) { lv_label_set_text(value, "会話が終わってから\n開始してください"); return; }
        sumi_noise_test_request();
    } else if (page == 17) {
        if (voice_busy() || muse_voice_notes_waiting()) { lv_label_set_text(value, "会話が終わってから\n変更してください"); return; }
        if (!sumi_asr_select(v != 0)) { lv_label_set_text(value, "APIキー未設定、または\n設定を保存できません"); return; }
        show_page(17);
    } else if (page == 18) {
        if (!muse_state_power().usb) { lv_label_set_text(value, "USBを接続してから\n開始してください"); return; }
        if (voice_busy() || muse_voice_notes_waiting() || charge_restore_pending) {
            lv_label_set_text(value, "会話・充電の比較を\n終えてから開始してね"); return;
        }
        sumi_panel_test_request();
    } else if (page == 19) {
        if (voice_busy() || muse_voice_notes_waiting()) {
            lv_label_set_text(value, "会話・未送信の音声が\nなくなってから変更できます"); return;
        }
        if (!save("wearable", v != 0)) {
            lv_label_set_text(value, "設定を保存できません\nもう一度選んでください"); return;
        }
        sumi_chat_set_wearable(v != 0); show_page(19);
    } else if (page == 20) {
        if (voice_busy() || muse_voice_notes_waiting() || sumi_wifi_active()) {
            lv_label_set_text(value, "会話・Wi-Fi設定を\n終えてから変更してください"); return;
        }
        if (!sumi_phone_set_mode(v != 0)) {
            lv_label_set_text(value, "設定を保存できません\nもう一度選んでください"); return;
        }
        show_page(20);
    } else if (page == 21) {
        sumi_phone_open_settings();
        lv_label_set_text(value, "5分間、スマホから\n設定を変更できます");
    } else if (page == 8) {
        sumi_input_end_call(); show_page(-1);
    } else show_page(v);
}
static void slider(lv_event_t *e) {
    int v = lv_slider_get_value(lv_event_get_target_obj(e));
    bool released = lv_event_get_code(e) == LV_EVENT_RELEASED;
    muse_state_poke();
    if (page == 10) {
        lv_label_set_text_fmt(value, "%.1f 倍", v / 10.0);
        if (released) { sumi_tts_set_rate(v * 10); save("tts_rate", v * 10); }
        return;
    }
    if (page == 11) {
        lv_label_set_text_fmt(value, "%d dB", v * 3);
        if (released) muse_settings_set_mic_gain(v * 3);
        return;
    }
    if (page == 0) { if (released) muse_settings_set_volume(v); }
    else if (page == 1) avatar_light = v;
    else badge_light = v;
    lv_label_set_text_fmt(value, "%d %%", v);
    if (released && page > 0) save(page == 1 ? "avatar_light" : "badge_light", v);
}
static void choose_wifi(lv_event_t *e) {
    int index = (int)(intptr_t)lv_event_get_user_data(e);
    muse_state_poke();
    if (page == WIFI_NETWORKS && index >= 0) {
        strlcpy(wifi_selection, wifi_networks[index].ssid, sizeof(wifi_selection));
        show_page(WIFI_NETWORK);
        return;
    }
    if (page == WIFI_NETWORK && index == 1) {
        show_page(WIFI_DELETE);
        return;
    }
    if (sumi_phone_mode() || voice_busy() || muse_voice_notes_waiting() || sumi_wifi_active()) {
        lv_label_set_text(value, "会話・Wi-Fi設定を\n終えてから操作してください");
        return;
    }
    if (page == WIFI_NETWORKS) {
        if (sumi_wifi_request()) show_page(WIFI_SETUP);
    } else if (page == WIFI_NETWORK) {
        if (sumi_wifi_select(wifi_selection)) show_page(WIFI_RESULT);
    } else if (page == WIFI_DELETE) {
        if (sumi_wifi_forget(wifi_selection)) show_page(WIFI_RESULT);
    }
}

static void show_wifi_networks(void) {
    button(panel, "Wi-Fiを追加・更新", 48, 300, choose_wifi, -1);
    value = label(panel, "", 115, 320);
    int count = muse_wifi_saved(wifi_networks, MUSE_WIFI_SAVED_MAX);
    if (!count) {
        lv_label_set_text(value, "保存済みWi-Fiはありません");
        return;
    }
    lv_label_set_text_fmt(value, "保存済み %d/%d 件", count, MUSE_WIFI_SAVED_MAX);
    lv_obj_t *list = lv_obj_create(panel);
    plain(list);
    lv_obj_align(list, LV_ALIGN_TOP_MID, 0, 152);
    lv_obj_set_size(list, 340, 142);
    lv_obj_add_flag(list, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_scroll_dir(list, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(list, LV_SCROLLBAR_MODE_ACTIVE);
    muse_wifi_status_t wifi;
    muse_wifi_status(&wifi);
    for (int i = 0; i < count; ++i) {
        char name[MUSE_SSID_MAX + 24];
        bool connected = wifi.state == MUSE_WIFI_CONNECTED && strcmp(wifi.ssid, wifi_networks[i].ssid) == 0;
        snprintf(name, sizeof(name), "%s%.*s", connected ? "接続中: " : "", MUSE_SSID_MAX, wifi_networks[i].ssid);
        lv_obj_t *row = button(list, name, i * 72, 300, choose_wifi, i);
        lv_label_set_long_mode(lv_obj_get_child(row, 0), LV_LABEL_LONG_MODE_DOTS);
    }
}

static void go_back(lv_event_t *e) {
    (void)e; muse_state_poke();
    if (page == WIFI_SETUP) sumi_wifi_cancel();
    if (page == WIFI_RESULT && sumi_wifi_active()) return;
    if (page == WIFI_DELETE) show_page(WIFI_NETWORK);
    else if (page == WIFI_SETUP || page == WIFI_NETWORK || page == WIFI_RESULT) show_page(WIFI_NETWORKS);
    else if (page >= 0) show_page(-1);
    else { menu = false; atomic_store(&menu_open, false); lv_obj_add_flag(panel, LV_OBJ_FLAG_HIDDEN);
           lv_obj_add_flag(back, LV_OBJ_FLAG_HIDDEN); set_screen(SUMI_BADGE); }
}
static void show_page(int which) {
    page = which; lv_obj_clean(panel); value = NULL;
    if (which < 0) {
        label(panel, "設定", 3, 280);
        lv_obj_t *list = lv_obj_create(panel); plain(list);
        lv_obj_set_pos(list, 0, 42); lv_obj_set_size(list, 340, 252);
        lv_obj_add_flag(list, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_scroll_dir(list, LV_DIR_VER);
        lv_obj_set_scrollbar_mode(list, LV_SCROLLBAR_MODE_ACTIVE);
        const int order[] = {20, WIFI_NETWORKS, 21, 12, 19, 17, 10, 0, 11, 13, 18, 16, 14, 15, 1, 2, 3, 4, 5};
        bool phone = sumi_phone_mode();
        int row = 0;
        for (unsigned i = 0; i < sizeof(order) / sizeof(order[0]); ++i) {
            int item = order[i]; char title[96];
            if (phone && (item == WIFI_NETWORKS || item == 10 || item == 17)) continue;
            if (item == 12) snprintf(title, sizeof(title), "会話先: %s", sumi_chat_selected() ? "M5Stack" : "メイン");
            else if (item == 19) snprintf(title, sizeof(title), "送信設定: %s", sumi_chat_wearable() ? "ON" : "OFF");
            else if (item == 17) snprintf(title, sizeof(title), "音声認識: %s", sumi_asr_enabled() ? "OpenAI" : "Muse");
            else if (item == 20) snprintf(title, sizeof(title), "接続モード: %s", phone ? "BLE" : "Wi-Fi");
            else snprintf(title, sizeof(title), "%s", titles[item]);
            button(list, title, 8 + row++ * 74, 300, choose, item);
        }
        return;
    }
    label(panel, which == WIFI_SETUP ? "スマートフォンでWi-Fi設定" : titles[which], 8, 320);
    if (which <= 2 || which == 10 || which == 11) {
        int v = which == 10 ? sumi_tts_rate() / 10 : which == 11 ? muse_settings_mic_gain() / 3 :
            which == 0 ? muse_settings_volume() : which == 1 ? avatar_light : badge_light;
        value = label(panel, "", 80, 300);
        if (which == 10) lv_label_set_text_fmt(value, "%.1f 倍", v / 10.0);
        else lv_label_set_text_fmt(value, which == 11 ? "%d dB" : "%d %%", which == 11 ? v * 3 : v);
        lv_obj_t *s = lv_slider_create(panel);
        lv_obj_set_size(s, 280, 24); lv_obj_align(s, LV_ALIGN_TOP_MID, 0, 164);
        lv_obj_set_style_pad_all(s, 18, LV_PART_KNOB);
        lv_slider_set_range(s, which == 10 ? 6 : which == 11 || which == 0 ? 0 : 1, which == 10 || which == 11 ? 12 : 100); lv_slider_set_value(s, v, LV_ANIM_OFF);
        lv_obj_add_event_cb(s, slider, LV_EVENT_VALUE_CHANGED, NULL);
        lv_obj_add_event_cb(s, slider, LV_EVENT_RELEASED, NULL);
        if (which == 10) label(panel, "小さい値ほどゆっくり\n次の返答から適用", 220, 320);
        if (which == 11) label(panel, "録音を聞き返して調整\n大きすぎると音が割れます", 220, 320);
    } else if (which <= 5) {
        for (int i = 1; i <= 4; ++i) {
            char s[24]; snprintf(s, sizeof(s), "%d 秒%s", i * 15, times[which - 3] == i * 15 ? "  *" : "");
            lv_obj_t *b = button(panel, s, 78 + ((i - 1) / 2) * 88, 140, choose, i * 15);
            lv_obj_set_x(b, (i % 2 ? -80 : 80));
        }
    } else if (which == 6) {
        button(panel, "Museの声を試す", 58, 300, choose, 0);
        button(panel, "sanoTTS-jp（本体）", 128, 280, choose, 1);
        value = label(panel, voice_busy() ? "会話が終わってから変更してね" : "Museが404なら本体へ切替", 214, 320);
    } else if (which == WIFI_NETWORKS) {
        show_wifi_networks();
    } else if (which == 8) {
        label(panel, "Museとの音声通話を終了します。", 82, 320);
        button(panel, "通話を終了", 174, 260, choose, 0);
    } else if (which == 12) {
        button(panel, "M5Stack会話用", 56, 300, choose, 1);
        button(panel, "メインチャット", 132, 300, choose, 0);
        value = label(panel, sumi_chat_selected() ? "選択中: M5Stack会話用" : "選択中: メインチャット", 222, 320);
    } else if (which == 13) {
        label(panel, "5秒間録音し、再生します\nMuseには送信しません", 66, 320);
        button(panel, "録音テストを開始", 180, 300, choose, 0);
    } else if (which == 14) {
        button(panel, "標準 185 mA", 56, 300, choose, 185);
        button(panel, "高速 425 mA", 132, 300, choose, 425);
        int current = sumi_charge_current();
        value = label(panel, current == 185 ? "選択中: 標準\nゆっくり充電" :
            current == 425 ? "選択中: 高速" : "現在の設定を確認できません", 222, 320);
    } else if (which == 15) {
        button(panel, "20秒止めて比較", 56, 300, choose, 1);
        button(panel, "今すぐ充電を再開", 132, 300, choose, 0);
        value = label(panel, charge_restore_pending ? "充電停止中\n終了後、自動で再開" :
            "USBをつないだまま\n音の変化を確認", 222, 320);
    } else if (which == 16) {
        button(panel, "20秒止めて比較", 56, 300, choose, 1);
        button(panel, "今すぐ音声を再開", 132, 300, choose, 0);
        value = label(panel, noise_message(sumi_noise_test_state()), 222, 320);
    } else if (which == 17) {
        button(panel, "OpenAI（日本語）", 56, 300, choose, 1);
        button(panel, "Muse", 132, 300, choose, 0);
        value = label(panel, sumi_asr_enabled() ? "選択中: OpenAI\n返答は本体で読み上げ" :
            sumi_asr_configured() ? "選択中: Muse\nOpenAIキー設定済み" : "選択中: Muse\nOpenAIキー未設定", 222, 320);
    } else if (which == 18) {
        label(panel, "画面の電源を20秒停止\n音の変化を確認します\n自動で画面が戻ります", 58, 320);
        button(panel, "20秒消して比較", 158, 300, choose, 1);
        value = label(panel, sumi_panel_test_state() == 4 ? "表示回路を再開しました" : "USBをつないだまま開始", 240, 320);
    } else if (which == 19) {
        label(panel, "mcu-wearable", 48, 320);
        button(panel, "ON：端末指定を付ける", 91, 300, choose, 1);
        button(panel, "OFF：端末指定なし", 163, 300, choose, 0);
        value = label(panel, sumi_chat_wearable() ? "選択中: ON" : "選択中: OFF", 246, 320);
    } else if (which == 20) {
        button(panel, "Wi-Fi（本体）", 56, 300, choose, 0);
        button(panel, "BLE（スマホ）", 132, 300, choose, 1);
        value = label(panel, sumi_phone_mode() ? "選択中: BLE\nスマホのIrodoriで読み上げ" :
            "選択中: Wi-Fi\n本体のsanoTTSで読み上げ", 216, 320);
    } else if (which == 21) {
        button(panel, "スマホ接続を開始", 75, 300, choose, 0);
        value = label(panel, "スマホアプリから\n本体設定を変更できます", 178, 320);
    } else if (which == WIFI_NETWORK || which == WIFI_DELETE) {
        lv_obj_t *ssid = label(panel, wifi_selection, 48, 320);
        lv_obj_set_style_text_font(ssid, &font, 0);
        lv_label_set_long_mode(ssid, LV_LABEL_LONG_MODE_WRAP);
        if (which == WIFI_NETWORK) {
            button(panel, "このWi-Fiに接続", 91, 300, choose_wifi, 0);
            button(panel, "保存済みWi-Fiを削除", 165, 300, choose_wifi, 1);
        } else {
            label(panel, "このWi-Fiを削除しますか？", 93, 320);
            button(panel, "削除する", 165, 300, choose_wifi, 0);
        }
        value = label(panel, "", 230, 320);
        lv_obj_set_style_text_font(value, &font, 0);
    } else value = label(panel, "準備中…", 62, 320);
}
static void tapped(lv_event_t *e) {
    (void)e;
    if (!shown || menu || muse_state_asleep() ||
        (screen != SUMI_BADGE && screen != SUMI_DIM)) return;
    muse_state_poke();
    badge_clock = !badge_clock;
    set_screen(SUMI_BADGE);
}
static void ellipse(int x, int y, int rx, int ry, uint16_t color) {
    for (int dy = -ry; dy <= ry; ++dy) {
        int yy = y + dy;
        if (yy < 0 || yy >= 360) continue;
        int span = (int)lroundf(rx * sqrtf(1.0f - (float)(dy * dy) / (ry * ry)));
        for (int dx = -span; dx <= span; ++dx) {
            int xx = x + dx;
            if (xx >= 0 && xx < 400) pixels[yy * 400 + xx] = color;
        }
    }
}
static void draw_avatar(muse_mode_t mode, uint32_t now) {
    static bool valid, last_blink, last_thinking;
    static int last_mouth;
    bool thinking = mode == MUSE_MODE_THINKING;
    bool blink = !thinking && now % 4000 >= 3850;
    int mouth = mode == MUSE_MODE_SPEAKING ? (int)lroundf(fminf(muse_state_level() * 16, 6)) : 0;
    /* Identical poses need no canvas rewrite or full-panel repaint.
       Repeated PSRAM traffic otherwise competes with speech synthesis. */
    if (valid && !thinking && !last_thinking && blink == last_blink && mouth == last_mouth) return;
    valid = true; last_thinking = thinking; last_blink = blink; last_mouth = mouth;
    for (int i = 0; i < 400 * 360; ++i) pixels[i] = 0xffff;
    float amplitude = mode == MUSE_MODE_THINKING ? 14.0f * sinf(now * 6.2831853f / 2800) : 0;
    for (int y = 0; y < 360; ++y) {
        float p = 1.0f - y / 359.0f;
        int shift = (int)lroundf(amplitude * p * p * (3 - 2 * p));
        memcpy(pixels + y * 400 + 20 + shift, sumi_portrait + y * 360, 720);
    }
    if (blink) {
        ellipse(180, 171, 19, 14, 0xff1b); ellipse(242, 173, 19, 14, 0xff1b);
        ellipse(180, 171, 14, 1, 0x59e8); ellipse(242, 173, 14, 1, 0x59e8);
    }
    if (mode == MUSE_MODE_SPEAKING) {
        if (mouth) { ellipse(209, 207, 12, 8, 0xff7c); ellipse(209, 207, 8, mouth, 0x8a69); }
    }
    lv_obj_invalidate(root);  /* Whole-panel repaint avoids AMOLED partial-flush seams. */
}
void sumi_ui_init(void) {
    sumi_asr_init();
    have_settings = nvs_open("sumi-muse", NVS_READWRITE, &settings) == ESP_OK;
    if (!load("configured", 0, 0, 1)) {
        muse_settings_set_volume(86); /* Existing 220/255 default, as percent. */
        muse_settings_set_speaker_on(true);
        save("configured", 1);
    }
    avatar_light = load("avatar_light", 25, 1, 100); badge_light = load("badge_light", 25, 1, 100);
    times[0] = load("avatar_s", 30, 15, 60); times[1] = load("badge_s", 60, 15, 60); times[2] = load("dim_s", 60, 15, 60);
    for (int i = 0; i < 3; ++i) if (times[i] % 15) times[i] = 30;
    sumi_tts_set_rate(load("tts_rate", 80, 60, 120));
    sumi_chat_select(load("chat", 1, 0, 1));
    sumi_chat_set_wearable(load("wearable", 1, 0, 1) != 0);
    int charge = load("charge_ma", 425, 185, 425);
    sumi_charge_set_current(charge == 185 ? 185 : 425);
    font = muse_font_cjk_16; font.fallback = &muse_font_latin_16;
    settings_font = sumi_font_24; settings_font.fallback = &font;
    root = lv_obj_create(lv_screen_active()); plain(root);
    lv_obj_set_size(root, 466, 466); lv_obj_set_pos(root, 0, 0);
    lv_obj_set_style_bg_color(root, lv_color_white(), 0); lv_obj_set_style_bg_opa(root, LV_OPA_COVER, 0);
    lv_obj_add_flag(root, LV_OBJ_FLAG_CLICKABLE); lv_obj_add_event_cb(root, tapped, LV_EVENT_CLICKED, NULL);
    pixels = heap_caps_malloc(400 * 360 * 2, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!pixels) { lv_obj_delete(root); root = NULL; ESP_LOGE("sumi_ui", "avatar buffer unavailable"); return; }
    canvas = lv_canvas_create(root); lv_canvas_set_buffer(canvas, pixels, 400, 360, LV_COLOR_FORMAT_RGB565);
    lv_obj_set_pos(canvas, 33, 106); lv_obj_remove_flag(canvas, LV_OBJ_FLAG_CLICKABLE);
    badge.header.magic = LV_IMAGE_HEADER_MAGIC; badge.header.cf = LV_COLOR_FORMAT_RGB565;
    badge.header.w = badge.header.h = 400; badge.header.stride = 800;
    badge.data_size = 400 * 400 * 2; badge.data = (const uint8_t *)sumi_badge;
    badge_image = lv_image_create(root); lv_image_set_src(badge_image, &badge); lv_obj_center(badge_image);
    lv_obj_remove_flag(badge_image, LV_OBJ_FLAG_CLICKABLE);
    clock_label = label(root, "--:--", 73, 300);
    lv_obj_set_style_text_font(clock_label, &sumi_clock_64, 0);
    lv_obj_add_flag(clock_label, LV_OBJ_FLAG_HIDDEN);
    caption = label(root, "", 63, 324); lv_obj_set_height(caption, LV_SIZE_CONTENT);
    lv_obj_set_style_min_height(caption, 66, 0);
    lv_obj_set_style_pad_all(caption, 12, 0); lv_obj_set_style_text_line_space(caption, 6, 0);
    lv_obj_set_style_bg_color(caption, lv_color_white(), 0); lv_obj_set_style_bg_opa(caption, LV_OPA_COVER, 0);
    lv_obj_set_style_border_color(caption, lv_color_hex(0xb99da8), 0); lv_obj_set_style_border_width(caption, 2, 0);
    lv_obj_set_style_radius(caption, 18, 0); lv_label_set_long_mode(caption, LV_LABEL_LONG_MODE_WRAP);
    status = label(root, "", 33, 300);
    lv_obj_set_style_text_font(status, &lv_font_montserrat_20, 0);
    rec = label(root, "REC", 135, 120); lv_obj_set_style_text_color(rec, lv_color_hex(0xd12d4c), 0);
    rec_dot = lv_obj_create(root); plain(rec_dot); lv_obj_set_size(rec_dot, 8, 8);
    lv_obj_set_pos(rec_dot, 204, 139); lv_obj_set_style_radius(rec_dot, 4, 0);
    lv_obj_set_style_bg_color(rec_dot, lv_color_hex(0xd12d4c), 0); lv_obj_set_style_bg_opa(rec_dot, LV_OPA_COVER, 0);
    meter = lv_bar_create(root); lv_obj_set_size(meter, 150, 6); lv_obj_align(meter, LV_ALIGN_TOP_MID, 0, 157);
    panel = lv_obj_create(root); plain(panel); lv_obj_set_size(panel, 340, 298); lv_obj_set_pos(panel, 63, 52);
    lv_obj_set_style_text_font(panel, &settings_font, 0);
    lv_obj_set_style_bg_color(panel, lv_color_white(), 0); lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
    back = button(root, "戻る", 354, 260, go_back, 0);
    lv_obj_set_style_text_font(lv_obj_get_child(back, 0), &settings_font, 0);
    lv_obj_add_flag(panel, LV_OBJ_FLAG_HIDDEN); lv_obj_add_flag(back, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(root, LV_OBJ_FLAG_HIDDEN); started = now_ms();
    muse_state_set_page(36, 2); muse_state_set_cjk_page(18, 2); muse_state_set_latin_page(36, 2);
}
void sumi_ui_prepare(void) {
    sumi_phone_apply_setting();
    restore_charging(); /* Also while hidden, asleep or disconnected from Muse. */
    sumi_clock_poll(muse_wifi_connected());
    static sumi_noise_state_t noise_previous = SUMI_NOISE_IDLE;
    sumi_noise_state_t noise = sumi_noise_test_state();
    if (page == 16 && value && noise != noise_previous) lv_label_set_text(value, noise_message(noise));
    noise_previous = noise;
    if (page == 18 && value && sumi_panel_test_state() == 4 &&
        strcmp(lv_label_get_text(value), "表示回路を再開しました"))
        lv_label_set_text(value, "表示回路を再開しました");
    if (!root) return;
    paired = muse_link_hatch_linked();
    muse_ble_status_t ble; muse_ble_status(&ble);
    shown = paired && !ble.passkey && muse_link_state() != MUSE_LINK_CONFIRM;
    lv_obj_set_flag(root, LV_OBJ_FLAG_HIDDEN, !shown);
    if (!shown) return;
    uint32_t now = now_ms();
    muse_mode_t mode = muse_state_mode(NULL);
    if (atomic_exchange(&wake, false)) {
        menu = false; atomic_store(&menu_open, false); badge_clock = false; set_screen(SUMI_BADGE);
    }
    if (mode != previous_mode && (mode == MUSE_MODE_LISTENING || mode == MUSE_MODE_SPEAKING)) {
        muse_state_set_asleep(false); menu = false; atomic_store(&menu_open, false); set_screen(SUMI_AVATAR);
    }
    previous_mode = mode;
    bool busy = mode == MUSE_MODE_LISTENING || mode == MUSE_MODE_THINKING || mode == MUSE_MODE_SPEAKING;
    if (busy && !muse_state_asleep()) { screen = SUMI_AVATAR; started = now; }
    if (atomic_exchange(&toggle, false) && !muse_state_asleep()) {
        menu = !menu; atomic_store(&menu_open, menu); if (menu) show_page(-1); else set_screen(SUMI_BADGE);
    }
    if (menu || sumi_wifi_active()) { muse_state_poke(); started = now; }
    else if (!muse_state_asleep()) {
        sumi_screen_t next = sumi_next_screen(screen, busy, now - started, times);
        if (screen != next) set_screen(next);
        if (screen == SUMI_OFF) muse_state_set_asleep(true);
    }
    lv_obj_set_flag(panel, LV_OBJ_FLAG_HIDDEN, !menu); lv_obj_set_flag(back, LV_OBJ_FLAG_HIDDEN, !menu);
}
int sumi_ui_brightness(int fallback) {
    if (!shown) return fallback;
    if (menu) return page == 2 ? badge_light : avatar_light;
    return screen == SUMI_AVATAR ? avatar_light : screen == SUMI_DIM ? 2 : badge_light;
}
bool sumi_ui_tick(void) {
    if (!shown || !root) return false;
    uint32_t now = now_ms(); muse_mode_t mode = muse_state_mode(NULL);
    bool avatar = !menu && screen == SUMI_AVATAR;
    lv_obj_set_flag(canvas, LV_OBJ_FLAG_HIDDEN, !avatar);
    bool badge_visible = !menu && (screen == SUMI_BADGE || screen == SUMI_DIM);
    lv_obj_set_flag(badge_image, LV_OBJ_FLAG_HIDDEN, !badge_visible);
    lv_image_set_scale(badge_image, badge_clock ? 179 : 256);
    lv_obj_align(badge_image, LV_ALIGN_CENTER, 0, badge_clock ? 70 : 0);
    lv_obj_set_flag(clock_label, LV_OBJ_FLAG_HIDDEN, !badge_visible || !badge_clock);
    if (badge_visible && badge_clock) {
        char clock_text[6]; sumi_clock_format(time(NULL), clock_text);
        if (strcmp(clock_text, lv_label_get_text(clock_label))) lv_label_set_text(clock_label, clock_text);
    }
    lv_obj_set_flag(caption, LV_OBJ_FLAG_HIDDEN, !avatar);
    bool recording = avatar && mode == MUSE_MODE_LISTENING;
    lv_obj_set_flag(rec, LV_OBJ_FLAG_HIDDEN, !recording); lv_obj_set_flag(rec_dot, LV_OBJ_FLAG_HIDDEN, !recording);
    lv_obj_set_flag(meter, LV_OBJ_FLAG_HIDDEN, !recording);
    if (recording) lv_bar_set_value(meter, (int)(muse_state_level() * 100), LV_ANIM_OFF);
    lv_obj_set_flag(status, LV_OBJ_FLAG_HIDDEN, menu || screen == SUMI_OFF);
    muse_power_t power = muse_state_power();
    const char *battery = power.battery_pct < 10 ? LV_SYMBOL_BATTERY_EMPTY :
        power.battery_pct < 35 ? LV_SYMBOL_BATTERY_1 : power.battery_pct < 60 ? LV_SYMBOL_BATTERY_2 :
        power.battery_pct < 85 ? LV_SYMBOL_BATTERY_3 : LV_SYMBOL_BATTERY_FULL;
    char info[80], percent[16];
    if (power.battery_pct < 0) snprintf(percent, sizeof(percent), "--%%");
    else snprintf(percent, sizeof(percent), "%d%%", power.battery_pct);
    bool phone = sumi_phone_mode();
    snprintf(info, sizeof(info), "%s %s %s   %s%s", battery, percent,
             power.charging ? LV_SYMBOL_CHARGE : "", phone ? LV_SYMBOL_BLUETOOTH : LV_SYMBOL_WIFI,
             (phone ? sumi_phone_ready() : muse_wifi_connected()) ? "" : " x");
    if (strcmp(info, lv_label_get_text(status))) lv_label_set_text(status, info);
    muse_state_caption(text, sizeof(text), &caption_version);
    const char *idle = "左ボタンを押して話しかけてね";
    if (text[0] && strcmp(text, "HOLD YELLOW TO TALK") && strcmp(text, "CALL ENDED")) idle = text;
    if (!strcmp(text, "HOLD LONGER TO TALK")) idle = "ボタンを押したまま話しかけてね";
    const char *line = mode == MUSE_MODE_LISTENING ?
        (sumi_mic_test_active() ? "録音テスト中（5秒）" : muse_state_progress() >= 1 ? "15秒録音したよ\nボタンを離して送信してね" : "お話を聞いてるよ") :
        mode == MUSE_MODE_THINKING ? (phone && text[0] ? text : "お返事を考えてるよ") :
        (mode == MUSE_MODE_SPEAKING || mode == MUSE_MODE_ERROR) && text[0] ? text : idle;
    if (strcmp(line, lv_label_get_text(caption))) lv_label_set_text(caption, line);
    if (menu && (page == WIFI_SETUP || page == WIFI_RESULT) && value) {
        char info[256]; sumi_wifi_status(info, sizeof(info));
        if (strcmp(info, lv_label_get_text(value))) lv_label_set_text(value, info);
    }
    if (avatar && now - last_draw >= 50) { draw_avatar(mode, now); last_draw = now; }
    return true;
}
