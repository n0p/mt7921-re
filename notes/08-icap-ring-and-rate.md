# 08 — ICAP ring placement, SRAM modes, packet-pool layout and capture rate (static, 2026-10-15)

Static analysis only: RAM firmware 20260224110949 (`work/decomp/all.c`, `work/dis/ram_0_00915000.txt`), ROM
(`work/decomp/rom_all.c`, `work/dis/rom_800000.txt`), the GCI map (`data/crmap/crmap.json`) and the MT7996 reference
(`work/ext/w1700k/src/include/hw/mt7996/{icap,cap_route,iq_format,dfe,mcu_map}.h`, the same ICAP/UMAC IP family).
Tags: [static] read from code or tables, [hw] measured on the rig (coordinator, 2026-10-15), [inferred].

## 1. The ICAP block (0x80021000) is MT7996's ICAPn, register for register
GCI module 0x5a (`crmap.txt:1120`) has the same field lists as MT7996 ICAPCR0..12 (`icap.h`) [static]:
- **CR0 0x80021090 (control)**: [0] EN, [1] CAPTURE (start, self-clearing), [3:2] TIME_ADJ, [7:4] (firmware writes 0xf),
  [10] SW_RST_B (active low), [13] RING, [17] WRAP (sticky), [22:20] SRC_SEL (0 = DFE path), [23] SWITCH.
  The firmware's start sequence (`FUN_96b6cc`, dis 0x96b6cc) writes EN=1, [7:4]=0xf, SRC_SEL, SWITCH=0 and RING, then
  CAPTURE. That gives 0x24f3 for a ring and 0x4f3 for a single shot; stop is 0x4f0.
- **CR2 0x80021098 / CR3 0x8002109c**: ADDR0_START / ADDR0_END in 4-byte units (inclusive end). CR5 0xa4 is the stop
  cycle. CR8 0xb0 [0] is the band, [27] FPGA_LOWRATE (MT7996: no effect). CR9 0xb4 is the live write pointer.
- **CR10 0x800210b8**: [19:16] are the input packers (MT7996 names: [16] 64→128, [17] 32→64, [18] 24→32, [19] 96→128).
  [28:26] and [31] are the SRAM-interface selects (MT7996: [27] SYSRAM_INTF_SEL2).
- **CR11 0xbc / CR12 0xc0**: SYSRAM_OFFSET / PSE_END_ADDR. The firmware zeroes both before every capture (`FUN_96be9a`).
  Its readout skips a hole when the read index reaches CR12 (adds CR11).
- MT7921-only words with no firmware writer: 0x800210c4 [31:28], 0xcc, 0xd0, 0xd4, 0xdc, 0xe0.
- Complete firmware use of the block (every GCI selector 0x5a.... in ROM and RAM): CR0 [0,1,7:4,10,13,17 read,22:20,23],
  CR2, CR3, CR5, CR8[0], CR9 (read), CR10 [31], [28:26], [19] and [18]. The [18] setter (ROM 0x832222) is never
  called by the RAM firmware. **Nothing ever writes CR10 [17:16].**

## 2. Firmware SRAM modes 0 and 1 (question 1)
- `FUN_96b96c(m)` (all.c @0x96b96c): ROM `0x832238` sets CR10[28:26] = 0 / 7, ROM `0x832262` sets CR10[31] = m, then
  `FUN_96be72` sets 0x88009004[27:24] = 0 / 0xf (the "lend" or bank grant, MT7996 cap_route GRANT_B). Ring bounds
  `FUN_96be4e` / `FUN_96be5c`: mode 0 = units 0..0xfffc, mode 1 = 0x20000..0x23ffc. [static]
- The stock start (`ext_cmd_56` op 0 → `FUN_95c58a`, dis 0x95c5a8: `ifcall9` to `jal FUN_96b96c` with a0 = 1)
  **always uses mode 1**. The status op (`FUN_95c6d2`) calls `FUN_96b96c(0)` once CAPTURE has cleared: it releases the
  grant and clears the interface bits before the dump op runs. [static]
- **Readout** (`FUN_96c276`, dis 0x96c344 `cmovn`): beat b is read at 0xe0200000 + 16b in mode 0. In mode 1 (12th
  argument ≠ 0) it is read at **MCU address 4·u = 0x00080000..0x0008fff0**, wrapping 0x23ffc → 0x20000 (0x96c496).
  So mode-1 samples land in a 64 KiB MCU SYSRAM range at 0x80000, which can be read only after the grant is released.
  The MT7996 doc says granted banks are frozen to other readers. This matches both measurements: [hw] 0xe0280000
  never changes, and an MCU read of 0x80000 hung while lend was set. [static, inferred]
