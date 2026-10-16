//! Soft-decision Viterbi decoder for the IEEE 802.11 convolutional code, and the depuncturer.
//!
//! Ports `tools/sdr/vit.c` (the C decoder that rx80211.py loads through ctypes) and its numpy fallback
//! `rx80211.viterbi`, with the same trellis, metric, tie-breaking and traceback. Given the same soft values it
//! returns the same bits, bit for bit, because it does the same f64 operations in the same order.
//!
//! # The code
//! A rate-1/2 convolutional code with constraint length K = 7 (IEEE 802.11-2020 17.3.5.6). Every input bit produces
//! two output bits, A and B. Each output is the parity (XOR) of a subset of the encoder register, which holds the
//! input bit and the 6 previous input bits. The subsets are the generator polynomials g0 = 133 and g1 = 171
//! (octal), read as masks over the register, delay 0 first:
//!
//! ```text
//!   delay:    0 1 2 3 4 5 6
//!   g0 = 133: 1 0 1 1 0 1 1   ->  A = in ^ d2 ^ d3 ^ d5 ^ d6
//!   g1 = 171: 1 1 1 1 0 0 1   ->  B = in ^ d1 ^ d2 ^ d3 ^ d6
//! ```
//!
//! # Trellis convention (as in vit.c)
//! A state is the 6 previous input bits: bit 5 is the most recent one (delay 1) and bit 0 the oldest (delay 6).
//! Input `b` in state `s` leads to state `(s >> 1) | (b << 5)`. So next state `ns` has exactly two predecessors,
//! `2 * (ns & 31)` ("even") and `2 * (ns & 31) + 1` ("odd"), and both are entered with input bit `ns >> 5`. The
//! decoder starts in state 0, the transmitter's initial state.
//!
//! # Metric (as in vit.c)
//! The soft values are LLR-like: positive means the coded bit is more likely 1, and 0 means no information (an
//! erasure: a bit removed by puncturing). Each transition expects a pair of +-1 symbols (a, b), where +1 stands for
//! bit 1. Its branch metric is the negated correlation `-(a * l0 + b * l1)`. Path metrics are minimised, and after
//! every step they are shifted so that the best one is 0. When both predecessors give the same metric, the even one
//! wins (`c1 < c0` is strict). At the end the traceback starts from the best state (the lowest index on ties), not
//! from state 0: the receiver also decodes the scrambled pad bits that follow the tail.

/// Constraint length K: each output depends on the input bit and the 6 bits before it.
pub const CONSTRAINT_LENGTH: usize = 7;
/// Trellis states: all values of the 6 previous input bits.
pub const NUM_STATES: usize = 1 << (CONSTRAINT_LENGTH - 1);
/// Generator polynomial of output A, as a mask over the 7-bit register (delay 0 = bit 6, delay 6 = bit 0).
pub const G0: u32 = 0o133;
/// Generator polynomial of output B.
pub const G1: u32 = 0o171;

/// vit.c's initial path metric of the states the encoder cannot start in.
const UNREACHABLE_METRIC: f64 = 1e18;
/// vit.c's initial value of the running minimum (larger than any reachable metric).
const MIN_SEARCH_START: f64 = 1e300;

/// Encoder outputs (A, B) when input bit `bit` (0/1) enters the encoder in state `state` (0..64).
pub const fn encoder_output(state: usize, bit: usize) -> (u8, u8) {
    let register = ((bit << 6) | state) as u32; // bit 6 = input (delay 0), bits 5..0 = delays 1..6
    (((register & G0).count_ones() & 1) as u8, ((register & G1).count_ones() & 1) as u8)
}

/// State after input bit `bit` enters in state `state`.
pub const fn next_state(state: usize, bit: usize) -> usize {
    (state >> 1) | (bit << 5)
}

/// The trellis splits into 32 "butterflies": butterfly j joins the two predecessors 2j and 2j+1 to the two next
/// states j (input bit 0) and j+32 (input bit 1).
const BUTTERFLIES: usize = NUM_STATES / 2;

/// For butterfly j, the expected output pair of the transition 2j -> j, as an index 2A + B into the per-step table
/// of branch correlations. The butterfly's other three transitions expect this pair or its complement, because both
/// generators tap the input bit (delay 0) and the oldest bit (delay 6):
/// - 2j+1 -> j: the oldest bit differs, so both outputs flip;
/// - 2j -> j+32: the input bit differs, so both outputs flip;
/// - 2j+1 -> j+32: both differ, so the outputs are those of 2j -> j.
///
/// Flipping both expected symbols negates the correlation exactly: (-a) l0 + (-b) l1 = -(a l0 + b l1) holds bit for
/// bit in IEEE arithmetic. So one correlation m per butterfly gives all four branches, with the values vit.c
/// computes per state (checked by the `butterfly_complements` test).
const BUTTERFLY_BRANCH: [usize; BUTTERFLIES] = butterfly_branches();

