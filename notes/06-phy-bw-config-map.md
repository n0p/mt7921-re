# PHY bandwidth configuration map (2026 RAM firmware + boot ROM, static analysis, 2026-10-15)

Purpose: find where the receive demodulator's rate/timing is set per channel width. Evidence comes from
work/decomp/all.c (RAM), rom_all.c (ROM), all2022.c, the DLM image (work/regions/ram_1_02015c00.bin, live =
download + 0x44c) and live snapshots (work/phydump/wide_*.bin, bw_*.bin; page list wide_pages_safe.txt).
New tools:
- tools/bwmap.py: call-graph walk printing PHY/RF writes; resolves the ROM API table at 0x822xxx from the ROM dump.
- tools/crtable.py: decodes the compressed CR tables. Output in work/phydump/crtableA.csv and crtableB.csv.

## Call path
CHANNEL_SWITCH / SET_RX_PATH, ext 0x08/0x4e → `ext_cmd_08__ext_cmd_4e` 0x964494:
- FUN_962eae maps the driver's cbw to the internal bw:

  | driver cbw | 0 or 1 (20/40) | 2 | 3 | 4 (CMD 10 MHz) | 5 (CMD 5 MHz) | 6 |
  |---|---|---|---|---|---|---|
  | internal bw | 0 | 1 | 2 | 5 | 4 | 3 |

- It then calls rlmPhySetChannel 0x963f6c → FUN_942672 (channel cache; struct +0x11 = bw enum: 0 = 20, 1 = 40,
  2 = 80, 3 = 160, 4 = 80+80, 5 = 5 MHz, 6 = 10 MHz) → FUN_941ea4 → **FUN_93bc9c**, the per-path PHY channel set.

FUN_93bc9c, per path (loop cVar17):
1. FUN_93abc4 / FUN_93b96a fill the config struct 0x02045e80 (byte 4 = bw enum).
2. Two tuple builders:
   - FUN_93c6b6 builds the table B tuple (band, channel class, path, …; **no bandwidth digit**);
   - FUN_93c6f8 builds the table A tuple (t0 band group, **t1 = bw with 5→3 and 6→4**, t2 = RX streams−1, t3, t4).
3. Table B is applied by FUN_93e706 and table A by FUN_93e62a, unless param_4 == 0xf0.
4. Helpers called with the bw enum: FUN_93c724, 93c87e, 93c9ac, 93ca3e, 93cab0 (below).
5. **Clock routine FUN_93dbd4 runs twice in narrow mode**:
   - first with bw forced to 0 (20 MHz), around the calibrations FUN_93b35a / 93aa70 / 93b6c8;
   - again with the narrow enum at the end (`if (bw - 5 < 2)` in FUN_93bc9c).
6. FUN_93db8a writes 34 band registers 0x8308fc30–0x8308fcb4 (+0x10000 for path 1) from the 2.4 GHz
   (0x02029710) or 5 GHz (0x02029688) tables. These do not depend on bandwidth.

The 2022 clock routine (FUN_93892c) is identical to the 2026 one. Static init FUN_93cc28 has no bandwidth terms.

## Bandwidth-dependent writes outside the tables

