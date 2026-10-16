# MT7921 D-die PHY clock tree vs channel width: is a 10 MHz (half-rate) PHY possible?

> Status: a static study written on 2026-10-16 during the 10 MHz investigation. Its ranked experiments were run
> the same day (notes/07, 2026-10-16 sections; notes/03): native 10 MHz is impossible on MT7961, because the
> transmitter divides by 1, 4 or 8 only and the demodulator has fixed 20 MHz numerology. §5 (what ends a PPDU)
> is answered by notes/10 §5: the MAC tick WFSYS_TICKCR0. Raw sources it cites (`work/…`) are local files,
> see README.md "Rebuilding work/".

Static analysis only (2026-10-16). The rig was not touched.

**Sources**
- `work/decomp/all.c` (RAM 2026), `rom_all.c` (ROM), `all2022.c`;
- `work/dis/rom_800000.txt`, `ram_0_00915000.txt`, `patch_0_00900000.txt`;
- `data/crmap/crmap.txt`;
- `work/phydump` snapshots (bw_b20/b40/b80 at 5180 MHz; wide_w20/w10/w5 at 2437 MHz; A_a10/a20; phy22/26);
- live DLM `work/dlm_live_B.bin` (base 0x02000000);
- the MT7996 reference `work/ext/w1700k/src/include/hw/mt7996/*.h`;
- the hardware TX code map that was added to notes/07 today.

**Tags**
- [static]: read from code or tables.
- [snap]: register snapshots.
- [hw]: measured on the rig (notes).
- [inferred]: reasoned from the evidence.
- [guess]: a guess.

## 1. Short answers

1. **No register changes a clock frequency between 20, 40 and 80 MHz.** [static + snap; "fixed clock" is inferred]
   - The channel switch writes only:
     - the per-path BW code 0x83081000 [27:24] (0/1/2/7);
     - the "20 MHz only" flags 0x83080000 [12] and 0x8300d06c bit 5;
     - the SFO constant 0x8300d0ac (<< 0/1/2/3);
     - the A-die analog filter class RF 0x160 [31:30] (0/1/2);
     - the A-die converter/filter class RF 0xb00/0xb04 (the same at 20 and 40 MHz, doubled at 80/160).
   - TX has no static per-width register. The TX pages 0x8308a000–0x8308e000 are identical at 20/40/80/10/5 MHz [snap]. The per-PPDU TX vector carries the bandwidth.
   - So "twice as fast at 40 MHz" is a choice of decimation and numerology inside a fixed-clock datapath.
2. **The only real clock divider is 0x83081000 [30:29]** (÷4, plus an extra ÷2), and [31] gates the data path.
   - Neither firmware (RAM 2026 or 2022), the ROM, the ROM patch, the GCI tables, nor the MT7996 reference has a ÷2 field. The hardware map (notes/07) confirms there is none in [31:28].
   - **One lever was never varied: the TX-vector bandwidth under the narrow divider.**
     - MediaTek's own MT7996 firmware builds its 10 MHz width as *MAC BW class 40 + TICKCR0 mode 1*, and its 5 MHz width as *class 20 + mode 2* (phy_chan.h:382-399, mac.h:223-226).
     - MT7921's firmware never selects the 40 class for narrow widths.
     - Every hardware narrow test so far sent 20 MHz TX vectors [inferred]: mt76 writes TXD DW6 BW = 0 for injected fixed-rate frames, and the MAC class is 0 at cbw 4.
     - If the narrow generator takes its base rate from the TX-vector BW, then BW 40 at ÷4 (or BW 80 at ÷8) is a 10 MHz half-rate PPDU. [guess; the cheapest test is T1]
   - On MT7921, the MT7996 "RATE BWCR" role belongs to **AGG 0x820e2004 [3:2]** (selector 0x20162, FUN_941ce6). The address 0x820ee008, which txmeter.sh's BWCR variable writes, reads 0 here and is not that register.
3. **End of PPDU.**
   - The firmware programs no TX-duration or preamble register per width. The only narrow-dependent MAC knob is TICKCR0 0x820b00a0.
   - The ÷8 deficit (about 80–96 µs) matches, within one symbol, a 20 µs legacy preamble counted at ÷4 instead of ÷8: (8−4)×20 = 80 µs.
   - The bursts with bit 31 clear (preamble + SIG + 1 symbol, then abort) show that the PHY aborts on data underflow.
   - §5 lists the candidates and four tests that tell them apart.