const fn butterfly_branches() -> [usize; BUTTERFLIES] {
    let mut table = [0; BUTTERFLIES];
    let mut j = 0;
    while j < BUTTERFLIES {
        let (a, b) = encoder_output(2 * j, 0);
        table[j] = 2 * a as usize + b as usize;
        j += 1;
    }
    table
}

/// Path metrics of all 64 states, grouped by parity: `even[i]` = state 2i, `odd[i]` = state 2i+1. Butterfly j reads
/// `even[j]` and `odd[j]`, its two predecessors.
type Metrics = [f64; BUTTERFLIES];

/// The decisions of one step: 1 when a state was entered from its odd predecessor, 0 from the even one.
/// `[0][j]` is state j, `[1][j]` state j+32, so state s is `[s >> 5][s & 31]`. (vit.c packs the same 64 decisions
/// into the bits of a u64; a byte each keeps the step loop free of bit manipulation.)
type StepDecisions = [[u8; BUTTERFLIES]; 2];

/// The add-compare-select of one trellis step for all 32 butterflies.
///
/// Inputs: the metrics of the predecessors (`even`, `odd`) and each butterfly's correlation `corr[j]` (the branch
/// 2j -> j). Outputs: the new metrics of states j (`low[j]`) and j+32 (`high[j]`), and for each new state whether it
/// was entered from the odd predecessor (1) or the even one (0).
///
/// The candidates are vit.c's `pm[pred] - corr`. The branches into j are m (from 2j) and -m (from 2j+1); those into
/// j+32 are -m (from 2j) and m (from 2j+1). x - (-m) is computed as x + m, the same value. The even predecessor wins
/// ties (vit.c: `c1 < c0`).
///
/// The selections are written without branches: on noisy input the decisions are coin flips, which a branch
/// predictor cannot learn (branchy code measured 4x slower).
#[inline(always)]
fn add_compare_select(
    even: &Metrics,
    odd: &Metrics,
    corr: &[f64; BUTTERFLIES],
    low: &mut Metrics,
    high: &mut Metrics,
    decisions: &mut StepDecisions,
) {
    for j in 0..BUTTERFLIES {
        let m = corr[j];
        let (into_low_even, into_low_odd) = (even[j] - m, odd[j] + m);
        let (into_high_even, into_high_odd) = (even[j] + m, odd[j] - m);
        let take_low_odd = into_low_odd < into_low_even;
        let take_high_odd = into_high_odd < into_high_even;
        low[j] = if take_low_odd { into_low_odd } else { into_low_even };
        high[j] = if take_high_odd { into_high_odd } else { into_high_even };
        decisions[0][j] = take_low_odd as u8;
        decisions[1][j] = take_high_odd as u8;
    }
}

/// vit.c's normalisation `pm = nm - min`, and the regrouping of the new metrics by parity for the next step:
/// state 2i is `low[2i]` when 2i < 32, else `high[2i - 32]`.
#[inline(always)]
fn normalise(low: &Metrics, high: &Metrics, best: f64, even: &mut Metrics, odd: &mut Metrics) {
    const HALF: usize = BUTTERFLIES / 2;
    for i in 0..HALF {
        even[i] = low[2 * i] - best;
        odd[i] = low[2 * i + 1] - best;
        even[i + HALF] = high[2 * i] - best;
        odd[i + HALF] = high[2 * i + 1] - best;
    }
}