- BirriaSDR and icap.py write CR10 = 0x9c000000 ([31] = 1, [28:26] = 7: the mode-1 interface bits) plus lend, but with
  mode-0 bounds 0..0xfffc, and their samples land at 0xe0200000 + 4u. The landing therefore follows the unit range:
  [0, 0x10000) goes to the packet pool, and 0x20000.. goes to the granted SYSRAM banks. [hw + inferred]
  The range 0x10000..0x1ffff is untested: in the pool view it is 0xe0240000..0xe027ffff, which includes firmware
  region 3 (code) at 0xe0270000. Do not try it.
- **WFDMA during capture**: mode 0 (pool) works, as the streamer proves. Mode 1 cannot be streamed while granted. Only
  an MT7996-style half hand-off could work (release banks the writer has left, DMA them, regrant), and the MT7921 bank
  semantics of [27:24] are unknown. Mode 1 is non-destructive [hw] but not streamable.

## 3. Packet pool (PLE/PSE) layout and allocator (question 3) — the cause of the TX damage
- **PBUF_CTRL at +0x004**: PLE 0x820c0004, PSE 0x820c8004 (GCI group 43). [11:0] TOTAL_PAGE_NUM, [25:17] PBUF_OFFSET in
  2 KiB banks, [31] page size (PLE 0 = 64 B, PSE 0 = 128 B). Writer: ROM `FUN_0082c74a`. Bank-to-page conversion:
  `FUN_0082c838` for PLE (banks<<5) and `FUN_0082c7c2` for PSE (banks<<4).
  GC at +0x000: bit 1 is held while the layout is written, and clearing it rebuilds the free lists; bit 2 means init
  done (`FUN_0082cbee` / `FUN_0082cbc4`). Group quotas at +0x110..+0x13c: [11:0] min, [27:16] max. [static]
- Live values [hw]: PLE 0x00000140 = 320 × 64 B at bank 0 (0xe0200000..0xe0204fff). PSE 0x00140d60 = offset 10 banks,
  3424 × 128 B (0xe0205000..0xe026ffff). The quotas sum to 3424 (HIF0 2544). The pool is 224 banks (448 KiB) and ends
  exactly where firmware region 3 starts (0xe0270000). **The mode-0 ring 0xe0200000..0xe023ffff covers the whole PLE
  plus 236 KiB of PSE.** That is why TX dies and the debug event comes back corrupted.
- The ROM computes each offset as the sum of the banks at lower "positions": PLE position at DLM 0x02014caa (u8), PSE
  position at 0x020142d1, **ICAP position at 0x020142d0 and ICAP banks at 0x02014cac (u16)**. Setters are ROM
  `0x82c79a(ple, pse, icap)` (positions must differ) and `0x82c7ba(banks)`. This is MT7996's PLE/PSE/ICAP partition. [static]
- RAM firmware init (`FUN_00916758` state 2) → `FUN_0094bb3e(boot mode)` → `FUN_0094ba1e(key)`. The key picks
  {PLE banks, PSE banks} from the table at live 0x0202ee44 (12 × {u32 key, u16, u16}) and the quota rows at
  0x0202ed6c (PLE, 3 × 6 B) and 0x0202e9c4 (PSE, 13 × 6 B). The boot mode is the TEST_CTRL switch-mode value
  (`ce_cmd_01`, all.c @0x933c84: 0 normal, 1 testmode, 2 ICAP):
  - normal → key 1 → profile 6: PLE 10, PSE 214 (live);
  - **ICAP mode (2)**: `0x82c79a(1, 2, 0)` (ICAP first), `0x82c7ba(128)`, key 5 → profile 4: PLE 10, PSE 54. The layout
    becomes **ICAP 0xe0200000..0xe023ffff (256 KiB = the mode-0 ring)**, PLE 0xe0240000, PSE 0xe0245000..0xe025ffff,
    with quotas summing to 852 (HIF0 128).

  This is MediaTek's own arrangement: in ICAP mode the mode-0 ring is the ICAP partition. In normal mode the partition
  is empty and the PLE starts at 0xe0200000, so a normal-mode ring destroys the pool. Captures under "ate.py mode 2"
  should therefore have been pool-safe; the earlier need to re-probe after ICAP sessions has other causes (the mode
  switch back, the bulk reads). [static; the last point is inferred]
