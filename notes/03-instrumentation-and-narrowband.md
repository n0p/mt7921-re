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
- **Why B shows "nothing at all" with codes 4/5 (hypothesis, to verify):** mt76's `mt76_connac2_mac_fill_rx_rate`
  (mt76_connac_mac.c) returns -EINVAL for any P-RXV FRAME_MODE ([14:12]) outside 0..3 (20/40/80/160). If the PHY
  reports narrow frames with bandwidth 4/5, mt76 drops every frame, including in monitor mode.
  mt7921dbg now has a kprobe on that function (`/sys/kernel/debug/mt7921dbg/rxbw`). It keeps a histogram of the
  RX-vector bandwidth of every parsed frame and the last raw vector with a value >= 4. With `rxbw_fix=1` (default)
  it rewrites 4..7 to 20 MHz so those frames reach the monitor interface. A proper driver fix later maps 4/5 to
  RATE_INFO_BW_5/RATE_INFO_BW_10.
- 2026-10-15 (guest restarted on the rebooted host from the recovered QEMU command line, card A 2-2 SS, card B 5-1 USB 2):
  - kprobe histogram, per target card: with codes 4/5 card B's driver parses **0** RX vectors (code 5) / 2 (code 4) per
    window, against 545 at baseline. The PHY delivers nothing. The mt76 RX-vector drop is real but is not the
    cause here.
  - MIB diff (tools/guest/mibdiff.sh, 2 s idle vs a burst of A's 5 MHz frames), B cbw 4:
    - baseline: RX/FCS/time counters 0x820ed038/044/5a8/698/76c/780/788.. all count;
    - code 5: only 0x820ed024/028/030 and 0x820ed02c (busy/energy-time-like) rise, 0x180b → 0x10a59 during A's
      burst. Energy is seen, but there is no packet detection and no RX/FCS events.
  - X410 802.11a/p frames at 20/10/5 MHz against B with codes 4/5 decode nothing. Companions tried, all fail:
    0x8300d06c 0x88/0x90/0xa0, TICKCR0 0/0xa, path-1 same code, 0x83080000 bit 12. Grid of [31:28] ∈
    {8,9,c,d,e,f,4,5} × code {4,5} × base {cbw 0, cbw 4}: nothing (≤1 stray 20 MHz frame).
  - Rate meter at the 20 MHz base: 0x84/0x85/0x94/0x95 leave the front end (tap 4) at 40 Msps but run tap 12 at
    20 Msps. Codes 4/5 alone insert a ÷2 between the front end and the channel filter/demodulator domain, the
    decimation a 10 MHz channel needs. With 0x94/0x95 the tap-4 amplitude drops 80 → 18 (gain/AGC change).
  - ICAP capture domains decoded from FUN_96c61c:
    - domain 4 = BirriaSDR's (MODE 0x13, bit 23);
    - domain 1 = MODE 0x14, 0x830ad448 [15:8] SEL, [19:18] rate, bit 24;
    - domain 2 = MODE 0x20/0x21, 0x830ad44c [7:0] SEL, [18:16] rate, bit 22/21.

    `ICAP_DOM` in icap.py selects them. Domain 1 live SELs: 0/2/4/6 (full rate), 16, 18–22 (low amplitude,
    ~128 distinct values). They show no holds in code 0 or code 5, so they are outside the narrowed chain.
  - **Codes 4/5 also stop DSSS/CCK reception** (2437 MHz, A's injected frames go out as 1 Mbps DSSS; c2c.sh CH=6 BAND=0):
    40/40 at baseline, 0 frames of any kind with 0x84/0x85. 0x81 (40 MHz) still delivers ~420 ambient frames.
    Whatever stops is common to both demodulators: a receive back-end clock/enable/handoff, not OFDM sync.
    Forcing every PHY clock enable on (0x83080004/08 and 0x83090004/08 = 0xffffff, as the ROM calibration does) does
    not revive it, and does not disturb normal reception.
  - With codes 4/5, A's **transmission is unchanged** (X410: still 5 MHz, decodable). The bandwidth code only acts on the
    receive chain behind the front end. TX width depends only on [30:29].
  - ATE receive at 2437 MHz: COMMAND=STARTRX leaves 0x83081000 at 0x82000000 (BW code 2) even after AT 0x47/0x48 = 0.
    The ATE RX counters and 0x83081010.. stay 0. MediaTek's test receive needs more setup than this; parked.
  - CONN_INFRA 0x7c00a008 = 0x12800800 (fields [29:24] 0x12, [23:18] 0x20, [11:6] 0x20) look like bus dividers.
    Not written.
- **TX ÷4 needs TICKCR0.** When the narrow bits are written by hand (0x83081000 bit 30), the PPDU is a valid 5 MHz
  frame, but TX never completes (the transmitter keys continuously) unless 0x820b00a0 [1:0] WLAN_CLK_MODE ≥ 1. The
  firmware's own narrow path (FUN_941d22 → FUN_941cfa) writes 0x5 (10 MHz) / 0xa (5 MHz) there. TICKCR0 scales
  the MAC/LMAC timing (TX end, slot, SIFS), not the PHY sample clock.

## 2026-10-15: PHY stage counters as a receive oracle, and what BW codes 4/5 really are
- **PHY counters** (firmware FUN_936cc8 / FUN_9624f2), each word [31:16] OFDM | [15:0] CCK:
  - 0x83081010 PD (packet detect);
  - 0x83081014 MDRDY (header decoded, RX vector ready);
  - 0x83081018 RxOk;
  - 0x8308101c [31:16] CCK SIG error | [15:0] SFD error;
  - 0x83081020 [31:16] OFDM SIG error | [15:0] TAG error;
  - 0x83081024 FCS error.

  Enable with 0x83088234 |= 0x30000 and 0x83082004 = (v & ~0xe07) | 0xa05. The counters saturate at 0xffff, so each
  measurement window clears them through the STSCNT_EN edge: 0x83082004 [11:9] = 0, then 5.

  These counters separate "no detection" (PD = 0), "detected but header lost" (PD > 0, MDRDY = SIG = 0) and "header
  decoded, driver/MAC drops it" (MDRDY > 0, no frame on the interface). Tools: tools/sdr/pdcnt.sh (X410 frames vs a B
  candidate, FREQ=2350 by default) and tools/guest/pdscan.py (a field scan scored by MDRDY+SIG).
- Results at 2350 MHz, card B, X410 frames at 20/10/5 MHz (moved from 5180 because ambient APs inflated PD):
  - native narrow (code 0, 0xd0000000): PD and MDRDY count only for 20 MHz frames;
  - codes 4/5 (0xd4/0xd5): PD counts (thousands per window, noise and frames alike) but MDRDY = SIG = 0. The chain
    detects packets after the ÷2, and nothing reaches the SIGNAL decoder.
- **pdscan** over page 0x83088000 with base 0xd4000000 (668 candidates, byte b → 2b and b/2): one hit,
  **0x83088014 byte 3 0xea → 0x75** (0x75070808). MDRDY 342, SIG 240, RxOk 46 per window, against 0 at baseline.
  Decode test with that value and the X410 sending 20/10/5 MHz frames: B decodes **87 of the 20 MHz frames and no
  10/5 MHz frame**. The field revives the 20 MHz-timed SIGNAL path behind the ÷2. It does not make a narrow
  demodulator. The scan was stopped at candidate ~50 of 668.
- **E1: are codes 4/5 a filtered narrow decimation or a lane split?** ICAP taps 12/13, 4096 samples at 40 Msps, B
  idle at 5180 on the 20 MHz base (tools/guest/icapsave.sh, work/icap/e1/). Noise power per band relative to
  0–2 MHz:

  | value | 0–2 | 2–4 | 4–6 | 6–8 | 8–10 | 10–14 | 14–20 MHz |
  |---|---|---|---|---|---|---|---|
  | 0x90 tap 12 (20 MHz, 40 Msps) | 0 | -0.4 | -1.0 | +0.1 | -1.6 | -12.8 | -28.2 |
  | 0x94 tap 12, raw 40 Msps | 0 | -7.1 | -9.1 | -9.3 | -12.5 | -13.9 | -19.1 |
  | 0x94 tap 12, ÷2 (20 Msps) | 0 | -7.0 | -8.3 | -8.2 | -9.8 | | |
  | 0x95 tap 12, ÷2 | 0 | -2.5 | -3.1 | -2.8 | -4.3 | | |
  | 0x95 tap 13, ÷2 | 0 | -2.7 | -3.3 | -3.6 | -7.2 | | |

  A 10 MHz channel filter would leave 6–8 MHz below -7 dB and 8–10 MHz below -20 dB. Neither code has a sharp
  ±5 MHz edge, so codes 4/5 decimate without a narrow channel filter (or split the band into lanes). That fits the
  decode test: the samples behind the ÷2 still carry a 20 MHz-wide signal, and the only thing the SIGNAL decoder can
  lock to is 20 MHz timing. **Codes 4/5 are not the hidden narrow receiver.**

## 2026-10-15: no narrow demodulator in hardware; narrow RX through a firmware IQ stream works
### Hardware leads closed (all measured on card B unless noted)
- RXFE_CTRL 0x8308f064 [7:4] = 1..15 and [1:0], WF_RXFE_01 0x8308f068 (MAN_DOWNSAMP, DLPF_MODE_MAN, ADC480M,
  AFIFO_CK320M, WB_SEL, RXFDIQ_TAP_NUM) at native cbw 4 / 2350 MHz against X410 10 MHz frames: no decode
  (tools/guest/pdscan.py list mode, then monitor capture). [1:0] = 1 and DLPF CTL 2 kill the chain. Note: the
  OFDM "RxOk" counter 0x83081018 [31:16] counts every frame handed to the MAC, bad FCS included.
- A-die RF 0xb00/0xb04 (the only bandwidth-class fields in FUN_93dbd4's per-path tail: 20/40/narrow 0x10/0xf/1,
  80+ 0x20/0x1f/2) halved or doubled on both paths at cbw 0: 20 MHz frames still decode, no 10/5 MHz decode. Not
  a sample-rate control.
- 0x83081000 = 0xb0 (bit 29 only) keeps 20 MHz RX; 0xb4/0xb5 kill RX; 0xf0 decodes nothing.
- 0x81026008 is the MCU/bus DVFS divider set (FUN_9495f6, "Switch DVFS P%d FAIL"), not a PHY clock.
- TICKCR0 0x820b00a0: bit 30 alone already gives the ÷4 TX with TICKCR0 = 0 (the TX only never ends), so
  WLAN_CLK_MODE is a MAC tick, not the baseband clock. MT7996's own 5/10 MHz support is the same three writes
  (BN_6188 0x83086010 ÷4/÷2, TICKCR0, RATE BWCR) and no BW code (workflow wf_68eb0de5: mt7996-analogs, synthesis).
- Conclusion: the demodulator runs only at 20 MHz numerology (on a 40 Msps stream for code 0, or a 20 Msps stream
  for codes 4/5 once 0x83088014[31:24] is rescaled). 802.11p (10 MHz) and 5 MHz reception are not possible in the
  MT7921 PHY. TX: 5 MHz (÷4) works, 10 MHz (÷2) does not exist.
- A MCU read of 0x00080000 (the firmware's ICAP mode-1 ring address, 4·idx without the 0xe0200000 base) hung card
  B's MCU; recovered by rigreset. ICAP ring mode 1 data is not host-visible at 0xe0280000.

### Firmware IQ streamer (fw/src/iqstream.c, BirriaSDR's DMA method inside the running firmware)
- At runtime events travel through the packet SRAM, so MCU WFDMA TX ring 0 (0x54000300: 4 descriptors at
  0x02109900, idx 2/2) is idle and maps to USB bulk IN 0x84 (mt76's data endpoint). hk_iqstream borrows it, posts
  descriptors whose payload points straight at the ICAP mode-0 ring (0xe0200000, 40 Msps, tap 4 = front end), and
  restores it. Packets carry a 48-byte header that mt7921 parses as an unsolicited MCU event (eid 0xcc, seq 0) and
  frees; mt7921dbg's kprobe on mt7921_mcu_rx_event copies them into a 64 MiB ring (debugfs iq/iqstat) as records
  with the absolute sample position.
- The debug command gained DBG_CALL_DETACH (op 4): it answers first, then calls, because the capture overwrites
  the packet SRAM that carries events. The hooks are now built with -ffixed-$fp (the firmware keeps fp == gp; a
  long-running hook must not clobber it under interrupts).
- MAC RX must stay enabled (0x820e3000 bit 9 clear): with RX disabled the PHY front end freezes (every sample
  (-8, 3)). TX disabled (bit 8) as in BirriaSDR.
- Throughput through QEMU's usb-host: card B (USB 2) 33 MB/s; card A (USB 3) 137 MB/s with 8 KiB packets,
  147 MB/s with 16 KiB packets (4080 samples, header + payload + trailer fitting mt76u's 4 x 4 KiB RX URB).
  Model ~19 us per packet + 6.5 us per KiB: at most ~150 MB/s, below the fixed 40 Msps x 4 bytes = 160 MB/s.
  Descriptors in flight do not help (1/2/3: same time per packet), and 3 in flight corrupt the stream (CW-tone
  chunk-phase test: 11 jumps > 0.5 rad; 1 in flight: 0 of 177).
- Hence an energy gate on the MCU: mean |I|+|Q| of the newest nsum samples vs a threshold; idle -> the read
  position skips to `pre` samples behind the producer; busy -> contiguous streaming, the ring absorbing the
  deficit (~20 ms). Config block iq_cfg (chunk, inflight, thresh, hang_us, pre, nsum, stop, dur_ms).
- The AGC hunts between gain states on noise (|x| ~1340 <-> ~150 every few ms), so the gate needs manual gain:
  BirriaSDR's recipe (RF 0x768 force on both paths, 0x83000108 digital, 0x83000150 D force, 0x830000c4 A/B/C
  forces); LNA 3 / filter 4 / digital 6 gives a steady |x| ~83 at 2350 MHz.
- tools: tools/guest/iqstream.sh (GAIN, THRESH, NSUM, HANG, PRE, DURMS, CHUNK), tools/sdr/iqrec.py (records ->
  contiguous segments), tools/sdr/iqdec.py (segments -> resample to W Msps -> rx80211).
- **Result (card A at 2350 MHz, cbw 0, X410 5 MHz 802.11a frames at gain 30, 150 frames 40 ms apart):** the gate
  opened for every frame (168 segments, 201 ms of samples out of 20 s); iqdec decoded **all 150 frames at 5 MHz,
  111 with valid FCS** ("X410-OFDM-05.00-000000.." in sequence, CFO ~16.5 kHz). The failures are SNR (in-band
  ~6 dB at this X410 power), not segment truncation.
- **The receiving card's own PHY corrupts narrow frames.** At 10 MHz the damage sat at fixed symbols (21–25, ~200 us
  into every frame; |DC| spikes in the raw samples there), and FCS-good frames were 11–27 of 150. Parking card A's
  20 MHz demodulator in BW code 4 (0x83081000 = 0x84000000; front-end tap 4 unaffected) removes it: 10 MHz frames
  **149/152 good FCS**, 5 MHz **149/154**. The streamer is not at fault: no discontinuity at chunk boundaries
  (mean |dx| 89 at and away from them), and the X410 frame decodes offline at 10 dB SNR. tools/sdr/symcheck.py
  tallies damaged symbol indices.
- With 3 descriptors in flight, or an ungated stream that drains continuously for 12 s, the producer itself slowed
  to ~12.6 Msps (contiguous positions, no overrun registered, nothing decodable). Continuous DMA reads starve the
  ICAP's SRAM writes. Gated operation at moderate traffic is unaffected.
- The gate now integrates first differences (|dI|+|dQ|). At 10 cm, card B's carrier leakage sits 1.2 kHz from card
  A's LO with |mean| ~1650 (noise ~30), and with plain |I|+|Q| it held the gate open continuously. iqdec applies a
  20 kHz DC notch before detection (a CW looks like an endless STF to the autocorrelation detector). Differenced
  noise per LNA code (filter 4, digital 6, 128-sample means): 17 / 27 / 46 / 84; p99.9 22 / 32 / 54 / 102.
  Absolute |I|+|Q|: 28 / 32 / 57 / 104.
- **Card-to-card narrow link (2350 MHz):** card B transmits with its own hardware (CHANNEL_SWITCH cbw 4 = ÷4 = 5 MHz
  PPDU, inject.py on mon_b, 150 x 255-byte frames at 6 Mbps-eq / 1.5 Mbps on air). Card A receives through the IQ
  stream (GAIN 1,4,6, THRESH 42, code 4) and iqdec decodes **143/150 with valid FCS**. tools/sdr/c2cstream.sh.
- mt7921dbg's IQ kprobe now accepts marked packets from any card: fwdbg rewrites the shared "target" on every
  command, and a chsw on card B had silently filtered out card A's stream.
- X410 ports (user, 2026-10-15): TX/RX0 of radios 0 and 1 go to external antennas; RX1 is the in-room antenna.
  capture.py / burst.py / psd.py now default to RX1. Earlier X410 captures (and the X410 frames received by the
  cards) went through the external antenna.
- Rig incident 2026-10-15 ~13:40: after a firmware reinstall reprobe, card B dropped off guest port 1-2 (USB 2) and
  re-enumerated on the host at **6-1 SuperSpeed** (its original USB 3 port), with the host's mt7921u/btusb bound to
  it. Fix, in the QEMU monitor (tmux vm23, Ctrl-a c): `device_del mtkB`, then
  `device_add usb-host,bus=xhci.0,hostbus=6,hostport=1,id=mtkB`. Card B is now guest 2-3 at 5 Gbps, so both cards
  can stream (~147 MB/s each).
- Back-to-back streams (tools/guest/iqloop.sh, no rig reset between runs): the hook must re-arm the capture
  (control 0x4f1, then GO 0x24f3), or the second run stalls with no producer advance (state 6). A detached call
  answers in 23 ms, and the debug command keeps working between runs.

### Real-time receiver and half-duplex nodes (2026-10-15 afternoon)
- The gate now tracks DC per sample (one-pole, ~100 kHz at 40 Msps) and integrates |I-dcI|+|Q-dcQ|. First
  differences, tried in between, attenuate a 5 MHz channel by ~10 dB at 40 Msps and missed the X410 frames.
  Noise thresholds (128-sample means, filter 4 / digital 6), from the noise maxima x ~1.05:
  - cbw 0: LNA 0/1/2/3 -> 40/55/75/140;
  - cbw 4 (narrow front end, ±4 MHz ~13 dB above out-of-band): LNA 0 mean 15, max 21.5 -> 30. LNA 1 there has
    3-6 us DC steps (p99.9 157) and false-triggers constantly.
- rx80211's Viterbi, deinterleaver and QAM demapper are vectorized (Viterbi 28.7 -> 3.7 ms for 912 bits,
  identical output): about 20-25 ms per 5 MHz frame, i.e. 40-50 frames/s per core. The 24 Mbps (16-QAM) self-test
  frame fails FCS, before and after this change: open.
- tools/guest/nbrxd.py: card setup (tune, code 4, manual gain, ICAP, MAC TX off); back-to-back stream slices;
  incremental record parsing and per-segment decode; output to a radiotap pcap (half/quarter-rate channel flags;
  tcpdump shows "1.5 Mb/s 2350 MHz 11g/5Mhz") and/or a TAP. X410, 300 frames: 275 delivered live with valid FCS.
- mt7921dbg's IQ collector has two slots ("iqbind": "<slot> <phy>", rings iq0/iq1, iqstat0/1), so both cards can
  stream at once. Before that, two readers of one ring split each other's records.
- **Race:** chsw.py, ate.py, outband.py and fwdbg.py all write mt7921dbg's shared "target" and then a command file.
  With two daemons running, card A's CHANNEL_SWITCH went to card B, so A's "transmissions" never reached the air at
  2350. Every such pair now runs under tools/guest/dbglock.py (flock /tmp/fwdbg.lock).
- **Half-duplex node:** `nbrxd.py A 2350 --cbw 4 ...` keeps the card in native narrow mode (hardware ÷4 TX), runs RX
  slices, and between slices opens a TX window: MAC TX on, inject, wait for the airtime, MAC TX off. A frame must
  never be queued in the packet SRAM while a slice's ring is writing it. Card A with 10-frame windows after 500 ms
  slices: card B's daemon decoded 262 of ~300 with valid FCS. `--tap NAME --netns NS` turns the daemon into an IP
  node: kernel frames on the TAP become OCB-style 802.11 data (addr3 = wildcard BSSID, LLC/SNAP, NOACK) and are sent
  in the next TX window. tools/guest/nbping.sh pings between two such nodes (nsA 10.99.0.1, nsB 10.99.0.2).
- **Half-duplex on one card is not robust (open problem).** Card B as a half-duplex node always failed: injected
  frames never complete ("timed out waiting for pending tx"), the MCU stops answering ("Message 000500cb timeout"),
  and the driver resets the card. This also happens with the gate shut (capture ring running, no DMA streaming),
  so the mode-0 ring's writes over the 256 KiB packet SRAM are the cause, not the ring-0 DMA takeover. TX with the
  receive registers set up but no capture started is still untested on B. Card A's 262-frame success was probably
  luck in which packet-SRAM structures got hit. MT_PLE_AC_QEMPTY (0x820c0500 + 0x40 n) shows frames still queued
  in the device (cleared bit), and tx_window now waits for all four words to read 0xffffffff.
- **Wedged card -> guest oops.** A card whose MCU stops answering fails its reprobe ("Failed to get patch
  semaphore"). mt7921u_mac_reset -> mt76u_resume_rx -> mt76u_submit_rx_buffers then dereferences NULL (+0xb8) in the
  mt76 kworker; the next bind/unbind hangs in D state. Recovery: host `sudo -n usbreset 006/<dev>` for the card,
  QEMU `system_reset` (tmux vm23, Ctrl-a c), then /root/fwre/loadstack.sh, insmod mt7921dbg, rigreset.sh.
- **Two-node IP test** (nbping.sh): with every node half-duplex, node B decoded A's ARP requests and answered, but
  B's replies never reached A, which is the failure above. A robust node for now = one card that only receives
  (stream) + one card that only transmits (hardware 5 MHz). A single-card node needs a capture region that leaves
  the datapath intact: MediaTek's mode 1 (0x88009004[27:24] = 0xf "lend", ring 0x20000..0x23ffc, read by the
  firmware at MCU address 0x80000) is the candidate.
- **"Deafness" was my daemon, not the radio.** It decoded synchronously between slices. The slice holding card B's
  channel-switch calibration bursts (strong tones at DC, 5 and ~9.5 MHz) produced dozens of segments, and decoding
  them took seconds, during which B's whole 6 s injection went by. Proof: a single 12 s slice, or slices with idle
  gaps and no decoding, keep hearing B (85/120; ~140 busy checks per slice). Register snapshots before/after show
  only AGC status and counters changing. Decoding now runs in a ProcessPoolExecutor (3 workers). Slices default to
  4 s with a 50 ms read tail (~97% duty). B -> A at 10 cm: 91/120 (gain 0,4,6), 99/120 (gain 0,0,0). B's own frames
  are ~95% clean as seen by the X410 on RX1; the rest are slice gaps and scattered failures on near-field, clipping
  frames. X410 frames at a normal level: 153/153.
- **Injected unicast frames are retried despite the radiotap NOACK flag.** The retries fall back to non-OFDM rates
  (X410: 1714 ms of emission for 80 small frames; broadcast copies decode, unicast never do). The card can never hear
  an ACK (no narrow receiver), so the node framing now always sends addr1 = broadcast, carries the real destination
  in addr3, and nbrxd's TAP side maps it back and drops (SA, seq) retries.
- **IP over the narrow link (tools/guest/nbudp.sh):** card B = transmit-only node (`--tx-only`, hardware ÷4,
  TAP nbB in nsB, 10.99.0.2), card A = receive-only node (`--rx-only`, firmware IQ stream + software receiver, TAP
  nbA in nsA, 10.99.0.1), 5 MHz at 2350 MHz. **48/60 UDP datagrams delivered** to a socket in nsA.
  A bidirectional single-card node still needs a capture region that does not break the card's TX path (above).