/// Decodes `nbits` bits from the soft values `llr[0 .. 2 * nbits]` (`llr[2t]`, `llr[2t + 1]` = outputs A and B of
/// step t; positive = 1, 0 = erasure). Port of vit.c `viterbi()`; returns one 0/1 byte per decoded bit.
///
/// vit.c loops over the 64 next states. This loops over the 32 butterflies, which produce the same candidate
/// metrics with the same f64 operations, so the decoded bits are identical to vit.c's (checked against it by the
/// `viterbi_matches_vit_c` integration test).
///
/// Panics if `llr` holds fewer than `2 * nbits` values.
pub fn viterbi(llr: &[f32], nbits: usize) -> Vec<u8> {
    assert!(llr.len() >= 2 * nbits, "viterbi: {} soft values for {} bits", llr.len(), nbits);
    let mut even: Metrics = [UNREACHABLE_METRIC; BUTTERFLIES];
    let mut odd: Metrics = [UNREACHABLE_METRIC; BUTTERFLIES];
    even[0] = 0.0; // the encoder starts in state 0
    let mut corr = [0.0f64; BUTTERFLIES];
    let mut low: Metrics = [0.0; BUTTERFLIES];
    let mut high: Metrics = [0.0; BUTTERFLIES];
    let mut decisions: Vec<StepDecisions> = vec![[[0; BUTTERFLIES]; 2]; nbits];
    for (t, step_decisions) in decisions.iter_mut().enumerate() {
        let l0 = llr[2 * t] as f64;
        let l1 = llr[2 * t + 1] as f64;
        // correlation a * l0 + b * l1 of the received pair with each expected pair (a, b) in +-1 form,
        // indexed by 2 * A + B (A, B = the expected bits); written as vit.c evaluates it (s0a * l0 + s0b * l1)
        let pair_corr = [-l0 + -l1, -l0 + l1, l0 + -l1, l0 + l1];
        for (c, &branch) in corr.iter_mut().zip(BUTTERFLY_BRANCH.iter()) {
            *c = pair_corr[branch];
        }
        add_compare_select(&even, &odd, &corr, &mut low, &mut high, step_decisions);
        let best = minimum(&low, &high);
        normalise(&low, &high, best, &mut even, &mut odd);
    }
    // traceback from the best final state (first minimum in state order, as vit.c's strict `<` scan)
    let metric = |s: usize| if s.is_multiple_of(2) { even[s / 2] } else { odd[s / 2] };
    let mut state = 0;
    for s in 1..NUM_STATES {
        if metric(s) < metric(state) {
            state = s;
        }
    }
    let mut bits = vec![0u8; nbits];
    for t in (0..nbits).rev() {
        bits[t] = (state >> 5) as u8; // the input bit that led into `state`
        state = 2 * (state & 31) + decisions[t][state >> 5][state & 31] as usize; // its predecessor
    }
    bits
}

/// The smallest of the 64 new path metrics, as vit.c's running minimum finds it (`nm[s] < mn` from mn = 1e300, so
/// NaNs are skipped). Eight independent partial minima replace one chain of 64 dependent comparisons; the minimum of
/// a set does not depend on the order of the comparisons, so the value is the same.
#[inline(always)]
fn minimum(low: &Metrics, high: &Metrics) -> f64 {
    const PARTS: usize = 8;
    let mut part = [MIN_SEARCH_START; PARTS];
    for chunk in low.as_chunks::<PARTS>().0.iter().chain(high.as_chunks::<PARTS>().0) {
        for k in 0..PARTS {
            part[k] = if chunk[k] < part[k] { chunk[k] } else { part[k] };
        }
    }
    let mut best = MIN_SEARCH_START;
    for p in part {
        best = if p < best { p } else { best };
    }
    best
}

/// Convolutional encoder (port of `ofdm80211.conv_encode`): starts in state 0, two output bits (A, B) per input bit.
pub fn conv_encode(bits: &[u8]) -> Vec<u8> {
    let mut state = 0;
    let mut out = Vec::with_capacity(2 * bits.len());
    for &b in bits {
        let (a, bb) = encoder_output(state, b as usize & 1);
        out.push(a);
        out.push(bb);
        state = next_state(state, b as usize & 1);
    }
    out
}

/// Coding rates of the 802.11 OFDM PHY. 2/3 and 3/4 are obtained from the rate-1/2 code by puncturing.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CodeRate {
    Half,
    TwoThirds,
    ThreeQuarters,
}

impl CodeRate {
    pub fn numerator(self) -> usize {
        match self {
            CodeRate::Half => 1,
            CodeRate::TwoThirds => 2,
            CodeRate::ThreeQuarters => 3,
        }
    }

    pub fn denominator(self) -> usize {
        match self {
            CodeRate::Half => 2,
            CodeRate::TwoThirds => 3,
            CodeRate::ThreeQuarters => 4,
        }
    }

    /// Puncturing pattern over the rate-1/2 output stream A0 B0 A1 B1 A2 B2 ...: `true` = transmitted, `false` =
    /// stolen (IEEE 802.11-2020 Figure 17-9; `ofdm80211.puncture`), repeated over the whole stream.
    /// - 2/3: A0 B0 A1 (B1 stolen): 3 of every 4 bits sent.
    /// - 3/4: A0 B0 A1 (B1, A2 stolen) B2: 4 of every 6 bits sent.
    pub fn puncture_pattern(self) -> &'static [bool] {
        match self {
            CodeRate::Half => &[true, true],
            CodeRate::TwoThirds => &[true, true, true, false],
            CodeRate::ThreeQuarters => &[true, true, true, false, false, true],
        }
    }
}

/// Puncturer (port of `ofdm80211.puncture`): drops the stolen bits of `coded` (the rate-1/2 encoder output).
pub fn puncture(coded: &[u8], rate: CodeRate) -> Vec<u8> {
    let pattern = rate.puncture_pattern();
    coded.iter().enumerate().filter(|(i, _)| pattern[i % pattern.len()]).map(|(_, &b)| b).collect()
}