| Writer | Register (path 1 = +0x10000) | 20 / 40 / 80 / 160 | Narrow (enum 5 = 5 MHz, 6 = 10 MHz) |
|---|---|---|---|
| FUN_93dbd4 | 0x83081000 [26:24] BW code | 0 / 1 / 2 / 7 | **0** (masked `& 0xf0ffffff`) |
| FUN_93dbd4 | 0x83081000 [30:28] | [28] = 5 GHz band (ROM 0x841182 sets it) | 10 MHz 0x5 (bit 30), 5 MHz 0x7 (bits 30+29) |
| FUN_93dbd4 | 0x83081000 [22:20] | primary-20 position (param[3]) | same |
| FUN_93dbd4 | 0x83080000 bit 12 | 1 at 20 MHz only, 0 for all other widths | **0** (treated as non-20) |
| FUN_93dbd4 | 0x8300d06c [6:3] (one-hot, mask 0x78) | 0x4 (bit 5) at 20, 0x8 (bit 6) at all others | **0x8** (as non-20); bits 3 and 4 are never written |
| FUN_93dbd4 | RF 0x160 [31:30] analog filter code ([29] = 5 GHz) | 0 / 1 / 2 / 2 | 0 |
| FUN_93dbd4 | RF 0xb04 [7:6], [28:24]; RF 0xb00 [29:24] | 1, 0xf, 0x10 at 20; 2, 0x1f, 0x20 above 20 | as 20 MHz |
| FUN_93c724 | **0x83086010 [27:16] and [11:0]** = round(0x190000 / f_MHz) | unscaled, frequency only | **÷2 (6) / ÷4 (5)**: 0x13c → 0x9e → 0x4f at 5180 |
| FUN_93c724 | 0x8308f104 [27:16] = round(0x280000 / f) | frequency only | same |
| FUN_93c87e | 0x8300d0ac [25:8] = round(0x1400000 / f_MHz) << s (SFO-from-CFO) | s = 0 / 1 / 2 / 3, 80+80: 2 | **s = 0, no narrow case** |
| FUN_93c9ac | 0x830000dc write port, 48 entries `0x80 \| idx<<16 \| v` | v = v20 + 48·bw (≈ +3 dB per doubling in 1/16 dB, inferred), table 0x02029448 | 20 MHz column |
| FUN_93ca3e | 0x830a0080 [7:4] = 2.4 GHz channel index | 40 MHz: ±2 | fixed 6 |
| FUN_93cab0 | 0x83088394 / 0x83088398 | 20: 0x00100300 / 0; 80: by primary position; 40: decompile cut | **0 / 0** |
| FUN_93c724 | 0x830a70a0, 0x830a77b4, 0x830a6014 | 160 and 80+80 only (secondary frequency ratios) | — |

Other clock-routine writes made on every switch, independent of bandwidth:
- 0x8300c004 [31:30] = 0 (**never set by any code**);
- 0x8300c008 bit 31 = 0 (dual path), 0x8300c02c [29:28] = 3, 0x8300c500 |= 4, 0x8300c704 [7:0] = 0xe4;
- 0x8300d010 |= 0x10, 0x830000fc [15:12] = 6, 0x830ae010 |= bit 16;
- 0x83088200 bit 27 cleared, 0x8308d024 bit 31 cleared;
- 0x830a3004 [27:16] / [6:4] = 0x8cc / 1, and DBDC single band 0x4ee / 4 (with 0x830a3000 |= 0x90000000 and 0x830ab3c4 bit 31);
- 0x8308b000 [31:30] and [27:26] = TX streams − 1;
- the 0x830ad890 and 0x830ad8c0 blocks from constant tables;
- 0x8308f060/64/68 tails; RF 0x64 path masks; RF 0x1f0 [15:8] = 0x50; RF 0x16c.

## Compressed CR tables (applier FUN_93e5d2)
Layout: descriptors {u32 index-byte pointer, u16 registers in group, u16 stride}. Each column row holds one variant
byte per group. The value-pool index is idx[variant + i·stride]; the pool value 0xF1F2F3F7 means "skip".

**Table A**:
- descriptors 0x02029820, addresses 0x0202a024 (185 registers), pool 0x0202a308, rows 0x02029b54 (stride 40, 40 groups);
- column = position of code t0 + 3·t1 + 9·t2 + 18·t3 + 36·t4 in the byte list 0x0202a004 (30 codes).

It reproduces the live 20/40/80 snapshots exactly (e.g. 0x83000140 a9/ac/af088802, 0x83088200 a2/22/024d45d7).

**Narrow aliasing**: the bandwidth digit has radix 3, so t1 = 3 or 4 aliases onto other columns.
- 10 MHz, 1 RX stream: code t0+12, the "40 MHz, t2 = 1" column (5 GHz low: c7).
- 5 MHz, 1 RX stream: code t0+9, the "20 MHz, t2 = 1" column (c5).
- With 2 RX streams they become the t3 = 1 columns c25 / c24.

So the "2022 narrow profiles" (work/phydump/narrow_profiles.json) are just these alias columns. Their 8 non-bandwidth
registers (0x8308829c, 0x830882ac/cc/d0, 0x830884bc, 0x830a708c/7520/7788) are the t2 stream variant.