- **Runtime re-partition is unsafe.** The free lists are rebuilt only on the GC bit-1 cycle. Doing that under live
  firmware (event/command pages, WFDMA RX prefetch, TX in flight) loses or duplicates pages. Only the boot init (or
  TEST_CTRL's full re-init) is safe, and test state is reported to drop the debug command. [inferred]

### Fix A (recommended): MediaTek's ICAP layout in normal mode, one instruction
`0x0094bb66: c7 14` (`beqz38 s1, 0x94bb8e`, the normal-mode branch) → **`84 e2`** (`movi55 $r7, 2`). The code then falls
into the ICAP-mode branch: positions (1, 2, 0), ICAP 128 banks, key 5. Only the pool layout changes; the boot mode
stays 0, so the rest of init is the normal one.

The existing streamer ring (units 0..0xfffc) is then the ICAP partition, and PLE/PSE live above it. Check after boot:
PLE PBUF_CTRL = 0x01000140, PSE = 0x01140360, PSE +0x110 = 0x00800080. Optional: ICAP banks via `0x0094bb80`
`44 00 00 80` (movi a0, 128), e.g. 64 → ring 0..0x7ffc. Region-0 code: download address = live address.

### Fix B (data only): free the top of the pool
In the image (download address = live − 0x44c):
- profile 6 PSE banks at dl 0x0202ea2e: `d6 00` → `36 00` (54);
- PSE quota row 6, dl 0x0202e74c (78 B) ← row 4, dl 0x0202e6b0.

The pool then ends at 0xe0220000. Ring units 0x8000..0xfffc (128 KiB) lie inside the proven pool-mapped range; the
streamer needs a ring base of 0x8000.

## 4. Capture rate and volume (question 2)
- The capture-mux rate field is 40·2^(3−m) MHz (MT7996 dfe.h; MT7921 [19:18] measured 3/2/1/0 = 40/80/160/320). **40 Msps
  is the floor.** [hw] domain 4 / MODE 0x13 runs at 40 Mwords/s. Domain 1 / MODE 0x14 runs at 80 (two chains).
  Domain 2 (MODE 0x20/0x21, rate [18:16] = firmware codes 7/6/4 or 5/3/1 by subtype and BW) runs at ~273 for every
  code, once 0x830a1004[7:0] is 1, 2 or 0x12. None goes below 160 MB/s for one chain.
- Narrow front ends (cbw 4/5 taps 0-9, or codes 4/5 tap 12) carry 20/10 Msps of information, but the ICAP still samples
  at 40 Msps (holds). No stride or decimation exists in the writer path.
- Packers (CR10 [19:16]) per MT7996:
  - 64→128 equals what MODE 0x13 already does;
  - 32→64 keeps every other sample but leaves the beat high half zero: same bytes in a linear ring;
  - 24→32 truncates I to 8 bits;
  - 96→128 serves two chains.

  The earlier "PACK 2/4" runs were CR10[17] / [18], inside the unprotected normal-mode ring, which corrupts the
  PLE anyway; the wedge is not attributable to the packer. A packer that lowers the CR9 advance is still worth a
  measurement in a safe ring (§5).
- MCU repacking is impossible: [hw] 3.5 Mwords/s from 0xe02xxxxx.
- **Transport is the real lever.** mt76u turns USB RX aggregation off (`mt792xu_dma_init` clears
  `MT_UDMA_WLCFG_0` 0x74000018 AGG_TO [7:0] / AGG_LMT [15:8] and WLCFG_1 PKT_LMT; AGG_EN [21] is left clear). It
  parses one event per ≤16 KiB URB (`usb.c` `mt76u_process_rx_entry`). BirriaSDR sustains **160.94 MB/s = gapless
  40 Msps on a Pi 5** with 32 KiB aggregation (TO 100, 30 packets) and 64 KiB host transfers (docs/mt7921.md:205-213).
  The measured ~147 MB/s ceiling (~19 µs per transfer + 6.5 µs/KiB) looks like a QEMU/URB-size artifact. [inferred]

## 5. Experiments (safest first) — see the fork report of 2026-10-15
1. Read only (MCU reads): PLE/PSE +0x000/+0x004/+0x110..0x13c; DLM 0x020142d0/d1 (u8), 0x02014caa (u8), 0x02014cac
   (u16), 0x020142d2/d4 (u16); table 0x0202ee44 (96 B, expect entry 6 = {1, 10, 214}).
2. Fix A, then the existing streamer with concurrent TX (30-frame test) and debug events intact.
3. Fix B if A misbehaves.
4. After A or B: an 18-bit reach test, ring at 0x18000..0x1bffc. Either it lands at 0xe0260000, or it aliases into
   the protected area; both are harmless.
5. Rate knobs, measured as the CR9 advance (hk_icaprate) inside the protected ring or in mode 1:
   - CR10 [16..19], one at a time, restored to 0 after;
   - CR8[27];
   - CR0[19];
   - the MT7921-only words 0x800210c4..0xe0.
6. Mode-1 readback (diagnostic): stop, clear lend and CR10 [31] / [28:26], then one MCU word at 0x00080000. Risk: MCU hang.
7. Transport: device RX aggregation needs a URB-splitting collector. Alternatively compare with bare metal (not
   possible on lorien without changing the host).

Hazards:
- MCU reads of unmapped or granted addresses hang the MCU.
- Host reads at or after 0xe0300000 reset the card.
- Never touch 0x8103xxxx.
- Never write 0x88009000 / 0x88009004 [23:0] / 0x88009008 (MT7996: MCU hang).
- Avoid host reads of the PSE block (MT7996: host lock-up); use fwdbg.
