"""Reproduce the emoji failure with the same dictionary used on C152."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_controls import ROOT, SDK


class SpeechText(unittest.TestCase):
    def test_real_dictionary_and_utf8_decoration_filter(self):
        source = r'''
#include "sumi_speech_text.h"
#include "sumi_core.h"
#include "jdict.h"
#include "openjtalk/text2mecab.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
static int analyze(jdict_t *d,const char *text){
 unsigned char key[1024];size_t n=sizeof(key);jdict_token_t tokens[96];
 void *arena=malloc(136*1024);assert(arena);
 assert(!jdict_encode_key(d,(const uint8_t*)text,strlen(text),key,&n));
 int count=jdict_analyze(d,key,n,arena,136*1024,tokens,96);free(arena);return count;
}
static int unreadable(jdict_t *d,const char *text){
 unsigned char key[1024];size_t n=sizeof(key);jdict_token_t tokens[96];
 void *arena=malloc(136*1024);assert(arena);
 assert(!jdict_encode_key(d,(const uint8_t*)text,strlen(text),key,&n));
 int count=jdict_analyze(d,key,n,arena,136*1024,tokens,96);assert(count>0);
 int missing=0;
 for(int i=0;i<count;i++)if(tokens[i].entry & JDICT_UNKNOWN_FLAG){
  char surface[128],feature[320];
  assert(jdict_key_to_utf8(d,key,tokens[i].begin,tokens[i].end,surface,sizeof(surface))>=0);
  missing+=jdict_unk_guess(d,tokens[i].entry,surface,feature,sizeof(feature))<0;
 }
 free(arena);return missing;
}
int main(int argc,char**argv){
 assert(argc==2);FILE*f=fopen(argv[1],"rb");assert(f);fseek(f,0,SEEK_END);long size=ftell(f);rewind(f);
 unsigned char*blob=malloc(size);assert(fread(blob,1,size,f)==(size_t)size);fclose(f);
 jdict_t dict;assert(!jdict_open(&dict,blob,size));
 assert(analyze(&dict,"うん、聞こえるよ、しろねこ🤍")<0);
 char first[]="うん、聞こえるよ、しろねこ🤍";
 assert(sumi_speech_text(first)&&!strcmp(first,"うん、聞こえるよ、しろねこ")&&analyze(&dict,first)>0);
 char reply[]="うん、聞こえるよ、しろねこ🤍\n今度は言葉もすごく綺麗に届いたよ。「M5Stackからの会話テストだよ、今度こそ声が聞こえるかな」って、ばっちりだよ。\n夜遅くまで改良してくれてた成果、出てる感じがするなぁ。";
 assert(sumi_speech_text(reply)&&strstr(reply,"M5Stack")&&strstr(reply,"しろねこ"));
 for(size_t pos=0;reply[pos];){size_t n=sumi_utf8_chunk(reply+pos,180);char chunk[181];memcpy(chunk,reply+pos,n);chunk[n]=0;assert(analyze(&dict,chunk)>0);pos+=n;}
 char decorated[]="  **こんにちは**🤍❤️👩‍💻\n世界🐈‍⬛。 ";
 assert(sumi_speech_text(decorated)&&!strcmp(decorated,"こんにちは 世界 。"));
 char only[]="🤍❤️👩‍💻\n**";assert(sumi_speech_text(only)&&!only[0]);
 char rare[]="𠮷田さん、20度。";assert(sumi_speech_text(rare)&&!strcmp(rare,"𠮷田さん、20度。"));
 char short_utf8[]={0xf0,0x9f,0};assert(!sumi_speech_text(short_utf8));
 char overlong[]={0xc0,0x80,0};assert(!sumi_speech_text(overlong));
 char surrogate[]={0xed,0xa0,0x80,0};assert(!sumi_speech_text(surrogate));
 const char *terms="M5StackからMuseへ。StopWatchで5秒、速度0.9倍。TTS。";
 size_t needed=sumi_speech_readings(terms,NULL,0);
 char readings[512],normalized[1536];assert(needed<sizeof(readings));
 assert(sumi_speech_readings(terms,readings,sizeof(readings))==needed);
 assert(strstr(readings,"エムファイブスタック")&&strstr(readings,"ミューズ")&&strstr(readings,"ストップウォッチ"));
 assert(sumi_speech_text(readings)&&text2mecab(normalized,sizeof(normalized),readings)==TEXT2MECAB_RESULT_SUCCESS);
 assert(unreadable(&dict,"M5Stackからの会話テストだよ。")>=2);
 assert(unreadable(&dict,"5秒")>0&&unreadable(&dict,normalized)==0);
 assert(strstr(normalized,"５秒")&&strstr(normalized,"０．９倍")&&strstr(normalized,"ＴＴＳ"));
 const char *boundaries="Museum XM5Stack M5Stacker StopWatcher M5StackのmUse";
 assert(sumi_speech_readings(boundaries,readings,sizeof(readings))!=SIZE_MAX);
 assert(!strcmp(readings,"Museum XM5Stack M5Stacker StopWatcher エムファイブスタックのミューズ"));
 char exact[31];size_t need=sumi_speech_readings("M5Stack",NULL,0);assert(need==30);
 assert(sumi_speech_readings("M5Stack",exact,need)==SIZE_MAX);
 assert(sumi_speech_readings("M5Stack",exact,need+1)==need&&!strcmp(exact,"エムファイブスタック"));
 assert(sumi_speech_readings("M",readings,sizeof(readings))==1&&!strcmp(readings,"M"));
 free(blob);return 0;
}
'''
        with tempfile.TemporaryDirectory() as td:
            p = Path(td); (p/'run.c').write_text(source)
            core = SDK/'components/sanotts/SanoTTS-jp/src/core'
            result = subprocess.run(['cc','-std=c11','-O1','-fsanitize=address,undefined','-I',str(core),
                '-I',str(ROOT/'overlay'),str(p/'run.c'),str(core/'jdict.c'),str(core/'openjtalk/text2mecab.c'),'-o',str(p/'run')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result = subprocess.run([str(p/'run'),str(ROOT/'.cache/dict.bin')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
