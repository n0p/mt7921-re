# Instrumented firmware build and the narrowband investigation

## make-based instrumented firmware (fw/)
- `fw/Makefile` builds hooks with `nds32le-elf-gcc` (Arch: nds32le-elf-binutils/gcc/newlib).
  Flags: `-march=v3 -mreduced-regs -mcmodel=large -mno-fp-as-gp`. The large model allows far calls from
  0xe027xxxx into firmware at 0x9xxxxx. The firmware keeps fp == gp, so fp-as-gp must stay off.
- Hooks link at HOOK_BASE=0xe027cb00 (free SRAM after region 3). `tools/mtfw_patch.py blob:` grows region 3 to
  hold them. **Region lengths must be multiples of 16**: a 4-byte-aligned length makes FW_START_REQ time out and
  leaves the chip stuck until the driver resets it.
- `fw/hooks.def` maps table slots to hook symbols (`ptr <download addr> <symbol>`). `tools/hookargs.py` resolves them.
- `make install` / `make restore` run tools/guest/fwswap.sh in the guest.

### Debug command (fw/src/dbgcmd.c), verified on hardware 2026-10-14
- Replaces CE cid 0xcb (stock stub `return 0`). ping/read/write/call, up to 64 words per command.
- Host side: `tools/guest/mt7921dbg/` out-of-tree module. It needs no changes to the user's mt76 stack: it finds
  the mt76_dev through the `regval` debugfs i_private and calls `mt76_mcu_send_and_get_msg`. The modversions CRC
  is taken from mt7921-common.ko's __versions (0xccc0a4fe). User tool: `tools/guest/fwdbg.py`, including `dump`.
- The reply skb arrives with the rxd already stripped, so the payload is at offset 0.
- **ILM is not writable at runtime, not even from the MCU.** The write faulted the firmware and the driver reset the
  device. ILM changes must therefore be made at build time. DLM, region 3 and MMIO are writable at runtime.

## PHY/RF register differential, fw 2026, monitor 5180 MHz (work/phydump/, tools/phydiff.py)
- 57 PHY/RF 4 KB pages referenced by firmware/ROM. 10 MHz and 5 MHz configurations are almost identical.
- (Retracted) `0x830003ec[9:8]` is NOT a BW field. It varies between runs at the same width.
- The large `0x830076xx/78xx/0x8300axxx` (WF0) and `0x830176xx/0x8301axxx` (WF1) differences are calibration
  results. ROM `0x83e2e0` clears them.
- Radiated output stays ~17–18 MHz wide on 2026 (user measurement). Hypothesis: the TX baseband sample clock is
  never divided. Proper 10 MHz 802.11p needs half-rate OFDM timing (8 µs symbols, 156.25 kHz spacing).
- Next: the same snapshots under fw2022, which really narrows TX, then diff 2022@10 against 2026@10.

## fw2022 vs fw2026 register differential (2026-10-14)
- Both firmwares were measured on card A, monitor 5180 MHz, 20/10/10/5 MHz. 2022 dumps were taken via host
  regval (tools/guest/snap2022.sh).
- 2022 reprograms clock/front-end registers per width. **2026 leaves them at their 20 MHz values.** Key registers:
  `0x83086010` 0x013c013c → 0x009e009e (10) → 0x004f004f (5), which halves exactly;
  `0x83081000` 0x9… → 0xd… (10) → 0xf… (5). Also 0x830882ac, 0x8308829c, 0x83088394, 0x83080000, 0x8300d06c, …
- Profiles (2022 narrow value where 2026 keeps the 20 MHz value, excluding cal/volatile registers):
  work/phydump/narrow_profiles.json (10 MHz: 35 regs, 5 MHz: 19 regs).
- Live two-card test (tools/guest/narrow_test.sh, 5180 MHz, 5 dBm, 300 frames, 6 Mbps):
  base 300/300 · profile on TX only 0/300 · profile on both 0/300 · restored 300/300.
  Writes persist (checked after 5 s). The profile changes the TX, but the profiled RX still decodes ambient 20 MHz
  traffic, so the RX side is not narrowed by these registers alone.
