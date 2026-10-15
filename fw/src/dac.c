/* The band tone generator as a slow IQ DAC. In its DC mode (FUN_009691d2(1, 0, 0, 0): chain 0, type 0, FREQ 0) the
 * transmitted carrier is L + g * (I + jQ), with (I, Q) the word at 0x8308fc08 (I [11:0], Q [27:16], 12-bit two's
 * complement) and L the LO leakage. A plain write takes effect at once; the fc0c [0] latch pulse instead knocks the
 * generator out of that state (X410 measurement, notes/09). Start the tone first, stop it with FUN_00969126 and
 * reset the rig afterwards (the stop leaves non-stock values behind, notes/07).
 */
#include "fw.h"

#define MCU_US_TIMER	0x89020068	/* free-running 1 MHz counter */
#define TONE_DC_IQ	0x8308fc08

/* cos(2 pi k / 64), Q15 */
static const short cos64[64] = {
	32767, 32609, 32137, 31356, 30273, 28898, 27245, 25329, 23170, 20787, 18204, 15446, 12539, 9512, 6393, 3212, 0,
	-3212, -6393, -9512, -12539, -15446, -18204, -20787, -23170, -25329, -27245, -28898, -30273, -31356, -32137,
	-32609, -32767, -32609, -32137, -31356, -30273, -28898, -27245, -25329, -23170, -20787, -18204, -15446, -12539,
	-9512, -6393, -3212, 0, 3212, 6393, 9512, 12539, 15446, 18204, 20787, 23170, 25329, 27245, 28898, 30273, 31356,
	32137, 32609
};

/* Play nsamp samples of amp * exp(j 2 pi step k / 64) into the DC I/Q word, one every period_us microseconds (0: as
 * fast as the loop runs), i.e. a tone at step / 64 / period_us MHz from the carrier. amp <= 2047.
 * Returns the elapsed microseconds (the achieved sample rate for period_us = 0). */
u32 hk_dacplay(u32 step, u32 period_us, u32 nsamp, u32 amp)
{
	volatile u32 *t = (volatile u32 *)MCU_US_TIMER, *dac = (volatile u32 *)TONE_DC_IQ;
	u32 t0 = *t, next = t0, ph = 0, i;
	s32 c, s;

	for (i = 0; i < nsamp; i++) {
		c = (cos64[ph & 63] * (s32)amp) >> 15;
		s = (cos64[(ph + 48) & 63] * (s32)amp) >> 15;	/* sin x = cos(x - pi/2) */
		*dac = ((u32)c & 0xfff) | (((u32)s & 0xfff) << 16);
		ph += step;
		if (period_us) {
			next += period_us;
			while ((s32)(*t - next) < 0)
				;
		}
	}
	return *t - t0;
}
