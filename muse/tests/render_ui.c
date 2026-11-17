#include "ui_platform.h"
#include "lvgl.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
static time_t clock_utc = 1794380700; /* 2026-11-11 16:05 JST */
static time_t test_time(time_t *out) { if(out)*out=clock_utc; return clock_utc; }
#define time test_time
#include "../overlay/sumi_ui.c"
#undef time
void sumi_clock_poll(bool connected) { (void)connected; }
const esp_app_desc_t *esp_app_get_description(void) {
    static const esp_app_desc_t desc = {.version = "0.1.6-test"};
    return &desc;
}


static uint32_t clock_ms;
static bool sleep_state, call, notes;
static muse_mode_t mode = MUSE_MODE_IDLE;
static char reply[400] = "こんにちは。今日も一緒に、\nゆっくりお話ししようね。";
static int charge=425, saved_charge, saved_wearable=-1;
static bool save_fail, wifi_connected=true;
static bool phone_mode, phone_ready;
static bool wifi_setting;
static const char *wifi_operation;
static char wifi_target[MUSE_SSID_MAX + 1];
static int battery_pct=75, pokes;
static bool charge_fail;
static bool usb_power=true, linked=true;
int64_t esp_timer_get_time(void) { return (int64_t)clock_ms * 1000; }
int nvs_set_i32(int a,const char*b,int c){(void)a;if(save_fail)return -1;if(!strcmp(b,"charge_ma"))saved_charge=c;if(!strcmp(b,"wearable"))saved_wearable=c;return 0;}
int nvs_get_i32(int a,const char*b,int*c){(void)a;if(!strcmp(b,"wearable")&&saved_wearable>=0){*c=saved_wearable;return 0;}if(!strcmp(b,"charge_ma")&&saved_charge){*c=saved_charge;return 0;}return -1;}
int nvs_commit(int a){(void)a;return 0;}
int nvs_open(const char*a,int b,int*c){(void)a;(void)b;*c=1;return 0;}
const char *esp_err_to_name(int a){(void)a;return "test";}
muse_mode_t muse_state_mode(float *s){if(s)*s=0;return mode;}
void muse_state_set_asleep(bool a){sleep_state=a;}
bool muse_state_asleep(void){return sleep_state;}
void muse_state_poke(void){++pokes;}
float muse_state_level(void){return 0.6f;}
float muse_state_progress(void){return 0.3f;}
muse_power_t muse_state_power(void){return (muse_power_t){battery_pct,3900,false,usb_power};}
bool muse_state_caption(char *out,size_t cap,uint32_t*v){snprintf(out,cap,"%s",reply);++*v;return true;}
void muse_state_set_page(int a,int b){(void)a;(void)b;}
void muse_state_set_cjk_page(int a,int b){assert(a==18 && b==2);}
void muse_state_set_latin_page(int a,int b){(void)a;(void)b;}
bool muse_voice_in_call(void){return call;}
bool muse_settings_voice_notes(void){return notes;}
void muse_settings_set_voice_notes(bool v){notes=v;}
void muse_settings_set_speaker_on(bool v){(void)v;}
int muse_settings_volume(void){return 86;}
void muse_settings_set_volume(int v){(void)v;}
bool muse_link_hatch_linked(void){return linked;}
int muse_link_state(void){return 5;}
void muse_ble_status(muse_ble_status_t*b){b->passkey=0;}
bool muse_wifi_connected(void){return wifi_connected;}
void muse_wifi_status(muse_wifi_status_t *out) {
    *out = (muse_wifi_status_t){.state = wifi_connected ? MUSE_WIFI_CONNECTED : MUSE_WIFI_OFF, .ssid = "Home"};
}
int muse_wifi_saved(muse_wifi_saved_t *out, int max) {
    assert(max >= 2);
    out[0] = (muse_wifi_saved_t){.ssid = "Home"};
    out[1] = (muse_wifi_saved_t){.ssid = "Hotspot"};
    return 2;
}
bool sumi_phone_mode(void){return phone_mode;}
bool sumi_phone_ready(void){return phone_ready;}
bool sumi_phone_set_mode(bool enabled){if(save_fail)return false;phone_mode=enabled;return true;}
void sumi_phone_apply_setting(void){}
void sumi_phone_open_settings(void){}
void sumi_phone_allow_muse_sync(void){}
bool sumi_wifi_request(void){wifi_operation="add";wifi_setting=true;return true;}
bool sumi_wifi_select(const char *ssid){wifi_operation="select";strlcpy(wifi_target,ssid,sizeof(wifi_target));wifi_setting=true;return true;}
bool sumi_wifi_forget(const char *ssid){wifi_operation="forget";strlcpy(wifi_target,ssid,sizeof(wifi_target));wifi_setting=true;return true;}
void sumi_wifi_cancel(void){wifi_setting=false;}
bool sumi_wifi_active(void){return wifi_setting;}
void sumi_wifi_status(char *out,unsigned cap){snprintf(out,cap,"Wi-Fi: SUMI-SETUP-1234\nパスワード: abcdef12\n\nhttp://192.168.4.1\nを開いてください。");}
void sumi_input_end_call(void){call=false;}
bool muse_voice_notes_waiting(void){return false;}
int muse_settings_mic_gain(void){return 30;}
void muse_settings_set_mic_gain(int v){(void)v;}
void sumi_mic_test_request(void){}
bool sumi_mic_test_active(void){return false;}
bool sumi_charge_set_current(int ma){if(charge_fail){charge=0;return false;}charge=ma;return true;}
int sumi_charge_current(void){return charge;}
static bool asr_key, asr_on;
void sumi_asr_init(void){}
bool sumi_asr_enabled(void){return asr_on;}
bool sumi_asr_configured(void){return asr_key;}
bool sumi_asr_select(bool on){if(on&&!asr_key)return false;asr_on=on;return true;}
static int rate=80;
int sumi_tts_rate(void){return rate;}
void sumi_tts_set_rate(int v){rate=v;}