## 2. Clock tree as the sources show it
```
XTAL 24/25/26/40 MHz
 └ CONN AFE PLL / clock mode 0x7c003004 [1:0],[3:2],[7:6],[17:16], 0x7c0030f4 (per crystal, ROM FUN_80ab32)   HAZARD
    └ WF PLL ─ WFSYS_ON 0x820b0000: LPCR gates (PLL_CKEN [4], DIGCK [1]); power/PLL sequence FUN_9397ea (0x820b00a8 [11:10])
        ├ MAC/LMAC clocks ─ TICKCR0 0x820b00a0: [1:0] WLAN_CLK_MODE, [4:2] LMAC_1X_MODE = 0 / 0x5 / 0xa
        └ D-die PHY clock (the same at every width)
            ├ A-die converters: RF 0xb00 [29:24], 0xb04 [28:24],[7:6] (20/40/narrow 0x10,0xf,1; 80+ 0x20,0x1f,2)
            │   analog LPF RF 0x160 [31:30] (0/1/2); TRX_CLK 0x83007254 [5:4] DAC on, [7:6] ADC on (enables only)
            ├ RX front end 0x8308f060..f068: AFIFO -> automatic decimation (MAN_DOWNSAMP=0) -> DLPF; output 2*BW
            │   narrow [30:28]=5: +-5 MHz, 7: +-2.5 MHz, output 20 Msps (x2 holds)                              [hw]
            ├ behind the FE: BN_MODE [27:24] 0/1/2/7 = 20/40/80/160 numerology; 4/5 = /2 before the sync stage   [hw]
            │   demodulator (sync, FFT, SIG): never divided                                                     [hw]
            └ TX: TX-vector BW (per PPDU) -> legacy generator at its own 20 MHz base
                  narrow path: [30] = /4, [29] = extra /2 (only with [30], needs [28]), [31] = data path         [hw]
   band clock forces 0x83080004/08 [23:0]; RST_CTRL 0x83080014; PHY-top 0x830a3000/04 + 0x830ab3c4 (DBDC only)
```

## 3. Register and field tables

### 3.1 BN_MODE 0x83081000 (path 1: 0x83091000)
Crmap module 0x48, group 30, 13 fields: [0] [1] [30:28] [27:24] [6] [31] [11:8] [3:2] [15:12] [5:4] [7] [22:20] [18:16].

The narrow columns are the firmware's own enums: "10 MHz" = cbw 4 → internal 5 → enum 6; "5 MHz" = cbw 5 → internal 4 → enum 5.

| field | 20 | 40 | 80 | 160/80+80 | fw "10 MHz" | fw "5 MHz" | role (confidence) |
|---|---|---|---|---|---|---|---|
| [31] | 1 | 1 | 1 | 1 | 1 | 1 | Data-path enable. With [31]=0 the PHY sends preamble + SIG, then aborts [hw]. Every firmware write keeps it [static] |
| [30] | 0 | 0 | 0 | 0 | 1 | 1 | Narrow ÷4 on the legacy generator (TX). On RX: FE filter ±BW/4 and ÷2 [hw] |
| [29] | 0 | 0 | 0 | 0 | 0 | 1 | Extra ÷2, only together with [30]; needs [28] (0xe is silent) [hw]. In MT7996 this is a bit of the RF-band field |
| [28] | band (2.4G = 0, 5G = 1) | | | | forced 1 | forced 1 | RF band. Must be 1 when [29] is set [hw] |
| [27:24] | 0 | 1 | 2 | 7 | 0 (cleared) | 0 | Per-path BW code. Selects FE decimation and demod numerology. Codes 4/5 add a ÷2 before the sync stage; 3 freezes the chain; ≥8 kills RX; codes 4/5 leave TX unchanged [hw] |
| [22:20] | primary-20 index | | | | same | same | Primary channel index [static] |

