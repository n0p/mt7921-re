/* IQ streaming: ICAP mode-0 ring -> MCU WFDMA TX ring 0 -> USB bulk IN 0x84 (BirriaSDR's method,
 * work/ext/birria/ours/firmware/iq_stream.S, inside the running firmware), with an energy gate.
 *
 * At runtime the firmware sends its events through the packet SRAM path, so MCU TX ring 0 (4 descriptors at
 * 0x02109900, indices 2/2 since boot) is idle; this borrows it and restores it afterwards. Each USB packet is a
 * 48-byte header that mt7921 parses as an unsolicited MCU event (rxd[0] PKT_TYPE 7, eid, seq 0, then freed),
 * followed by 4 * chunk bytes of raw ring words read straight from SRAM by the DMA, plus 4 trailer bytes the USB
 * engine consumes. Keep one descriptor in flight (iq_cfg.inflight 1): three in flight corrupted the stream in a
 * CW-tone phase test, as BirriaSDR warns for queued SRAM reads.
 *
 * Throughput (card A, USB 3 through QEMU) is ~19 us per packet + 6.5 us per KiB, at most ~150 MB/s: below the ICAP's
 * fixed 40 Msps x 4 bytes. So the stream is gated: once the newest iq_cfg.nsum samples have had a mean DC-removed |I|+|Q| below
 * iq_cfg.thresh for iq_cfg.hang_us, the read position skips ahead to iq_cfg.pre samples behind the producer; while
 * there is energy it streams contiguously, and the 256 KiB ring absorbs the deficit for ~20 ms. A ring overrun also
 * skips ahead instead of stopping. Each header carries the payload's absolute sample position, so the host can lay
 * the chunks on one time axis. thresh 0 streams everything (skipping on overrun).
 *
 * Header words: [9] packet sequence, [10] "QIQQ" marker, [11] absolute position (40 Msps samples, mod 2^32) of the
 * payload's first sample.
 *
 * The host programs the capture (tools/guest/icap.py, ICAP_SETUP_ONLY=1) and iq_cfg, then calls this through the
 * debug command's DBG_CALL_DETACH, which answers before the call: the ring overwrites the packet SRAM that carries
 * events. It stops after npkt packets, after iq_cfg.dur_ms, when iq_cfg.stop becomes non-zero (the host writes it
 * through the USB register path, which does not need the MCU), or on a stall. Results land in iq_stat.
 */
#include "fw.h"

#define WFDMA_MCU	0x54000000u
#define WF(o)		(*(volatile u32 *)(WFDMA_MCU + (o)))
#define GLO_CFG		0x208
#define RST_DTX_IDX	0x20c
#define TX0_BASE	0x300
#define TX0_CNT		0x304
#define TX0_CIDX	0x308
#define TX0_DIDX	0x30c
#define GLO_TX_EN	(1u << 0)
#define GLO_BUSY	((1u << 1) | (1u << 3))
#define GLO_B30		(1u << 30)

#define ICAP_CTL	(*(volatile u32 *)0x80021090)
#define ICAP_RING_START	(*(volatile u32 *)0x80021098)	/* first ring index (4-byte samples) */
#define ICAP_RING_END	(*(volatile u32 *)0x8002109c)	/* last 4-sample group: ring samples - 4 */
#define ICAP_PROD	(*(volatile u32 *)0x800210b4)	/* producer, sample (word) index */
#define ICAP_ARM	0x4f1		/* BirriaSDR's armed state; GO only starts a capture from here */
#define ICAP_GO		0x24f3
#define ICAP_STOP	0x4f0
#define US_NOW		(*(volatile u32 *)0x89020068)

#define RING		0xe0200000u
#define RING_MAX	65536u			/* the 256 KiB packet area */
#define CHUNK_MAX	4080u			/* 48 + 4 * 4080 + 4 <= 16384, mt76u's RX URB */
#define HDR_BYTES	48u
#define NSLOT		4u

enum { IQ_DONE = 2, IQ_STOPPED = 3, IQ_BUSY = 4, IQ_NOT_IDLE = 5, IQ_SRC_TIMEOUT = 6, IQ_DMA_TIMEOUT = 7 };

struct dmad {
	u32 sdp0, ctrl, sdp1, info;
};

struct iq_cfg {
	u32 chunk;	/* samples per packet, multiple of 4, at most CHUNK_MAX */
	u32 inflight;	/* descriptors in flight, 1..3 */
	u32 thresh;	/* gate threshold on the mean DC-removed |I|+|Q| of the newest nsum samples; 0 = no gate */
	u32 hang_us;	/* keep streaming this long after the last check above thresh */
	u32 pre;	/* samples kept behind the producer while skipping (pre-trigger) */
	u32 nsum;	/* samples per energy check */
	u32 stop;	/* the host writes non-zero to end the stream */
	u32 dur_ms;	/* wall-clock limit (a gated stream may send nothing for a long time); 0 = none */
	u32 ratio16;	/* adaptive gate: busy when the check exceeds the idle noise floor x ratio16 / 16 (and thresh); 0 = off */
	u32 hits;	/* consecutive checks above the gate before opening it (rejects us-long DC steps); 0 = 1 */
};

