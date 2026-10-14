# Tuning MT7961 to 2300–2400 MHz (13 cm amateur band): static analysis and patch

Firmware: WIFI_RAM_CODE_MT7961_1 build 20260224110949 (live addresses) and the boot ROM dump (work/rom/).
This was done offline only and has **not been tested on hardware yet**.

## Summary
- The firmware already has a **frequency override**: CHANNEL_SWITCH's `outband_freq` (MHz) replaces the
  channel-derived centre frequency in the RF context that the ROM synthesizer routine uses. 2.4 GHz synthesizer
  programming is pure arithmetic on the frequency in kHz. There is no channel-indexed table and no range check on it.
- **No firmware patch is needed on the CHANNEL_SWITCH path.** Send EXT CHANNEL_SWITCH with a normal 2.4 GHz channel
  (e.g. ch 1) and `outband_freq = 2350`.
- The UNI SNIFFER path (monitor mode in the user's stack) hard-codes outband = 0. The opt-in build patch
  `make OUTBAND=1` takes it from the sniffer TLV pad bytes instead (u16 MHz at TLV +14).
- The physical limit is the VCO. It runs at 2×f, so 4600–4800 MHz for 2300–2400 MHz, against 4824–4968 MHz for
  channels 1–14. w1700k-sdr reports an A-die 2.4 GHz lock range of 2.081–2.748 GHz (MT7976), so 2.3 GHz is
  plausible but unverified.

## Call chain (channel → synthesizer)
1. Entry points
   - EXT CHANNEL_SWITCH / SET_RX_PATH (ext 0x08 / 0x4e) → `0x964494`. Payload (driver `mt7921_mcu_set_chan_info`,
     handler offsets = payload + 0x40): +0 control_ch, +1 center_ch, +2 bw, +10 channel_band,
     **+12 outband_freq (LE32, MHz)** → `uVar14 = *(buf+0x4c)` → passed as rlmPhySetChannel arg 12.
     Base frequency: `channel_band 0 && control_ch < 15` → 2407000 kHz (5 GHz: 5000000, band 2: 5950000, band 3: 4000000).
   - UNI SNIFFER config TLV (tag 1) → `0x923ca0`: switch on ch_band (1 → 2407000 kHz), then
     `jal rlmPhySetChannel` at **0x923d12** with stack args from 0x923cf4..0x923d0a. Arg 12 is hard-coded:
     `movi55 a0,0; swi37.sp a0,[+0x14]` (0x923d00/0x923d02). s1 = TLV pointer.
2. `rlmPhySetChannel` **0x963f6c** → `FUN_942672(…, param_12 = outband, …, param_14 = channel struct)`.
   - centre kHz = base + ch × 5000. Special cases: ch 14 on 2.4 G → 2484000; ch > 0xb6 → 4000000 + ch × 5000.
   - struct: [0] base, [1] centre kHz, [2] centre2, **[3] = outband × 1000 (kHz)**, +0x10 primary idx, +0x11 bw enum.
   - caches: `FUN_94251a(0xe, 0, outband*1000)`, (0) base, (1) ch, (3) bw, …
3. `FUN_941ea4` → `FUN_93bc9c` (PHY/RF bring-up) → `FUN_93b96a(ctx=0x02045e80, 2, …)`:
   `ctx+0x0c = struct[3]` (outband kHz) **if non-zero**, otherwise struct[1]. This holds for bw enums other than
   3/4 (160 / 80+80), which use [1]/[2]. `ctx[0] = FUN_93b866(f)` = RF sub-band: **0 for any f < 4850 MHz**
   (2.4 GHz family), 1..0x12 for 4.9–7 GHz.
4. ROM RF/cal API `*0x008229c4` = **0x83c558** → `*0x0201456c` (runtime-installed op table, ops 1/3/8/0xb/0x10/…).
   The context reaches the ROM synthesizer routine **FUN_84502e**:
   - writes RF 0x180 (path/mode) and **RF 0x184 = frequency in kHz** (useful readback),
   - 2.4 GHz (`ctx[0] == 0`): f doubled (VCO = 2f), reference constant 60, fractional-N:
     `N = (f/500*2*80) / 9600` (2412 MHz → 80.4; 2350 → 78.3), integer part in bits [29:20] of RF 0xc68/0xca8
     (10 bits), fraction ×2^18 rounded. The 5 GHz sub-band table is not used for 2.4 GHz,
   - VCO band selection is a **hardware auto-calibration**: RF 0xcc4 bits 30/29, 0xce0 = 0x95856473, then
     ROM poll of RF 0xd7c bit 21, then 0xd20/0xcc4 restore. No software VCO band table.
   - RF access helpers: read 0x83a662(reg, path), write 0x83a66c(reg, val, path).

## Gating checks
| Check | Where | Effect for 2.3 GHz via outband |
|---|---|---|
| channel validity | FUN_941d94 | only ch ≠ 0 / ≠ 0xff plus band checks; no upper bound. Use a normal ch (1). |
| "same channel → skip" | FUN_942544 | compares base, ch, table-bw, bw, switch_reason, **not outband**. Changing only the outband frequency does not retune, so hop to another channel first. |
| ch > 0xb6 → 4 GHz base | FUN_942672 | irrelevant if a normal channel number is used. |
| RF sub-band classifier | FUN_93b866 | f < 4850 MHz → 0 (2.4 G). OK. |
| channel index `(f − 2407000)/5000` (unsigned) | FUN_93bc9c 0x93bee2..0x93beea, FUN_93b6c8 | wraps for f < 2407 MHz (2350 → 0x66). It is only stored in the cal record and compared against spur-channel lists (FUN_93a9da), never used as an array index. Harmless: worst case a spur-avoidance tweak is skipped. |
| SHA monitor (0x915000..0x955000) | ROM patch | covers 0x923d12, but the hash is taken after the (patched) image is loaded, so it is consistent. |

## Patch for the sniffer path (opt-in, `cd fw && make OUTBAND=1`)
- `fw/asm/sniffer_outband_tramp.S`, linked at **0x96dc00**. That is the 12 zero bytes of region 0's trailer pad,
  just before the PDA word, within ±16 MB `jal` range of the caller:
  ```
  96dc00: 02 f3 80 07   lhi  $r15, [$r7 + 14]     ; u16 MHz from TLV pad (+14) (s1 = TLV pointer)
  96dc04: 14 ff 80 05   swi  $r15, [$sp + 0x14]   ; rlmPhySetChannel arg 12 (outband)
  96dc08: 48 ff b1 b2   j    0x963f6c             ; tail-jump, lp unchanged
  ```
- `fw/asm/sniffer_outband_call.S` at **0x923d12**: `4902012d` (jal 0x963f6c) → `49024f77` (jal 0x96dc00).
- `fw/patches/outband.def` lists both as `bin` patches (new hookargs.py kind). The default `make` output is
  unchanged (md5 ff224b8d…). `make OUTBAND=1 OUT=out/WIFI_RAM_CODE_MT7961_1.outband.bin` builds and verifies
  (md5 fe840b96…).
- Behaviour-neutral with the stock driver (pad = 0 → arg 12 = 0, as before).
- Driver side (not done): set the sniffer `config_tlv.pad[1..2]` = MHz (e.g. module param `outband_mhz`), and make
  sure the channel actually changes (FUN_942544) so the firmware re-runs the RF bring-up.
- tools/mtfw_patch.py now allows byte patches up to the region's last word (the trailer pad is plain padding).

## No-patch path (try first)
- EXT CHANNEL_SWITCH through mt7921dbg `raw`: cmd id `0x000008ed` (`MCU_EXT_CMD(CHANNEL_SWITCH)`), followed by the
  80-byte `mt7921_mcu_set_chan_info` request: control_ch 1, center_ch 1, bw 0, tx_streams 1, rx_streams 1,
  switch_reason 0, band_idx 0, channel_band 0, **outband_freq = MHz at offset 12 (LE32)**, rest 0.
  Send it after the driver's own sniffer/RLM config, and after a hop to another channel.
- Checks through fwdbg on the instrumented image:
  - `call 0x0094252e 0xe 0` → cached outband kHz,
  - `call 0x0083a662 0x184 0` → RF 0x184 (frequency in kHz written by the ROM synth routine),
  - `call 0x0083a662 0xd7c 0` → VCO calibration status (bit 21).

## Risks
- VCO may not lock at 4.6–4.8 GHz: auto-cal fails, and the LO lands elsewhere or the TX is spurious. Check the
  RF 0xd7c status and the X410 centre-of-gravity.
- The front-end (FEM / 2.4 GHz band-pass filter on the dongle) attenuates below 2400 MHz. Expect reduced TX
  power and RX sensitivity. Measure it.
- Per-channel TX power / calibration tables follow the nominal control channel (e.g. ch 1), not the real frequency.
- **Regulatory:** 2300–2400 MHz is outside the ISM band. TX there needs an amateur licence (and an ID). For
  bring-up use 2400–2412 MHz first (still ISM), then low power or cabled with attenuators.

## Hardware test plan (X410 on lorien, tools/sdr/)
1. In-band sanity (legal everywhere): card A monitor on ch 1 (NOHT), inject 6 Mbps frames, `psd.py 2412` → CoG
   ≈ 0. Send CHANNEL_SWITCH with outband 2402 (after a hop). `psd.py 2402` must show the burst centred
   (CoG ≈ 0 kHz), and `psd.py 2412` must show it at −10 MHz. Read RF 0x184 = 2402000.
2. Step outband 2400 → 2380 → 2350 → 2320 → 2300 (licence / attenuators). For each step: CoG, peak power
   (front-end roll-off), OBW, RF 0xd7c bit 21, and spurs at ±f_ref.
3. RX: X410 `txframes.py <f> 20 300 5 6 40` at 2402 / 2350, card B with the same outband, count good FCS.
4. Sniffer path: `make OUTBAND=1` image plus a driver (or `raw` sniffer TLV) with pad = MHz; repeat 1–3.
5. Optional sweep for the VCO limit: decrease until RF 0xd7c reports cal failure or the CoG stops tracking.

## Hardware validation (2026-10-14, X410 + cards A/B at lorien; user confirmed amateur licence for 2300–2400 MHz)
Tool: tools/guest/outband.py sends EXT CHANNEL_SWITCH (raw cmd 0x08ed, 76-byte set_chan_info, outband MHz at +12)
through mt7921dbg and reads back the cached outband, RF 0x184 and RF 0xd7c through fwdbg. Always hop first
(`outband.py <phy> 0 11`) because the firmware skips a "same channel" switch. Stock 2026 firmware, no patch.
- Synth readback: RF 0x184 = requested kHz for every value from 2412 down to 2100 MHz. RF 0xd7c stays 0x50200000
  (not a usable lock indicator).
- RX (X410 802.11a 20 MHz frames, 6 Mbps, gain 40 → card B outband):
  2402 245/300 · 2400 183/200 (−54 dBm) · 2380 160/200 (−53) · 2350 167/200 (−52) · 2320 162/200 (−54)
  · 2310 181/200 · 2300 197/200 · 2250 192/200 (−65) · 2200 172/200 (−60). B tuned 2402 + X410 2412 → 0.
- TX (card A outband 2350, 5 dBm): X410 sees the emission centred (CoG −176 kHz) and ~14.6 MHz wide. Peak is
  ~12 dB lower than in-band at the same setting, so expect front-end attenuation on TX below 2.4 GHz (to quantify).
- Card to card (A and B both outband 2350): 99/100 frames, −24 dBm (cards close together).
- Injected frames go out as 1 Mbps DSSS regardless of the radiotap rate (known issue of this stack). The monitor
  shows the nominal channel frequency (2412), not the outband one.
- Next: driver integration. Make outband part of the channel config the driver sends (sniffer/monitor path and
  OCB), so it survives rescans/resets. The firmware `make OUTBAND=1` sniffer patch or a driver-side CHANNEL_SWITCH
  after the sniffer config are both options.