Writers:
- RAM `FUN_93dbd4`, all.c @0x93dbd4. The narrow writes are at 43227/43233 (DBDC branch) and 43281/43288. The bandwidth-code writes are at 43242–43248 and 43300–43306.
- `FUN_93bc9c` (all.c @0x93bc9c/42241) runs it twice in narrow mode: first with bw forced to 0, then with the narrow enum.
- ROM `FUN_841182` (rom_all.c @0x841182) and its 2026-patch twin at 0x906050: [30:28] = band.
- ROM BW writer `0x840fd8` (dis rom_800000.txt:89814; ROM pointer table 0x851818 → DLM 0x02014a94):
  - [27:24] = {0,1,2}[a0], else 3; [30:28] = a1;
  - its only callers are calibrations, passing a0 = 1/2/3 and a1 = band (rom_all.c @0x841c2c, 56622, 57548, 57956).
- An undecompiled ROM calibration at 0x84099a writes [27:24] = 0/1/2 with step counts 4/6/10 (dis 0x840b12–0x840b5a).
- ContiTx `FUN_968f2e` (all.c @0x968f2e) writes 0x80000000 | code<<24, code ∈ {0,1,2,7}.

### 3.2 Width-dependent writes on CHANNEL_SWITCH (RAM 2026; 2022 is identical)
Path: ext 0x08/0x4e `0x964494` → `FUN_962eae` (cbw → internal bw) → rlmPhySetChannel `0x963f6c` → `FUN_942672` → `FUN_941ea4` → `FUN_93bc9c`, plus `FUN_941d22` for the MAC word.

| register / field | 20 | 40 | 80 | 160 | enum 6 / enum 5 | writer (all.c line) | role, confidence |
|---|---|---|---|---|---|---|---|
| 0x83080000 [12] (+0x83090000) | 1 | 0 | 0 | 0 | 0 / 0 | FUN_93dbd4 43238, 43293 | "20 MHz only" flag [static] |
| 0x8300d06c [6:3] (+0x8301d06c) | b5 | b6 | b6 | b6 | b6 / b6 | 43225, 43239, 43277, 43294 | One-hot demod class; bits 3 and 4 never set [static] |
| 0x8300d0ac [25:8] | round(20·2^20/f)<<0 | <<1 | <<2 | <<3 (80+80: <<2 plus a second frequency) | <<0 | FUN_93c87e 42625 (shifts 42645–42661) | SFO-from-CFO, scales with sample rate [static] |
| 0x83086010 [27:16],[11:0] | round(0x190000/f) | same | same | same | ÷2 / ÷4 | FUN_93c724 42585–42594 | Rate/carrier ratio. Firmware scales it for narrow, so **MediaTek expected a slower demodulator in narrow mode** [static/inferred] |
| 0x8308f104 [27:16] | round(0x280000/f) | same | same | same | same | 42602 | Frequency only [static] |
| 0x830a70a0, 0x830a77b4, 0x830a6014 | – | – | – | secondary-frequency ratios | – | FUN_93c724 | 160 and 80+80 only [static] |
| RF 0x160 [31:30] | 0 | 1 | 2 | 2 | 0 | 43343–43370 | A-die analog LPF class [inferred] |
| RF 0xb04 [7:6], [28:24]; RF 0xb00 [29:24] | 1, 0xf, 0x10 | same | 2, 0x1f, 0x20 | same as 80 | 20 MHz values | 43354–43387 | Converter/filter class. Halving or doubling them did not change 20 MHz RX, so they are not a sample-rate select [hw] |
| TICKCR0 0x820b00a0 | 0 | 0 | 0 | 0 | 0x5 / 0xa | FUN_941d22 → FUN_941cfa (47837 → 47822) | MAC tick. The writer accepts only WLAN_CLK_MODE < 3 and LMAC_1X_MODE < 5 [static] |
| AGG 0x820e2004 [3:2] | 0 (1 if SCO≠0) | 1 | 2 | 3 | SCO≠0 (normally 0) | FUN_941ce6 47812 (dis 0x941d2c–0x941d4e) | MAC BW class; MT7996's RATE BWCR role [static/inferred] |
| RMAC 0x820e5090 [7:0] | control channel | | | | | FUN_941d22 | [static] |
| Table A (FUN_93e62a) | columns t1 = 0/1/2 | | | | t1 = 4 aliases to "40 MHz, t2=1"; t1 = 3 to "20 MHz, t2=1" | 43535 | At cbw 4 the TX-FE values are already the 40 MHz ones: 0x8308f130 = 0x8800739c, 0x830a0000 = 0x03ff2101, 0x830a0050 = 0x215 [snap A_a10] |
| 0x83088394 / 0x83088398 | 0x00100300 / 0 | 0x200 / 0x1ff000 | 0x00300200 / 0x300 | | 0 / 0 | FUN_93cab0 | Primary position [snap] |
| 0x8308f0a4 [30], 0x8308f130 [23], [29], [25] | 5G and 20 / 20-only | | 80-only | | alias column | Table A | TX-FE class bits. 40 MHz values on top of ÷4 still gave 5 MHz [hw] |

