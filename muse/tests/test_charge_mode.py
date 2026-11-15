"""Run the board's charging register sequence against a fault-injected PMIC."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from test_controls import SDK
from test_audio_gate import function


class ChargeMode(unittest.TestCase):
    def test_charger_preserves_rails_and_unlocks_on_failure(self):
        source = (SDK / 'components/muse/boards/board_m5stack_stopwatch.c').read_text()
        code = r'''
#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
#define PMIC_PWR_CFG 6
#define PMIC_PWR_CHARGE 1
#define PMIC_PWR_5V_OUT 8
#define ESP_OK 0
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define portMAX_DELAY 0
typedef int esp_err_t;
static int s_pmic, s_pmic_lock, operations, fail_at;
static bool locked, mismatch;
static uint8_t power;
static void xSemaphoreTake(int lock,int wait){(void)lock;(void)wait;assert(!locked);locked=true;}
static void xSemaphoreGive(int lock){(void)lock;assert(locked);locked=false;}
static int reg_read(int dev,uint8_t reg,void*data,int n){
 (void)dev;assert(reg==PMIC_PWR_CFG&&n==1);if(++operations==fail_at)return -1;
 *(uint8_t*)data=power;if(mismatch&&!locked)*(uint8_t*)data^=1;return 0;
}
static int reg_write(int dev,uint8_t reg,const void*data,int n){
 (void)dev;assert(locked&&reg==PMIC_PWR_CFG&&n==1);if(++operations==fail_at)return -1;
 power=*(const uint8_t*)data;return 0;
}
''' + function(source, 'static esp_err_t pmic_update(') + function(source, 'int sumi_charge_enabled(') + function(source, 'int sumi_ext_output_enabled(') + function(source, 'bool sumi_charge_enable(') + r'''
int main(void){
 power=0x1f;assert(sumi_charge_enable(false)&&power==0x1e&&!locked);
 assert(pmic_update(PMIC_PWR_CFG,0x10,0)==0&&power==0x0e&&!locked);
 assert(sumi_charge_enable(true)&&power==0x0f&&!locked);
 for(int at=1;at<=3;++at){power=0x1f;operations=0;fail_at=at;assert(!sumi_charge_enable(false)&&!locked);}
 fail_at=0;mismatch=true;assert(!sumi_charge_enable(false)&&!locked);
 mismatch=false;assert(sumi_charge_enable(true)&&!locked);
 operations=0;fail_at=1;assert(sumi_charge_enabled()==-1);
 operations=0;fail_at=1;assert(sumi_ext_output_enabled()==-1);
 fail_at=0;power=0x1f;assert(sumi_ext_output_enabled()==1);
 assert(pmic_update(PMIC_PWR_CFG,PMIC_PWR_5V_OUT,0)==ESP_OK);
 assert(power==0x17 && sumi_ext_output_enabled()==0 && sumi_charge_enabled()==1);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            p = Path(td); (p / 'run.c').write_text(code)
            result = subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',
                                     str(p/'run.c'),'-o',str(p/'run')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result = subprocess.run([str(p/'run')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_pin_mode_preservation_readback_and_failures(self):
        source = (SDK / 'components/muse/boards/board_m5stack_stopwatch.c').read_text()
        defs = '\n'.join(re.findall(r'^#define PMIC_(?:GPIO_MODE|GPIO_OUT|GPIO_OD|GPIO_PUPD0|GPIO_FUNC0|CHG_PROG) .+$', source, re.M))
        code = r'''
#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
#include <string.h>
#define BIT(n) (1U<<(n))
#define ESP_OK 0
#define ESP_LOGI(...) ((void)0)
static int s_pmic, s_charge_ma, operations, fail_at;
static bool mismatch;
static uint8_t regs[256];
''' + defs + r'''
static int pmic_update(uint8_t reg,uint8_t mask,uint8_t value){
 if(++operations==fail_at)return -1;
 if(reg==PMIC_GPIO_OUT)assert(!(value&PMIC_CHG_PROG));
 regs[reg]=(regs[reg]&~mask)|(value&mask);return 0;
}
static int reg_read(int dev,uint8_t reg,void*data,int n){
 (void)dev;assert(n==1);if(++operations==fail_at)return -1;
 *(uint8_t*)data=regs[reg];if(mismatch&&reg==PMIC_GPIO_MODE)*(uint8_t*)data^=PMIC_CHG_PROG;return 0;
}
''' + function(source, 'bool sumi_charge_set_current(') + r'''
int main(void){
 memset(regs,0xa5,sizeof(regs));
 assert(sumi_charge_set_current(425)&&s_charge_ma==425);
 assert((regs[PMIC_GPIO_MODE]&8)==8 && (regs[PMIC_GPIO_OUT]&8)==0);
 assert(sumi_charge_set_current(185)&&s_charge_ma==185);
 assert((regs[PMIC_GPIO_MODE]&8)==0 && (regs[PMIC_GPIO_PUPD0]&0xc0)==0);
 for(unsigned i=0;i<256;++i){
  unsigned mask=(i==PMIC_GPIO_MODE||i==PMIC_GPIO_OUT||i==PMIC_GPIO_OD)?8:
                (i==PMIC_GPIO_PUPD0||i==PMIC_GPIO_FUNC0)?0xc0:0;
  assert((regs[i]&~mask)==(0xa5&~mask));
 }
 operations=0;assert(!sumi_charge_set_current(300)&&operations==0&&s_charge_ma==185);
 for(int at=1;at<=11;++at){operations=0;fail_at=at;assert(!sumi_charge_set_current(425)&&s_charge_ma==0);}
 fail_at=0;mismatch=true;assert(!sumi_charge_set_current(425)&&s_charge_ma==0);
 mismatch=false;assert(sumi_charge_set_current(425)&&s_charge_ma==425);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            p = Path(td); (p / 'run.c').write_text(code)
            result = subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=address,undefined',
                                     str(p/'run.c'),'-o',str(p/'run')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result = subprocess.run([str(p/'run')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
