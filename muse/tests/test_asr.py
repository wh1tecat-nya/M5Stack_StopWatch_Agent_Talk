"""Exercise the actual direct-ASR transport without contacting any service."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_controls import ROOT, SDK


class ASR(unittest.TestCase):
    def test_upload_framing_secrets_errors_and_cancellation(self):
        platform = r'''
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef int esp_err_t;
typedef int nvs_handle_t;
typedef void *esp_http_client_handle_t;
#define ESP_OK 0
#define NVS_READONLY 0
#define NVS_READWRITE 1
#define HTTP_METHOD_POST 1
#define ESP_LOGI(...) ((void)0)
typedef struct {const char *url;int method;void(*crt_bundle_attach)(void);int timeout_ms;bool disable_auto_redirect;int buffer_size,buffer_size_tx;} esp_http_client_config_t;
void esp_crt_bundle_attach(void);
int64_t esp_timer_get_time(void);
int nvs_open(const char*,int,nvs_handle_t*);
int nvs_get_str(int,const char*,char*,size_t*);
int nvs_get_i32(int,const char*,int32_t*);
int nvs_set_str(int,const char*,const char*);
int nvs_set_i32(int,const char*,int32_t);
int nvs_commit(int);
void nvs_close(int);
void *esp_http_client_init(const esp_http_client_config_t*);
int esp_http_client_set_header(void*,const char*,const char*);
int esp_http_client_open(void*,int);
int esp_http_client_write(void*,const char*,int);
int esp_http_client_fetch_headers(void*);
int esp_http_client_get_status_code(void*);
int esp_http_client_read(void*,char*,int);
bool esp_http_client_is_complete_data_received(void*);
int esp_http_client_cleanup(void*);
'''
        harness = r'''
#include "platform.h"
#include "sumi_asr.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static char key[512]="sk-test_01234567890123456789", sent[8192];
static const char *response="{\"text\":\"今度こそ声が聞こえるかな？\"}";
static int mode, status=200, total, used, read_at, cleanup, fail_write, fail_save, cancel_at;
static bool incomplete;
static int64_t clock_us;
void esp_crt_bundle_attach(void){}
int64_t esp_timer_get_time(void){return clock_us;}
int nvs_open(const char*n,int m,int*h){assert(!strcmp(n,"sumi-muse"));(void)m;*h=1;return 0;}
int nvs_get_str(int h,const char*n,char*out,size_t*size){(void)h;assert(!strcmp(n,"asr_key"));if(out){assert(*size>strlen(key));strcpy(out,key);}*size=strlen(key)+1;return 0;}
int nvs_get_i32(int h,const char*n,int32_t*out){(void)h;assert(!strcmp(n,"asr"));*out=mode;return 0;}
int nvs_set_str(int h,const char*n,const char*s){(void)h;(void)n;if(fail_save)return -1;strcpy(key,s);return 0;}
int nvs_set_i32(int h,const char*n,int32_t v){(void)h;(void)n;if(fail_save)return -1;mode=v;return 0;}
int nvs_commit(int h){(void)h;return 0;}
void nvs_close(int h){(void)h;}
void *esp_http_client_init(const esp_http_client_config_t*c){
 assert(!strcmp(c->url,"https://api.openai.com/v1/audio/transcriptions"));
 assert(c->crt_bundle_attach==esp_crt_bundle_attach&&c->disable_auto_redirect);
 used=read_at=0;memset(sent,0,sizeof(sent));return (void*)1;
}
int esp_http_client_set_header(void*h,const char*n,const char*v){(void)h;
 if(!strcmp(n,"Authorization")){assert(!strncmp(v,"Bearer sk-",10));}
 else assert(!strcmp(n,"Content-Type")&&strstr(v,"boundary=sumi-asr-2026"));return 0;}
int esp_http_client_open(void*h,int n){(void)h;total=n;assert(n<(int)sizeof(sent));return 0;}
int esp_http_client_write(void*h,const char*p,int n){(void)h;if(fail_write)return -1;
 if(n>17)n=17;assert(used+n<=(int)sizeof(sent));memcpy(sent+used,p,n);used+=n;clock_us+=1000;return n;}
int esp_http_client_fetch_headers(void*h){(void)h;assert(used==total);return strlen(response);}
int esp_http_client_get_status_code(void*h){(void)h;return status;}
int esp_http_client_read(void*h,char*p,int n){(void)h;int remaining=strlen(response)-read_at;if(n>remaining)n=remaining;if(n>7)n=7;memcpy(p,response+read_at,n);read_at+=n;return n;}
bool esp_http_client_is_complete_data_received(void*h){(void)h;return !incomplete&&read_at==(int)strlen(response);}
int esp_http_client_cleanup(void*h){(void)h;++cleanup;return 0;}
static bool cancelled(void){return cancel_at&&used>=cancel_at;}
int main(void){
 int16_t pcm[160];for(int i=0;i<160;i++)pcm[i]=i*120-8000;
 char out[256],why[256];
 assert(!sumi_asr_key_valid("sk-bad\r\nInjected: true")&&!sumi_asr_key_valid("wrong-prefix-0123456789"));
 sumi_asr_init();assert(sumi_asr_configured()&&!sumi_asr_enabled());
 assert(sumi_asr_select(true)&&sumi_asr_enabled());fail_save=1;
 assert(!sumi_asr_select(false)&&sumi_asr_enabled());fail_save=0;
 assert(sumi_asr_transcribe(pcm,160,out,sizeof(out),cancelled,why,sizeof(why)));
 assert(!strcmp(out,"今度こそ声が聞こえるかな？")&&cleanup==1);
 assert(strstr(sent,"gpt-transcribe")&&strstr(sent,"languages[]")&&strstr(sent,"\r\nja\r\n")&&strstr(sent,"M5Stack"));
 char *wav=strstr(sent,"RIFF");assert(wav&&!memcmp(wav+8,"WAVEfmt ",8));
 assert((unsigned char)wav[40]==64&&(unsigned char)wav[41]==1);assert(!memcmp(wav+44,pcm,sizeof(pcm)));
 assert(!memcmp(sent+used-21,"\r\n--sumi-asr-2026--\r\n",21));
 status=401;assert(!sumi_asr_transcribe(pcm,160,out,sizeof(out),cancelled,why,sizeof(why))&&strstr(why,"401")&&!out[0]);
 status=429;assert(!sumi_asr_transcribe(pcm,160,out,sizeof(out),cancelled,why,sizeof(why))&&strstr(why,"429"));
 status=200;response="not JSON";assert(!sumi_asr_transcribe(pcm,160,out,sizeof(out),cancelled,why,sizeof(why)));
 response="{\"text\":\"hello\"}";incomplete=true;assert(!sumi_asr_transcribe(pcm,160,out,sizeof(out),cancelled,why,sizeof(why)));incomplete=false;
 assert(!sumi_asr_transcribe(pcm,160,out,3,cancelled,why,sizeof(why)));
 fail_write=1;assert(!sumi_asr_transcribe(pcm,160,out,sizeof(out),cancelled,why,sizeof(why)));fail_write=0;
 cancel_at=100;assert(!sumi_asr_transcribe(pcm,160,out,sizeof(out),cancelled,why,sizeof(why))&&used<total);
 assert(!sumi_asr_transcribe(pcm,240001,out,sizeof(out),cancelled,why,sizeof(why)));
 assert(!strstr(why,key));return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p/'platform.h').write_text(platform)
            for name in ['esp_http_client.h','esp_crt_bundle.h','esp_timer.h','esp_log.h','nvs.h']:
                (p/name).write_text('#include "platform.h"\n')
            (p/'run.c').write_text(harness)
            json = SDK/'managed_components/espressif__cjson/cJSON'
            result = subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-error=deprecated-declarations','-fsanitize=address,undefined',
                '-I',str(p),'-I',str(ROOT/'overlay'),'-I',str(json),str(p/'run.c'),str(ROOT/'overlay/sumi_asr.c'),
                str(json/'cJSON.c'),'-o',str(p/'run')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result = subprocess.run([str(p/'run')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