struct iq_stat {
	u32 state, sent, max_backlog, t_us, skipped, overruns, busy_checks, checks, floor;
};

struct iq_cfg iq_cfg = { 4080, 1, 0, 500, 4096, 32, 0, 2000, 0, 1 };
struct iq_stat iq_stat;
static struct dmad iq_ring[NSLOT] __attribute__((aligned(16)));
static u32 iq_hdr[NSLOT][16] __attribute__((aligned(64)));

static inline void dsb(void)
{
	__asm__ volatile("dsb" ::: "memory");
}

/* Unwrapped producer position: add the index advance since the last poll; the index wraps at ring samples. */
static inline u32 track(u32 *p, u32 *last, u32 ring)
{
	u32 now = ICAP_PROD & 0xffff, d = now - *last;

	if ((s32)d < 0)
		d += ring;
	*p += d;
	*last = now;
	return *p;
}

/* Sum of |I - dcI| + |Q - dcQ| over the n samples ending a few samples behind ring index idx. dcI/dcQ follow the
 * samples with a one-pole tracker (~100 kHz at 40 Msps) kept across calls, so the receiver's DC offset and the carrier
 * leakage of a nearby transmitter (kHz from the LO) do not hold the gate open, while narrow channels keep their full
 * energy. (First differences did the same for DC but cost ~10 dB on a 5 MHz channel at 40 Msps.) */
static s32 dc_i, dc_q;		/* DC estimates x 64 */
static u32 floor16;		/* idle noise floor of the gate's check energy x 16, kept across slices */

static u32 energy(u32 idx, u32 ring, u32 n)
{
	volatile u32 *buf = (volatile u32 *)RING;
	u32 i, k, e = 0;

	k = idx >= n + 8 ? idx - n - 8 : idx + ring - n - 8;
	for (i = 0; i < n; i++) {
		u32 w = buf[k];
		s32 q = (s32)(w << 18) >> 18, v = (s32)(w << 2) >> 18;	/* Q [13:0], I [29:16] */

		dc_q += ((q << 6) - dc_q) >> 6;
		dc_i += ((v << 6) - dc_i) >> 6;
		q -= dc_q >> 6;
		v -= dc_i >> 6;
		e += (q < 0 ? -q : q) + (v < 0 ? -v : v);
		if (++k == ring)
			k = 0;
	}
	return e;
}

/* npkt: packets to send; eid: event id; settle_us: wait before the capture starts; timeout_us: producer / DMA stall
 * limit.
 * Returns state << 24 | packets sent. */