static void flush(lv_display_t *display,const lv_area_t *area,uint8_t *p){(void)area;(void)p;lv_display_flush_ready(display);}
static void tick(unsigned ms){clock_ms+=ms;lv_tick_inc(ms);sumi_ui_prepare();if(!sleep_state)sumi_ui_tick();lv_timer_handler();}
static void picture(const char *name){
    lv_obj_update_layout(root);
    lv_draw_buf_t *b=lv_snapshot_take(root,LV_COLOR_FORMAT_RGB888); assert(b);
    char path[120];snprintf(path,sizeof(path),"%s.ppm",name);
    FILE *f=fopen(path,"wb");assert(f);fprintf(f,"P6\n466 466\n255\n");
    for(unsigned y=0;y<466;++y)for(unsigned x=0;x<466;++x){
        unsigned char *p=b->data+y*b->header.stride+x*3;
        unsigned char rgb[]={p[2],p[1],p[0]};fwrite(rgb,1,3,f);
    }
    fclose(f);lv_draw_buf_destroy(b);
}
int main(void){
    lv_init();lv_display_t*d=lv_display_create(466,466);
    _Alignas(4) static uint16_t draw[466*40];lv_display_set_buffers(d,draw,NULL,sizeof(draw),LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(d,flush);sumi_ui_init();tick(1);assert(charge==425);
    assert(screen==SUMI_BADGE);picture("badge");
    assert(!lv_obj_has_flag(status,LV_OBJ_FLAG_HIDDEN));
    tick(7000);assert(!lv_obj_has_flag(status,LV_OBJ_FLAG_HIDDEN));
    lv_obj_send_event(root,LV_EVENT_CLICKED,NULL);tick(1);
    assert(badge_clock && !lv_obj_has_flag(clock_label,LV_OBJ_FLAG_HIDDEN));
    assert(lv_image_get_scale_x(badge_image)==179);picture("badge-clock");
    char expected[6];sumi_clock_format(clock_utc,expected);
    assert(!strcmp(lv_label_get_text(clock_label),expected));
    clock_utc=0;tick(1);assert(!strcmp(lv_label_get_text(clock_label),"--:--"));picture("badge-clock-unsynced");
    clock_utc=1794380700;
    wifi_connected=false;battery_pct=-1;tick(1);
    assert(strstr(lv_label_get_text(status),"--%") && strstr(lv_label_get_text(status)," x"));
    wifi_connected=true;battery_pct=75;
    lv_obj_send_event(root,LV_EVENT_CLICKED,NULL);tick(1);
    assert(!badge_clock && lv_image_get_scale_x(badge_image)==256);
    tick(60000);assert(screen==SUMI_DIM && !lv_obj_has_flag(status,LV_OBJ_FLAG_HIDDEN));
    lv_obj_send_event(root,LV_EVENT_CLICKED,NULL);tick(1);
    assert(screen==SUMI_BADGE && badge_clock);
    tick(60000);tick(60000);assert(sleep_state);
    int old_pokes=pokes;lv_obj_send_event(root,LV_EVENT_CLICKED,NULL);tick(1);
    assert(sleep_state && screen==SUMI_OFF && pokes==old_pokes);
    muse_state_set_asleep(false);sumi_ui_wake();tick(1);
    assert(!badge_clock && screen==SUMI_BADGE);

    mode=MUSE_MODE_LISTENING;tick(50);assert(screen==SUMI_AVATAR);picture("recording");
    mode=MUSE_MODE_THINKING;tick(150000);assert(screen==SUMI_AVATAR && !sleep_state);picture("thinking");
    mode=MUSE_MODE_SPEAKING;tick(50);picture("speaking");
    mode=MUSE_MODE_IDLE;tick(30000);assert(screen==SUMI_BADGE);
    tick(60000);assert(screen==SUMI_DIM);tick(60000);assert(sleep_state);
    muse_state_set_asleep(false);sumi_ui_wake();tick(1);assert(screen==SUMI_BADGE && !menu);
    sumi_ui_toggle_settings();tick(1);assert(menu);picture("settings");
    lv_obj_t *settings_list = lv_obj_get_child(panel, 1);
    lv_obj_t *system_info = lv_obj_get_child(settings_list, -1);
    assert(!strcmp(lv_label_get_text(lv_obj_get_child(system_info, 0)), "システム情報"));
    lv_obj_send_event(system_info, LV_EVENT_CLICKED, NULL);
    assert(page == SYSTEM_INFO);
    assert(!strcmp(lv_label_get_text(lv_obj_get_child(panel, 2)), "0.1.6-test"));
    tick(1);picture("system-info");
    lv_obj_send_event(back, LV_EVENT_CLICKED, NULL);assert(page == -1);
    show_page(0);tick(1);picture("volume");
    show_page(6);tick(1);picture("voice");
    show_page(10);tick(1);picture("speech-speed");
    lv_obj_t *speed_slider=lv_obj_get_child(panel,2);
    lv_slider_set_value(speed_slider,6,LV_ANIM_OFF);
    lv_obj_send_event(speed_slider,LV_EVENT_RELEASED,NULL);assert(rate==60);
    show_page(11);tick(1);picture("mic-gain");
    show_page(12);tick(1);picture("chat-selection");
    assert(sumi_chat_selected()==1);
    lv_obj_t *chat_choices=lv_obj_get_child(panel,1);
    lv_obj_send_event(lv_obj_get_child(chat_choices,0),LV_EVENT_CLICKED,NULL);assert(sumi_chat_selected()==0);
    show_page(12);chat_choices=lv_obj_get_child(panel,1);
    lv_obj_send_event(lv_obj_get_child(chat_choices,1),LV_EVENT_CLICKED,NULL);assert(sumi_chat_selected()==1);
    show_page(19);tick(1);picture("send-settings-default-on");
    assert(sumi_chat_wearable() && saved_wearable==-1);
    lv_obj_send_event(lv_obj_get_child(panel,3),LV_EVENT_CLICKED,NULL);
    assert(!sumi_chat_wearable() && saved_wearable==0);tick(1);picture("send-settings-off");
    call=true;lv_obj_send_event(lv_obj_get_child(panel,2),LV_EVENT_CLICKED,NULL);assert(!sumi_chat_wearable());call=false;
    save_fail=true;lv_obj_send_event(lv_obj_get_child(panel,2),LV_EVENT_CLICKED,NULL);assert(!sumi_chat_wearable());save_fail=false;
    lv_obj_send_event(lv_obj_get_child(panel,2),LV_EVENT_CLICKED,NULL);
    assert(sumi_chat_wearable() && saved_wearable==1);tick(1);picture("send-settings-on");
    lv_obj_send_event(lv_obj_get_child(panel,3),LV_EVENT_CLICKED,NULL);assert(!sumi_chat_wearable() && !saved_wearable);
    lv_obj_send_event(lv_obj_get_child(panel,2),LV_EVENT_CLICKED,NULL);assert(sumi_chat_wearable());
    show_page(13);tick(1);picture("mic-test");
    show_page(14);tick(1);picture("charge-fast");
    lv_obj_send_event(lv_obj_get_child(panel,1),LV_EVENT_CLICKED,NULL);
    assert(charge==185 && saved_charge==185);tick(1);picture("charge-standard");
    charge_fail=true;lv_obj_send_event(lv_obj_get_child(panel,2),LV_EVENT_CLICKED,NULL);
    assert(charge==0 && saved_charge==185);tick(1);picture("charge-error");
    charge_fail=false;
    show_page(17);tick(1);picture("asr-unconfigured");
    lv_obj_send_event(lv_obj_get_child(panel,1),LV_EVENT_CLICKED,NULL);assert(!asr_on);
    asr_key=true;lv_obj_send_event(lv_obj_get_child(panel,1),LV_EVENT_CLICKED,NULL);assert(asr_on);tick(1);picture("asr-openai");
    lv_obj_send_event(lv_obj_get_child(panel,2),LV_EVENT_CLICKED,NULL);assert(!asr_on);
    show_page(3);tick(1);picture("display-time");
    show_page(6);
    call=true; notes=false;
    lv_obj_send_event(lv_obj_get_child(panel,2),LV_EVENT_CLICKED,NULL);
    assert(!notes && page==6); /* no mid-call switch */
    call=false;
    lv_obj_send_event(lv_obj_get_child(panel,2),LV_EVENT_CLICKED,NULL);
    assert(notes && page==-1);
    show_page(WIFI_NETWORKS);
    lv_obj_t *wifi_list = lv_obj_get_child(panel, 3);
    lv_obj_send_event(lv_obj_get_child(wifi_list, 1), LV_EVENT_CLICKED, NULL);
    assert(page == WIFI_NETWORK && !strcmp(wifi_selection, "Hotspot"));
    lv_obj_send_event(lv_obj_get_child(panel, 3), LV_EVENT_CLICKED, NULL);
    assert(page == WIFI_DELETE && !wifi_setting);
    lv_obj_send_event(back, LV_EVENT_CLICKED, NULL);
    assert(page == WIFI_NETWORK && !wifi_setting);
    call = true;
    lv_obj_send_event(lv_obj_get_child(panel, 2), LV_EVENT_CLICKED, NULL);
    assert(page == WIFI_NETWORK && !wifi_setting);
    call = false;
    lv_obj_send_event(lv_obj_get_child(panel, 2), LV_EVENT_CLICKED, NULL);
    assert(page == WIFI_RESULT && wifi_setting && !strcmp(wifi_operation, "select") && !strcmp(wifi_target, "Hotspot"));
    lv_obj_send_event(back, LV_EVENT_CLICKED, NULL);
    assert(page == WIFI_RESULT);
    wifi_setting = false;
    lv_obj_send_event(back, LV_EVENT_CLICKED, NULL);
    wifi_list = lv_obj_get_child(panel, 3);
    lv_obj_send_event(lv_obj_get_child(wifi_list, 0), LV_EVENT_CLICKED, NULL);
    lv_obj_send_event(lv_obj_get_child(panel, 3), LV_EVENT_CLICKED, NULL);
    lv_obj_send_event(lv_obj_get_child(panel, 3), LV_EVENT_CLICKED, NULL);
    assert(page == WIFI_RESULT && wifi_setting && !strcmp(wifi_operation, "forget") && !strcmp(wifi_target, "Home"));
    wifi_setting = false;
    lv_obj_send_event(back, LV_EVENT_CLICKED, NULL);
    lv_obj_send_event(lv_obj_get_child(panel, 1), LV_EVENT_CLICKED, NULL);
    assert(page == WIFI_SETUP && wifi_setting && !strcmp(wifi_operation, "add"));
    lv_obj_send_event(back, LV_EVENT_CLICKED, NULL);
    assert(page == WIFI_NETWORKS && !wifi_setting);
    show_page(20);
    mode=MUSE_MODE_THINKING;
    lv_obj_send_event(lv_obj_get_child(panel,2),LV_EVENT_CLICKED,NULL);
    assert(!phone_mode && page==20);
    mode=MUSE_MODE_IDLE;
    save_fail=true;
    lv_obj_send_event(lv_obj_get_child(panel,2),LV_EVENT_CLICKED,NULL);
    assert(!phone_mode);
    save_fail=false;
    lv_obj_send_event(lv_obj_get_child(panel,2),LV_EVENT_CLICKED,NULL);
    assert(phone_mode);
    assert(sumi_ui_setting(SUMI_SETTING_MODE) == 1);
    assert(sumi_ui_apply_setting(SUMI_SETTING_MIC_GAIN, 35) == 3);
    assert(sumi_ui_apply_setting(SUMI_SETTING_AVATAR_TIME, 45) == 0);
    assert(sumi_ui_setting(SUMI_SETTING_AVATAR_TIME) == 45);
    assert(sumi_ui_apply_setting(SUMI_SETTING_WEARABLE, 0) == 0);
    assert(!sumi_chat_wearable() && saved_wearable == 0);
    mode=MUSE_MODE_SPEAKING;
    assert(sumi_ui_apply_setting(SUMI_SETTING_WEARABLE, 1) == 2 && !sumi_chat_wearable());
    mode=MUSE_MODE_IDLE;
    assert(sumi_ui_apply_setting(SUMI_SETTING_WEARABLE, 1) == 0);
    assert(sumi_chat_wearable() && saved_wearable == 1);
    tick(1);picture("connection-ble");
    menu=false;atomic_store(&menu_open,false);set_screen(SUMI_BADGE);tick(1);
    assert(strstr(lv_label_get_text(status),LV_SYMBOL_BLUETOOTH) && strstr(lv_label_get_text(status),"x"));
    phone_ready=true;tick(1);
    assert(!strstr(lv_label_get_text(status),"x"));
    menu=true;atomic_store(&menu_open,true);show_page(20);
    lv_obj_send_event(lv_obj_get_child(panel,1),LV_EVENT_CLICKED,NULL);
    assert(!phone_mode);
    menu=false; atomic_store(&menu_open,false); mode=MUSE_MODE_THINKING;tick(1);
    mode=MUSE_MODE_IDLE;snprintf(reply,sizeof(reply),"読み上げに失敗しました。辞書と返答文を確認してください");tick(1);
    assert(!strcmp(lv_label_get_text(caption),reply)); /* SDK reports errors in IDLE */
    lv_obj_update_layout(root);
    lv_point_t size;
    lv_text_get_size(&size, reply, &font, 0, 6, lv_obj_get_content_width(caption), LV_TEXT_FLAG_NONE);
    assert(lv_obj_get_content_height(caption) >= size.y);
    picture("speech-error");
    menu=true;atomic_store(&menu_open,true);
    show_page(9);tick(1);picture("wifi");
    /* Recreate the UI through its real startup path with persisted settings. */
    lv_obj_delete(root);free(pixels);root=panel=NULL;sumi_ui_init();assert(charge==185 && sumi_chat_wearable());
    saved_wearable=0;
    saved_charge=300;
    lv_obj_delete(root);free(pixels);root=panel=NULL;sumi_ui_init();assert(charge==425 && !sumi_chat_wearable());
    puts("UI: phases, waiting, wake-only, settings; snapshots: PASS");
    return 0;
}