Bandwidth-dependent registers in table A (5 GHz, 20/40/80) are all AGC/threshold-like scalings plus 80 MHz-only tweaks:
- 0x83000004, 0x83000140 [27:24] (9/c/f), 0x83000148 [23:16] (3a/3d/3e), 0x8300016c [15:8] (3a/3d/3f),
  0x830001b4/1c0/244 [7:0] (0/3d/3e), 0x83088010/14/18, 0x83088200 [31,29] (5 GHz: 20 = both, 40 = bit 29,
  80 = none; 2.4 GHz 20 = none), 0x8308f0a4 bit 30, 0x8308f130, 0x830a7014 [23:16] (b4/b7/ba), 0x830a770c/7710,
  0x830a7788 bit 3;
- 80 MHz only: 0x830a2004/2104/21dc/21e4, 0x830a7850, 0x8300e024, 0x83088550/210/380/2bc, 0x8308f150.

**No table entry encodes a sample rate, FFT size or symbol timing.**

**Table B** (FUN_93e706): descriptors 0x0202a80c, addresses 0x0202aca8 (143 registers), pool 0x0202aee4, rows
0x0202aa8c (stride 26). Its codes use radix 1/2/4/12/24/48 at 0x0202ac94. Band/channel class only, no bandwidth digit.

## Other relevant facts
- ROM PHY power-on FUN_841182:
  - clears 0x83080000 bit 12 and 0x830a3000 bit 31; sets 0x830a3004 [15:8] = 0x32;
  - sets 0x830a3008 |= 0x8c0000 and 0x830a3024 |= 0xc0000;
  - sets 0x83081000 [30:28] = 0 (2.4 GHz) or 1 (5 GHz); writes 0x8308fcb0/b4 magic values.
- The ICAP setup clears 0x830a3008 [22:21].
- The ROM calibration save list (0x008508f4, 230 registers) and the RAM save list (0x0202c608, 140 registers) cover
  the clock routine's registers plus 0x8300d000/d004, 0x8300c50c/c744, 0x830ae0xx (tone generator 0x830ae00c, used by
  FUN_9691d2 TxTone) and 0x830ad8xx–daxx (calibration/ICAP engine). They add no further mode fields.
- 0x8300d000 page: constant demodulator coefficient tables (0x8300d074–0x8300d0a4) and thresholds. Its only
  bandwidth-class fields are d06c and d0ac; d00c, d0f0, d100 and d114 depend on band.
- ContiTx FUN_968f2e writes 0x83081000 = code<<24 | 0x80000000, with no narrow bits.
- No MT7921 code references 0x8103xxxx (MT7996 TICKCR0). There is no ROM register-field (GCI) layer.

## Structural conclusions
1. Neither the 2026 nor the 2022 firmware configures anything for narrow receive beyond:
   - the per-path clock bits 0x83081000 [30:29];
   - the halved/quartered 0x83086010;
   - the "non-20" flags (0x83080000 bit 12 = 0, 0x8300d06c = 0x40);
   - table A columns aliased to 40 MHz (10 MHz) or 20 MHz (5 MHz) AGC settings.

   The demodulator's bandwidth input stays at BW code 0 (20 MHz timing), which matches the hardware result.
2. The demodulator's mode is selected by 0x83081000 [26:24] (per path; codes 3–15 kill RX on hardware) together with
   the band-level 0x8300d06c [6:3] one-hot field and 0x83080000 bit 12. Everything else that scales with bandwidth is
   AGC/threshold/SFO bookkeeping.
3. Unused encodings that a narrow demodulator mode could live in:
   - 0x8300d06c bits 3 and 4: inside the firmware's mask, never set;
   - 0x8300c004 [31:30]: cleared on every switch, never set;
   - 0x83088200 bits 27/28/30;
   - RF 0xb00 / 0xb04 below their 20 MHz codes.
4. The narrow SFO constant (0x8300d0ac), the AGC power table (0x830000dc) and the table A thresholds are wrong for
   5/10 MHz even if a narrow demodulator exists. They need fixing for sensitivity, not for timing.

## Ranked register tests ("demodulator at ÷2/÷4 with the front end in native narrow")
Setup: card A sends verified 5 MHz frames (CHANNEL_SWITCH cbw 4). Put B in **cbw 5** (front end ÷4, ±2.5 MHz) for ÷4
candidates, and in cbw 4 for ÷2 candidates, which need a 10 MHz source (X410). Count decodes from A's SA, and also
check that 20 MHz decoding breaks or survives. Writes go through fwdbg (MCU); RF writes use FUN_83a66c(reg, val, path).
Apply the same writes to path 1 (+0x10000) where noted.