Words written on every switch that do not depend on width:
- 0x830a3004 [27:16]/[6:4] = 0x8cc/1 single band, 0x4ee/4 DBDC (with 0x830a3000 [31,28] and 0x830ab3c4 [31]). It reads 0x08cc3210 at every width [snap].
- 0x8300c004 [31:30] = 0 (never set).
- 0x8300c02c [29:28] = 3; 0x830000fc [15:12] = 6; 0x830ae010 [16].
- 0x8308f068 [27:26] = 2; 0x8308f064 [19] = 0; 0x8308f060 [4:0] = 1.
- 0x8308b000 [31:30],[27:26] = TX streams − 1.

### 3.3 Clock enables, forces and resets (no rate select)
- **0x83080004 / 0x83080008 [23:0]** (+0x8309…):
  - stock value 0x3800 at every width [snap];
  - TxTone writes 0xffffff (all.c @0x9691d2), the ROM calibration FUN_846514 sets |= 0xffffff, ContiTx writes 0x38, tone stop writes 0;
  - the ICAP helpers FUN_96b672/96b67c → FUN_96c546/554 set |= 0x40.
  - These are force-ons (MT7996 BN_CLK0/1, phy.h:979-1006).
- **0x83007254 TRX_CLK**: [5:4] DAC on, [7:6] ADC on. It reads 0 everywhere [snap], and forcing it did not narrow anything [hw] (MT7996 phy.h:394-403).
- **0x83080014 RST_CTRL** (crmap module 0x4c, group 0): MT7996 names [17] TXFD, [16] TXTD, [15] TXFE, [3] FFT, [2] RXTD, [1] RXFE. Logic resets only.
- **0x8308f068 WF_RXFE_01** (crmap module 0x58, group 52):
  - fields ADC480M_MODE [16], AFIFO_CK320M [3], MAN_DOWNSAMP [1:0], WB_SEL [15:12], DLPF_MODE_MAN [6:4];
  - it reads 0x08000000 at every width and mode [snap];
  - firmware writes only [27:26]; the ROM sets [2] and [7] (selectors 0x58068b and 0x580687);
  - all fields were tried at cbw 4 and none decoded 10 MHz frames [hw].

### 3.4 Fields that look like BW selects but are chain selects
- 0x83080000 [5:4] = {0,1,2}[x] (ROM FUN_841342, mode 2, table 0x850768): x is the **chain index**. FUN_847e1c passes param[1] = path, which is also its ×0x10000 offset [static].
- 0x83007240 [29:28] (FUN_841418, table 0x850764): chain index as well [static].

### 3.5 MAC timing: the MT7921 TMAC layout is MT7996's, not MT7915's
- **ICR0 is 0x820e4014**: EIFS [8:0], RIFS [14:10], SIFS [22:16], SLOT [30:24].
  - ROM setter FUN_8282e6 (rom_all.c @0x8282e6): type 0 = slot, 1 = SIFS, 2 = RIFS, 3 = EIFS.
  - RAM caller FUN_94463a writes slot 9/20 and EIFS 0x54.
- **CDTR/ODTR are 0x820e40c8 and 0x820e40cc** ([31:16] and [15:0]): ROM FUN_8288d0, called from RAM FUN_94bf08 (all.c @0x94bf08).
- **0x820e4010** ([24:16] = 0x78, [8:0] = 0x82, [15:9] = 0x1e, [25] = 1) and **0x820e400c** ([24:16] = 0xf0, [7:0] = 1):
  - set at init by ROM FUN_828848, called from RAM FUN_9443fc (all.c @0x9443fc);
  - the layout matches TRCR0's I2T/TR2T fields [inferred].