- Still missing: pages outside 0x83xxxxxx (top/clock 0x80/0x81, TMAC 0x820e4000) and the A-die (MT7972) RF
  registers (indirect access, EXT RF_REG_ACCESS 0x02).
- SDR check pending: tools/guest/narrow_tx.sh runs OFF → STOCK → PROFILE → OFF phases.

## Root cause of "accepts 5/10 but emits 20" (2026-10-14, verified on hardware)
- With the X410 on lorien (tools/sdr/psd.py: burst-gated PSD, OBW, timeline), per-register bisection shows that only
  `0x83081000` changes the emission. Bits 30:28 set the clock mode: 20 MHz 0x9/0x8 (5G/2.4G), 10 MHz 0xd, 5 MHz 0xf.
- `0x83081000` is programmed by FUN_93dbd4 (2026) / FUN_93892c (2022) from the channel struct's bw byte (+0x11).
  The routine handles 5 MHz (enum 5 → 0x7…) and 10 MHz (enum 6 → 0x5…) natively, **in the 2026 firmware too**.
- Monitor mode in the user's stack sends UNI SNIFFER config (bw 4/5 = 5/10 MHz, passed straight into rlmPhySetChannel)
  and **then** the BugB UNI BSS_INFO RLM TLV (CMD_CBW 4). The RLM handler maps that to 20 MHz and overwrites it.
  Firmware cached bw (call `FUN_94252e(3,0)` via fwdbg) = 0.
- Re-sending only the sniffer config last (tools/guest/nbset.sh, through mt7921dbg `raw`) gives cached bw 5/4, clock
  0xd…/0xf…, and `0x83086010` halved (2.4G: 0x2a0 → 0x150). **Native narrow mode works without any register hack.**
- Native-mode on-air result (2437 MHz, X410): the emission narrows (−20 dB OBW 14 → 3.8 MHz @10, 4.6 @5) but it is
  continuous (no frame bursts), and card B receives 0/300 in every narrow combination. Host TX queues drain normally.
  Remaining gap: the TX/RX datapath around the clock switch (MAC timing / PLCP, A-die LPF/DAC, RA BW≤3 assert).
- Driver-side fix to carry into the user's patch set: in monitor 5/10 MHz, send the RLM TLV *before* the sniffer
  config (or skip it), so the sniffer bw is the last one applied.

## Native narrow mode: TX behaviour (2026-10-14, X410)
- Without injection, native 10/5 MHz mode emits nothing. The narrow emission is triggered by TX.
- 7.68 Msps capture at 2437 MHz, frames every 50 ms: native 10 MHz shows one unbroken emission of ~455 ms after the
  first frame. Native 5 MHz emits for the whole capture. **The first frame keys the TX and it never unkeys**, i.e. TX
  completion never happens. The −20 dB OBW of that emission is ~3.7 MHz (10) / ~4.6 MHz (5).
- Wide register diff (92 safe pages incl. MAC): TMAC timing (0x820e4000: SIFS/slot/PLCP/CCA timeouts) does NOT change
  in narrow mode. Changed: 0x820cd1xx (MDP), 0x820d423x, 0x820e336c (ARB), RMAC 0x820e51xx (the latter likely from
  the sniffer replay), plus the PHY clock registers.
- Next: why TX never completes. Candidates: PHY TX-end signalling at the divided clock, the TXD/TX-vector BW field
  (2 bits, 20/40/80/160 only; RA assert `TxPhyCfg->BW <= 3`), and the A-die TX LPF/DAC rate (RF builder).

## Rig incidents and gotchas
- Reading 0x80xxxxxx pages through the MCU debug command faults the MCU. The resulting driver-reset storm plus
  concurrent debug commands corrupted an mt76 list and wedged the guest kernel (RCU stall). Fixed with QEMU
  `system_reset`. Safe snapshot pages: work/phydump/wide_pages_safe.txt (0x8102, 0x820c–0x820f, 0x83).
- After a guest reboot the mt7921 stack must be loaded with tools/guest/loadstack.sh (/root/stackok, in order).
  /lib/modules has mixed builds that fail with "disagrees about version of symbol".
