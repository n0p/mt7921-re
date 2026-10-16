#!/usr/bin/env python3
"""Source of tools/ghidra/annotations/rom.tsv (boot ROM, program rom_800000.bin): ROM functions with what notes/01-09
established. Registers come from regs.tsv (gen_ram.py regs). Edit here, then: python3 gen_rom.py > rom.tsv"""
E = []


def f(addr, name, comment=""):
    E.append((addr, "func", name, " ".join(comment.split())))


def r(addr, name, comment=""):
    E.append((addr, "reg", name, " ".join(comment.split())))


# ---- kernel helpers ----------------------------------------------------------------------------------------------
f(0x80187e, "tmrNow", "Returns the MCU timer 0x89020034 (time base of the ROM's timeouts and delays).")
f(0x802160, "tmrDelay", "Busy-wait n + 1 ticks of tmrNow.")
f(0x802178, "tmrDelay2", "Busy-wait n + 1 ticks of a second timer (FUN_801f16).")
f(0x800506, "romAssert", "Assert failure: (source file, line); halts with the location.")
f(0x8004ac, "romAssertMsg", "Assert with a message: (source file, line, format, a, b, c).")
f(0x800930, "irqRegister", "Registers an interrupt handler: (vector, handler, name string).")
f(0x800bba, "irqConfigure", "Interrupt configuration through DLM 0x02002024 (vector, mode).")
f(0x800bc4, "irqMask", "Masks an interrupt line (0x89010090 / 0x89010094 by line number).")
f(0x800c0a, "irqUnmask", "Unmasks an interrupt line (0x89010080 / 0x89010084 by line number).")
f(0x800c7e, "irqSave", "Disables interrupts (GIE) and saves the previous PSW state into *out.")

# ---- CONN AFE / clocks ---------------------------------------------------------------------------------------------
f(0x80ab32, "connAfeSetXtal", "Per-crystal CONN AFE setup (xtal_hz = 24/25/26/40 MHz): CONN_AFE_XTALCNT 0x7c0030f4 = "
  "two crystal-cycle counts (50.5 and 30.3 x f_MHz); CONN_AFE_XTALMODE 0x7c003004 [1:0]/[7:6] = 2/1 for 24 AND 40 MHz "
  "(3/3 for 25, 3/1 for 26), [3:2] = [17:16] = 3. Identical codes for 24 and 40 MHz: crystal-type settings, not PLL "
  "ratios; no clock divider here (notes/07). HAZARD: never write these.")
f(0x806de6, "connAfeInitPulse", "CONN AFE init pulse sequence on 0x7c003000/04/08 bits (reset/enable steps with "
  "delays).")

# ---- GCI register-field layer (crmap, notes/05) --------------------------------------------------------------------
f(0x826aac, "gciModuleIfcInit", "Fills the module interface array DLM 0x02014f04 + 4*module (0x5c modules): base "
  "addresses and field tables per module (notes/05, data/crmap).")
f(0x82693a, "gciAccessorInit", "Fills the GCI accessor table DLM 0x02013800..0x0201388c (MT7996 'FIT').")
f(0x826860, "gciGetModuleIfc", "GetChipModuleIfc(module) = *(gp + 73476 + 4*module); module < 92 (accessor +0x84).")
f(0x826b70, "gciSet", "Set(selector, value): one read-modify-write (accessor +0x0c). Selector = module<<16 | group<<5 | "
  "field.")
f(0x826b7a, "gciSetPairs", "Varargs pairs (n, sel, val, ...): one read-modify-write per pair (accessor +0x20, MT7996 "
  "non-transaction setter).")
f(0x826bb0, "gciSetPairsTxn", "Transaction varargs set (accessor +0x28).")
f(0x826c22, "gciGetCore", "Common getter behind gciGetByte/Word/Dword.")
f(0x826c7e, "gciGetByte", "Get byte (sel, *out) (accessor +0x00).")
f(0x826c90, "gciGetWord", "Get word (sel, *out) (accessor +0x04).")
f(0x826ca2, "gciGetDword", "Get dword (sel, *out) (accessor +0x08).")
f(0x826cde, "gciSetTriples", "Varargs triples, n % 3 == 0 (accessor +0x1c).")