- 0x820e4008 [8:0] = 0xbe (2.4G) / 4 (5G) on every switch (FUN_94c754).
- mt76's mt792x_regs.h uses MT7915 offsets (CDTR 0x090, ODTR 0x094, ICR0 0x0a4, TRCR0 0x09c). **On MT7921 those are not the timing registers.** [static GCI; inferred for mt76]
- None of these depends on width. Narrow MAC timing relies on TICKCR0 alone.

## 4. Is there a ÷2 anywhere?
Everything checked comes back negative.

**TX**
- [hw] All 16 values of 0x83081000 [31:28] were measured:
  - only 0xc/0xd give ÷4 and 0xf gives ÷8;
  - 0xe, 0x2 and 0x7 are silent;
  - with bit 31 clear, frames abort.
- [hw] Codes 0–15 in [27:24] leave TX unchanged.
- [hw] On a 40 or 80 MHz PHY, ÷4 still gives a 5 MHz PPDU centred on the LO. The narrow path takes the legacy generator before the 40 MHz mixer/upsampler.
- [static] No other narrow write exists in RAM 2026/2022, the ROM or the ROM patch.
  - The ROM BW writer only ever emits [27:24] ∈ {1,2,3} and [30:28] = band.
  - The ROM's PHY GCI selectors cover only TOAE, TPC and RXFE [2]/[7] (selector census: 0x480243/44, 0x580687/8b, 0x54xxxx).
- [snap] The TX pages are identical across widths.
- [hw] TICKCR0 changes MAC timing only.

**RX**
- [hw] Codes 4/5 halve only the stage in front of the sync. 0x83088014 [31:24] 0xea → 0x75 made 20 MHz frames decode at 20 Msps.
- 0x83088014 [31:24] is 0xea at 20, 40 and 80 MHz [snap], so the sync constants are fixed for the primary-20 at 40 Msps.
- [inferred] Model: FE → (codes 4/5: ÷2) → sync → FFT/SIG.
  - A 10 MHz demodulator would also need a ÷2 *after* the sync.
  - No field for that was found.

**What is left (untested)**
- **The TX-vector BW under the narrow divider** [guess]:
  - MT7996 = class 40 for 10 MHz;
  - J-mode (802.11j, the reason for the narrow code: 4.9 GHz bases 0x3d12c4 / 0x4c5504 in FUN_942672) also implies 10 MHz channels.
- **0x83081000 [30:28] = 6** may be an unfinished ÷2 or ÷16 code that lacks an enable. It was silent on every base [hw], so it is low priority.

## 5. What ends a PPDU (÷8 tail loss)
**Facts**
- [hw] ÷4 with TICKCR0 = 0x5: the PPDU is complete.
- [hw] ÷4 with TICKCR0 = 0: the PPDU is valid but TX never ends.
- [hw] ÷8 with 0xa: the last 3 symbols are lost (about 96 µs), and the MAC retries 11–15× even for broadcast frames.
- [hw] [31] = 0 at ÷4: about 96 µs is sent (preamble 64 + SIG 16 + one symbol 16), then the PHY aborts, and the MAC retries hundreds of times.
- [static] The firmware programs no TX duration, PLCP or preamble register per width (TMAC values are init-time constants, §3.5).

**Reading of the facts**
- A MAC timer scaled ×2 (0x5) would cut ÷4 PPDUs in half, so the end is driven by the PHY, or by a MAC timer of at least ×4.
- "Never ends" with WLAN_CLK_MODE = 0 suggests that WLAN_CLK_MODE configures the clock-domain crossing of the PHY→MAC TX-end handshake [inferred].
- The ÷8 deficit ≈ (8−4) × 20 µs (preamble) = 80 µs, which garbles 3 symbols at 32 µs each. Candidates:
  - **(a)** The block that times the preamble or TX start runs in the [30] (÷4) domain and does not see [29]. TICKCR0 would then be irrelevant, and the clean fix is ≥3 symbols of padding (already in nbrxd).
  - **(b)** A pipeline or flush allowance scaled by TICKCR0 that is ×4 for both 0x5 and 0xa.
  - **(c)** A MAC abort, which the TX status flags would show.
- Tests E1–E4 below separate these.

