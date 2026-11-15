"""The production speech worker against a deterministic synthetic PCM source."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

PLATFORM = r'''
#pragma once
#include <atomic>
#include <chrono>
#include <cassert>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <mutex>
#include <thread>
#include <vector>
#define MALLOC_CAP_SPIRAM 1
#define MALLOC_CAP_8BIT 2
#define MALLOC_CAP_INTERNAL 4
#define pdTRUE 1
#define pdPASS 1
#define portMAX_DELAY 0xffffffff
#define pdMS_TO_TICKS(x) (x)
#define ESP_LOGE(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
inline int64_t esp_timer_get_time(){return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();}
struct Queue { unsigned cap,size; std::mutex mutex; std::condition_variable wake; std::deque<std::vector<char>> data; };
using QueueHandle_t=Queue*;
inline void* heap_caps_malloc(size_t n,int){return malloc(n);}
inline Queue* xQueueCreateWithCaps(unsigned cap,unsigned size,int){auto q=new Queue;q->cap=cap;q->size=size;return q;}
inline void vQueueDeleteWithCaps(Queue*q){delete q;}
inline int xQueueSend(Queue*q,const void*p,uint32_t wait){
 std::unique_lock<std::mutex> lock(q->mutex);
 if(q->data.size()>=q->cap && (!wait || !q->wake.wait_for(lock,std::chrono::milliseconds(wait),[&]{return q->data.size()<q->cap;})))return 0;
 q->data.emplace_back((const char*)p,(const char*)p+q->size); q->wake.notify_all();return 1;
}
inline int xQueueReceive(Queue*q,void*p,uint32_t wait){
 std::unique_lock<std::mutex> lock(q->mutex);
 if(q->data.empty()){
  if(wait==portMAX_DELAY)q->wake.wait(lock,[&]{return !q->data.empty();});
  else if(!wait || !q->wake.wait_for(lock,std::chrono::milliseconds(wait),[&]{return !q->data.empty();}))return 0;
 }
 memcpy(p,q->data.front().data(),q->size);q->data.pop_front();q->wake.notify_all();return 1;
}
inline int xQueuePeek(Queue*q,void*p,uint32_t){
 std::lock_guard<std::mutex> lock(q->mutex);if(q->data.empty())return 0;
 memcpy(p,q->data.front().data(),q->size);return 1;
}
struct Notification {std::mutex mutex;std::condition_variable wake;bool pending=false;};
using TaskHandle_t=Notification*;
inline thread_local Notification notification;
inline thread_local int task_caps;
inline TaskHandle_t xTaskGetCurrentTaskHandle(){return &notification;}
inline void xTaskNotifyGive(TaskHandle_t task){std::lock_guard<std::mutex> lock(task->mutex);task->pending=true;task->wake.notify_one();}
inline int ulTaskNotifyTake(int,uint32_t){std::unique_lock<std::mutex> lock(notification.mutex);notification.wake.wait(lock,[]{return notification.pending;});notification.pending=false;return 1;}
inline void vTaskDeleteWithCaps(void*){}
inline int xTaskCreateWithCaps(void(*fn)(void*),const char*,unsigned,void*arg,unsigned,void*,int caps){std::thread([=]{task_caps=caps;fn(arg);}).detach();return 1;}
inline int xTaskCreatePinnedToCoreWithCaps(void(*fn)(void*),const char*name,unsigned stack,void*arg,unsigned priority,void*task,int core,int caps){assert(core==0);return xTaskCreateWithCaps(fn,name,stack,arg,priority,task,caps);}
'''

MODEL = r'''
#pragma once
#include "platform.h"
#include <cstddef>
#include <cstdint>
#include <cstring>
inline std::atomic<float> last_scale{0};
inline std::atomic<bool> partial_ready{false}, resume_partial{false};
class SanoTTS {
 bool initialized=false;
public:
 bool ready()const{return initialized;}
 bool begin(){assert(task_caps & MALLOC_CAP_INTERNAL);initialized=true;return true;}
 bool kanjiReady()const{return initialized;}
 void setLengthScale(float value){last_scale=value;} const char*lastError()const{return "fake failure";}
 bool synthesize(const char*text,size_t n,void(*cb)(const int16_t*,size_t,void*),void*user){
  assert(task_caps & MALLOC_CAP_SPIRAM);
  if(!n || !strncmp(text,"！",3))return false;
  int16_t pcm[512];for(auto &v:pcm)v=!strncmp(text,"Ｂ",3)?2222:1111;
  for(int i=0;i<(!strncmp(text,"Ｐ",3)?100:10);++i){
   cb(pcm,512,user);
   if(!strncmp(text,"Ｐ",3) && i==44){partial_ready=true;while(!resume_partial)std::this_thread::sleep_for(std::chrono::milliseconds(1));}
  }
  return true;
 }
};
'''

class SpeechQueue(unittest.TestCase):
    def test_cancellation_backpressure_and_failed_speech(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / 'platform.h').write_text(PLATFORM)
            (p / 'SanoTTS.h').write_text(MODEL)
            for header in ['esp_heap_caps.h','esp_log.h','esp_timer.h','freertos/FreeRTOS.h','freertos/idf_additions.h',
                           'freertos/queue.h','freertos/task.h']:
                h = p / header; h.parent.mkdir(parents=True, exist_ok=True)
                h.write_text('#include "platform.h"\n')
            (p / 'run.cpp').write_text(r'''
#include "platform.h"
#include "sumi_tts.h"
#include "SanoTTS.h"
#include <cmath>
#include <cassert>
int main(){
 assert(sumi_tts_rate()==80);sumi_tts_set_rate(60);assert(sumi_tts_rate()==60);
 sumi_tts_set_rate(0);sumi_tts_set_rate(121);assert(sumi_tts_rate()==60);
 std::string a(4000,'A'); assert(sumi_tts_start(a.c_str()));
 std::this_thread::sleep_for(std::chrono::milliseconds(20)); // producer blocked by full PCM queue
 assert(sumi_tts_start("B🤍\n**")); // decorations must not reach the speech model
 bool done=false,failed=false;size_t total=0;int16_t pcm[256];
 auto until=std::chrono::steady_clock::now()+std::chrono::seconds(3);
 while(!done && std::chrono::steady_clock::now()<until){
  size_t n=sumi_tts_read(pcm,&done,&failed);for(size_t i=0;i<n;++i)assert(pcm[i]==2222);total+=n;
  if(!n)std::this_thread::sleep_for(std::chrono::milliseconds(1));
 }
 assert(done && !failed && total==5120*16000/22050);
 assert(std::abs(last_scale.load() - 1.2187f*1.15f/0.6f)<0.0001f);
 assert(sumi_tts_start("!"));done=false;
 while(!done && std::chrono::steady_clock::now()<until){
  assert(sumi_tts_read(pcm,&done,&failed)==0);std::this_thread::sleep_for(std::chrono::milliseconds(1));
 }
 assert(done && failed);
 assert(sumi_tts_start(a.c_str()));sumi_tts_cancel();
 std::this_thread::sleep_for(std::chrono::milliseconds(20));
 assert(sumi_tts_read(pcm,&done,&failed)==0 && !done);
 assert(sumi_tts_start("P"));
 while(!partial_ready)std::this_thread::sleep_for(std::chrono::milliseconds(1));
 assert(sumi_tts_read(pcm,&done,&failed)==0 && !done); // 1.04 s is below the 1.6 s pre-roll
 resume_partial=true;total=0;done=false;
 until=std::chrono::steady_clock::now()+std::chrono::seconds(3);
 while(!done && std::chrono::steady_clock::now()<until){
  size_t n=sumi_tts_read(pcm,&done,&failed);total+=n;
  if(!n)std::this_thread::sleep_for(std::chrono::milliseconds(1));
 }
 assert(done && !failed && total==51200*16000/22050);
 assert(sumi_tts_start("🤍❤️👩‍💻\n**"));done=false;
 while(!done && std::chrono::steady_clock::now()<until){
  assert(sumi_tts_read(pcm,&done,&failed)==0);std::this_thread::sleep_for(std::chrono::milliseconds(1));
 }
 assert(done && !failed);
 std::puts("speech pre-roll, short replies, cancellation, backpressure, failed synthesis: PASS");
}
''')
            lib = ROOT/'.cache/muse-gadget-sdk-463bd2a05380fea4211b0099ad0f70933c108b20/esp32/components/sanotts/SanoTTS-jp/src'
            result = subprocess.run(['cc','-std=c11','-O1','-fsanitize=address,undefined','-c',
                str(lib/'core/openjtalk/text2mecab.c'),'-o',str(p/'normalize.o')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            command = [os.environ.get('CXX','c++'),'-std=c++17','-O1','-pthread','-fsanitize=address,undefined',
                '-I',str(p),'-I',str(ROOT/'overlay'),'-I',str(lib),str(p/'normalize.o'),
                str(ROOT/'overlay/sumi_tts.cpp'),str(p/'run.cpp'),'-o',str(p/'run')]
            result = subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result = subprocess.run([str(p/'run')],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__ == '__main__': unittest.main()