# ---- MAC timing ---------------------------------------------------------------------------------------------------
f(0x8282e6, "macSetIfs", "MAC IFS setter: type 0 = slot, 1 = SIFS, 2 = RIFS, 3 = EIFS.")
f(0x828848, "macIfsInitRom", "MAC IFS init (called from RAM macIfsInit).")
f(0x8288d0, "tmacSetDtrRom", "TMAC CDTR 0x820e40c8 / ODTR 0x820e40cc ([31:16] / [15:0]).")
f(0x82c2d6, "lpSetTimers", "Low-power timers 0x820b00a4 [11:0], 0x820b00c0..d4 from a config struct.")

# ---- SRAM partition (PLE / PSE / ICAP, notes/08) -------------------------------------------------------------------
f(0x82c74a, "sramBankCfg", "Bank configuration word: 2 KiB banks, [31] page size (PLE 0 = 64 B, PSE 0 = 128 B).")
f(0x82c79a, "sramPartitionOrder", "(ple, pse, icap) positions of the PLE / PSE / ICAP partitions (must differ); "
  "MT7996's PLE/PSE/ICAP partition. ICAP mode uses (1, 2, 0).")
f(0x82c7ba, "sramPartitionBanks", "(banks) partition size; ICAP mode uses 128.")
f(0x82c7c2, "pseSetBanks", "PSE bank count (banks << 4).")
f(0x82c838, "pleSetBanks", "PLE bank count (banks << 5).")
f(0x82cbc4, "sramWaitDone1", "Waits for the partition update to finish.")
f(0x82cbee, "sramWaitDone2", "Waits for the partition update to finish.")

# ---- ICAP ---------------------------------------------------------------------------------------------------------
f(0x832222, "icapSetCr10Bit18", "ICAP_CR10 [18] setter (MT7996 packer 24 -> 32); never called by the RAM firmware.")
f(0x832238, "icapSetCr10Mode", "ICAP_CR10 [28:26] = 0 / 7 (SRAM interface select).")
f(0x832262, "icapSetCr10Bit31", "ICAP_CR10 [31] = m (SRAM mode).")

# ---- RF (A-die) ---------------------------------------------------------------------------------------------------
f(0x83a662, "rfRead", "A-die RF register read (reg, path). Callable through fwdbg: call 0x83a662 reg path.")
f(0x83a66c, "rfWrite", "A-die RF register write (reg, value, path). HAZARD: never RF 0xbc, 0x21c, 0xcc4, 0xd20, 0xce0 "
  "(power, PLL, VCO calibration).")
f(0x83c558, "rfCalApi", "ROM RF/calibration API (via *0x008229c4): runtime op table *0x0201456c (notes/04).")
f(0x84502e, "rfSynthSetFreq", "RF synthesizer programming: LO words RF 0xc68/0xca8 (integer [29:20], 18-bit fraction "
  "[19:2], about 114 Hz per LSB at 2.4 GHz) (notes/04).")
f(0x83e2e0, "phyCalResultsClear", "Clears the per-chain calibration result words (0x830076xx/78xx, 0x8300axxx).")

# ---- PHY ----------------------------------------------------------------------------------------------------------
f(0x840fd8, "phySetBwRom", "ROM PHY_BN_MODE writer: [27:24] = {0,1,2}[a0] else 3, [30:28] = a1 (band). Only callers are "
  "calibrations passing a0 = 1/2/3 and a1 = band, so the ROM has no narrow code either (notes/07).")
f(0x84099a, "phyCalBwSteps", "Calibration that writes PHY_BN_MODE [27:24] = 0/1/2 with step counts 4/6/10.")
f(0x841182, "phyPowerOnBand", "PHY power-on per band: PHY_BN_MODE [30:28] = band, 0x830a3008 |= 0x8c0000, "
  "0x830a3024 |= 0xc0000; 2026 patch twin at 0x906050.")
f(0x841342, "phySetChainIdx", "PHY_BN_CTRL 0x83080000 [5:4] = {0,1,2}[x] (table 0x850768): x is the chain index.")
f(0x841418, "phySetChainIdx2", "0x83007240 [29:28] (table 0x850764): chain index as well.")
f(0x846514, "phyCalForceClocks", "Calibration step: PHY_BN_CLKFORCE0/1 |= 0xffffff (all band clocks forced on).")
f(0x83f074, "txToneStartRom", "ROM twin of RAM txToneStart, for 4 chains.")
f(0x8433ba, "phyCalRoute", "Calibration routing: 0x7c003014 [11:7] = 0x800 (path 0) / 0x200 (path 1).")
f(0x847a5e, "phyCalRxLoop", "Calibration (path): 0x7c003014 [6:3] = 8 >> path, PHY_RXFE_CTRL |= 3, then a reset pulse "
  "on the chain's 0x8300e00c word.")