## 6. Ranked experiments
Base for every test: 2350 MHz; `chsw.py B 11 0 0` first. Observe with card A's 30 Msps stream and `txmeter.py` (STF 0.8/1.6/3.2/6.4 µs = 20/10/5/2.5 MHz timing, plus decode at all widths), or with the X410 on RX1. Restore with chsw, then rigreset.

**Preparation**

**P0. Add two parameters to the mt7921dbg kretprobe on `mt76_connac2_mac_write_txwi`** (it already rewrites DW3):
- `tx_bw`: `w6 = (w6 & ~0x7) | BIT(2) /*FIXED_BW*/ | bw`;
- `tx_rate`: overwrites DW6 [29:16], e.g. 0x4b = OFDM 6 Mbps, 0x80 = HT MCS0 (mode [9:6] = 2).

Set `TX_STATUS_HOST` and a PID in DW5 so that TXS DW0 is logged: [30:29] BW, [20] TXOP_TIMEOUT, [18] QUEUE_TIMEOUT, [17:16] RTS/ACK.

**P1 (reads only).** After `chsw` at cbw 0, cbw 1 with centre 3, cbw 2, cbw 4, cbw 4 with ctrl 1 and centre 3, and cbw 5, read:
- 0x820e2004 (expected: [3:2] = 0, 1, 2, 0, **1**, 0);
- 0x820b00a0;
- 0x83081000.

**P2 (reads only).** Inject one frame each of {6 Mbps BW20, 54 Mbps BW20, HT MCS0 BW20, HT MCS0 BW40 (P0)}. After each, read 0x8308a830–0x8308a85c (12 words; ContiTx copies these as "the last TX vector"). The diff gives the TXV BW, mode and length bit positions needed for T4.

**10 MHz PPDU**

**T1 (best lead): TX-vector BW 40 under ÷4 (the MT7996 recipe).**
- Run A, on a 40 MHz PHY (the firmware sets AGG class 1):
  - `chsw.py B 1 0 1 1 1 2350 3`;
  - then 0x83081000 = (read value & 0x0fffffff) | 0xd0000000, keeping [26:24] = 1;
  - 0x820b00a0 = 0x5;
  - `tx_bw=1`, `tx_rate=0x4b`, `TXC=1`;
  - send 40 frames.
  - Success = STF 1.6 µs and FFT 6.4 µs, with decodes at 10 MHz. Other outcomes:
    - quarter-rate non-HT duplicate: STF 3.2 µs, two 5 MHz lobes at ±2.5 MHz;
    - unchanged 5 MHz;
    - silence.
- Run B, on the narrow base with MAC class 40 from the switch itself:
  - `chsw.py B 1 0 4 1 1 2350 3` (cbw 4, ctrl ≠ centre gives SCO = 1, so AGG [3:2] = 1, TICKCR0 = 0x5, BN 0xd…);
  - plus [26:24] = 1 or 0x83080000 |= 0x1000 (needed to emit, per notes/07);
  - `tx_bw` = 1; then repeat with FIXED_BW cleared (`w6 &= ~0x7`), so the MAC picks the bandwidth from its class.
- Diagnostic: `tx_rate=0x80` with `tx_bw=1` (HT40 MCS0) at ÷4 should give a 10 MHz-wide signal with 78 kHz subcarriers. That confirms the generator uses the 40 MHz base for BW40 vectors.
- Risk: low. A failed narrow path gives at most a 40 MHz non-HT duplicate at 2330–2370 MHz, still inside the band.

**T2: TX-vector BW 80 under ÷8.**
- the 80 MHz PHY used for the notes/07 map (cbw 2 with a centre channel; BN_MODE [26:24] = 2, AGG [3:2] = 2);
- 0x83081000 = (v & 0x0fffffff) | 0xf0000000;
- TICKCR0 = 0xa; `tx_bw=2`; `TXC=1`.
- If the class base holds, 80 MHz ÷ 8 = a 10 MHz half-rate PPDU.
- Control run: `tx_bw=1` at 0xf on a 40 MHz PHY should give 5 MHz timing (STF 3.2 µs).

**T3: TX vector written directly into the test-signal generator** (after P2).
- Write 0x8308a008–0x8308a024 and 0x8308a040–0x8308a04c from a latched legacy TXV with only the BW bits changed.
- Write 0x83081000 = 0xd1000000 and 0x8308b000 = 0x44000000; start with 0x8308a004 = 0x80000000.
- Stop with FUN_968e82 `call 0x968e82`, then rigreset.
- This bypasses any MAC clamp of the TXD bandwidth.