/// Depuncturer (port of `rx80211.depuncture`): re-inserts a 0 (erasure) at every stolen position, so the Viterbi
/// decoder sees the full rate-1/2 stream.
///
/// As in the Python, the pattern is walked from its start while received values remain. If the values run out
/// inside a pattern, the erasures before the next transmitted position are still appended. (For whole OFDM symbols
/// this never happens: every NCBPS is a multiple of the received bits per pattern.)
pub fn depuncture(received: &[f32], rate: CodeRate) -> Vec<f32> {
    if rate == CodeRate::Half {
        return received.to_vec(); // nothing is stolen
    }
    let pattern = rate.puncture_pattern();
    let sent_per_pattern = pattern.iter().filter(|&&keep| keep).count();
    let mut out = Vec::with_capacity(received.len() / sent_per_pattern * pattern.len() + pattern.len());
    let mut k = 0;
    'walk: while k < received.len() {
        for &keep in pattern {
            if keep {
                if k >= received.len() {
                    break 'walk;
                }
                out.push(received[k]);
                k += 1;
            } else {
                out.push(0.0);
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The trellis tables match the explicit tap equations of rx80211._trellis / ofdm80211.conv_encode.
    #[test]
    fn encoder_matches_tap_equations() {
        for s in 0..NUM_STATES {
            for b in 0..2 {
                // r[0] = input, r[d] = bit entered d steps ago = (s >> (6 - d)) & 1
                let r = |d: usize| if d == 0 { b } else { (s >> (6 - d)) & 1 };
                let a = r(0) ^ r(2) ^ r(3) ^ r(5) ^ r(6);
                let bb = r(0) ^ r(1) ^ r(2) ^ r(3) ^ r(6);
                assert_eq!(encoder_output(s, b), (a as u8, bb as u8));
            }
        }
    }

    /// The four transitions of each butterfly expect the pair of 2j -> j or its complement (BUTTERFLY_BRANCH).
    #[test]
    fn butterfly_complements() {
        for (j, &branch) in BUTTERFLY_BRANCH.iter().enumerate() {
            let (a, b) = encoder_output(2 * j, 0);
            assert_eq!(2 * a as usize + b as usize, branch);
            assert_eq!(next_state(2 * j, 0), j);
            assert_eq!(next_state(2 * j + 1, 1), j + BUTTERFLIES);
            assert_eq!(encoder_output(2 * j + 1, 0), (a ^ 1, b ^ 1));
            assert_eq!(encoder_output(2 * j, 1), (a ^ 1, b ^ 1));
            assert_eq!(encoder_output(2 * j + 1, 1), (a, b));
        }
    }

    fn pseudo_random_bits(n: usize, seed: u32) -> Vec<u8> {
        let mut s = seed;
        (0..n)
            .map(|_| {
                s = s.wrapping_mul(1103515245).wrapping_add(12345);
                ((s >> 16) & 1) as u8
            })
            .collect()
    }

    #[test]
    fn noiseless_roundtrip_all_rates() {
        let mut bits = pseudo_random_bits(600, 7);
        bits.extend([0u8; 6]); // tail
        for rate in [CodeRate::Half, CodeRate::TwoThirds, CodeRate::ThreeQuarters] {
            let sent = puncture(&conv_encode(&bits), rate);
            let soft: Vec<f32> = sent.iter().map(|&b| if b == 1 { 1.0 } else { -1.0 }).collect();
            let coded = depuncture(&soft, rate);
            assert_eq!(coded.len(), 2 * bits.len());
            assert_eq!(viterbi(&coded, bits.len()), bits, "{rate:?}");
        }
    }

    #[test]
    fn corrects_bit_errors() {
        let mut bits = pseudo_random_bits(300, 3);
        bits.extend([0u8; 6]);
        let mut soft: Vec<f32> = conv_encode(&bits).iter().map(|&b| if b == 1 { 1.0 } else { -1.0 }).collect();
        for i in (5..soft.len()).step_by(37) {
            soft[i] = -soft[i]; // isolated hard errors, well within the code's free distance
        }
        assert_eq!(viterbi(&soft, bits.len()), bits);
    }

    #[test]
    fn depuncture_patterns() {
        let r: Vec<f32> = (1..=8).map(|v| v as f32).collect();
        assert_eq!(depuncture(&r, CodeRate::ThreeQuarters), [1., 2., 3., 0., 0., 4., 5., 6., 7., 0., 0., 8.]);
        let r: Vec<f32> = (1..=6).map(|v| v as f32).collect();
        assert_eq!(depuncture(&r, CodeRate::TwoThirds), [1., 2., 3., 0., 4., 5., 6., 0.]);
        // running out inside a pattern (rx80211.depuncture appends the erasures before the next sent position)
        let r = [1.0f32, 2.0, 3.0];
        assert_eq!(depuncture(&r, CodeRate::ThreeQuarters), [1., 2., 3., 0., 0.]);
    }
}
