# MediaTek's 5/10 MHz recipe: MT7996 (w1700k) and MT7921 compared (static study, 2026-10-16)

> Status: a static study written on 2026-10-16 during the 10 MHz investigation. Its experiments E0–E6 were run
> the same day (notes/07, 2026-10-16 sections): no setting gives a 10 MHz PPDU on MT7921. The MAC tick it
> discusses (TICKCR0 0x5 / 0xa) turned out to decide when a narrow PPDU ends (notes/10 §5). Raw sources it cites
> (`work/…`) are local files, see README.md "Rebuilding work/".

This is a static study only: no hardware was touched.

**Sources**
- The w1700k reference, with paths relative to `work/ext/w1700k/src`.
- The MT7921 decompilations, cited by line number:
  - `work/decomp/all.c` (RAM 2026);
  - `all2022.c`;
  - `rom_all.c`.
- The disassembly `work/dis/ram_0_00915000.txt`, cited by address.
- `data/crmap/`.
- `notes/01-09`.

**Tags**
- [S]: read in code or tables.
- [M]: measured earlier on the rig (from the notes).
- [I]: inferred.
- [G]: guess.

## Summary
1. **MT7996's documented narrow recipe does not write any PHY sample-rate field.**
   - For MediaTek's 5 MHz (class 6) and 10 MHz (class 7), the channel set writes four things:
     - TICKCR0: 2/2 for 5 MHz, 1/1 for 10 MHz;
     - the MAC bandwidth class (RATE BWCR);
     - BN_6188 at 1/4 or 1/2 of its 20 MHz value;
     - the 20 MHz AGC column.
   - It skips BN_MODE [27:24] ("nothing for classes >= 5") and never sets BN_MODE [30].
2. **MT7921's firmware makes the same writes**, from the same routine, with the same values and in the same order.
   - It adds one write that MT7996 does not have: BN_MODE 0x83081000 [30:28] = 5 for 10 MHz and 7 for 5 MHz, on path 0 only.
   - The other rate-related narrow values in both firmwares encode 10 MHz as a divide-by-2: TICKCR0 = 1, and 0x83086010 is halved.
   - The silicon responds to code 5 with a TX divide-by-4 and an RX front end at divide-by-2. That mismatch is why "10 MHz" comes out as 5 MHz.
   - MT7996 offers no PHY write that MT7921 lacks.
3. **The only MT7996 value that MT7921 does not reproduce is the MAC bandwidth class.**
   - On MT7996, RATE BWCR [4:2] is "0 / 1 as 20 / 40" for 5 / 10 MHz.
   - On MT7921 the equivalent register is AGG 0x820e2004 [3:2], not 0x820ee008.
   - MT7921 writes (control != centre) there, which is 0 for both narrow widths.
4. **802.11p numerology (64 tones, 156.25 kHz spacing, 8 us symbols) requires the 20 MHz class at divide-by-2.**
   - The 40 MHz class at divide-by-4 does occupy 10 MHz.
   - For HT40/VHT40 PPDUs it produces 128 tones at 78.125 kHz.
   - For legacy rates it produces one or two 5 MHz PPDUs at +-2.5 MHz.
   - MT7996's own tables (the 20 MHz AGC column, BN_6188 at 1/2) point to "20 MHz at half clock", not "40 MHz at quarter clock" [I].
5. **The best untried TX candidates, in order:**
   1. BN_MODE [30:28] = 4 or 6 (0xc... / 0xe...), which has never been measured on TX;
   2. MAC BWCR = 1;
   3. TICKCR0 field combinations, including LMAC_1X_MODE = 3 or 4, which have never been tried.
6. **Hazard:** `tools/guest/txmeter.sh` reads 0x820ee008 on every run, but no MT7921 image references the 0x820ee000 page.

## 1. MT7996 recipe (w1700k headers)
MediaTek's handler for UNI 0x34 tag 0 is V:e00b0a74. Sources: phy_cal.h:983-1001; chanset.c:1160-1180 and 1432-1470.

