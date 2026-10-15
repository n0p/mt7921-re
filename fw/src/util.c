/* Small measurement helpers, reached through the debug command's DBG_CALL (op 3, addr = symbol from
 * build/hooks.nm, arg[0..3]).
 */
#include "fw.h"

#define MCU_US_TIMER	0x89020068	/* free-running 1 MHz counter (measured 1.012e6 ticks per host second) */

/* Advance of the counter at addr over us microseconds, masked: the rate of a hardware producer index such as the
 * ICAP write pointer 0x800210b4 (16-bit sample index). Keep us short enough that the counter cannot wrap. */
u32 hk_delta(u32 addr, u32 us, u32 mask, u32 unused)
{
	volatile u32 *t = (volatile u32 *)MCU_US_TIMER, *p = (volatile u32 *)addr;
	u32 t0 = *t, v0 = *p;

	(void)unused;
	while (*t - t0 < us)
		;
	return (*p - v0) & mask;
}

/* ICAP capture rate: write start to the capture control 0x80021090, wait settle us, count the producer index
 * 0x800210b4 (16-bit sample units) over us microseconds, write stop, return the advance. The whole capture runs
 * inside this call: while the ring is being written the packet SRAM cannot carry the debug command's event. */
u32 hk_icaprate(u32 us, u32 start, u32 stop, u32 settle)
{
	volatile u32 *ctl = (volatile u32 *)0x80021090, *prod = (volatile u32 *)0x800210b4;
	volatile u32 *t = (volatile u32 *)MCU_US_TIMER;
	u32 t0, v0, v1;

	*ctl = start;
	t0 = *t;
	while (*t - t0 < settle)
		;
	t0 = *t;
	v0 = *prod;
	while (*t - t0 < us)
		;
	v1 = *prod;
	*ctl = stop;
	return (v1 - v0) & 0xffff;
}

/* MCU memory throughput: n words read from src with a stride (in words), summed (dst 0) or copied to dst contiguously.
 * Returns elapsed microseconds; the sum lands in bench_sink so the reads cannot be optimized away. */
u32 bench_sink;

u32 hk_bench(u32 src, u32 n, u32 stride, u32 dst)
{
	volatile u32 *s = (volatile u32 *)src, *d = (volatile u32 *)dst, *t = (volatile u32 *)MCU_US_TIMER;
	u32 t0 = *t, i, acc = 0;

	if (dst)
		for (i = 0; i < n; i++)
			d[i] = s[i * stride];
	else
		for (i = 0; i < n; i++)
			acc += s[i * stride];
	bench_sink = acc;
	return *t - t0;
}
