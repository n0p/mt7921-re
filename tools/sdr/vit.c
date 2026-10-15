/* K=7 (133, 171) soft-decision Viterbi for rx80211.py, loaded through ctypes. Same trellis, metric and traceback as
 * the numpy fallback there: llr holds 2 * nbits soft values (positive = 1, 0 = erasure); next state ns has the
 * predecessors 2(ns & 31) and 2(ns & 31) + 1, both entered with input bit ns >> 5; s0a/s0b/s1a/s1b are the expected
 * +-1 symbols of those two transitions. */
#include <stdint.h>
#include <stdlib.h>

int viterbi(const double *llr, int nbits, const signed char *s0a, const signed char *s0b, const signed char *s1a,
	    const signed char *s1b, unsigned char *out)
{
	double pm[64], nm[64];
	uint64_t *dec = malloc(sizeof(*dec) * (nbits > 0 ? nbits : 1));
	int s, t;

	if (!dec)
		return -1;
	for (s = 0; s < 64; s++)
		pm[s] = 1e18;
	pm[0] = 0;
	for (t = 0; t < nbits; t++) {
		double l0 = llr[2 * t], l1 = llr[2 * t + 1], mn = 1e300;
		uint64_t d = 0;

		for (s = 0; s < 64; s++) {
			int p0 = 2 * (s & 31);
			double c0 = pm[p0] - (s0a[s] * l0 + s0b[s] * l1);
			double c1 = pm[p0 + 1] - (s1a[s] * l0 + s1b[s] * l1);

			if (c1 < c0) {
				nm[s] = c1;
				d |= 1ULL << s;
			} else {
				nm[s] = c0;
			}
			if (nm[s] < mn)
				mn = nm[s];
		}
		for (s = 0; s < 64; s++)
			pm[s] = nm[s] - mn;
		dec[t] = d;
	}
	s = 0;
	for (t = 1; t < 64; t++)
		if (pm[t] < pm[s])
			s = t;
	for (t = nbits - 1; t >= 0; t--) {
		out[t] = s >> 5;
		s = 2 * (s & 31) + (int)((dec[t] >> s) & 1);
	}
	free(dec);
	return 0;
}