**Handler order**
1. RMAC SMPS_CTRL.
2. Read the old RATE BWCR.
3. ARB_SCR: TX/RX off.
4. TX power.
5. Channel flow V:e00d708a: P block, tables A/B, RX front end, D-die RX setup, synthesizer.
6. Thermal TX power.
7. MAC words (V:e00e5c9a): RATE BWCR, then TICKCR0 BN0 and BN1, then RMAC CHFREQ.
8. EDCCA.
9. ARB_SCR.
10. Tail: if the new BWCR code is smaller than the old one, the old code is written back.

**BW classes**
- 0..4 = 20/40/80/160/320 MHz (phy_chan.h:55-61).
- 5 = 80+80.
- 6 = 5 MHz and 7 = 10 MHz [I]. The BN_6188 numerators are 1/4 at class 6 and 1/2 at class 7, and MT7921 uses the same numerators for its own 5/10 MHz enums.

| Register (band 0) | Field | 20 / 40 MHz | 10 MHz (class 7) | 5 MHz (class 6) | Source |
|---|---|---|---|---|---|
| WFSYS_ON TICKCR0 0x81031024 (BN1 0x81032024) | [1:0] WLAN_CLK_MODE, then [4:2] LMAC_1X_MODE; one RMW each (ROM FIT +0x20) | 0 / 0 | 1 / 1 (V:e00e5d5a) | 2 / 2 (V:e00e5d02) | conn.h:731-741; phy_chan.h:392-400 [S] |
| LMAC RATE BWCR 0x820ee008 | [4:2] RF_BW0 (stock 0x80) | 0, or 1 if control != centre; 80 -> 2, 160/80+80 -> 3, 320 -> 4 | "0 / 1 as 20 / 40" (note a) | same | phy_chan.h:382-390; mac.h:220-229, 259-260 [S] |
| BN_6188 0x83086188 | [11:0], then [27:16] | round(0x190000 / f_MHz) (classes 0..5) | round(0xc8000 / f) = 1/2 | round(0x64000 / f) = 1/4 | phy_chan.h:191-198; phy.h:1274-1284 [S] |
| DFE_F168 0x8308f168 | [17:0] | round(0xa000000 / f) | same | same | phy_chan.h:194; phy.h:1615-1622 [S] |
| CHAIN_D0AC +0xd0ac, chains 0..4 | [20:0] | round(0xa000000 / f) << s, s = class; 80+80: s = 2 | not documented | not documented | phy_chan.h:195-203 [S] |
| CSD_AGC_TBL +0xdc, per chain | column = AGC_COL[class], {0,1,2,3,4,2,0,0} | col 0 / 1 | col 0 (20 MHz) | col 0 | phy_chan.h:1323-1330; phy.h:196-210 [S] |
| BN_MODE 0x83081000 | [27:24] class | 0 / 1 | not written (stale) | not written | phy.h:1162-1192; phy_cal.h:1434-1438; chanset.c:1101-1116 [S] |
| (same) | [29:28] RF band; [30] and [31] kept; [23:20] primary index | band | band; [30] untouched | same | phy.h:1175-1183; phy_cal.h:1446-1451 [S] |
| Table B key: 3·k1 + rf + 15·(nss−1) + 75·noelna, k1 = class; only class 5 is remapped (to 2) | radix overflow | as class | 5 GHz with 4 RX streams -> key 67 (BW80, 5-stream column); 2.4 GHz -> 66, not in the list (WM assert) | key 64 (BW40, 5-stream column) | phy_map.h:459-500; phy_chan.h:1096-1104 [S] |
| D-die setup bits (note b) | CHAIN_D010[7]; DFE_E338[1:0]; WFCOMM_D24C[15]; DFE_F000[3:1]; WFCOMM_0080[7:4] | per class | as 20 MHz, except 0080 -> 6 | same | chanset.c:1060-1116, 849-866; phy_chan.h:211-220 [S] |
| A-die RX BW 0x100 | class, capped at 160/320 | 0 / 1 | not documented; 20 MHz is the narrowest code | | chanset.c:1022-1027; adie.h:459-465; rfctl.h:72 [S] |
| TMAC SIFS/slot/EIFS, PLCP/CCA timeouts | not written by the channel set | | | | mac.h:220-245 [S] |
| TX-power thermal list, c = (class >= 3) | | | uses the "wide" list (TX only) | | phy_cal.h:1131 [S] |

