/* MT7961 WM firmware (build 20260224110949): types and firmware entry points used by hooks.
 * Addresses come from fw/fwsyms.ld; all are live (runtime) addresses.
 */
#ifndef MT7961_FW_H
#define MT7961_FW_H

typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef signed int s32;

/* Command packet as seen by CE/EXT handlers: pkt->buf points at the MCU TXD copy.
 * Payload starts at buf + 0x40; seq at +0x27; set_query at +0x26; len (u16) at +0x20.
 */
struct fw_pkt {
	u32 rsv[3];
	u8 *buf;
};

#define CMD_PAYLOAD(p)	((p)->buf + 0x40)
#define CMD_SEQ(p)	((p)->buf[0x27])
#define CMD_LEN(p)	(*(u16 *)((p)->buf + 0x20))

/* Event (MCU -> host) header fields relative to evt->buf; payload starts at +0x24 */
#define EVT_LEN(e)	(*(u16 *)((e)->buf + 0x18))
#define EVT_EID(e)	((e)->buf[0x1c])
#define EVT_SEQ(e)	((e)->buf[0x1d])
#define EVT_PAYLOAD(e)	((e)->buf + 0x24)

/* firmware functions */
struct fw_pkt *fw_evt_alloc(u32 len);		/* 0x9178ea: len counts from header +0x18 */
void fw_evt_finalize(struct fw_pkt *e);		/* 0x953dde */
u32 fw_evt_send(struct fw_pkt *e);		/* 0x953ece */
void fw_log_ctl(u32 module, u32 level);		/* 0x921898: selects module/level for the next fw_printf */
void fw_printf(const char *fmt, ...);		/* 0x9217ba */
void *fw_memset(void *d, int c, u32 n);		/* 0x915918 */

#define fw_log(...) do { fw_log_ctl(0x16, 2); fw_printf(__VA_ARGS__); } while (0)

#endif
