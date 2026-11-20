#include "rtp_reorder.h"
#include <stdio.h>
#include <string.h>

static int failed;
#define EXPECT(x) do { if (!(x)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); failed++; } } while (0)
typedef struct { uint16_t out[96]; int n; uint16_t missing[96]; int missing_n; } Sink;
static void make(uint8_t *p, uint16_t s) { memset(p, 0, 12); p[0]=0x80; p[1]=100; p[2]=(uint8_t)(s>>8); p[3]=(uint8_t)s; }
static void emit(void *u, const uint8_t *p, int n) { Sink *s=u; if(n>=12) s->out[s->n++]=rtp_reorder_read_be16(p+2); }
static void miss(void *u,uint16_t first,uint16_t count) { Sink *s=u; for(uint16_t i=0;i<count;i++)s->missing[s->missing_n++]=(uint16_t)(first+i); }
static void input(RtpReorder*r,uint16_t s,int64_t t){uint8_t p[12];make(p,s);rtp_reorder_input(r,p,12,t);}
static void init(RtpReorder*r,Sink*s,int capacity,int64_t wait_us){memset(s,0,sizeof(*s));RtpReorderHooks h={s,emit,miss};rtp_reorder_init(r,&h,capacity,wait_us);}
static void test_order(void){RtpReorder r;Sink s;init(&r,&s,8,10000);input(&r,100,0);input(&r,102,10);input(&r,101,1000);input(&r,103,1100);EXPECT(s.n==4);for(int i=0;i<4;i++)EXPECT(s.out[i]==100+i);EXPECT(s.missing_n==0);EXPECT(r.reordered_interval==1);}
static void test_timeout(void){RtpReorder r;Sink s;init(&r,&s,8,10000);input(&r,100,0);input(&r,102,10);rtp_reorder_poll(&r,10010);EXPECT(s.n==2);EXPECT(s.out[0]==100&&s.out[1]==102);EXPECT(s.missing_n==1&&s.missing[0]==101);input(&r,101,11000);EXPECT(r.late_drop_interval==1);}
static void test_late_within_10ms(void){RtpReorder r;Sink s;init(&r,&s,8,10000);input(&r,100,0);input(&r,102,10);rtp_reorder_poll(&r,9999);EXPECT(s.n==1);EXPECT(s.missing_n==0);input(&r,101,6000);EXPECT(s.n==3);EXPECT(s.out[0]==100&&s.out[1]==101&&s.out[2]==102);EXPECT(r.reordered_interval==1);EXPECT(r.missing_interval==0);EXPECT(r.late_drop_interval==0);}
static void test_wait_boundaries(void){RtpReorder r;Sink s;init(&r,&s,8,0);input(&r,100,0);input(&r,102,10);rtp_reorder_poll(&r,10);EXPECT(s.missing_n==1);init(&r,&s,8,10000);input(&r,100,0);input(&r,102,10);rtp_reorder_poll(&r,10009);EXPECT(s.missing_n==0);rtp_reorder_poll(&r,10010);EXPECT(s.missing_n==1);init(&r,&s,8,20000);input(&r,100,0);input(&r,102,10);rtp_reorder_poll(&r,20009);EXPECT(s.missing_n==0);rtp_reorder_poll(&r,20010);EXPECT(s.missing_n==1);}
static void test_wrap_and_duplicate(void){RtpReorder r;Sink s;init(&r,&s,8,10000);input(&r,65534,0);input(&r,0,1);input(&r,65535,2);input(&r,0,3);input(&r,1,4);EXPECT(s.n==4);EXPECT(s.out[0]==65534&&s.out[1]==65535&&s.out[2]==0&&s.out[3]==1);EXPECT(r.late_drop_interval==1);}
static void test_capacity_and_flush(void){RtpReorder r;Sink s;init(&r,&s,1,10000);input(&r,100,0);input(&r,102,1);input(&r,103,2);EXPECT(r.missing_interval==1);EXPECT(s.n==3&&s.out[1]==102&&s.out[2]==103);init(&r,&s,64,10000);input(&r,100,0);for(uint16_t n=102;n<=165;n++)input(&r,n,n);EXPECT(rtp_reorder_count(&r)==64);input(&r,101,1000);EXPECT(s.n==66&&s.out[1]==101&&s.out[65]==165);EXPECT(s.missing_n==0);input(&r,200,2000);EXPECT(rtp_reorder_count(&r)==1);rtp_reorder_flush(&r);EXPECT(rtp_reorder_count(&r)==0);input(&r,300,3000);EXPECT(s.out[s.n-1]==300);}
static void test_invalid_init_defaults(void){RtpReorder r;Sink s;init(&r,&s,0,-1);EXPECT(r.capacity==RTP_REORDER_DEFAULT_CAPACITY);EXPECT(r.wait_us==RTP_REORDER_DEFAULT_WAIT_US);}
int main(void){test_order();test_timeout();test_late_within_10ms();test_wait_boundaries();test_wrap_and_duplicate();test_capacity_and_flush();test_invalid_init_defaults();if(failed)return 1;puts("rtp_reorder_selftest: ok");return 0;}