f(0x847e1c, "phyCalChain", "Calibration helper; passes the path as the chain index (also its x0x10000 offset).")

# ---- AXI DMA (mcu/driver/axi_dma.c): 4 channels at 0x80010000 + 0x100 * ch; unused by the RAM firmware -------------
f(0x816742, "axiDmaInit", "Registers the AXI_DMA0..3 interrupt handlers and resets the per-channel callbacks.")
f(0x8167ea, "axiDmaBusy", "Returns AXI_DMAn_STS [0].")
f(0x81683c, "axiDmaClearSts", "AXI_DMAn_STS = 0.")
f(0x816892, "axiDmaSet04", "Channel +0x04 [0] = v.")
f(0x81690e, "axiDmaSet08", "Channel +0x08 [0] = v.")
f(0x81698a, "axiDmaGet08", "Returns channel +0x08 [0].")
f(0x8169dc, "axiDmaSet0cBit0", "Channel +0x0c [0] = v (start).")
f(0x816a58, "axiDmaGet0c", "Returns channel +0x0c [0].")
f(0x816aaa, "axiDmaSet0cBit1", "Channel +0x0c [1].")
f(0x816b2e, "axiDmaStartStop", "(ch, 0): start and wait up to 1000 ticks for completion; (ch, 1): stop.")
f(0x816ba2, "axiDmaSet10Bit1", "Channel +0x10 [1].")
f(0x816c26, "axiDmaSet10Bit0", "Channel +0x10 [0].")
f(0x816ca2, "axiDmaGet10", "Returns channel +0x10 [0].")
f(0x816cf4, "axiDmaStart10", "Start through +0x10 [0] and wait.")
f(0x816d5e, "axiDmaSet14", "Channel +0x14 [0].")
f(0x816dda, "axiDmaGet14", "Returns channel +0x14 [0].")
f(0x816e2c, "axiDmaStart14", "Start through +0x14 [0] and wait.")
f(0x816e96, "axiDmaGetCtrl", "Returns AXI_DMAn_CTRL (+0x18).")
f(0x816ee6, "axiDmaSetCtrl", "AXI_DMAn_CTRL (+0x18) = v.")
f(0x816f34, "axiDmaSetAddr0", "AXI_DMAn_ADDR0 (+0x1c) = v.")
f(0x816f82, "axiDmaSetAddr1", "AXI_DMAn_ADDR1 (+0x20) = v.")
f(0x816fd2, "axiDmaSet2c", "Channel +0x2c = v (ring/second address modes).")
f(0x817022, "axiDmaSetLen", "AXI_DMAn_LEN (+0x24) = v.")
f(0x817072, "axiDmaConfig", "Configures a channel from a cfg struct: ADDR0 = cfg[1], ADDR1 = cfg[2], LEN = cfg[3]; "
  "mode from cfg[8,10,12,13,15] selects which control fields go into AXI_DMAn_CTRL ([29:28] cfg[6], [25:24] cfg[7], "
  "[20] cfg[8], [18:16] cfg[9], [15] cfg[10], [14:5] cfg[11], [0] cfg[14]); callback cfg[17]. A store to a PHY "
  "register costs 238 ns on the bus, so no DMA can feed a 10 Msps DAC (notes/07).")
f(0x81722e, "axiDmaChanAlloc", "Allocates a free channel from a per-class bitmap (0xdeaddead = none).")
f(0x8172ac, "axiDmaChanFree", "Frees a channel bit.")

r(0x89020034, "MCU_TMR", "MCU timer read by tmrNow")
r(0x89010080, "MCU_IRQ_UNMASK", "interrupt unmask (irqUnmask)")
r(0x89010090, "MCU_IRQ_MASK", "interrupt mask (irqMask)")

for a, k, n, c in E:
    print(f"{a:#010x}\t{k}\t{n}\t{c}")
