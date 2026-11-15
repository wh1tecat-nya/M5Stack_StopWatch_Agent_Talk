"""Behavior checks for badge time, sleep touch and voltage-derived percentage."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_controls import ROOT, SDK


class BadgePower(unittest.TestCase):
    def run_c(self, code, headers=None, extra=None):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'run.c').write_text(code)
            for name, text in (headers or {}).items():
                (p / name).write_text(text)
            result = subprocess.run(['cc', '-std=c11', '-D_POSIX_C_SOURCE=200809L',
                '-fsanitize=address,undefined', '-Wall', '-Wextra', '-Werror',
                '-I', str(p), '-I', str(ROOT / 'overlay'), str(p / 'run.c'),
                *(extra or []), '-o', str(p / 'run')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(p / 'run')], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_voltage_noise_source_changes_recovery_and_low_cell(self):
        self.run_c(r'''
#include <assert.h>
#include <stdlib.h>
#include "sumi_battery.h"
int main(void) {
    sumi_battery_t b={0}; int64_t now=0;
    assert(sumi_battery_update(&b,65535,false,now)==-1);
    int initial=sumi_battery_update(&b,3900,false,now);
    for(int i=0;i<120;++i) {
        now+=2000;
        int mv=i%20==0?3300:i%20==1?4300:3900+(i%3-1)*8;
        assert(abs(sumi_battery_update(&b,mv,false,now)-initial)<=1);
    }
    assert(sumi_battery_update(&b,0,false,now+1)==b.percent);
    int before=b.percent;
    for(int i=0;i<10;++i) { now+=2000; assert(sumi_battery_update(&b,4150,true,now)==before); }
    int previous=b.percent;
    for(int i=0;i<300;++i) {
        now+=2000; int current=sumi_battery_update(&b,4150,true,now);
        assert(current>=previous && current-previous<=1);previous=current;
    }
    assert(b.percent>before+10);
    before=b.percent;
    for(int i=0;i<10;++i) {now+=2000;assert(sumi_battery_update(&b,3800,false,now)==before);}
    for(int i=0;i<180;++i) {
        now+=2000;int current=sumi_battery_update(&b,3800,false,now);
        assert(current<=previous && previous-current<=1);previous=current;
    }
    assert(b.percent<before-5);
    /* Load recovery cannot make the gauge jump upward. */
    before=b.percent;
    for(int i=0;i<60;++i) {now+=2000;sumi_battery_update(&b,4000,false,now);}
    assert(b.percent<=before);
    /* An actual exhausted cell overrides the normal slew limit. */
    for(int i=0;i<12;++i) {now+=2000;sumi_battery_update(&b,3350,false,now);}
    assert(b.percent==0);
    b=(sumi_battery_t){0};now=0;
    sumi_battery_update(&b,3900,false,now);
    for(int i=0;i<10;++i) {now+=10000;sumi_battery_update(&b,3350,false,now);}
    assert(b.percent==0); /* paused input polling cadence */
}
''')

    def test_clock_jst_boundaries_and_unset_time(self):
        self.run_c(r'''
#include <assert.h>
#include "sumi_clock.h"
int main(void) {
    char s[6];
    sumi_clock_format(0,s);assert(!strcmp(s,"--:--"));
    sumi_clock_format(1704067200,s);assert(!strcmp(s,"09:00"));
    sumi_clock_format(1704121199,s);assert(!strcmp(s,"23:59"));
    sumi_clock_format(1704121200,s);assert(!strcmp(s,"00:00"));
    sumi_clock_format(1704121620,s);assert(!strcmp(s,"00:07"));
}
''')

    def test_sleep_cover_never_wakes_on_touch(self):
        source = (SDK / 'components/muse/muse_ui.c').read_text()
        start = source.index('static void on_cover_event(')
        code = source[start:source.index('\n}\n', start)+3]
        self.run_c(r'''
#include <assert.h>
#include <stdbool.h>
typedef int lv_event_t;
enum {LV_EVENT_PRESSED,LV_EVENT_RELEASED,LV_EVENT_PRESS_LOST,LV_OBJ_FLAG_HIDDEN};
static bool asleep=true;
static int cover, *s_cover=&cover;
static int lv_event_get_code(lv_event_t *e){return *e;}
static bool muse_state_asleep(void){return asleep;}
static void lv_obj_add_flag(int *o,int f){assert(f==LV_OBJ_FLAG_HIDDEN);++*o;}
''' + code + r'''
int main(void){
    for(int i=0;i<3;++i){lv_event_t e=i;on_cover_event(&e);assert(asleep && !cover);}
    asleep=false;lv_event_t e=LV_EVENT_RELEASED;on_cover_event(&e);assert(cover==1);
}
''')

    def test_power_read_fault_does_not_become_zero_percent(self):
        source = (SDK / 'components/muse/boards/board_m5stack_stopwatch.c').read_text()
        start = source.index('static esp_err_t read_power(')
        code = source[start:source.index('\n}\n', start)+3]
        self.run_c(r'''
#include <assert.h>
#include <stddef.h>
#include "sumi_battery.h"
typedef int esp_err_t;
typedef struct {int battery_pct,battery_mv;bool charging,usb;} muse_power_t;
enum {ESP_OK,ESP_FAIL,ESP_ERR_INVALID_RESPONSE,PMIC_GPIO_IN,PMIC_VBAT_MV,PMIC_CHG_STAT=8};
#define ESP_RETURN_ON_ERROR(expr,...) do{int e=(expr);if(e)return e;}while(0)
#define ESP_RETURN_ON_FALSE(expr,err,...) do{if(!(expr))return (err);}while(0)
static int s_pmic, voltage=3900, fault;
static int reg_read(int dev,int reg,uint8_t *out,size_t size){
    (void)dev;(void)size;if(fault && reg==PMIC_VBAT_MV)return ESP_FAIL;
    if(reg==PMIC_GPIO_IN){*out=0;return ESP_OK;}
    out[0]=voltage&255;out[1]=voltage>>8;return ESP_OK;
}
static int read_usb_power(bool *usb){*usb=false;return ESP_OK;}
static int64_t esp_timer_get_time(void){return 1000000;}
''' + code + r'''
int main(void){
    muse_power_t p={0};assert(read_power(&p)==ESP_OK && p.battery_mv==3900);
    int pct=p.battery_pct;fault=1;assert(read_power(&p)==ESP_FAIL && p.battery_pct==pct);
    fault=0;voltage=65535;assert(read_power(&p)==ESP_ERR_INVALID_RESPONSE && p.battery_pct==pct);
}
''')

    def test_time_service_retries_without_blocking_or_reinitializing(self):
        headers = {
            'esp_netif_sntp.h': '''#pragma once
#include <stdbool.h>
typedef int esp_err_t;
#define ESP_OK 0
typedef struct {bool start, wait_for_sync;} esp_sntp_config_t;
#define ESP_NETIF_SNTP_DEFAULT_CONFIG(server) ((esp_sntp_config_t){true,true})
int esp_netif_sntp_init(const esp_sntp_config_t *);
int esp_netif_sntp_start(void);
''',
            'esp_timer.h': '#include <stdint.h>\nint64_t esp_timer_get_time(void);\n',
            'esp_log.h': '#define ESP_LOGW(...) ((void)0)\n',
        }
        self.run_c(r'''
#include <assert.h>
#include <stdint.h>
#include "sumi_clock.h"
#include "esp_netif_sntp.h"
static int inits,starts;static int64_t now;
int64_t esp_timer_get_time(void){return now;}
int esp_netif_sntp_init(const esp_sntp_config_t *c){assert(!c->start&&!c->wait_for_sync);++inits;return 0;}
int esp_netif_sntp_start(void){return ++starts==1 ? -1 : 0;}
int main(void){
    sumi_clock_poll(false);assert(!inits);
    sumi_clock_poll(true);assert(inits==1 && starts==1);
    now=59000000;sumi_clock_poll(true);assert(starts==1);
    now=60000000;sumi_clock_poll(true);assert(starts==2 && inits==1);
    now+=60000000;sumi_clock_poll(true);assert(starts==2);
    sumi_clock_poll(false);sumi_clock_poll(true);assert(starts==3 && inits==1);
}
''', headers, [str(ROOT/'overlay/sumi_clock.c')])
