# MT7961 (MT7921) firmware: layout and channel-bandwidth path

Firmware: linux-firmware 20260916, `WIFI_RAM_CODE_MT7961_1.bin` build 20260224110949,
`WIFI_MT7961_patch_mcu_1_2_hdr.bin` build 20260224110909a.

## Container
- RAM image: 5 regions + 36-byte trailer. **No region encrypted.** Trailer `crc` = `crc32(file[:-4])`
  (verified: 0x5775a48a). The driver does not check it, but keep it consistent anyway.
  | # | addr | len | flags | content |
  |---|------|-----|-------|---------|
  | 0 | 0x00915000 | 363536 | OVERRIDE_ADDR (entry) | main WM code (ILM) |
  | 1 | 0x02015c00 | 272400 | - | data / strings / cmd tables (DLM) |
  | 2 | 0x00404400 | 15376 | - | small code/data |
  | 3 | 0xe0270000 | 51920 | - | code (also uses EX9 table) |
  | 4 | (none) | 88416 | NON_DL, type 2 | not downloaded; scrambled ("US-06" header), likely log DB |
- ROM patch: one section at 0x00900000 (0x16780 bytes), overlapping RAM region 0 by 6 KB. It is a separate boot
  stage (loaded and activated before RAM). It is not analyzed in the RAM Ghidra program.

## CPU / analysis setup
- Andes NDS32 v3, little-endian data, **instructions stored big-endian**.
- `$gp = 0x02002bb4`. It is set by the boot ROM and never in RAM, and was recovered statistically (tools/find_gp.py).
- EX9 instruction table ITB = 0x0096c7bc (set in `ram_set_itb` 0x915074). It has 512 entries and about 14k `ex9.it` uses.
  EX9 `jal`/`j` targets are region-absolute: `(PC & 0xfe000000) | imm24<<1`.
- Ghidra: custom language `NDS32:LE:32:mt7961` (tools/ghidra/NDS32_MT7961). It inlines all 512 EX9 entries and
  sets gp as a tracked constant. The project is rebuilt with `tools/ghidra_build.sh`.
- Boot ROM (0x800000..) is not available. RAM calls ROM APIs through pointer tables at 0x822xxx.

## MCU command dispatch
- CE/legacy table @ 0x02025ccc: `{u32 cid; u32 handler}` × 70, labelled `ce_cmd_XX`.
  Examples: 0x01 TEST_CTRL → 0x933c84, 0x0a SET_RX_FILTER → 0x9540e4, 0x1d SET_EDCA → 0x951f00,
  0xc0 REG_RW → 0x921c44, 0xed EXT_CID → 0x920cc8 (sub-dispatcher).
- EXT table @ 0x02018c94: `{u8 ext_cid; pad[3]; handler}` × 38 (count stored at 0x02025efc), labelled `ext_cmd_XX`.
  0x02 RF_REG_ACCESS → 0x96394c, 0x04 RF_TEST → 0x933d86, **0x08 CHANNEL_SWITCH and 0x4e SET_RX_PATH →
  0x964494**, 0x01 EFUSE_ACCESS → 0x966b9e.
- In the handler, the command packet is at `*(arg+0xc)`. The payload starts at +0x40, and ext_cid is at +0x29.

## CHANNEL_SWITCH handler (0x964494) vs driver `mt7921_mcu_set_chan_info`
- Payload: +0x40 control_ch, +0x41 center_ch, **+0x42 bw (CMD_CBW_*)**, +0x44 rx_streams, +0x45 switch_reason,
  +0x46 band_idx, +0x4a channel_band, +0x51 ap_bw.
- `FUN_962eae` maps CMD_CBW to an internal width: 20/40→0, 80→1, 160→2, 80+80→3, **5MHz(5)→4, 10MHz(4)→5**.
- Table @0x0202173c `[0,1,2,3,6,5,4]` (indexed by CMD_CBW, `bw < 7`) has dedicated 10/5 MHz slots.
- `rlmPhySetChannel` (0x963f6c) receives the internal BW and stores it in a per-band cache through
  `FUN_94251a(3, band, bw)`. There is **no rejection of 4/5** anywhere in this path. Only the coex helper
  (0x95a0c2) logs "Channel BW is on J-mode?!" for widths outside 0..3. That message is informational.
- Unknown: whether the ROM-side PHY/RF routines really narrow the baseband for internal BW 4/5.
  → Test on hardware with experiment 1 (driver-only, see patches/).

## TEST_CTRL (ce_cmd_01, 0x933c84)
- Requires packet len field == 0x2c. action @+0x40, param0 @+0x44.
- action 0 = switch mode (param0 = mode). Other actions require `FUN_916a48() == 4` and then go to 0x933bf6.
- ICAP/IQ strings exist: "Current FWOpMode isn't ICAP", "IcapDumpEvent" (0x95c7a0). Still to be analyzed.