**Notes**
- (a) phy_chan.h:384-385 says "5 / 10 MHz (0 / 1 as 20 / 40)".
  - This can mean 5 -> 0 and 10 -> 1, or "by the 20/40 rule" (1 when control != centre).
  - MT7921's twin of V:e00e5c9a applies the 20/40 rule to both narrow widths (section 2), which favours the second reading.
  - The MT7996 binary is not in the tree, so this cannot be checked directly.
- (b) The D-die setup bits:
  - CHAIN_D010[7] is cleared only at BW40.
  - DFE_E338[1:0] = 1 below 320 MHz.
  - WFCOMM_D24C[15] is set at BW80 only.
  - DFE_F000[3:1] is written at BW160 only.
  - WFCOMM_0080[7:4] is the 2.4 GHz primary channel; for any width other than 20 MHz it is 6.
- (c) Coverage of the w1700k decode.
  - It reproduces three traced stock channel sets: 20, 160 and 320 MHz (phy_cal.h:974-981).
  - The narrow branches were only read statically and were never executed.
  - The port rejects classes >= 5 (chanset.c:312).
  - A narrow-only helper elsewhere in V:e00d708a would therefore not be documented.
- (d) Clock versus class.
  - The sample rate at the capture taps is 2 x the channel bandwidth, set by the BN_MODE class (phy.h:2908-2915; chanset.h:66-67).
  - The narrow classes write no class code. So MT7996's only rate-like narrow writes are TICKCR0 and the BN_6188 constant.
  - Both describe "the 20 MHz class at 1/2 or 1/4 clock", assuming TICKCR0 slows the PHY there at all [I].
  - Nothing in the headers makes 10 MHz the 40 MHz class: the AGC column is the 20 MHz one, and BN_MODE is never set to 1.
  - The only mention of "40" is the ambiguous BWCR line, and BWCR is a MAC register.

## 2. MT7921 mapping
The 2026 RAM image is cited. all2022.c and the rom_all.c image contain the same code (rom_all.c @0x93dbd4, 110789-110802).

**Call path**
1. ext 0x08/0x4e handler at 0x964494 (all.c @0x964494).
2. FUN_962eae (all.c @0x962eae) maps the driver bandwidth: CMD_CBW 4 -> internal 5, and CMD_CBW 5 -> internal 4.
3. sco is computed: 0, 1 (centre > control) or 3 (centre < control) (all.c @0x964494).
4. rlmPhySetChannel at 0x963f6c (all.c @0x963f6c; called at :86759).
5. FUN_941e00 sets struct +0x11: internal 5 -> 6 (10 MHz), internal 4 -> 5 (5 MHz).
6. FUN_93bc9c (all.c @0x93bc9c) and its helpers program the PHY.
7. FUN_941d22 (all.c @0x941d22; called at :86414) writes the MAC words.

