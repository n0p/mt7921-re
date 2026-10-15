# TX capabilities: IQ/waveform transmit and a 10 MHz (÷2) PPDU (static analysis, 2026-10-15)

Question: can the MT7921 (MT7961 + MT7972) transmit a stored or arbitrary baseband waveform, or a true half-rate
(10 MHz, 802.11p / 11bd) OFDM PPDU? Today only the hardware ÷4 is known: 0x83081000 bit 30, a valid 5 MHz PPDU.
Bits 30+29 give ÷8, which has never decoded.

Sources:
- work/decomp/all.c (RAM 2026), rom_all.c, all2022.c;
- work/dis/ram_0_00915000.txt (`dis`);
- work/crmap;
- snapshots work/phydump/wide_*.bin;
- the MT7996 reference work/ext/w1700k/src/include/hw/mt7996/*.h;
- BirriaSDR.

This was a static pass only; the rig was not touched.

## Summary
- **No TX playback path exists.** Nothing reads SRAM samples into the TX chain.
  - The ICAP block (0x80021000, crmap module 0x5a) is a capture-only writer. MT7996 icap.h documents
    SRC_SEL 0..7 as capture sources only.
  - No image (RAM, DLM, patch or ROM strings) mentions playback, DAC, waveform, RBIST TX or PRBS.
  - The MT7996 DFE "MemDPD" engine writes into a DSP's memory, and MT7921 has no DSP.
- **Three on-chip TX sources exist besides normal PPDUs:**
  1. the band tone generator (0x8308fc00..fc20);
  2. the band test-signal generator (0x8308a004.., the ATE continuous-TX source);
  3. the DPD training engine (0x830ae000..).

  Only the tone generator is useful. It can emit a DC (LO) carrier with a programmable 12-bit I/Q value (fc08), or
  tones at fixed bins. **If fc08 can be rewritten live, the MCU becomes a slow IQ DAC.** That would allow
  narrowband FSK, PSK or chirp transmission for a long-range mode at the MCU write rate (MS/s order, to be
  measured).
- **No ÷2 TX control was found in the firmware or the MT7996 reference.** However, the firmware *intends* its
  0x5 code to mean 10 MHz, and the hardware gives ÷4 instead (see "Narrow clock" below). Two [30:28] codes,
  **0x4 and 0x6, have never been tried on TX** and are the only cheap ÷2 candidates left.
- A 10 MHz 802.11p PPDU cannot be built any other way:
  - HT/VHT 40 at ÷4 has 128 tones at 78 kHz spacing, not 64 tones at 156 kHz;
  - HE uses a 256-point FFT;
  - 11bd NGV-SIG cannot be generated.

  So without a hardware ÷2, the card's best 11p-family TX is a 5 MHz quarter-rate PPDU, which ath9k can receive
  in its 5 MHz mode.

## 1. Band tone generator (TxTone)
RAM `FUN_9691d2` (all.c:91709; dis 129982..0x96951a) prints
`TxTone: AndIdx=%d,Type=%d,FREQ=%d,OfsI=%d,OfsQ=%d,Band=%d`.
- Arguments: a0 = chain mask (bit 0 = chain 0, bit 1 = chain 1), a1 = Type, a2 = FREQ, a3 unused.
  OfsI (a4), OfsQ (a5) and Band (stack) are only printed.
- ROM twin: `FUN_83f074` (rom_all.c:52781), with 4 chains.
- ATE path: TEST_CTRL command 15 (SINGLE_TONE, the same id as MT7996 `UNI_TM_CMD_SINGLE_TONE`).
  The call chain is dispatcher 0x9308e2 → 0x930606 → struct stub 0x964d30 → wrapper 0x943fae.
  The wrapper struct is: +0 mask, +1 Type, +2 FREQ, +3, +4 OfsI, +8 OfsQ, +12 Band.

Register writes (path 1 twins at +0x10000), in order:

| register | value | role |
|---|---|---|
| 0x8300c000 / 0x8301c000 | 0x00274027 (stock 0xc0c80806) | per-chain TSSI control |
| 0x830ad4d0 / 0x830ad4d8 | mask<<24 \| 0x101f1f01 / 0x101f1f00 (stock 0x01010101) | TX lane/source mux [inferred] |
| 0x8300c508 / 0x8301c508 | \|= 0x80008000 | [31] enables a tone power override; [23:16] = power (setter FUN_96951c/969534, all.c:91813) |
| **0x8308fc00** | [31] on, [30] GO, [23] = 1, [17:16] = 3, **[15:8] bin (mod 0x60)**, Type bits [25:24] / [19] | tone control |
| **0x8308fc08** | FREQ 0: **0x0c000c00** (I = Q = -1024, 12-bit two's complement); other FREQ: 0 | DC I/Q value [inferred from the forced OfsI = OfsQ = -1024 print] |
| 0x8308fc0c | 0x10122 (FREQ 0), 0x3120 (±1, ±2), 0x3220 (±4, ±8), 0x3310 (FREQ 10); **[0] load pulse** (set, then cleared after fc00/fc08) | config + latch |
| 0x8308fc04, 0x830ae00c | FREQ 10 only: 0x90800200, 0x80600240 (\|0x40../0xc0.. by Type) | DPD engine target (MT7996 DPD_TARGET) |
| 0x8300d008 / 0x8301d008 | 0x1dd0 for a chain in the mask, otherwise 0x1d90 (stock 0x1c10) | chain TX force ([7:6]) |
| 0x83080004/08, 0x83090004/08 | 0xffffff (stock 0x3800 / 0) | band clock forces |
| 0x8308fc20 / 0x8309fc20 | 1 | tone path enable |
| fc0c \|= 1, then &= ~1; then fc00 \|= 1<<30 | | latch and GO |

FREQ selects the bin: 0 = DC; 1..4 = +1, +2, +4, +8; 5..8 = -1, -2, -4, -8 (0x5f, 0x5e, 0x5c, 0x58).
The bin spacing is unknown. MT7996 infers 5 MHz per bin at its 480 Msps class, which would make bins too coarse
for FSK unless the generator follows the narrow ÷4/÷8 clock.

Stop is `FUN_969126` (all.c:91675). **It does not restore stock values.** It leaves:
- 0x83080004/08 = 0 (stock 0x3800);
- 0x8300d008 = 0x1d00 (stock 0x1c10);
- 0x8300c000 = 0x802741a0 (stock 0xc0c80806).

Run rigreset after tone tests.

MT7996 equivalent: BN +0xfc00/fc0c "band tone generator" (phy_cal.h:2077-2099). Its TX flatness calibration steps
one tone across the channel. Confidence: high for the register sequence, medium for the fc08 = DC I/Q reading.

## 2. Band test-signal generator (ContiTx)
- RAM `FUN_968f2e` (all.c:91545, dis 0x968f2e..0x969124) prints `ContiTx DBDC,CtrCh,CentCh,BW,Mask,Rate,Mode`.
- ATE path: commands 10 (CW) and 4/6 (dispatcher 0x9308e2 → 0x930646 → stub 0x964d26 → wrapper 0x943f76).
- It copies 12 words, **0x8308a830..0x8308a85c → 0x8308a008..0x8308a024 and 0x8308a040..0x8308a04c**. The
  0x8308a830 block reads 0 in idle snapshots and nothing writes it, so it is probably a hardware latch of the last
  TX vector.
- It sets 0x83081000 = 0x80000000 | code<<24, **clearing [30:28]**.
- It writes 0x8308b000 = 0x44000000, 0x8308b004 = 0x30000000, 0x830ac004 = 0x16011400, and
  0x83080004/08 = 0x38.
- It starts with **0x8308a004 = 0x80000000**. Stop is `FUN_968e82` (all.c:91520): 0x8308a004 = 0 and
  RST_CTRL = 0x38048.
- MT7996 calls this block the "band test-signal generator" (phy_cal.h:2192). Its config word 0 [10:8] holds a
  bandwidth code 0..4 (20..320 MHz) and has no narrow class.
- Use: continuous PPDUs with a pseudo-random payload. It is not IQ playback.

## 3. DPD training engine
0x830ae000.. is MT7996 WF_COMM +0xe000 (phy_cal.h:2266-2310): a run bit, a correlation readout and an amplitude
target. It is calibration-only, with no sample memory exposed. Not usable for modulation. Confidence: medium.

## 4. Narrow clock and ÷2
- The firmware writes [30:28] in `FUN_93dbd4` (all.c:43124): 0x5 for enum 6 ("10 MHz") and 0x7 for enum 5
  ("5 MHz"). It forces [28] = 1 even at 2.4 GHz.
- The same channel set halves 0x83086010 (MT7996 BN_6188) for "10 MHz" and quarters it for "5 MHz". MT7996 does the
  same for its 10/5 MHz classes (phy_chan.h:191-205), and TICKCR0 gets 0x5/0xa.
- So the firmware believes 0x5 is ÷2, but the hardware gives **TX ÷4** at 0x5 (0xd with bit 31) and ÷8 at 0x7.
- On RX the same codes give a front end at ÷2 and ÷4.
- Codes 0xa/0xb (bit 29 without bit 30) leave TX at 20 MHz.
- **Codes 0xc (bit 30 without 28) and 0xe (30+29 without 28) were never measured on TX.** If [28] is the extra
  ÷2 on the TX side, 0xc gives ÷2.
- The ROM BW writer `FUN_840fd8` (dis 0x840fd8) writes [27:24] from a {0,1,2} table and [30:28] = mode. Its
  callers pass only the band (0/1), so no other code exists in ROM.
- 0x83007254 [5:4] (TX-DAC) and [7:6] (RX-ADC) are clock *enables*. They select no frequency (MT7996
  phy.h:394-403).
- No TXFE/TXTD/TXFD rate field was found. RST_CTRL bits 15-17 name these blocks, but no register map exists.
  Earlier tests copying 40 MHz TX front-end values (0x8308f0a4, 0x8308f130, 0x830860b4, 0x830a0000/4/50) on top of
  ÷4 still gave 5 MHz.
- The CONN AFE clock-mode fields 0x7c003004 [1:0], [7:6], [17:16] are set per crystal by ROM `FUN_80ab32`
  (rom_all.c:9752). They feed every clock. HAZARD: do not touch.

## 5. Other narrowband TX handles
- The LO synthesizer words RF 0xc68/0xca8 (notes/04) have an integer [29:20] and an 18-bit fraction [19:2],
  about **114 Hz per LSB** at 2.4 GHz (f = N × 30 MHz). Stepping the fraction during a carrier would give
  direct-LO FSK. It is limited by the A-die SPI write latency and PLL settling (both unmeasured).
- Tone power is set by 0x8300c508 [31] + [23:16]. TX gain is set by the per-chain manual TX gain (MT7996
  RFINTF +0x250 analogue, unverified here).

## Experiments (ranked, safest first)
The observer is the X410 on RX1 (the in-room antenna; capture.py now defaults to it), or card A's IQ stream. The
transmitting card sits at 2350 MHz unless noted.

1. **÷2 via BN_MODE [30:28].**
   - Base: `chsw <P> 11 0 0; chsw <P> 1 0 4 1 1 2350`. The firmware then sets 0xd and TICKCR0 = 0x5.
   - Write `0x83081000 = (v & 0x8fffffff) | (c << 28)` for c = 4 (expect 0xc…) and c = 6 (0xe…), keeping bit 31
     as read.
   - Inject `inject.py mon_x 200 20 12 64`.
   - Capture 25 Msps and run `decmulti.py … 25 20,10,5,2.5`.
   - Success: decodes at 10 MHz.
   - Repeat at 5180 (base 0x9) and with TICKCR0 = 0x5 / 0xa.
   - Optionally run `icapcode.sh` taps 4/12 for the RX rate under 0xc/0xe.
   - Restore with chsw. Risk: low (the firmware writes this word itself).
2. **TICKCR0 alone at 20 MHz.**
   - cbw 0 (0x8…), then 0x820b00a0 = 0x5 / 0x1 / 0x4. Inject and decode at all widths.
   - Rules out a MAC/WF clock ÷2. MT7996's only narrow knob besides BN_6188 is TICKCR0. Risk: low.
3. **Tone generator characterisation.**
   - `fwdbg call 0x9691d2 1 0 F 0` for F = 0..8 (chain 0).
   - Capture 25 Msps: tone offsets → bin spacing.
   - Repeat with 0x83081000 bit 30, then bits 30+29 set after the call: does the spacing scale /4, /8?
   - Stop with `call 0x969126`, then **rigreset** (it leaves 0x83080004/08 = 0, 0x8300d008 = 0x1d00).
   - Risk: CW on air (licensed band), otherwise low.
4. **fc08 as a live DC I/Q DAC.**
   - With F = 0 running, write fc08 = 0x04000400, then 0x02000200 and 0x0c000400 (12-bit I in [11:0], Q in
     [27:16]). Look for 180° and 90° phase and -6 dB steps within one capture.
   - If nothing changes, add the latch: fc0c |= 1, then &= ~1.
   - Then a firmware hook (fw/src) that loops over an N-entry sine table writing fc08 (± latch), paced by
     0x89020068. The tone offset gives the real update rate and the spur level.
   - Success at ≥ 0.5 MS/s → narrowband GFSK/DBPSK/chirp modulator in a hook, fed from DLM.
   - Risk: low-medium (MCU busy loop; keep it bounded).
5. **Bin-hop FSK.**
   - With a tone running, rewrite fc00 [15:8] (keep [31:30], [23], [17:16]).
   - Check arbitrary bins (0x03, 0x10, 0x5d) and phase continuity.
   - Useful only if the spacing is small (see 3).
6. **LO-fraction FSK.**
   - During an F = 0 carrier, write RF 0xc68/0xca8 with ±N LSB on the fraction (keep LOAD [31]) via
     `fwdbg call 0x83a66c reg val path`.
   - Measure the shift and settle time.
   - Read the current word first and restore it. Never touch the VCO calibration registers (0xcc4, 0xd20, 0xce0).
     Risk: medium (loss of lock → rigreset).
7. **ContiTx under narrow bits.** Lowest priority: modulated continuous TX for occupied-bandwidth checks without
   MAC completion.
   - Send one frame, then call wrapper 0x943f76 with a DLM struct: +0 CtrCh, +1 CentCh, +2 BW, +3 Mask,
     +4 u16 Rate, +6 DBDC, +7 Mode.
   - Set bit 30 (or the 0xc / 0xe code) afterwards.
   - Stop with `call 0x968e82 0 0`, then rigreset.

Avoid:
- MCU reads of unmapped addresses (0x00080000 hung the MCU);
- 0x8103xxxx;
- CONN AFE 0x7c003xxx;
- RF 0xcc4/0xd20 (VCO calibration);
- leaving the tone or ContiTx running across a rigreset.