- Guest /bin/sh is dash (no `\x` in printf). Local shell is zsh (no word splitting of `$VAR` commands).
- Keep X410 captures small on lorien (it is a shared host): ≤ 1 s at 7.68 Msps or ≤ 0.5 s at 30.72 Msps.
- Kernel/driver builds go on lorien (user's instruction 2026-10-14), not on the laptop.

## Reference 802.11a/p transmitter (X410) and narrow RX tests (2026-10-14)
- tools/sdr/ofdm80211.py: numpy 802.11a/p PPDU generator (all legacy rates). tools/sdr/txframes.py plays it on the
  X410 (FPGA X4_200: MCR 250 MHz only, so 10/5 Msps exact and 20 MHz via exact 5/4 FFT resampling at 25 Msps;
  send in one call or it underflows). Validated: card B decodes X410 20 MHz frames 299/300 with good FCS at gain 40.
- tools/sdr/rxmatrix.sh: B mode set and verified (fw cached bw, clk0/clk1, antenna mask), X410 frames, B counts.
- Native narrow mode programs **only RF path 0** (0x8308xxxx). Path 1 (0x8309xxxx: 0x83091000, 0x8309829c,
  0x83098394, 0x83092130, 0x83096010) keeps its 20 MHz values.
- Results with B forced 1x1 (iw set antenna 1 1), 2437 MHz:
  | B mode | X410 | B good/300 |
  | 20 | 20 MHz | 255 |
  | native 10 (clk0 0xd…) | 20 MHz | 299  (RX still runs at 20 MHz) |
  | native 10 | 10 MHz | 0 |
  | native 5 (clk0 0xf…) | 5 MHz | 0 (receiver produces noise "frames": random rates/addresses, −75 dBm) |
  | native 5 | 20 MHz | 0 |
- Conclusion so far: the firmware's native 5/10 MHz mode is incomplete. It switches the TX-side clock of path 0
  (garbage TX, ~225× stretched bursts, no OFDM CP structure). In 10 MHz it leaves RX at 20 MHz, and in 5 MHz it
  breaks RX. A working narrowband PHY needs the RX decimation/clock setup, which is still unidentified.

## Native ÷4 TX is a correct 5 MHz 802.11 PPDU; the receiver is the blocker (2026-10-14, evening)
### TX
- CHANNEL_SWITCH cbw 4 (driver CMD_CBW_10MHZ → firmware internal bw 5 → `0x83081000` = 0xd…, bit 30) on card A at
  5180 MHz, path 0. The X410 captured at exactly 5 Msps and `tools/sdr/rx80211.py` decoded the frames
  "MT7921-NBW-…" from SA 02:00:00:00:00:01 with **valid FCS** (`tools/sdr/capnb.sh`, `decmulti.py`).
  So the hardware ÷4 is a standard 802.11a quarter-rate (5 MHz) PPDU. cbw 5 (bits 30+29) gives ÷8, i.e. 2.5 MHz.
  **The firmware's "10 MHz" is really 5 MHz on air**, and its "5 MHz" is 2.5 MHz.
- No ÷2 (true 10 MHz) found:
  - bit 29 alone (0xa…/0xb…) leaves TX at 20 MHz (decoded at 20 MHz);
  - 40 MHz mode (cbw 1, centre 38) + bit 30 keys the TX permanently, giving an undecodable emission ~4 MHz wide at the LO;
  - TX front-end 40 MHz values (0x8308f0a4, 0x8308f130, 0x830860b4, 0x830a0000/4/50) on top of ÷4: still 5 MHz.
- rx80211 gained a coarse CFO stage (STF, lag 16). At 5 MHz the card-to-X410 offset (~ −37 kHz) is at the edge of
  the LTF-only range (±39 kHz).

### RX: the demodulator stays at 20 MHz timing (all tests at 5180 MHz, card B path 0, X410 802.11a/p frames)
- B native cbw 4 (and cbw 5) decodes X410 **20 MHz** frames (198/200) and never 10 or 5 MHz frames. The 2022
  firmware's native narrow mode behaves identically (`tools/guest/rig2022.sh`, `NOSTAT=1 nbtest.sh`).
- The RX front end does narrow: ICAP noise spectra (`work/icap/tap_c*`) show the pre-channel-filter taps 0–9 band
  limited to ±5 MHz (cbw 4) / ±2.5 MHz (cbw 5), with sample hold ×2 and interpolated images. The demodulator's
  input and timing are unchanged.
- Tried, none narrows the demodulator (B decodes only 20 MHz frames, or nothing):
  - path-1 mirror of the narrow values;
  - the full 2022 10 MHz and 5 MHz register profiles;
  - `0x83081000` BW field codes 3–15 with and without bit 30 (code ≥ 8 = bit 27 kills RX), and bit 23;
  - 40 MHz mode + bit 30 against 5/10 MHz frames placed in the primary half;
  - RX/TX ADC clock forces `0x83007254` = 0x40/0x80/0xc0/0xd0/0xe0/0xf0.
- `0x8300d0ac[25:8]` = round(20 MHz·2^20/f_c) << bw (×2 at 40, ×4 at 80) is the SFO-from-CFO constant. The firmware
  has no narrow case for it, which is consistent with a demodulator that only runs at 20/40/80/160 MHz timing.
- MT7996 (w1700k-sdr register reference) implements 5/10 MHz with WFSYS_ON TICKCR0 (0x81031024: WLAN_CLK_MODE
  [1:0], LMAC_1X_MODE [4:2] = 1 for 10 MHz, 2 for 5 MHz) plus RATE BWCR 40/20 codes. MT7921 shares MT7996's
  0x8102xxxx (WF_TOP_MISC_ON) layout, but **an MCU read of 0x81031000 hangs the MT7921 MCU**. No MT7921 code
  references 0x8103xxxx, so this block does not exist here.

### RX through ICAP + software demodulation works (proof of concept)
- `tools/guest/icapnb.sh`: A transmits native 5 MHz frames back to back; B in native cbw 4 and ICAP test mode
  captures tap 4 at 40 Msps (ring = 65536 samples = 1.64 ms); decode on the host with resample 40→5 and rx80211.
  Result: **"MT7921-NBW-00039990" received card to card with valid FCS.** A has only ~1 frame per 4.6 ms on air, so
  most 1.64 ms windows miss a whole frame.
- ICAP divider 0x830ad448[19:18]: 3 = 40, 2 = 80, 1 = 160, 0 = 320 Msps. 40 Msps is the slowest.
- Readout: the word-by-word regval path is safe (~25 s per ring). mt7921dbg `mem` (MT_VEND_READ_EXT, 0x1f vendor
  type, ≤ 64 B per transfer, ~1.8 s per 256 KiB) works in normal mode. **After an ICAP capture, sustained `mem`
  reads time out, the driver resets the card, and once the guest kernel panicked in `__queue_work`** (recovered
  with QEMU system_reset). Hence `icap.py` uses regval unless ICAP_BULK=1.
- ICAP test mode leaves the packet SRAM unusable: always re-probe (rigreset) before and after an ICAP session.
- The firmware's own ICAP flow (FUN_96b96c / 96be4e / 96be5c / 96be72) has two SRAM modes:
  - mode 0: ring words 0..0xfffc, i.e. the 256 KiB packet area 0xe0200000–0xe023ffff;
  - mode 1: `0x88009004[27:24]` = 0xf lends banks to ICAP, ring words 0x20000..0x23ffc, i.e. 64 KiB at 0xe0280000.
  0xe0280000–0xe028ffff stays unchanged during normal RX (unused by the datapath), but it holds only 0.41 ms at 40 Msps.
  0xe0290000.. holds descriptor-like data. **Reading 0xe0300000 returns 0xffffffff and the card then resets.**

### Rig incident 2026-10-14 ~19:40 (card B moved to USB 2)
- A port "power cycle" for BirriaSDR (host `usb6-port1/disable` 1→0) did **not** remove power. Card B dropped its
  SuperSpeed link and re-enumerated on the USB 2 companion port (host 5-1, 480 Mbps). The host's own mt7921u bound it
  and loaded the host's 2024 ROM patch (20241106151007a: no eFuse risk). Toggling `usb5-port1/disable` did not
  restore SuperSpeed.
- Card B was re-attached to the VM with QEMU `device_add usb-host,bus=xhci.0,port=1,hostbus=5,hostport=1,id=mtkB`
  (it is guest 1-1 now) and works for Wi-Fi at 480 Mbps. **A physical replug into its USB 3 port is needed** for
  any IQ streaming at 160 MB/s. The QEMU line for the SuperSpeed port was hostbus=6,hostport=1.
- Guest: `drivers_autoprobe=0` left the re-enumerated device unconfigured. After restoring it, `echo 1-1 >
  /sys/bus/usb/drivers_probe` bound it. fwswap.sh's interface glob covers 1-1:1.3.
- Earlier: an ICAP packing experiment (0x800210b8[19:16] = 2/4) wedged card B ("MCU is not ready for firmware
  download") across driver re-probes and a QEMU replug. A host `usbreset 006/002` recovered it.
- BirriaSDR (work/ext/birria/ours, built in the guest at /root/birria/ours/build) is patched for 0e8d:7961 and
  2.3–2.5 GHz (`BIRRIA_FREQ`). It refuses a chip whose ROM patch is loaded: it needs a cold boot that mt7921u never
  touches, which neither the guest nor host USB reset provides.

## 2026-10-15: narrow rate codes in the PHY bandwidth field, and the clock-mode register
- Static RE (notes/05, notes/06): the ROM has MT7996's register-field ("GCI") layer, with 1114 registers decoded in
  work/crmap/. MT7921's TICKCR0 is **0x820b00a0** (WFSYS_ON at 0x820b0000): [1:0] WLAN_CLK_MODE, [4:2] LMAC_1X_MODE.
  Measured on card B: 0 at cbw 0, **0x5 at cbw 4, 0xa at cbw 5**. The firmware already sets it, and the demodulator
  still decodes 20 MHz there, so on MT7921 it is a MAC/WLAN tick scaler, not the demodulator clock. The ICAP 40 Msps
  clock is unaffected: the earlier card-B capture decoded at exactly 5 MHz.
- **Rate meter**: tools/guest/icapcode.sh takes ICAP hold counts per tap while 0x83081000 changes; runs of 2 at the
  40 Msps capture clock mean data at 20 Msps. Base native cbw 4 (bits 30:28 = 0xd):

  | 0x83081000 | tap 4 (front end) | tap 12 (after channel filter) |
  |---|---|---|
  | 0xd0000000 (firmware value, BW code 0) | 20 Msps | **40 Msps** |
  | 0xd1/0xd2/0xd6/0xd7 | 40 Msps or faster | 40 Msps |
  | 0xd3 | frozen (constant) | frozen |
  | **0xd4 / 0xd5** | 20 Msps | **20 Msps** |

  The cbw 5 base (0xf…) gives the same pattern. **BW codes 4 and 5 in [27:24] move the post-front-end chain to the
  narrow rate.** The firmware never writes them: FUN_93dbd4 clears [27:24] for narrow, and ContiTx maps only to
  0/1/2/7. This is the missing piece behind "front end narrows, demodulator doesn't".
- With codes 4/5, B reports **no frames at all**: no ambient 20 MHz beacons, no noise detections, nothing from A's
  5 MHz frames (tools/guest/c2c.sh, B only and symmetric). The 20 MHz demodulator behaviour is gone. Whether packet
  detection works in that state (MAC dropping frames vs. a dead demodulator) is the next question:
  tools/guest/mibdiff.sh (MIB counters) and companion fields from notes/06 (0x8300d06c bits 3/4, 0x83080000 bit 12,
  0x8300d0ac SFO).
- 0x8300d06c field [6:3] (16 values, A 5 MHz → B cbw 4): no decode.
- Harness without the X410: tools/guest/c2c.sh. A sends verified 5 MHz frames; candidates are written on B (or on
  both with BOTH=1) and restored. It counts frames from A's SA, bad-FCS frames and non-beacon frames.
  tools/guest/txicap.sh: card B captures card A's transmission through ICAP (B at cbw 0 = 40 Msps) for offline
  decoding. It replaces the X410 for TX-width checks.
- 2026-10-15 ~00:40: the lab host (= "lorien", 155.54.95.95) went down for the announced reboot during a mistaken
  `fwdbg dump` call that had its arguments swapped and read MCU address 0x800. Correct usage: `fwdbg.py <phy> dump
  <out> <page>...`.
