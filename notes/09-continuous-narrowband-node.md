# 09 — Narrowed IQ capture, a continuous narrowband node, and the ÷8 (2.5 MHz) transmitter (2026-10-15, late)

Goal: a narrowband long-range mesh node / 802.11p-style modem on one MT7921AU. This note covers what came after
notes/03's "single-card TRX enablers": a capture rate the rig can stream continuously, a real-time receiver for it,
a half-duplex node on top, the 2.5 MHz transmitter, and the rig changes they needed. All radio tests were at
2350 MHz (licensed ham band). Card A and card B sat 10 cm apart at manual gain 0,0,0 unless noted. The X410
transmitted from TX/RX0 (external antenna), so the cards used gain 3,4,6 for it.

## Hardware capabilities for narrowband TRX (summary)
| Capability | Status | How |
|---|---|---|
| Narrow (5/10 MHz) hardware demodulator | none | notes/03 |
| IQ capture, 40 Msps | works, too much for USB through QEMU unless gated | ICAP packer 0 |
| IQ capture, 30 Msps continuous | works, 120 MB/s, 0 overruns; the 802.11p (10 MHz) receive mode | packer 8 (CR10 [19]) |
| IQ capture, 10 Msps continuous | works, 80 MB/s, 0 overruns; the long-range mode | packer 2 (CR10 [17]) |
| TX ÷4, 5 MHz OFDM | works | BN_MODE 0x83081000 [30:28] = 5 (native cbw 4) |
| TX ÷8, 2.5 MHz OFDM | works (also as a half-duplex node, on card A) with a software workaround: the PPDU's end is lost (3 symbols on short frames, more on long ones: notes/10 §5), MAC retries | [30:28] = 7, below |
| TX ÷2, 10 MHz (802.11p/bd) | not available | notes/07 |
| IQ playback | not available | notes/07 |
| Tone generator as an MCU-driven IQ DAC | works, but the output collapses unpredictably | §5b below |

## 1. ICAP packers narrow the capture after all
notes/03 called "narrowing in hardware not possible". That was wrong for one reason: the input packers (CR10
0x800210b8 [19:16]) decimate.

They were measured with an X410 CW tone at +1.016 MHz. Native cbw 4, tap 4, BW code 4 in 0x83081000 [27:24]. The
analysis is the phase step between successive ring words (tools/sdr/tonephase.py):

| CR10 [19:16] | Words | Phase step per word at 1.026 MHz | Meaning |
|---|---|---|---|
| 0 | dense, 40 M/s | 0°, 18.5°, 0°, 18.5° | the cbw-4 front end is 20 Msps, held ×2 |
| 2 | 20 M/s, beat [s0 s1 0 0] | 37.2° uniform | uniform 10 Msps (100.8 ns) |
| 8 | dense, 30 M/s | 12.3° uniform | 30 Msps: the 20 Msps samples re-timed by repetition (images at ±10 MHz, −23 dB) |
| 3, 10, 11 | as 2 | as 2 | [17] wins; [16] (64→128) has no effect |
| 9 | as 8 | as 8 | |

**Packer-2 beat layout.** The pair normally sits in words 0–1, with words 2–3 zero. In about 2.3% of beats (runs of
~16 beats, recurring about every 140 µs) it sits in words 2–3 instead. The beat where it moves holds the same pair in
both halves.

The correct extraction takes the half with data, once per beat (tools/sdr/iqrec.py `sample_words`). This yields
exactly 2 samples per beat and no slips. It needs native cbw 4 (front end band-limited to ±5 MHz): the packer drops
samples without filtering.

**Decode check.** X410 5 MHz frames at gain 3,4,6; every decode was checked against the X410 sequence numbers:

| Capture | Mode | Good frames | LTF SNR (median) |
|---|---|---|---|
| packer 0 | gated | 98/98 | 16.4 dB |
| packer 2 | continuous 3 s | 98/98 | 16.7 dB |
| packer 8 | continuous 3 s | 98/98 | 16.5 dB |

- **9 s continuous at 10 Msps:** 0 overruns, 0 skipped chunks, 0 drops; peak backlog was 20% of the ring.
- Earlier "bad FCS" counts on packer 2 were false alarms on noise (LTF SNR around −9 dB), not damaged frames.

