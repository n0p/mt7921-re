# 10. FEC for the 2.5 / 5 MHz link (2026-10-16)

Goal: a reliable narrow-band mesh link, with as much as possible done by the card. The card's transmitter does the
PHY coding. Reception of narrow frames is always the host's job: the card's demodulator is fixed at 20 MHz numerology
(notes/03, 07). So "FEC in the card" means choosing what the card transmits, and the host receiver must decode it.

Tools:
- mt7921dbg `tx_rate`, `tx_dw6_set` / `tx_dw6_clr`, `tx_count` (TXD overrides for injected frames);
- tools/guest/txscan.py + tools/sdr/txscan.py (TX scans; `TXS=2` uses both TX chains);
- tools/sdr/sigdec.py: PPDU format and coding fields as transmitted. It decodes L-SIG, HT-SIG and HE-SIG-A, including
  HE-ER-SU, and checks their CRCs;
- tools/sdr/perawgn.py: PER vs SNR of real card waveforms with added white noise;
- tools/trunctest.sh + tools/sdr/trunccheck.py: how many symbols of each narrow PPDU a card really sends (§5);
- tools/nbnode: the Rust nodes `nbmesh` (§6) and `p11node` (§7), and tools/guest/nbmeshtest.sh (two-node ping test).

## 1. What the card transmits at ÷4 (5 MHz), checked with the SIG fields
Card B, 2350 MHz, 119-byte PSDUs. A coding option counts only when the transmitted SIG field says so with a valid CRC.

| Format (rate code) | Coding the card applies | Notes |
|---|---|---|
| Legacy 6–54 (0x40 \| L-SIG rate) | BCC 1/2, 2/3, 3/4 | 36 and 54 never decode at ÷4, even without noise (EVM-limited) |
| HT-MF MCS0–7 (0x80 + n) | BCC; **LDPC** (DW6 [11]); short GI (DW6 [15:14] = 1) | `ldpc=1` checked for MCS0/3/7 |
| HT-MF + **STBC** (rate bit 13), 2 TX chains (`TXS=2`) | Alamouti over both chains | `stbc=1`; +3.6 dB total power from the second chain |
| VHT MCS0 (0x100) | BCC, LDPC | format checked (VHT-SIG-A not parsed) |
| HE-SU MCS0 (0x200) | BCC, **LDPC**, **DCM** (rate bit 4), GI 0.8/1.6/3.2 us + HE-LTF 1x/2x/4x (DW6 [15:14], [13:12]) | `dcm=1`, `gi_ltf=3` checked; STBC request ignored (`stbc=0`) |
| HE-ER-SU (0x240) | BCC/LDPC, DCM, 106-tone (rate bit 5), GI 3.2 + 4x LTF | the most robust 802.11ax mode; `crc_ok` with DCM+LDPC+3.2 us |

At ÷4 every duration is 4x: an HE symbol is 51.2 us of FFT plus up to 12.8 us of guard interval (3.8 km of path
difference).

## 2. Robustness of what we decode today (legacy BCC)
perawgn.py on 2-6 card frames per rate, 10 noise realizations per SNR point, 119-byte frames. SNR = signal / noise
in the decode bandwidth.

| Receiver | 6 (BPSK 1/2) | 9 (BPSK 3/4) | 12 (QPSK 1/2) | 24 (16-QAM 1/2) | 48 |
|---|---|---|---|---|---|
| as it was (STF threshold 0.85) | 7 dB | 7 dB | 8 dB | 17 dB | 22 dB |
| STF threshold 0.6 | 4 dB | 5 dB | 8 dB | – | – |
| + channel smoothing +-1 | 4 dB | 4 dB | 7 dB | – | – |
| + channel smoothing +-2 | **3 dB** | 4 dB | 7 dB | – | – |

(Rates are named at 20 MHz timing; on air at ÷4 they are 1.5 / 2.25 / 3 / 6 / 12 Mbps, and half that at ÷8.)

- **The preamble detector limited sensitivity, not the code.** The normalized lag-16 STF autocorrelation is about
  SNR / (SNR + 1), so the old 0.85 threshold needed about 7.5 dB. At 0.6 the floor drops by 3 dB, and a 2-5% error
  floor at high SNR disappears. False alarms remain cheap: L-SIG parity, the LTF SNR gate and the FCS reject them.
- Averaging the LTF channel estimate over neighbouring subcarriers gains about 1 dB at BPSK/QPSK. The default is +-1
  (78 kHz subcarriers at ÷4). Wider smoothing would hurt frequency-selective channels.
- Both are now the defaults in tools/sdr/rx80211.py (`STF_THRESH = 0.6`, `H_SMOOTH = 1`) and in the Rust receiver
  (tools/nbnode).