| MT7996 item | MT7921 address | MT7921 writer and values: enum 6 (10 MHz) / enum 5 (5 MHz) | Confidence |
|---|---|---|---|
| TICKCR0 | 0x820b00a0: [1:0] WLAN_CLK_MODE, [4:2] LMAC_1X_MODE. WFSYS_ON, crmap module 0x1c group 10; one copy only | FUN_941cfa, called from FUN_941d22 (all.c @0x941cfa, 47859; 0x941d52-0x941d62): 0x05 / 0x0a, else 0. Bounds: WLAN <= 2, LMAC <= 4. Same slot and same order as MT7996 | S; M (0x5 / 0xa read on card B) |
| RATE BWCR [4:2] | AGG 0x820e2004 [3:2]; band 1 at 0x820f2004. GCI selector 0x20162 + band << 16 (crmap module 2, group 11, field 2) | FUN_941ce6 (all.c @0x941ce6, 47854; 0x941d2c-0x941d4e): 80 -> 2, 160 or 80+80 -> 3, otherwise (sco != 0). For narrow widths with control == centre this gives 0 for both. No read and no write-back, unlike MT7996 | writer S. Role I: same routine and same codes, connac2 keeps BWCR in AGG (compare mt7603 MT_AGG_BWCR [3:2]). Never read on hardware |
| RMAC CHFREQ | 0x820e5090 [7:0] | FUN_941d22 (all.c @0x941d22) | S |
| BN_6188 | 0x83086010 [11:0] and [27:16]; path 1 at 0x83096010 | FUN_93c724 (all.c @0x93c724; 0x93c74a-0x93c794): round(0xc8000 / f) for 10 MHz, round(0x64000 / f) for 5 MHz, 0x190000 otherwise. f is the channel-number centre (2412 MHz for ch 1), not the outband 2350 | S: identical numerators; M |
| DFE_F168 | 0x8308f104 [27:16] = round(0x280000 / f) | FUN_93c724; no bandwidth term | I |
| CHAIN_D0AC | 0x8300d0ac / 0x8301d0ac [25:8] = round(0x1400000 / f) << s; same per-chain offset as MT7996 | FUN_93c87e (all.c @0x93c87e): s = 0/1/2/3, 80+80: 2; narrow: s = 0 (no case) | S |
| AGC column | 0x830000dc / 0x830100dc (per-chain +0xdc, 48 words) | FUN_93c9ac (all.c @0x93c9ac): enums 0, 5 and 6 use the 20 MHz column | S |
| WFCOMM_0080 | 0x830a0080 [7:4] | FUN_93ca3e (all.c @0x93ca3e): widths other than 20 MHz -> 6. Only written for 2.4 GHz channel frequencies, so not at 2350 | S |
| BN_MODE class | 0x83081000 [27:24] | FUN_93dbd4 (all.c @0x93dbd4): 0/1/2/7 for 20/40/80/160; narrow -> 0 (all.c @0x93dbd4, 43281-43290). In dual-path mode only path 0 is written; 0x83091000 is untouched | S, M |
| (no MT7996 equivalent) | 0x83081000 [30:28] | FUN_93dbd4: 5 / 7, forcing [28] = 1 even at 2.4 GHz. Written by the second clock-routine call, after the calibrations (all.c @0x93bc9c) | S; M: TX divide-by-4 / 8, RX front end divide-by-2 / 4 |
| Table key bandwidth digit | Table A tuple t1 maps 5 -> 3 and 6 -> 4 in a radix-3 digit (FUN_93c6f8, all.c @0x93c6f8). 10 MHz therefore takes the "40 MHz, +1 stream" column and 5 MHz the "20 MHz, +1 stream" column. Table B has no bandwidth digit | | S |
| (MT7921 only) | 0x83080000 [12] = 0 (20 MHz-only flag); 0x8300d06c / 0x8301d06c [6:3] = 0x8 (the non-20 one-hot value); RF 0x160 [31:30] = 0 and RF 0xb00 / 0xb04 at their 20 MHz values, so the A-die filter stays at 20 MHz | FUN_93dbd4 | S |
| TMAC timing | 0x820e4090 (CDTR), 0x820e4094 (ODTR), 0x820e40a4 (ICR0) | Written in microseconds by mt76 (`mt792x_mac_set_timeing`) after every CHANNEL_SWITCH; the firmware leaves them alone | S; M (unchanged in narrow mode) |
| (MT7996 RATE page) | 0x820ee000 | Not referenced by RAM 2026, RAM 2022 or the ROM. No GCI module covers it, and mt76 defines no RATE block for connac2 | S: treat it as absent |