## 2. Receiver (tools/sdr/rx80211.py, iqchunkdec.py)
**Bug fixed: 16/64-QAM never decoded.** The LTF reference kept the transmitter's IFFT gain 64/√52, so equalised data
symbols were 8.9× too large. BPSK/QPSK only use the sign, so they were unaffected. All 8 rates now pass the selftest.

**Gates before the SIGNAL decode:**
- LTF SNR, from the two LTF symbols, must be at least −1 dB.
- The LTF must spread its energy: no subcarrier may hold more than 25% of it. Any CW spur autocorrelates like an STF
  and repeats exactly across the LTF. At gain 0,0,0 the receiver's own spurs produced about 170 false decodes per
  second.

**Speed.** One core went from about 10× to about 1.2× real time at 10 Msps on the guest (1.0× on the laptop):
- Viterbi in C through ctypes (tools/sdr/vit.c, compiled on first use, numpy fallback);
- vectorised demodulation and descrambler;
- O(N) STF sums;
- only the needed span is de-rotated;
- PSDU length capped at 1600 bytes in nbrxd.

**iqchunkdec.py** decodes arbitrarily long record files with bounded memory: overlapping blocks, deduplicated by
absolute position.

## 3. Continuous node (tools/guest/nbrxd.py)
**What `--pack 2` (or 8) changes:**
- runs ungated by default, so frames below the noise floor, which no energy gate sees, reach the preamble detector;
- defaults to cbw 4;
- decodes 100 ms blocks (`--block-ms`) overlapping by 16 ms, deduplicated by ring position;
- `--show-bad` prints FCS failures (rate, length, SNR, CFO);
- each frame line shows SNR and decode delay.

**Faster half-duplex turnaround:**
- the daemon polls every 4 ms once it has ended a slice for TX;
- early-ended slices skip the two MCU round trips (stats, tuning check).

**Results:**

| Test | Delivered |
|---|---|
| X410 → single node, real time | 368/372 (the 4 lost fall in two ~100 ms slice restarts) |
| X410 802.11p 10 MHz frames → `--pack 8 --width 10`, real time | 296/300 good FCS, no false decodes |
| One-way UDP, transmit-only → continuous receiver | 97/100 and 98/100 |
| One-way UDP, TRX sender (packer 0 or 2) → continuous receiver | 49/50 |
| Bidirectional UDP, both TRX, continuous 10 Msps, Poisson 5/s each way, 30 s | A→B 138/170 (81%), B→A 98/132 (74%) |
| Ping, both nodes continuous | 9/10 in the best run |

- Bidirectional run: median one-way latency 0.20–0.23 s.
- With synchronized senders (same 200 ms cadence) the same test gave 5% and 53%: the nodes transmitted together and
  were each deaf meanwhile. A mesh needs random access.

