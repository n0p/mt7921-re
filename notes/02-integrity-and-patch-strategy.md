# Firmware integrity on MT7961, and how to load modified firmware

## Verified on hardware (2026-10-14)
**Modified RAM firmware boots.** A one-character DLM edit (`TxTone` → `ZxTone`, download address 0x02022668)
was built with `tools/mtfw_patch.py` and loaded by the stock driver. The chip booted normally, and the live string
read back as `ZxTone` at 0x02022ab4.

## What the loader actually checks
- **Per-region PDA download checksum.** Each downloaded region ends with 12 zero bytes plus one word chosen so a
  word-wise LFSR over the whole region ends at 0:
  `parity = popcount(state & 0x10921111) & 1; state = (state >> 1) ^ (parity << 31) ^ word`.
  The download engine checks it (MMIO 0x54000814 must read 0). The algorithm comes from BirriaSDR
  `tools/rebuild_firmware.py`. All four regions of both the 2024 and 2026 images check to 0.
- **File trailer crc32** over `file[:-4]`. The driver never sends it to the chip. We keep it consistent anyway.
- The earlier patched blobs failed because they updated only the file crc and left the region PDA word stale.
- Retracted: the 256-byte high-entropy block at 0x96db00 is **not** a signature. That claim is withdrawn.
- The ROM patch section doesn't checksum to 0 with this LFSR. Its scheme is still unknown, and we don't need to modify it.

## Runtime layout (verified against live memory)
- Real `$gp = 0x02003000`.
- Boot copies `.data` up by 0x44c, from load 0x020170fc to run 0x02017548, length 112596, then clears bss
  (tools/mkruntime.py). The runtime DLM image matches live memory 4096/4096 and 2048/2048.
- Patching uses **download addresses**; hooking and reading use **live addresses**. For data at or after
  0x020170fc, live = download + 0x44c.

## Host access to MCU memory (debugfs regidx/regval → USB vendor request, full 32-bit address)
| Range | Read | Write |
|---|---|---|
| ILM 0x915000.. | yes | **no**: WRITE_EXT stalls (-110) and the driver auto-resets the device |
| DLM 0x02015c00.. | yes | yes |
| Region 3 0xe0270000.. | yes | yes |
| Boot ROM 0x800000.. | yes | n/a |
Free SRAM: 0xe027cb00 onward. It isn't referenced by the RAM image and contains power-up noise.

## SHA monitor (ROM patch)
Enabled by the RAM firmware through ROM API 4. It watches 0x915000..0x955000. The IRQ handler only logs
"SHA_mon triggered" and re-arms. It matters only for runtime patching, not for boot-time images.

## Strategy
Boot-time images built by `make`: extract regions, compile NDS32 hooks, place them in region 3 extended into
free SRAM (or in an added region), patch call sites in ILM, fix the PDA words and crc, and install.
Runtime host writes (DLM, region 3) are for live experiments without reloading.

## Artifacts gathered 2026-10-14 (afternoon)
- Boot ROM dump: `work/rom/rom_800000_512k.bin` (512 KB, mirrored at 0x880000, content to ~0x858000).
  sha256 of the 1 MB raw dump: 172fd464…6247. The ROM reset code sets `gp = 0x02003000`, which confirms the runtime gp.
  Ghidra program `rom_800000.bin` (language NDS32:LE:32:mt7961rom), decompiled to work/decomp/rom_all.c.
- 2022 RAM firmware (build 20220110230951, AES-encrypted on disk) dumped **decrypted** from live memory:
  `work/fw2022_live/*.bin`. Its ITB = 0x96aec4 (language NDS32:LE:32:mt7961_2022), Ghidra project work/ghidra22,
  decompiled to work/decomp/all2022.c. Its CHANNEL_SWITCH handler is 0x962d20 and rlmPhySetChannel is 0x96282e.
- Both builds have the same CBW translators: 0x962eae/0x9618d0 (5→4, 10→5) and 0x962e58/0x96187c (rlm_phy.c).
- Firmware ABI: NDS32 v3 with the reduced register set (push25 up to s4; r11–r14 unused). Build hooks with
  `-march=v3 -mreduced-regs`.
- CE dispatcher 0x920dbc: count at live 0x02026114 (70), table at live 0x02026118 {u32 cid, u32 handler}.
  Event reply recipe (from ce_cmd_c0): `pkt = FUN_9178ea(len)`, `buf = *(pkt+0xc)`, `buf+0x18 = len`,
  `+0x1c = eid`, `+0x1d = seq` (copy from cmd +0x27), payload at +0x24, then `FUN_953dde(pkt); FUN_953ece(pkt)`.
- Never load a 2025+ ROM patch on these cards. w1700k-sdr reports that recent MediaTek ROM patches burn an eFuse gate.
  Both the guest and the lab host use the 2024 patch (20241106151007a).
