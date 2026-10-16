"""Compile the production send loops: stale ACK bursts must preserve their alarm."""

from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "src/udprdma.c").read_text(encoding="utf-8")


def function(name):
    match = re.search(r"^int " + name + r"\(", source, re.MULTILINE)
    assert match, name
    start = source.index("{", match.start())
    end, depth = start + 1, 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start() : end]


HARNESS = r"""
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
#include "udprdma_seq.h"
#define UDPRDMA_ERR_INVAL -4
#define UDPRDMA_ERR_NOTCONN -5
#define UDPRDMA_ERR_NACK -2
#define UDPRDMA_OK 0
#define UDPRDMA_MAX_PAYLOAD 1408
#define UDPRDMA_MAX_APP_HDR 32
#define UDPRDMA_MAX_RETRIES 4
#define UDPRDMA_RETX_TIMEOUT_US 500000
#define EF_RX_ACK 4
#define EF_TIMEOUT 64
#define STATE_CONNECTED 2
#define STATE_DISCONNECTED 3
#define WEF_OR 1
#define M_PRINTF(...) ((void)0)
#define M_DEBUG(...) ((void)0)
typedef int iop_sys_clock_t;
typedef struct {int state,event_flag;uint16_t tx_seq_nr,tx_seq_nr_acked;} udprdma_socket_t;
static int sends,alarms,cancels,active,waits,scenario,pending_timeout;
static uint16_t first_seq;
static unsigned int _timeout_cb(void *arg){return 0;}
static void ClearEventFlag(int flag,int mask){
 if(scenario==3&&waits==1)pending_timeout=1; /* alarm fires just before clear */
 if(!(mask&EF_TIMEOUT))pending_timeout=0;
}
static void USec2SysClock(int us,iop_sys_clock_t *clock){assert(us==500000);*clock=us;}
static void SetAlarm(iop_sys_clock_t *clock,unsigned int(*cb)(void*),void *arg){assert(!active);active=1;alarms++;}
static void CancelAlarm(unsigned int(*cb)(void*),void *arg){assert(active);active=0;cancels++;}
static void sent(udprdma_socket_t *s){assert(s->tx_seq_nr==first_seq);sends++;s->tx_seq_nr=(s->tx_seq_nr+1)&4095;}
static void _send_data(udprdma_socket_t*s,const void*data,uint32_t size){sent(s);}
static void _send_data_ll(udprdma_socket_t*s,const void*h,uint32_t hs,const void*d,uint32_t ds){sent(s);}
static udprdma_socket_t *current;
static void WaitEventFlag(int flag,int mask,int mode,uint32_t *bits){
 assert(active);assert(mask==(EF_RX_ACK|EF_TIMEOUT));assert(waits<100);waits++;
 if(pending_timeout){*bits=EF_TIMEOUT;return;}
 if(scenario==3){current->tx_seq_nr_acked=sends==1?(first_seq-1)&4095:first_seq;*bits=EF_RX_ACK;return;}
 if(scenario==0){
   /* Six stale ACKs followed by the actual ACK: still one transmission/alarm. */
   current->tx_seq_nr_acked=waits<=6?(first_seq-1)&4095:first_seq;
   *bits=EF_RX_ACK;
 }else if(scenario==1){
   /* Each attempt receives six stale ACKs, then its ORIGINAL alarm expires. */
   current->tx_seq_nr_acked=(first_seq-1)&4095;
   *bits=waits%7==0?EF_TIMEOUT:EF_RX_ACK;
 }else{
   /* A real timeout causes a retry with the same request sequence. */
   current->tx_seq_nr_acked=first_seq;
   *bits=waits==1?EF_TIMEOUT:(EF_RX_ACK|EF_TIMEOUT);
 }
}
@FUNCTIONS@
int main(void){
 uint8_t data[16]={0};
 for(int scatter=0;scatter<2;scatter++)for(int seq=0;seq<3;seq++)for(scenario=0;scenario<4;scenario++){
  sends=alarms=cancels=active=waits=pending_timeout=0;
  first_seq=seq==0?100:seq==1?0:4095;
  udprdma_socket_t s={STATE_CONNECTED,1,first_seq,(first_seq-1)&4095};current=&s;
  int result=scatter?udprdma_send_ll(&s,data,8,data+8,8):udprdma_send(&s,data,16);
  assert(!active&&alarms==cancels);
  if(scenario==0){assert(result==0&&sends==1&&alarms==1&&waits==7);}
  else if(scenario==1){assert(result==UDPRDMA_ERR_NACK&&sends==4&&alarms==4&&waits==28&&s.state==STATE_DISCONNECTED);}
  else if(scenario==2){assert(result==0&&sends==2&&alarms==2&&waits==2);}
  else{assert(result==0&&sends==2&&alarms==2&&waits==3);}
 }
 puts("24 actual-source send-wait cases passed");return 0;
}
"""
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    c = root / "send_wait.c"
    exe = root / "send_wait.exe"
    c.write_text(
        HARNESS.replace(
            "@FUNCTIONS@", function("udprdma_send") + "\n" + function("udprdma_send_ll")
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "gcc",
            "-std=c99",
            "-Wall",
            "-Wextra",
            "-Wno-unused-parameter",
            "-I",
            str(ROOT / "src"),
            str(c),
            "-o",
            str(exe),
        ],
        check=True,
        timeout=60,
    )
    subprocess.run([str(exe)], check=True, timeout=10)