## 3. Why MT7921's "10 MHz" comes out as 5 MHz
1. **MT7921 adds a PHY divider write** [S+M].
   - Only MT7921 writes a PHY narrow code (BN_MODE [30:28] = 5).
   - The other rate-related writes in both firmwares encode 10 MHz as divide-by-2: TICKCR0 = 1/1, and BN_6188 / 0x83086010 at 1/2.
   - The MT7961 silicon divides the TX by 4 and the RX front end by 2 for that code.
   - The TX output is a clean quarter-rate PPDU, and TX completes once TICKCR0 >= 1, so this is not a timing failure. For the same code, the TX chain's narrow divider is twice the RX front end's.
   - The firmware never writes codes 4 or 6.
2. **TICKCR0 is not the PHY clock on MT7921** [M].
   - MT7996's recipe has no other rate knob, so on MT7996 TICKCR0 would have to slow the PHY [I].
   - On MT7921 the same values do not change any sample rate:
     - TICKCR0 = 0x5 with BN_MODE 0xb gave a 20 MHz TX;
     - divide-by-4 TX also appears with TICKCR0 = 0 (the TX then never ends);
     - at TICKCR0 = 0x5 the demodulator still decodes 20 MHz frames.
   - Copying MT7996's TICKCR0 values therefore changes nothing: they are already applied.