u32 hk_iqstream(u32 npkt, u32 eid, u32 settle_us, u32 timeout_us)
{
	u32 glo = WF(GLO_CFG), base = WF(TX0_BASE), cnt = WF(TX0_CNT);
	u32 chunk = iq_cfg.chunk & ~3u, inflight = iq_cfg.inflight, ring = 0;
	u32 thresh = iq_cfg.thresh * iq_cfg.nsum, busy_until = 0, run = 0;
	u32 hits = iq_cfg.hits ? iq_cfg.hits : 1;
	u32 slot = 0, seq = 0, err = IQ_DONE, last, p, r, ridx, t, t0, t_prog, i;

	fw_memset(&iq_stat, 0, sizeof(iq_stat));
	iq_stat.state = 1;
	if (!chunk || chunk > CHUNK_MAX)
		chunk = CHUNK_MAX;
	if (!inflight || inflight >= NSLOT)
		inflight = 1;
	while (ring + chunk <= RING_MAX)
		ring += chunk;
	if (glo & GLO_BUSY) {
		iq_stat.state = IQ_BUSY;
		return IQ_BUSY << 24;
	}
	if (WF(TX0_CIDX) != WF(TX0_DIDX)) {
		iq_stat.state = IQ_NOT_IDLE;
		return IQ_NOT_IDLE << 24;
	}

	for (i = 0; i < NSLOT; i++) {
		u32 *h = iq_hdr[i];

		fw_memset(h, 0, sizeof(iq_hdr[i]));
		h[0] = 0x38000000u | (HDR_BYTES + 4 * chunk);	/* rxd[0]: PKT_TYPE 7 (event), byte count */
		h[6] = 0x38000000u | (HDR_BYTES - 24 + 4 * chunk);	/* MCU rxd len, pkt_type_id */
		h[7] = eid & 0xff;				/* eid; seq 0 and option 0: unsolicited */
		h[8] = 0xf5;					/* ext_eid */
		h[10] = 0x51514951;				/* "QIQQ" marker */
		fw_memset(&iq_ring[i], 0, sizeof(iq_ring[i]));
	}
	dsb();

	WF(GLO_CFG) = glo & ~(GLO_TX_EN | GLO_B30);
	WF(TX0_BASE) = (u32)iq_ring;
	WF(TX0_CNT) = NSLOT;
	WF(RST_DTX_IDX) = 1;
	WF(TX0_CIDX) = 0;
	WF(GLO_CFG) = (glo & ~GLO_B30) | GLO_TX_EN;
	dsb();

	/* settle_us before the capture starts: the detached debug call's response event (and any other packet still
	 * queued in the packet SRAM) must leave before the ring overwrites it */
	t = US_NOW;
	while (US_NOW - t < settle_us)
		;
	ICAP_RING_START = 0;
	ICAP_RING_END = ring - 4;
	ICAP_CTL = ICAP_ARM;	/* re-arm: after a previous run's STOP, GO alone does not restart the capture */
	dsb();
	ICAP_CTL = ICAP_GO;
	t = US_NOW;
	while (US_NOW - t < 100)	/* run-in */
		;
	last = ICAP_PROD & 0xffff;
	p = last;
	for (ridx = 0; ridx < p; ridx += chunk)	/* first whole chunk after the start */
		;
	r = ridx;
	if (ridx >= ring)
		ridx = 0;
	t0 = t_prog = US_NOW;

	while (seq < npkt) {
		struct dmad *d = &iq_ring[slot];
		u32 now, p0 = p, over;

		now = US_NOW;
		if (iq_cfg.stop || (iq_cfg.dur_ms && now - t0 > 1000 * iq_cfg.dur_ms)) {
			err = IQ_STOPPED;
			break;
		}
		if (track(&p, &last, ring) != p0)
			t_prog = now;
		else if (now - t_prog > timeout_us) {
			err = IQ_SRC_TIMEOUT;
			break;
		}
		if (thresh || iq_cfg.ratio16) {
			u32 e = energy(last, ring, iq_cfg.nsum), lim = thresh;

			iq_stat.checks++;
			if (iq_cfg.ratio16) {	/* floor x ratio, the floor learning only from checks below it */
				u32 adapt = (floor16 >> 4) * iq_cfg.ratio16 >> 4;

				if (!floor16)
					floor16 = e << 4;
				if (adapt > lim)
					lim = adapt;
				if (e < (floor16 >> 4) * 2)
					floor16 += (s32)((e << 4) - floor16) >> 7;
			}
			run = e > lim ? run + 1 : 0;
			if (run >= hits) {
				busy_until = now + iq_cfg.hang_us;
				iq_stat.busy_checks++;
			}
		}
		over = (s32)(p - r) > (s32)(ring - (inflight + 2) * chunk);
		if (((thresh || iq_cfg.ratio16) && (s32)(busy_until - now) <= 0) || over) {
			iq_stat.overruns += over;
			while ((s32)(p - r) > (s32)(iq_cfg.pre + chunk)) {	/* idle (or overrun): skip ahead */
				r += chunk;
				ridx += chunk;
				if (ridx >= ring)
					ridx = 0;
				iq_stat.skipped++;
			}
			continue;
		}
		if ((s32)(p - r) < (s32)chunk)		/* p - r < 0 before the first boundary */
			continue;
		if ((s32)(p - r) > (s32)iq_stat.max_backlog)
			iq_stat.max_backlog = p - r;

		t = US_NOW;
		while (((slot - WF(TX0_DIDX)) & (NSLOT - 1)) >= inflight) {	/* descriptors still owned by the DMA */
			track(&p, &last, ring);
			if (US_NOW - t > timeout_us) {
				err = IQ_DMA_TIMEOUT;
				goto stop;
			}
		}
		iq_hdr[slot][9] = seq;
		iq_hdr[slot][11] = r;
		d->sdp0 = (u32)iq_hdr[slot];
		d->sdp1 = RING + 4 * ridx;
		d->info = 0;
		d->ctrl = (HDR_BYTES << 16) | (1u << 14) | (4 * chunk + 4);
		dsb();
		slot = (slot + 1) % NSLOT;
		WF(TX0_CIDX) = slot;
		r += chunk;
		ridx += chunk;
		if (ridx >= ring)
			ridx = 0;
		iq_stat.sent = ++seq;
	}

stop:
	iq_stat.t_us = US_NOW - t0;
	ICAP_CTL = ICAP_STOP;
	/* Let the descriptors in flight finish before the ring is reset under them: resetting it while the host has
	 * stalled bulk IN (two cards streaming through one VM) wedged the USB DMA, and the card's vendor requests timed
	 * out until the driver reset it. */
	t = US_NOW;
	while (WF(TX0_DIDX) != WF(TX0_CIDX) && US_NOW - t < timeout_us)
		;
	WF(GLO_CFG) = glo & ~(GLO_TX_EN | GLO_B30);
	WF(TX0_BASE) = base;
	WF(TX0_CNT) = cnt;
	WF(RST_DTX_IDX) = 1;
	WF(TX0_CIDX) = 0;
	WF(GLO_CFG) = glo;
	dsb();
	iq_cfg.stop = 0;
	iq_stat.floor = floor16 >> 4;
	iq_stat.state = err;
	return err << 24 | seq;
}