## 4. The ÷8 transmitter: 2.5 MHz OFDM
**What B emits with [30:28] = 7 at native cbw 4** (card A's 10 Msps stream, tools/sdr/burstana.py):
- a real 2.5 MHz OFDM PPDU: ±1.1 MHz occupied, STF period 6.4 µs, CP lag 25.6 µs;
- decoded at 2.5 MHz it gives SNR 38 dB and correct header and payload.

**Defect 1: the tail is lost.**
- Every PPDU loses its last 3 OFDM symbols (about 96 µs). This was checked on 322 copies of 30 frames: all were
  byte-perfect through byte 111 of 119, and the last 7 bytes were always garbage.
  - **Superseded (notes/10 §5):** the loss grows with the frame. The PHY stops the PPDU about 1 ms after the end
    computed with the MAC's own tick, so only ~27 + N/4 of N symbols arrive. With TICKCR0 = 0xa it is ~30 + N/2.
    The same tick also cut 5 MHz frames longer than ~260 bytes.
- So the hardware FCS never survives. That is why ÷8 "never decoded" before.
- Likely cause: the MAC or PHY times the PPDU with a ÷4 preamble (80 µs short).

**Defect 2: the MAC retries every frame about 11–15 times.** It treats the truncated PPDU as a failure. The copies
carry no Retry bit.

**Workarounds, all in nbrxd `--width 2.5`:**
- The sender appends a CRC32 of the body plus 4 OFDM symbols of padding. The receiver checks that inner CRC and
  rebuilds a normal frame (`div8_unwrap`).
- Tap.write now drops any repeat of the last (SA, seq), not only frames with the Retry bit.
- mt7921dbg gained a kretprobe on `mt76_connac2_mac_write_txwi`. Module parameter `tx_count` rewrites TXD DW3
  REM_TX_COUNT of injected frames (mt76 hard-codes 15). nbrxd sets it to 1 in ÷8 mode.

**Results:**
- Transmit-only ÷8 sender: 49/50 and 29/30 UDP. With `tx_count=1`, 51 decodes for 51 frames, so no retries.
- TRX-mode ÷8 senders: 17–28/30 in the good runs.
- **TRX ÷8 can key an unmodulated carrier.**
  - In some runs the PA keyed a bare carrier instead of the PPDU (±0.01 MHz wide, autocorrelation 0.99).
  - tools/guest/div8bisect.sh tied it to the four manual-gain forces applied before the capture setup:
    - gain forces alone: 0/30 twice;
    - each force alone: 29–30/30;
    - capture setup, then the forces: 29/30;
    - no setup at all: 29/30.
  - nbrxd now runs the capture setup before applying gain, and switches the divider right after the channel switch.
  - TRX ÷8 still failed in some two-card runs afterwards, mixed with card B's USB wedges (below).
  - **On card A alone it works.** Setup: 500 ms continuous RX slices, each followed by a 5-frame ÷8 TX window (window
    mode, `tx_count=1`), observed by the X410 on RX1 (2.5 Msps, tools/sdr/div8dec.py).
  - Over 25 s: 28 cycles, 0 failed slices, 0 vendor timeouts. In a 6 s capture, 23 frames had a valid inner CRC:
    5 windows with 3, 5, 5, 5 and 5 frames (the first was cut by the capture start).

## 5b. The band tone generator as an MCU-driven IQ DAC (notes/07 §1 experiments 3–4)
Card A keyed the DC tone (`FUN_009691d2(1, 0, 0, 0)`). The X410 on RX1 recorded at 0.25–0.5 Msps
(tools/guest/tonedac.sh, dacbw.sh; tools/sdr/toneana.py, dacana.py).

**The DC word is a live, linear DAC.**
- Each plain write to 0x8308fc08 (I [11:0], Q [27:16], 12-bit) changes the carrier at once.
- The four plateaus fit y = L + g·d (L = LO leakage, |L| ≈ 1.1 × |g|·1024). The magnitude-only solution is
  self-consistent: the solved cos θ came out at 1.03–1.06, i.e. 1 within noise.
- The fc0c [0] latch pulse instead knocks the generator off the carrier: do not use it.

**Waveform playback.** fw/src/dac.c `hk_dacplay(step, period_us, nsamp, amp)` writes amp·exp(j2πk·step/64) at a paced
rate:
- The phase step's sign is correct: +15.6 kHz and −15.6 kHz appear on the right side.
- 15.6 kHz at 250 ksps gave a line at −0.2 to −1 dB relative to the carrier. The zero-order-hold images sit at
  ±250 kHz, about −25 dB.
- 62.5 kHz at 500 ksps gave a line at +0.6 dB relative to the carrier. The image was −34 dB and the products at
  ±125 kHz about −33 dB.
- Maximum unpaced write rate: 1.45 Msps (0.69 µs per sample). With a 1 µs period the loop only reaches 0.82 Msps.

**Unsolved: the output collapses.**
- In most playback sessions the carrier dropped by about 20 dB, at or soon after the start of the hk_dacplay call,
  and stayed down until the tone was stopped.
- In the first sweep this started with a 90°-per-sample tone. In the second sweep it also hit 11.25° and 22.5° runs.
- Candidates: the TSSI/power-control loop that TxTone arms (0x8300c000), or firmware background work between calls.
- Follow-up (tools/guest/dactssi.sh): the same +15.6 kHz / 250 ksps playback in 6 fresh tone sessions.
  - Plain: collapsed about 0.35 s after tone-on (as the playback starts), 2 of 2.
  - Writing the stock TSSI control 0xc0c80806 back into 0x8300c000/0x8301c000: worked once (+15.6 kHz line level with
    the carrier) and collapsed once.
  - Clearing the tone power override (0x8300c508 [31]/[15]): no carrier at all.
  - Power control is involved but is not the whole story.
- Until this is understood, the DAC is a demonstrated capability, not a usable modem. A narrowband FSK/chirp
  long-range mode built on it would be the next project.

## 5. Rig changes and limits (QEMU guest on lorien)
**VM.** It now runs from `~/vm23.sh` on lorien (root tmux session vm23) with:
- 12 vCPUs and 8 GB (it had 4 and 2 GB);
- one emulated xHCI per card (xhciA: 2-2, xhciB: 6-1);
- `-name vm23,debug-threads=on`.

**IRQ pinning.** loadstack.sh pins each controller's IRQ to its own vCPU (the last two).
- With one shared xHCI, all completions of both cards hit one vCPU. Two 80 MB/s streams wedged a card.
- Even alone, one card at 80 MB/s keeps its IRQ vCPU about 98% busy.
- nbrxd now keeps itself and its decoder workers off those CPUs.

**Firmware.** hk_iqstream's teardown now waits up to the caller's timeout for in-flight descriptors before it resets
MCU TX ring 0 (it waited 100 ms). nbrxd passes a 2 s DMA timeout. Resetting the ring under a stalled transfer
wedged the USB DMA. With this, two cards streamed 10 Msps continuously for 25 s each (2.24 GB, 0 overruns).

**Open problem: USB wedges.**
- A card stops answering USB control requests (`vendor request ... failed:-110`, then `Message ... timeout`), with no
  earlier driver message. In two-node TRX soaks it was almost always card B (lorien bus 6, second root complex),
  typically right after a TX window and the following slice restart.
- The driver's reset path can then oops in mt76u. Once it left the guest in soft lockups.
- **Mitigations tried in 120 s bidirectional soaks** (continuous 10 Msps on both cards, ~4 frames/s each way):

  | Variant | Soaks that wedged |
  |---|---|
  | anytime TX | 3 of 3 |
  | + 20 ms settle after TX | 1 of 1 |
  | + host polls only while the stream is quiet | 1 of 1 |
  | window TX (MCU path, MAC TX off only once the PLE queues are empty; now the default) | 1 of 2 |

  The clean window soak delivered 54% / 52% with latency p50 0.23 s, p90 0.37 s (half-duplex turnaround and
  collisions, no MAC).
- The firmware teardown change (above) did fix the dual-stream wedges.
- **The wedges are card B's, not the node design's.**
  - Card B wedged within 1 s of receive-only streaming, with no TX at all.
  - Card A in the same 5 minutes streamed 1.45 M packets (23.7 GB) with 0 failed slices and 0 timeouts.
  - Card A as a half-duplex node (300 ms slices, each followed by a 2-frame TX window, window mode) ran ~800
    TX-then-restart cycles in 5 minutes with no failure.
  - Card B sits on lorien's second root complex (bus 6, 0000:23:00.3). Earlier in the project it fell back to USB 2
    and dropped off the guest. The host logs no link errors when it wedges.
  - A node on a sound card/port is stable. Testing with card B on another port or host is the next step.

### Card B investigation (2026-10-15/16, tools/guest/dualtrx.sh, tools/wedgestats.sh, tools/vmpause.sh)
**Card B is not defective.** The host ports match: same AMD controller model on each die, SuperSpeed, no
over-current, no U1/U2 LPM. Chip ID and revision are identical (0x7961 / 0x8a10).

Every isolated stress passed on card B:

| Stress | Result |
|---|---|
| Streaming alone 120 s | 9.6 GB, 0 errors |
| Both cards streaming 120 s | clean |
| Both cards, 545 slice starts/stops each | clean |
| B as a half-duplex node, 476 TX-then-RX cycles | clean |
| B receiving while A sends 2000 frames at 10 cm | 1949/2000, clean |
| B as a half-duplex sender with 677 early stops | clean |
| VM frozen 0.3 / 1 / 3 s (QEMU keeps the host transfers going) | no effect |
| A re-tuning 10 times next to B (calibration bursts) | no effect |
| Two-node 120 s UDP soaks with the current code | 2 of 2 clean (delivery 38–59%) |

**Where it does fail: two patterns.**
1. **The first slice after a firmware reload (rigreset).** The card's USB control path dies right after the slice
   starts (the first iq_stat poll fails), or during the setup's MCU commands. It hit card B twice and card A once.
2. **A cascade between cards.** When one card wedges, the driver's USB reset of it stalls QEMU's USB service, and the
   other card fails seconds later. Seen A→B and B→A.
   - Once, B's hook was stranded instead: state 1 forever, ring 0 still borrowed, its DMA index reset underneath and
     the MCU WFDMA global config rewritten by B's own firmware recovery.
   - A one-card-per-host node would not see this pattern.

**Pattern 1 is still unexplained. A tested hypothesis that failed:**
- **Hypothesis:** hk_iqstream's global TX-DMA disable/enable around the ring-0 takeover aborted a firmware event in
  flight.
- **Fix tried (build f2cdc060…):** settle first, no global toggle. It **broke streaming**: without the TX_EN cycle the
  engine never picks up ring 0's new base. The first descriptor hung (state 7), and the following starts returned
  IQ_BUSY. The toggle is required. It is reverted; the installed build is ac2b34a5… again.
- **Nothing else uses this engine.** The other MCU TX rings are idle (TX2) or unconfigured (TX1, TX3); the firmware's
  command rings are RX1/RX3. So there is no third-party transfer to abort.

**Stuck-busy MCU DMA (found on the way).**
- An aborted ring-0 transfer can leave the MCU WFDMA TX engine "busy": GLO_CFG 0x54000208 bit 1, read-only, with
  every ring idle.
- USB resets, VM resets and the driver's WF-subsystem reset do not clear it, and every later stream start returns
  IQ_BUSY.
- A logic reset of the block (0x54000100 [5:4] low, then high) clears it. It also wipes the firmware's ring setup,
  and a firmware reload rebuilds that. tools/rigrecover.sh now does both when it finds the bit set.

**Baseline wedge rate (tools/wedgestats.sh, 12 two-card dualtrx runs of 60 s, build ac2b34a5):**
- 6 runs had vendor timeouts: card B 5, card A 1.
- 3 of the 6 came at the second card's first slice, 3 mid-run or at the end.
- A 30 s settle after the firmware reload made no difference (3 of 6 either way).
- **Recovery, in order:**
  1. `sudo -n usbreset 002/<dev>` and `006/<dev>` on lorien (both cards: a VM reset alone does not recover a wedged
     card);
  2. QEMU `system_reset` (tmux vm23, Ctrl-a c);
  3. `loadstack.sh`;
  4. `insmod mt7921dbg/mt7921dbg.ko`;
  5. `rigreset.sh`.
- The guest log is now streamed to `/root/dmesg_boot_*.log` to catch the next oops.

## 6. Tools added
- **tools/sdr:**
  - iqchunkdec.py;
  - vit.c;
  - rx80211 changes (SNR, gates, 16-QAM fix, speed);
  - iqrec `sample_words`.
- **tools/guest:**
  - nbrxd `--pack`, `--block-ms`, `--show-bad`, `--width 2.5`;
  - nbudp.sh: `RXC`, `PACK`, `TXTRX`, `TXPACK`, `WIDTH`;
  - nbping.sh / nblink.sh: `PACK`, `NBARGS`; nblink uses Poisson send times;
  - txdiv8.sh, div8bisect.sh, inject8.py, tonedac.sh, dacbw.sh, dactssi.sh;
  - mt7921dbg `tx_count`.
- **tools/sdr:** toneana.py, dacana.py, burstana.py, div8dec.py.
- **tools/rigrecover.sh:** full rig recovery (laptop side).
- **fw:**
  - iqstream.c teardown wait;
  - dac.c `hk_dacplay`.

  Installed build md5 ac2b34a570a590c7746e4322c3ffaea0. The symbols moved (iq_stat 0xe027d400); /root/fwre/hooks.nm
  is updated.