3. **The MAC bandwidth class differs** [S/I].
   - MT7996's BWCR may be 1 for 10 MHz; MT7921 writes 0 for both widths. This is a MAC-side setting.
   - The mt76 copy under `work/ext/mt7921-11p-linux/.../mt76_connac_mac.c:638-641` leaves TXD DW6 FIXED_BW clear (check that the rig's stack does the same). In that case the MAC picks the PPDU bandwidth, and BWCR could change it, for example into a 40 MHz non-HT duplicate.
   - None of these outcomes is 802.11p numerology.
4. **Path coverage** [S]: MT7921 writes the code on path 0 only. This does not matter for 1-stream TX on path 0.
5. **Shared, unfinished bookkeeping** [S]: both firmwares leave D0AC without a narrow shift, alias the table columns and use the 20 MHz AGC column. This affects RX sensitivity, not TX width.
6. **TICKCR0 is not simply "the divider ratio"** [M].
   - Divide-by-4 with TICKCR0 1/1 gives complete PPDUs.
   - Divide-by-4 with TICKCR0 0 never ends.
   - Divide-by-8 with 2/2 loses its last ~80 us.
   - LMAC_1X_MODE 3 and 4 are legal in FUN_941cfa but have never been tried.

## 4. Ranked experiments
**Common setup**
- Frequency: 2350 MHz.
- Transmitter: card B, as in txmeter.sh. Observer: card A at 30 Msps, or the X410 on RX1.
- Base for every run:

```
chsw.py $P 11 0 0
chsw.py $P 1 0 4 1 1 2350      # CMD_CBW_10MHZ
```

- First, remove txmeter.sh's read and write of 0x820ee008 (see section 5).

**E0 (read-only).** Read 0x83081000, 0x83091000, 0x820b00a0, 0x820e2004, 0x83086010, 0x8300d0ac, 0x8300d06c and 0x83080000. Expected values [S]:
- 0x83081000 = 0xd0000000;
- 0x83091000 keeps its 20 MHz value (0x8...);
- 0x820b00a0 [4:0] = 0x05;
- 0x820e2004 [3:2] = 0 (the first read ever);
- 0x83086010 = 0x01540154 (computed for 2412 MHz);
- 0x8300d0ac [25:8] = 0x21f7;
- 0x8300d06c [6:3] = 8;
- 0x83080000 [12] = 0.

1. **E1: BN_MODE narrow code on TX** (the most promising).
   - Write 0x83081000 = 0xc0000000, then 0xe0000000. Keep TICKCR0 at 0x05; for 0xe, also try 0x0a. Example:

     ```
     txmeter.sh 2350 4 0xc0000000 0x5 40
     ```

   - Then try the values with [31] = 0: 0x40000000, 0x50000000, 0x60000000.
   - Optional: mirror each value into 0x83091000.
   - Measure:
     - the STF period (1.6 us means 10 MHz timing);
     - the FFT period (6.4 us);
     - the burst length;
     - the FCS at 10 MHz.
   - For each code, also measure the RX front-end ratio with `icapcode.sh` on taps 4 and 12.
   - Why: [30:28] is one 3-bit GCI field (crmap module 0x48, group 30). In normal mode [28] is the band bit, and the narrow path forces it to 1. [30] without [28] is the only corner of that field nobody has tried.
   - Risk: low.
2. **E2: MT7996's 10 MHz MAC class.**
   - (a) Native route:

     ```
     txmeter.sh 2350 4:3 keep keep 40
     ```

     A centre different from the control channel gives sco = 1, so the firmware writes 0x820e2004 [3:2] = 1 itself. FUN_941e48 has no narrow case, so the primary index stays 0; confirm that 0x83081000 [22:20] = 0.
   - (b) Direct write:

     ```
     X="0x820e2004&=0xfffffff3 0x820e2004|=0x4" txmeter.sh 2350 4 keep keep 40
     ```

   - (c) Repeat (b) with the best E1 code.
   - Measure the TX numerology. Possible outcomes:
     - one 5 MHz PPDU;
     - two 5 MHz copies at +-2.5 MHz (a 40-class duplicate at divide-by-4);
     - 10 MHz timing.
   - Repeat with TXD DW6 forced to FIXED_BW = 1, BW = 0, using the mt7921dbg txwi kretprobe (as for tx_count).
   - RX check: `pdcnt.sh` with X410 802.11p frames, BWCR = 1 combined with [27:24] in {0, 4, 5}.
   - Risk: low.
3. **E3: TICKCR0 field decoupling.** MT7996's recipe implies TICKCR0 is the rate knob.
   - On the base, try T in {0x01, 0x04, 0x02, 0x08, 0x06, 0x09, 0x0d, 0x0e, 0x11, 0x12}:

     ```
     txmeter.sh 2350 4 keep T
     ```

     Also run `pdcnt` with 10 MHz frames for T in {0x01, 0x04}.
     - Values with WLAN_CLK_MODE = 0 (0x04, 0x08) are expected to leave TX unfinished at divide-by-4. Use short injections for those.
   - On the cbw 5 base (0xf..., firmware T = 0x0a), try T in {0x0e, 0x12, 0x09}. Check whether the divide-by-8 PPDU keeps its last 3 symbols.
   - On cbw 0 (0x80000000), try T = 0x05. Check whether TX stays at 20 MHz and completes.
   - Stay within WLAN_CLK_MODE <= 2 and LMAC_1X_MODE <= 4.
   - Risk: low to moderate. A stuck TX needs a rigreset.
4. **E4: "40 MHz class at divide-by-4" diagnostic.**
   - Set up a 40 MHz channel:

     ```
     chsw.py $P 11 0 0
     chsw.py $P 1 0 1 1 1 2350 3
     ```

     This puts the 40 MHz class at LO 2350 with the primary in the lower half. The firmware writes BWCR = 1 and TICKCR0 = 0.
   - Then set 0x83081000 = 0xd1000000 and 0x820b00a0 = 0x05:

     ```
     txmeter.sh 2350 1:3 0xd1000000 0x5 40
     ```

     Inject legacy 6 Mbps frames; optionally also HT40 MCS0 with FIXED_BW and BW = 1.
   - Expected [I]:
     - legacy frames: one or two 5 MHz PPDUs at +-2.5 MHz;
     - HT40: 10 MHz wide, with a 12.8 us FFT period.
   - This answers the 40-class question but cannot give 802.11p.
   - The emission stays within 2330-2370 MHz.
   - The earlier notes/03 run of this setup keyed forever because TICKCR0 was 0.
5. **E5: demodulator.** Run after any hit in E1-E3, or as a last sweep on 0xd.
   - The X410 sends 802.11p 10 MHz frames at 2350.
   - Card B sweeps the following grid (C is the code that hit):
     - BN_MODE in {C, C|0x04000000, C|0x05000000};
     - TICKCR0 in {0x05, the hit value};
     - BWCR in {0, 1};
     - 0x83088014 [31:24] in {0xea, 0x75}.
   - Also write the 10 MHz values for a 2350 MHz centre: 0x8300d0ac / 0x8301d0ac [25:8] = 0x116e, and 0x83086010 = 0x015d015d.
   - Score the PD, MDRDY, RxOk and SIG-error counters at 0x83081010..24.
   - Success: MDRDY and RxOk count the 10 MHz frames.
6. **E6 (guess; moderate risk): TX-vector bandwidth field.**
   - Locate the bandwidth bits by comparing the TX-vector latch 0x8308a830..0x8308a85c after a 20 MHz TX and after an E4 40 MHz TX. MT7996's generator word 0 keeps the bandwidth at [10:8] (phy_cal.h:2353-2356).
   - Start ContiTx (wrapper 0x943f76) on the base.
   - Write 4 or 5 into that field of the generator copy at 0x8308a008, with BN_MODE 0xd or 0xc. The MAC/TXD path can only express 0..3, so these values have never reached the PHY.
   - Stop with FUN_968e82, then rigreset.

## 5. Hazards
1. **Do not use MT7996 addresses on MT7921.**
   - MCU reads of 0x8103xxxx (where MT7996 keeps TICKCR0, at 0x81031024) hang the MT7921 MCU.
   - The 0x820ee000 page (MT7996 RATE BWCR) does not appear in any MT7921 image or in the GCI map, so it carries the same class of risk.
   - `tools/guest/txmeter.sh` reads 0x820ee008 on every run (`BWCR $(r 0x820ee008)`), and writes it when BWCR is set. Remove both, and use 0x820e2004 [3:2] instead.
   - Use 0x83086010 / 0x8308f104 / 0x8308f064, not 0x83086188 / 0x8308f168 / 0x8308f110.
2. **WFSYS_ON (0x820b0000): only TICKCR0 may be written.**
   - Write only 0x820b00a0 [4:0], from the MCU (fwdbg), with WLAN_CLK_MODE <= 2 and LMAC_1X_MODE <= 4.
   - Never write:
     - LPCR 0x820b0004 / 08 / 14 / 18 (DIGCK, PLL_CKEN, LP0_OWN);
     - 0x820b0024..0x38 (power handshake);
     - 0x820b00a8 [11:10];
     - 0x820b00ac;
     - 0x820b009c and 0x820b00c0..d4.
   - On MT7996, a write in WFSYS_ON can wedge the part until power-off, and the 0x820b0000 alias is quarantined for host reads (conn.h:684-690).
3. **BN_MODE [30] with TICKCR0 [1:0] = 0 means TX never ends.** The PA stays keyed, the queue stalls and the driver resets. Set TICKCR0 first, and restore with chsw or rigreset.
4. **BN_MODE [27:24] codes are destructive:**
   - codes >= 8 kill RX;
   - code 3 freezes the chain;
   - codes 4 and 5 stop all reception, DSSS included.

   Restore with chsw.
5. **Reads that reset or hang:**
   - Host reads at or after 0xe0300000 reset the card.
   - MCU reads of 0x00080000, and of 0x80xxxxxx pages not on the safe list, hang or fault the MCU.
6. **Off-limits blocks:**
   - CONN AFE 0x7c003xxx;
   - CONN_INFRA 0x7c005004/08 and 0x7c00a008;
   - 0x88009000, 0x88009004 [23:0] and 0x88009008. The only allowed use of 0x88009004 is [27:24], as the ICAP lend.
   - RF 0xcc4, 0xd20 and 0xce0 (VCO calibration).
7. **E2 with BWCR = 1 and FIXED_BW clear** may make the MAC request a 40 MHz TX vector from a 20-class PHY. The reaction is unknown, possibly a stuck TX.
8. **Stop generators before a rig reset:** ContiTx (FUN_968e82) and the tone generator (FUN_969126).
9. **Transmit only in 2300-2400 MHz.**
