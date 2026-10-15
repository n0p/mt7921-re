/* Debug MCU command: replaces the stub handler of CE cid 0xcb (stock: `return 0`).
 *
 * Host -> MCU payload (little endian):
 *   u32 op; u32 addr; u32 n; u32 arg[4]; u32 data[n]
 * MCU -> host event (eid DBG_EID), payload:
 *   u32 magic; u32 op; u32 status; u32 ret; u32 n; u32 data[n]
 *
 * Runs on the MCU, so unlike the host bus it can also write ILM.
 */
#include "fw.h"

#define DBG_MAGIC	0x4d543739	/* "97TM" */
#define DBG_EID		0xcb
#define DBG_MAX_WORDS	64

enum {
	DBG_PING = 0,
	DBG_READ = 1,	/* read n words at addr */
	DBG_WRITE = 2,	/* write data[0..n) to addr */
	DBG_CALL = 3,	/* ret = ((fn)addr)(arg[0..3]) */
	DBG_CALL_DETACH = 4,	/* answer first (ret 0), then call ((fn)addr)(arg[0..3]): for calls that keep the MCU
				 * busy for long or overwrite the packet SRAM that carries events (iqstream.c) */
};

struct dbg_req {
	u32 op, addr, n, arg[4];
	u32 data[];
};

struct dbg_rsp {
	u32 magic, op, status, ret, n;
	u32 data[];
};

typedef u32 (*dbg_fn)(u32, u32, u32, u32);

u32 hk_dbgcmd(struct fw_pkt *cmd)
{
	struct dbg_req *req = (struct dbg_req *)CMD_PAYLOAD(cmd);
	u32 n = req->n > DBG_MAX_WORDS ? DBG_MAX_WORDS : req->n;
	u32 out = req->op == DBG_READ ? n : 0;
	u32 plen = sizeof(struct dbg_rsp) + 4 * out;
	struct fw_pkt *evt = fw_evt_alloc(0x0c + plen);
	struct dbg_rsp *rsp;
	volatile u32 *p = (volatile u32 *)req->addr;
	u32 i, detach = 0, a[4];
	dbg_fn fn = (dbg_fn)req->addr;

	if (!evt)
		return 0xc000009a;

	rsp = (struct dbg_rsp *)EVT_PAYLOAD(evt);
	rsp->magic = DBG_MAGIC;
	rsp->op = req->op;
	rsp->status = 0;
	rsp->ret = 0;
	rsp->n = out;

	switch (req->op) {
	case DBG_PING:
		rsp->ret = 0x20260224;
		break;
	case DBG_READ:
		for (i = 0; i < n; i++)
			rsp->data[i] = p[i];
		break;
	case DBG_WRITE:
		for (i = 0; i < n; i++)
			p[i] = req->data[i];
		__asm__ volatile("isb" ::: "memory");
		rsp->ret = n;
		break;
	case DBG_CALL:
		rsp->ret = ((dbg_fn)req->addr)(req->arg[0], req->arg[1], req->arg[2], req->arg[3]);
		break;
	case DBG_CALL_DETACH:
		for (i = 0; i < 4; i++)
			a[i] = req->arg[i];	/* the command buffer may be overwritten during the call */
		detach = 1;
		break;
	default:
		rsp->status = 1;
	}

	EVT_LEN(evt) = 0x0c + plen;
	EVT_EID(evt) = DBG_EID;
	EVT_SEQ(evt) = CMD_SEQ(cmd);
	fw_evt_finalize(evt);
	fw_evt_send(evt);
	if (detach)
		fn(a[0], a[1], a[2], a[3]);
	return 0;
}