- A 3 dB threshold for BPSK 1/2 is within ~1 dB of what the code allows on short frames.

## 3. What the card's other codes would add (literature values; needs host decoding first)
- **LDPC** (HT/VHT/HE): about 1.5–2 dB over BCC at the same rate, more on long frames.
- **DCM** (HE): each bit on two subcarriers half a band apart, so half the rate, about 3 dB, plus frequency diversity.
- **HE-ER-SU**: preamble boosted 3 dB (detection at low SNR). The 106-tone RU puts the power into about half the band
  (+3.6 dB per tone).
- **STBC** (HT, 2 chains): transmit diversity. Nothing in AWGN, but in fading, several dB to 10 dB at 1% PER. Most
  relevant for long NLOS links.
- **Long GI** (HE 3.2 us, 12.8 us at ÷4): tolerates delay spread that would otherwise break the 3.2 us legacy GI.
- **÷8 instead of ÷4**: half the rate and half the noise bandwidth, +3 dB at the same power. It costs the tail-loss
  workaround (notes/09 §4).

## 4. Plan
1. **Now (implemented, tested on hardware):**
   - legacy BPSK 1/2 at ÷4 or ÷8, with the improved receiver (also the Rust receiver's defaults);
   - packet-level erasure coding in the mesh daemon (tools/nbnode `nbmesh --fec k,m`: Reed-Solomon over GF(256), k
     data frames + m parity frames, so any k of k + m recover the block). There are no MAC acknowledgements at narrow
     widths (the card cannot decode narrow frames), so losses from fading and collisions are repaired without
     retransmission. On the two-card link it halved the round-trip loss (§6);
   - the 5 MHz MAC tick (§5), without which long frames never arrive.
2. **Next:**
   - HT-MF reception in the host receiver: HT-SIG (sigdec has it), HT-LTF, 52 data tones;
   - then STBC combining (Alamouti; channel per TX chain from the two HT-LTFs);
   - then the 802.11 LDPC decoder (layered min-sum; the 802.11n codes, 648/1296/1944 bits).
3. **Later:** HE-ER-SU with DCM + LDPC + 3.2 us GI: a 256-point receiver, the largest single gain and the largest
   amount of work.

Card configuration for each profile: tools/guest/nbprep.py (`--fec`) or nbmesh (`--profile`). The card transmits
every profile in the table above. The host receivers decode legacy only, until step 2 lands.

## 5. TX truncation: the MAC tick decides when a narrow PPDU ends
tools/trunctest.sh: card A runs nbmesh (TUN, FEC off) and pings an absent peer with `ping -p 5a` (every data byte
0x5a). The X410 captures on RX1, and tools/sdr/trunccheck.py counts per PSDU length the symbols that arrive before
the first wrong byte. Legacy 6 Mbps, 2350 MHz.

**÷4 (5 MHz).** The channel switch at cbw 4 writes WFSYS_TICKCR0 = 0x5 (10 MHz MAC timing).

| PSDU (bytes) | symbols N | kept at TICKCR0 0x5 | kept at 0xa | FCS ok at 0xa |
|---|---|---|---|---|
| 264 | 89 | 88 (whole) | 88 | 2/2 |
| 464 | 156 | 139–141 | 155 | 8/8 |
| 764 | 256 | 189–190 | 255 | 8/8 |
| 1064 | 356 | 239–241 | 355 | 8/8 |
| 1364 | 456 | 289–290 | 455 | 8/8 |

The last symbol only carries tail and pad bits, so "N − 1 kept" is a whole frame.

**At 0x5, frames longer than ~260 bytes are cut**, and every earlier 5 MHz test used short frames. At 0xa, which is
the firmware's own 5 MHz value (notes/05), every length arrives with a valid hardware FCS. nbmesh, nbprep.py and
nbrxd.py now write 0xa after each channel switch (nbrxd's ÷8 padding is still the old fixed 4 symbols).

**÷8 (2.5 MHz).**

| PSDU | N | kept, 0x5 | kept, 0xa | kept, 0xe | kept, 0xf |
|---|---|---|---|---|---|
| 136 | 47 | 39 | – | – | – |
| 160 | 55 | 42–43 | 54 (whole) | – | – |
| 200 | 68 | 45–46 | – | – | – |
| 280 | 95 | 52 | 77–78 | 77 | 39–40 |
| 380 | 128 | 60–61 | – | – | – |
| 580 | 195 | 77–78 | – | – | – |
| 780 | 261 | – | 160–161 | 160–161 | 60–61 |

One model fits every row. **The PHY stops the PPDU about 1 ms after the end the MAC computes with its own tick:**
- 0x5 = 10 MHz timing: about 27 + N/4 symbols survive at ÷8, and 62 + N/2 at ÷4;
- 0xa = 5 MHz timing: 30 + N/2 at ÷8, and the whole PPDU at ÷4.

The TX end follows WLAN_CLK_MODE [1:0]: 0xe (LMAC_1X_MODE 3) behaves as 0xa. A [1:0] value of 3 behaves as 20 MHz
timing (0xf: about 30 + N/8). **5 MHz is the slowest MAC timing there is, so no setting makes ÷8 PPDUs whole.**
This replaces notes/09 §4's "last 3 symbols", which held only for its 119-byte frames.

What ÷8 senders do now (tools/nbnode profile::div8_pad_bytes):
- the inner CRC after the body is followed by enough padding that the part which arrives covers it:
  p = max(4, d − 56) symbols for d symbols up to the end of the inner CRC (28 + N/2, i.e. 2 symbols of margin);
- the receiver finds the inner CRC from the PSDU length alone (profile::div8_inner_end; the length grows strictly
  with the CRC position);
- long frames pay nearly double airtime.

Measured on card A → card B: 26 of 27 frames arrived, every inner CRC intact, PSDUs up to 1608 bytes. Before this
change, every frame longer than ~150 bytes failed.

Two more ÷8 limits:
- **Longest PPDU:** a 1608-byte PSDU (17.3 ms on air) leaves the card. A 2167-byte one (23 ms) was not seen to,
  but that run may have been spoiled by a frame left stuck in the queue by the run before it (a rig reset fixed
  the state), so the boundary lies between those two and is not pinned down. nbmesh caps the MTU so that every frame
  fits 17.5 ms: 828 bytes with FEC in TAP mode.
- **Late drain:** for long ÷8 PPDUs the device queue reports empty late, so the stall detector re-tunes after many
  windows. The frames themselves get through.

## 6. nbmesh on hardware (two cards 10 cm apart, 2350 MHz)
tools/guest/nbmeshtest.sh: node A (10.42.0.1, netns nsa) and node B (10.42.0.2, netns nsb) on the two cards, pings
A → B every 0.5 s. Gain 0,0,0: at 3,4,6 the 10 cm neighbour clips the receiver, and B heard nothing from A.

| run | round trips | notes |
|---|---|---|
| 5 MHz, TUN, FEC 4,2, 56-byte pings | 54/60 (90%) | RTT 207–425 ms; FEC rebuilt 8 + 14 packets |
| same, FEC off | 48/60 (80%) | |
| 5 MHz, TAP, FEC 4,2 | 36/40 (90%) | ARP over the link; 9 + 7 rebuilt |
| 5 MHz, TUN, FEC 4,2, 1300-byte pings (1376-byte PSDUs) | 59/60 (98%) | RTT mean 365 ms; 9 + 10 rebuilt; needs §5's tick |

**Near-field TX stalls.**
- After the other node's bursts at 10 cm, a card's MAC can keep deferring its own transmissions. One PLE AC queue
  (WCID 19, the reserved entry that injected frames use) stays non-empty, and each window then cost the 0.5 s drain
  limit, which also blinded the node's receiver.
- These checks failed to explain it:
  - a card alone, at either gain: no stall;
  - A transmitting while B only receives: no stall, B delivered everything;
  - 5 dB less TX power: still stalls;
  - the air during a stall: quiet (X410, 0.3 s at 20 Msps);
  - the cards' own MAC: receives nothing;
  - nbrxd's `--regain` (the gain forces again before every slice): no help.
- **A channel switch clears it at once.** nbmesh declares a window stalled after its airtime + 60 ms and re-runs the
  radio setup (~50 ms). In the 60-ping run that happened in 13 of A's 63 windows and 5 of B's 60, and most queued
  frames still arrived.
- With the old fixed 0.5 s limit and no re-tune, the same link managed 2–18 of 40 round trips.

## 7. p11node: 802.11p reception (tools/nbnode)
- The card is tuned in cbw 4 (10 MHz native, front end ±5 MHz), streams ICAP packer 8 (30 Msps), and the host
  resamples to 10 Msps.
- X410 → card A at 2350 MHz, 10 MHz frames with LLC/SNAP + GeoNetworking (txloop.py `ETHERTYPE=8947`): 6702 of
  6702 frames in 16 s, no dropped blocks or overruns. On the TAP, tcpdump decodes them as `GeoNet`.
- Channels 180 (5900 MHz) and 178 (5890 MHz): tuned (RF readback), no traffic near the lab.
- It cannot transmit: no ÷2 exists (notes/07).