**T4: AGG 0x820e2004 [3:2] = 1/2 alone** on the 20 MHz base with 0xd, `tx_bw` untouched. It rules the MAC class in or out separately from the TXD bandwidth. Low prior: the 40 MHz PHY run in notes/07 already had class 1 and gave 5 MHz.

**÷8 end of PPDU**

**E1. TICKCR0 grid at [31:28] = 0xf**, TICKCR0 ∈ {0x5, 0x9, 0xa, 0xb, 0xd, 0xe, 0xf, 0x11, 0x12, 0x13}; values outside the firmware range (W = 3, L ≥ 5) last.
- Measure the number of lost symbols (inner-CRC position or burst length from burstana.py/div8dec.py), the TXS flags, and the retry count with TXC = 15.
- Run the same grid at 0xd to see which values keep ÷4 working.

**E2. Length sweep at 0xf** (N_sym 6…200, 6 Mbps). A constant deficit in µs points to the preamble or fixed-time model; a deficit proportional to length points to a ratio error.

**E3. HT-mixed MCS0 at 0xf** (`tx_rate=0x80`). The 20 MHz preamble is 36 µs, so model (a) predicts a 144 µs deficit (4.5 symbols) instead of 80 µs.

**E4. Scale TMAC 0x820e4010 [24:16]/[8:0] and 0x820e400c [24:16] by ×2 and ×4 at 0xf** (restore the init values afterwards). Low prior.

**RX: 10 MHz-timed demodulator** (low prior; the IQ path already works)

**R1.**
- Base `chsw.py B 1 0 4 1 1 2350`, then 0x83081000 = 0xd5000000 (and 0xd4000000).
- **0x8308f064 [7:4] set to the same code** (5 or 4): MT7996's PHY selector writes RXFE_CTRL [7:4] together with the BW code. Repeat for 0x8309f064 if 2 RX chains are used.
- 0x83088014 [31:24] ∈ {0xea, 0x75, 0x3a}.
- Stimulus: X410 802.11p 10 MHz frames. Measure with `pdcnt.sh` (PD/MDRDY/SIG/RxOk) and count monitor decodes (`rxbw_fix=1`).
- Success = MDRDY > 0 and good-FCS 10 MHz frames.
- Expected to fail unless code 5 (or RXFE_CTRL [7:4]) adds the missing ÷2 after the sync.

## 7. Hazards
**Never touch**
- MCU reads of 0x8103xxxx hang the MCU. That address is MT7996's TICKCR0; MT7921's is 0x820b00a0.
- Host reads at or after 0xe0300000 reset the card.
- MCU page dumps of 0x80xxxxxx or of unmapped memory (for example 0x00080000) hang the MCU.
- Never write CONN AFE 0x7c003xxx (crystal clock mode, the clock tree's root).
- Never write 0x88009000 / 0x88009004 [23:0] / 0x88009008.
- WFSYS_ON other than TICKCR0: 0x820b0000–0x820b001c (LPCR own/sleep), 0x820b0024–0x820b0038, 0x820b00a8 (PLL/power sequence, FUN_9397ea).
- A-die RF 0xbc, 0x21c, 0xcc4, 0xd20 (power, PLL, VCO calibration).
- 0x83080014 [17:0] holds PHY blocks in reset.

**Recoverable effects**
- 0x83081000:
  - [27:24] ≥ 8 kills RX;
  - [31] = 0 causes an abort/retry storm (use TXC = 1);
  - 0x2, 0x7 and 0xe at [31:28] are silent.
  - Recover with chsw.
- TICKCR0 with WLAN_CLK_MODE = 0 under ÷4/÷8 keys the PA continuously. Values outside the firmware range are untested. Have `rigrecover.sh` ready.
- TxTone and ContiTx stop routines leave forces behind (0x83080004/08, 0x8300d008, 0x8300c000). Run rigreset.

**RF and licence**
- Transmit only in 2300–2400 MHz. A 40 or 80 MHz PHY at 2350 MHz spans 2330–2370 or 2310–2390 MHz if the narrow path falls back to full rate.