| # | Write (path 0; path 1 = +0x10000) | Values | Why |
|---|---|---|---|
| 1 | 0x8300d06c [6:3] (+ 0x8301d06c) | 0x90 (bit 4), 0x88 (bit 3), keep bit 7 | One-hot mode field in the demod page; the firmware uses only bit 5 (20) and bit 6 (non-20) |
| 2 | 0x8300c004 [31:30] (+ 0x8301c004) | 0x6808081d, 0xa808081d, 0xe808081d | 2-bit band-level field cleared on every switch and never set; same shape as the per-path divider |
| 3 | #1 combined with #2 | d06c bit 4 + c004 = 1 (÷2); d06c bit 3 + c004 = 2 or 3 (÷4) | A one-hot mode plus a divider pair, like 0x83081000 [26:24] + [30:29] |
| 4 | 0x830a3004 [6:4] | 2, 3, 4 (keep [27:16] = 0x8cc) | PHY-top clock word. Writing 0x04ee3240 killed RX (hardware 2026-10-14), so it governs the receiver |
| 5 | 0x830a3004 [27:16] | 0x466, 0x233 (½, ¼ of 0x8cc) | Count-like field next to [6:4]. If it is a cycle budget, scaling it may follow a slower demod clock |
| 6 | RF 0xb00 [29:24] | 0x08 (10), 0x04 (5); 20 MHz = 0x10, 40+ = 0x20 | A-die value that doubles with bandwidth class; written only by the clock routine. Possible ADC/IF rate or filter code |
| 7 | RF 0xb04 [28:24], [7:6] | 0x07 / 0x03 and 0; 20 MHz = 0xf, 1 | Paired with #6: 0xf → 0x1f and 1 → 2 for wider modes |
| 8 | 0x83080000 bit 12 (+ 0x83090000) | set 0x1000; also try bits 13, 14 | 20 MHz-only front-end flag that narrow clears; the front end/demod may expect it with BW code 0 |
| 9 | 0x83088200 bit 27, 28, 30 (+ 0x83098200) | set individually | Per-path RX word: bit 27 cleared every switch, [31:29] carry the bandwidth class from table A |
| 10 | 0x8308d024 bit 31 (+ 0x8309d024) | set | Cleared on every switch by the clock routine |
| 11 | 0x830a3008 [23], [22:21], [19:18]; 0x830a3024 [19:18] | toggle | ROM power-on and ICAP clock enables in the PHY-top page |
| 12 | 0x830ae010 bit 16; 0x830ae008 bits 31/27 | clear | Set on every switch / at init; this page holds tone/test clocks |
| 13 | 0x8300d010 bit 4; bits 23, 29 | clear / set | Demod-page control bits touched by the clock routine (bit 4) and init |
| 14 | 0x8300c02c [29:28] | 1, 2 (normally 3) | Set on every switch; could be per-path clock enables |
| 15 | 0x8300c704 [7:0] | 0x72, 0x39 (normally 0xe4) | Constant rewritten on every switch; unknown count |
| 16 | 0x830a3000 [31,28] + 0x830ab3c4 [31] + 0x830a3004 = 0x04ee3240 | DBDC single-band clock set | Previously decoded 20 MHz only against X410 5/10 frames; retest against card A 5 MHz in cbw 5 |
| 17 | 0x8300d0ac [25:8] | 0x0007e900 (10), 0x0003f400 (5) at 5180 | Correct the SFO constant (no narrow case in FUN_93c87e). Complementary |
| 18 | Table A c4 (20 MHz, 1 stream) values in cbw 4 instead of the 40 MHz alias | 19 registers (see crtableA.csv) | Undo the radix-3 alias. Thresholds, complementary |
| 19 | 0x830000dc 48-entry table | v20 − 48 (10), v20 − 96 (5) | AGC power scale ≈ −3/−6 dB. Complementary |
| 20 | 0x83088394 / 0x83088398 | 0x00100300 / 0 (20 MHz values) | Narrow writes 0/0. Unknown use (subcarrier/spur?); cheap |

Already excluded on hardware:
- 0x83081000 bits 23 and 27, BW codes 3–15, and every [31:28] value;
- 0x83007254 forces; the path-1 mirror; both 2022 profiles; 40 MHz + bit 30.
