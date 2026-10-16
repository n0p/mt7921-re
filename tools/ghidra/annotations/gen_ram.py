#!/usr/bin/env python3
"""Source of tools/ghidra/annotations/ram.tsv (functions of the 2026 RAM image, program ram_0_00915000.bin) and
regs.tsv (MMIO registers, applied to every program), with what notes/01-09 established. Edit here, then:
python3 gen_ram.py > ram.tsv; python3 gen_ram.py regs > regs.tsv; tools/annotate.sh"""
F = "func"
REG = "reg"
E = []


def add(addr, kind, name, comment=""):
    E.append((addr, kind, name, " ".join(comment.split())))


# ---- boot, events, logging ----------------------------------------------------------------------------------
add(0x916758, F, "ramInitMain", "RAM firmware init state machine; state 2 calls sramProfileSelect(boot mode) "
    "(PLE/PSE/ICAP partition profile, notes/08).")
add(0x916a48, F, "getOpMode", "Current operating mode; ce_cmd_01 actions other than 'switch mode' require 4 (notes/01).")
add(0x9178ea, F, "evtAlloc", "Allocate an MCU->host event packet of len bytes; buf = *(pkt+0xc) (notes/02 event "
    "recipe). Used by fw hooks as fw_evt_alloc.")
add(0x921898, F, "dbgLogPrefix", "Debug-log line prefix (timestamp / module tag) before a dbgPrintf call.")
add(0x9217ba, F, "dbgPrintf", "Debug log printf (EX9 #2). Misdetected as non-returning by Ghidra, which is why "
    "SetupMt7961.java disables that analyzer.")
add(0x953dde, F, "evtFinalize", "Finalize an event packet (header fields, length) before evtSend (notes/02).")
add(0x953ece, F, "evtSend", "Queue an event packet to the host (WFDMA / USB bulk in).")
add(0x915918, F, "fwMemset", "memset(dst, val, len).")

# ---- MCU command handlers -------------------------------------------------------------------------------------
add(0x933c84, F, "-", "ce_cmd_01: mode switch (action 0 = switch: 0 normal, 1 testmode, 2 ICAP); other actions "
    "need getOpMode() == 4 (notes/01, 08).")
add(0x964494, F, "extChannelSwitch", "EXT CHANNEL_SWITCH / SET_RX_PATH (ext 0x08 / 0x4e). Payload from the driver's "
    "mt7921_mcu_set_chan_info; the sniffer 'outband' kHz override lands here (notes/04). Path: cbwToInternalBw -> "
    "rlmPhySetChannel -> phySwitchChannel -> phySetChannelTop -> phyChannelSet, plus macSetChannelWord.")
add(0x95c58a, F, "icapStart", "ext_cmd_56 op 0: start an ICAP capture; always uses SRAM mode 1 via icapSetMode(1) "
    "(notes/08).")
add(0x95c6d2, F, "icapStatus", "ext_cmd_56 status op: calls icapSetMode(0) once CAPTURE has cleared, releasing the "
    "lent banks (notes/08).")

# ---- channel set: cbw -> PHY / MAC ----------------------------------------------------------------------------
add(0x962eae, F, "cbwToInternalBw", "Map the driver's CMD_CBW to the internal width: 20/40 -> 0, 80 -> 1, 160 -> 2, "
    "80+80 -> 3, 5 MHz(5) -> 4, 10 MHz(4) -> 5 (notes/01). No rejection of 4/5 on the way.")
add(0x963f6c, F, "rlmPhySetChannel", "MediaTek rlmPhySetChannel: calls phySwitchChannel and, on a real channel "
    "change (== 2), macSetChannelWord (TICKCR0, AGG BW class) (notes/05).")
add(0x96314e, F, "rlmPhySetChannel2", "Second channel-set path; passes the internal bw from chCacheGet(3, band) "
    "(notes/05).")
add(0x942672, F, "phySwitchChannel", "Channel switch core (param_12 = outband kHz, param_14 = channel struct). "
    "ch > 0xb6 selects the 4 GHz base. Skips when chSameCheck says nothing changed (notes/04).")
add(0x942544, F, "chSameCheck", "'Same channel -> skip': compares base, ch, table bw, bw, switch reason, NOT the "
    "outband frequency; hop to another channel first to retune (notes/04).")
add(0x94251a, F, "chCacheSet", "Channel cache setter: (0) base, (1) ch, (3) bw, (0xe) outband kHz (notes/04).")
add(0x94252e, F, "chCacheGet", "Channel cache getter (3, band) = cached internal bw.")
add(0x941d94, F, "chValid", "Channel validity: only ch != 0 / != 0xff plus band checks, no upper bound (notes/04).")
add(0x941ea4, F, "phySetChannelTop", "Top of the PHY/RF channel bring-up -> phyChannelSet (notes/04, 06).")
add(0x941e00, F, "chSetNarrowEnum", "Sets channel struct +0x11: internal 5 -> 6 ('10 MHz'), internal 4 -> 5 "
    "('5 MHz').")
add(0x941e48, F, "chPrimaryIndex", "Primary-20 index for BN_MODE [22:20]; no narrow case (index stays 0).")
add(0x93bc9c, F, "phyChannelSet", "Per-path PHY channel set. Runs twice in narrow mode: first with bw forced to 0 "
    "(20 MHz) around the calibrations, then with the narrow enum. Also stores the 2.4 GHz channel index "
    "(f - 2407000)/5000, which wraps below 2407 MHz (only used for spur lists) (notes/04, 06).")
add(0x93abc4, F, "phyCfgFill", "Fills the config struct 0x02045e80 (byte 4 = bw enum) (notes/06).")
add(0x93b96a, F, "phyCfgFillRf", "phyCfgFill twin for the RF context (ctx 0x02045e80, 2, ...) (notes/04).")
add(0x93b866, F, "rfSubBand", "RF sub-band classifier: 0 for any f < 4850 MHz (2.4 GHz) (notes/04).")
add(0x93a9da, F, "rfSpurCheck", "Compares the channel index against spur-channel lists (notes/04).")
add(0x93b35a, F, "phyChanCal1", "Calibration run around the 20 MHz pass of phyChannelSet (notes/06).")
add(0x93aa70, F, "phyChanCal2", "Calibration run around the 20 MHz pass of phyChannelSet (notes/06).")
add(0x93b6c8, F, "phyChanCal3", "Calibration run around the 20 MHz pass of phyChannelSet; stores the channel index "
    "in the cal record (notes/04, 06).")
add(0x93c6b6, F, "crTableBKey", "Builds the table B tuple (band, channel class, path, ...; no bandwidth digit) "
    "(notes/06).")
add(0x93c6f8, F, "crTableAKey", "Builds the table A tuple: t0 band group, t1 = bw with 5 -> 3 and 6 -> 4 (10 MHz takes "
    "the '40 MHz, +1 stream' column, 5 MHz the '20 MHz, +1 stream' one), t2 = RX streams - 1 (notes/06).")
add(0x93e5d2, F, "crTableApply", "Compressed CR table applier (descriptor + address + pool tables, notes/06).")
add(0x93e62a, F, "crTableAApply", "Applies table A (per-bandwidth columns t1 = 0/1/2) unless param_4 == 0xf0.")
add(0x93e706, F, "crTableBApply", "Applies table B: descriptors 0x0202a80c, 143 addresses at 0x0202aca8, pool "
    "0x0202aee4 (notes/06).")
add(0x93c724, F, "phySetSfoConst", "PHY_SFO_CONST 0x83086010 [27:16]/[11:0] = round(0x190000 / f_MHz), halved for "
    "'10 MHz' and quartered for '5 MHz' (MT7996 BN_6188); 0x8308f104 [27:16] = round(0x280000 / f) (notes/06).")
add(0x93c87e, F, "phySetSfoCfo", "PHY_WF0_SFO_CFO 0x8300d0ac [25:8] = round(0x1400000 / f_MHz) << s, s = 0/1/2/3 "
    "for 20/40/80/160 (80+80: 2); no narrow case (notes/06).")
add(0x93c9ac, F, "phySetAgcColumn", "AGC table port 0x830000dc: 48 entries 0x80 | idx<<16 | v, v = v20 + 48*bw "
    "(table 0x02029448); enums 0, 5, 6 use the 20 MHz column (notes/06).")
add(0x93ca3e, F, "phySetPrimCh2g", "PHY_WFCOMM_PRIMCH 0x830a0080 [7:4] = 2.4 GHz channel index (40 MHz: +-2; other "
    "widths 6); only for 2.4 GHz channel frequencies (notes/06).")
add(0x93cab0, F, "phySetPrimPos", "0x83088394 / 0x83088398 primary position (20: 0x00100300 / 0; narrow 0 / 0) "
    "(notes/06).")
add(0x93cc28, F, "phyStaticInit", "Static PHY init: TX tables 0x8308c000..c070 (+0x10000 band 1), 0x830a38xx, "
    "0x830ad4c4..; no bandwidth terms (notes/06).")
add(0x93db8a, F, "phySetBand2gRegs", "Writes 34 band registers 0x8308fc30..0x8308fcb4 (+0x10000 path 1) for 2.4 GHz "
    "(notes/06).")
add(0x93dbd4, F, "phySetBwClock", "Writes PHY_BN_MODE 0x83081000: [27:24] PHY BW class (0/1/2/7 = 20/40/80/160, "
    "masked to 0 for narrow), [30:28] = 0x5 for enum 6 ('10 MHz') and 0x7 for enum 5 ('5 MHz'), forcing [28] even at "
    "2.4 GHz. The hardware gives TX /4 at 0xd and /8 at 0xf: there is no /2 (notes/07). Per-path tail: A-die RF "
    "0xb00/0xb04 converter class (not a sample-rate control). The 2022 twin is FUN_93892c.")
add(0x941cfa, F, "wfsysSetTickcr0", "WFSYS_TICKCR0 0x820b00a0: [1:0] WLAN_CLK_MODE, [4:2] LMAC_1X_MODE when clk < 3. "
    "A MAC/LMAC tick, not the PHY clock: 0x5 at cbw 4, 0xa at cbw 5. With the narrow divider and WLAN_CLK_MODE = 0 "
    "the transmitter never ends a PPDU (notes/03, 05).")
add(0x941ce6, F, "aggSetBwClass", "AGG_BWCR 0x820e2004 [3:2] (GCI selector 0x20162): 0 for 20 (1 if SCO != 0), 1 "
    "for 40, 2 for 80, 3 for 160/80+80. MT7996's RATE BWCR role (notes/05).")
add(0x941d22, F, "macSetChannelWord", "Channel-set MAC routine (freq, ch, bw, p4, band): v = bw==4 ? 2 : bw==5 ? 1 : 0, "
    "wfsysSetTickcr0(v, v) -> TICKCR0 0xa (5 MHz) / 0x5 (10 MHz); aggSetBwClass (notes/05).")

# ---- MAC / power ----------------------------------------------------------------------------------------------
add(0x944072, F, "arbSetTxRx", "ARB_SCR 0x820e3000 fields 0/1: MAC TX/RX enables (bit 8 = TX off, bit 9 = RX off; "
    "RX must stay on or the ICAP front end freezes) (notes/03, 05).")
add(0x9443fc, F, "macIfsInit", "MAC IFS init; calls ROM FUN_828848 (notes/clock tree).")
add(0x94463a, F, "macSetSlotEifs", "Writes slot 9/20 and EIFS 0x54 through ROM macSetIfs.")
add(0x94bf08, F, "tmacSetDtr", "TMAC CDTR/ODTR 0x820e40c8 / 0x820e40cc via ROM FUN_8288d0.")
add(0x94c754, F, "tmacSetBandParam", "TMAC 0x820e4008 [8:0] = 0xbe (2.4 GHz) / 4 (5 GHz) on every switch.")
add(0x9397ea, F, "wfsysPowerSeq", "WF power sequence (0x7c005008 handshakes): mode 2 = wake (A-die RF 0xbc = 0x11, "
    "WFSYS_PWR_SEQ 0x820b00a8 [11:10] = 3), mode 3 = sleep (RF 0xbc = 1, [11:10] = 2); then RF 0x21c/0xcc4 PLL/VCO "
    "steps. Power states, not a clock-rate selection (notes/03, 07).")
add(0x947ff4, F, "wfsysPowerSeqHal", "HAL wrapper of wfsysPowerSeq: (0, 2) on wake, (0, 3) before sleep.")
add(0x939e60, F, "wfsysSetAc2", "0x820b00ac [2].")
add(0x9495f6, F, "dvfsSwitch", "MCU/bus DVFS set 0x81026008 ('Switch DVFS P%d FAIL'); not a PHY clock (notes/03).")
add(0x94bb3e, F, "sramProfileSelect", "Boot mode -> SRAM partition key -> PLE/PSE/ICAP profile (notes/08).")
add(0x94ba1e, F, "sramProfileApply", "Applies the PLE/PSE/ICAP partition profile for a key (ICAP mode: PLE 10, "
    "PSE 54) (notes/08).")

# ---- PHY counters, RA ------------------------------------------------------------------------------------------
add(0x936cc8, F, "phyReadRxCounters", "Reads the PHY stage counters 0x83081010.. ([31:16] OFDM | [15:0] CCK: PD, "
    "MDRDY, RxOk, SIG/SFD errors, SIG/TAG errors, FCS errors); enable via 0x83088234 |= 0x30000 and STSCNT "
    "0x83082004 (notes/03).")
add(0x9624f2, F, "phyReadRxCounters2", "Second reader of the PHY stage counters (notes/03).")
add(0x95fbe2, F, "raUpdateTxPhyCfg", "RA wrapper: recomputes per-station rates (raRateCalc); asserts "
    "prTxPhyCfg->BW <= 3 (a software range check, not a hardware gate).")
add(0x943268, F, "raRateCalc", "RA helper: PHY rate for (mode, rate, ..., bw).")

# ---- ATE: ContiTx and TX tone -----------------------------------------------------------------------------------
add(0x9308e2, F, "ateDispatch", "ATE command dispatcher (commands 10 = CW, 4/6 = continuous TX) (notes/07).")
add(0x930646, F, "ateContiTx", "ATE continuous-TX path -> stub 0x964d26 -> wrapper 0x943f76.")
add(0x964d26, F, "ateContiTxStub", "ATE stub towards the ContiTx wrappers.")
add(0x943f76, F, "ateContiTxWrap", "ContiTx wrapper: DLM struct +0 CtrCh, +1 CentCh, +2 BW, +3 Mask, +4 u16 Rate, "
    "+6 DBDC, +7 Mode (notes/07).")
add(0x943fae, F, "ateContiTxStartWrap", "Calls contiTxStart(DBDC, CtrCh, CentCh, BW, Mask, Rate, Mode, ...).")
add(0x943f9c, F, "ateContiTxStopWrap", "Calls contiTxStop(DBDC, Rate).")
add(0x968e52, F, "contiTxSaveRegs", "Saves 21 PHY registers (pointer/value table at DLM 0x0202ef04) before ContiTx.")
add(0x968e6a, F, "contiTxRestoreRegs", "Writes back the 21 registers contiTxSaveRegs saved (called by contiTxStop).")
add(0x968f2e, F, "contiTxStart", "ContiTx start (DBDC, CtrCh, CentCh, BW, Mask, Rate, Mode): BW 0/1/2/3 -> class "
    "0/1/2/7; copies the TX-vector latch PHY_TXV_LATCH0.. (0x8308a830..85c, the last TX vector) into the test-signal "
    "generator PHY_SIGGEN_CFG0.. (0x8308a008..a024, a040..a04c); PHY_BN_MODE = 0x80000000 | class<<24 (narrow bits "
    "cleared); 0x8308b000 = 0x44000000, 0x8308b004 = 0x30000000, 0x830ac004 = 0x16011400, 0x8308b020 = 0x100000 when "
    "Rate has no mode bits, 0x8308b024 = 0x10 (OFDM), 0x83081044 = 0x10 then 0x30, 0x830ad4d8 = 0x101f1f10. The "
    "decompilation stops at an ifcall9 (0x9690da); the disassembly continues: 0x8300c000/0x8301c000 bit 31 cleared, "
    "0x83080004/08 = 0x38, 0x830ac054 = 0x10, PHY_SIGGEN_START = 0x80000000. Replayed by hand (tools/guest/"
    "contitx.sh), this generator emits an unmodulated LO carrier: a calibration source, not a PPDU source (notes/07).")
add(0x968e82, F, "contiTxStop", "ContiTx stop (DBDC, Rate): PHY_SIGGEN_START = 0, 0x8300d008/0x8301d008 = 0x1d10, "
    "0x83081044 = 0, 0x830ac004 = 0x16151400, 0x8308b000 = 0xcc000000, 0x830ad4d8 = 0, 0x8300c000 bit 31 set; the "
    "decompilation stops at an ifcall9, the disassembly continues: 0x83080004/08 = 0, 0x8308b004 = 0, "
    "PHY_RST_CTRL = 0x38048, contiTxRestoreRegs. Stop it before any rig reset (notes/07).")
add(0x9691d2, F, "txToneStart", "TX tone (TxTone): (chain, type, FREQ, ...). type 0 FREQ 0 = DC tone whose I/Q is "
    "PHY_TONE_DC_IQ 0x8308fc08 (I [11:0], Q [27:16]): a live linear IQ DAC, at most 4.2 M writes/s from the MCU "
    "(fw hk_dacplay, hk_wrspeed; notes/09, 07). Forces the band clocks (0x83080004/08 = 0xffffff).")
add(0x969126, F, "txToneStop", "Stops the tone generator; does NOT restore stock values (leaves 0x83080004/08 = 0, "
    "0x8300c000 = 0x802741a0, 0x8300d008 = 0x1d00, ...). Stop before any rig reset, then reload (notes/07).")
add(0x96951c, F, "txTonePowerWf0", "Tone power override 0x8300c508 [31] + [23:16] (path 0).")
add(0x969534, F, "txTonePowerWf1", "Tone power override 0x8301c508 [31] + [23:16] (path 1).")

# ---- ICAP -------------------------------------------------------------------------------------------------------
add(0x96b96c, F, "icapSetMode", "ICAP SRAM mode m: ROM 0x832238 sets CR10 [28:26] = 0 / 7, ROM 0x832262 CR10 [31] = m; "
    "mode 0 = ring in the packet SRAM at 0xe0200000 (units 0..0xfffc), mode 1 = banks lent to ICAP "
    "(0x88009004 [27:24] = 0xf, units 0x20000..0x23ffc, not host-visible) (notes/08).")
add(0x96b6cc, F, "icapHwStart", "ICAP start: EN = 1, [7:4] = 0xf, SRC_SEL, SWITCH = 0, RING (notes/08).")
add(0x96be4e, F, "icapRingStart", "ICAP ring bounds (start) per mode (notes/08).")
add(0x96be5c, F, "icapRingEnd", "ICAP ring bounds (end) per mode (notes/08).")
add(0x96be72, F, "icapLendBanks", "0x88009004 [27:24] = 0 / 0xf: lend SRAM banks to ICAP (MT7996 cap_route GRANT_B). "
    "HAZARD: never write 0x88009004 [23:0] (notes/08).")
add(0x96be9a, F, "icapClearOffsets", "Zeroes ICAP CR11 (SYSRAM_OFFSET) and CR12 (PSE_END_ADDR) before every capture.")
add(0x96c276, F, "icapReadout", "Readout: beat b at 0xe0200000 + 16b in mode 0 (notes/08).")
add(0x96c61c, F, "icapSelectDomain", "ICAP capture domains: 4 = BirriaSDR's (MODE 0x13, bit 23), 1 = MODE 0x14 "
    "(0x830ad448 [15:8] SEL, [19:18] rate, bit 24), 2 = MODE 0x20/0x21 (0x830ad44c) (notes/03).")
add(0x96b672, F, "icapHelper1", "ICAP helper -> FUN_96c546 (sets |= 0x40).")
add(0x96b67c, F, "icapHelper2", "ICAP helper -> FUN_96c554 (sets |= 0x40).")

# ---- GCI accessor table (DLM 0x02013800.., filled by ROM gciAccessorInit; notes/05) -------------------------------
for off, op, what in [(0x00, "GetB", "get byte (sel, *out)"), (0x04, "GetW", "get word (sel, *out)"),
                      (0x08, "GetD", "get dword (sel, *out)"), (0x0c, "Set", "set (sel, value): one read-modify-write"),
                      (0x1c, "SetTriples", "varargs triples"), (0x20, "SetPairs", "varargs (n, sel, val, ...)"),
                      (0x28, "TxnSetPairs", "transaction varargs (n, sel, val, ...)"),
                      (0x84, "GetModuleIfc", "GetChipModuleIfc(module)")]:
    add(0x02013800 + off, "label", f"gci{op}_p", f"GCI accessor pointer: {what}. tools/gci_comment.py decodes the "
        "selectors at every call (module<<16 | group<<5 | field, work/crmap/crmap.json)")

# ---- registers --------------------------------------------------------------------------------------------------
R = [
    # band 0 PHY (0x8308xxxx); band 1 is +0x10000
    (0x83080000, "PHY_BN_CTRL", "[12] '20 MHz only' flag (1 at 20, 0 otherwise); [5:4] chain index"),
    (0x83080004, "PHY_BN_CLKFORCE0", "band clock forces [23:0]: tone 0xffffff, ContiTx 0x38, tone stop 0"),
    (0x83080008, "PHY_BN_CLKFORCE1", "band clock forces [23:0] (second word)"),
    (0x83080014, "PHY_RST_CTRL", "logic resets, 1 = held: [17] TXFD [16] TXTD [15] TXFE ... [3] FFT [2] RXTD [1] RXFE. "
     "HAZARD: [17:0] holds PHY blocks in reset"),
    (0x83081000, "PHY_BN_MODE", "[31] narrow data path, [30] TX /4, [29] extra /2 (with [30] and [28]), [28] band, "
     "[27:24] PHY BW class (0/1/2/7 = 20/40/80/160; 4/5 RX /2 behind the FE and TX /8 when [30] = 0), [22:20] primary "
     "index. TX only ever runs /1, /4 or /8: no /2 (notes/07)"),
    (0x83081010, "PHY_CNT_PD", "packet detects [31:16] OFDM | [15:0] CCK"),
    (0x83081014, "PHY_CNT_MDRDY", "header decoded / RX vector ready [31:16] OFDM | [15:0] CCK"),
    (0x83081018, "PHY_CNT_RXOK", "frames handed to the MAC (bad FCS included)"),
    (0x8308101c, "PHY_CNT_CCKERR", "[31:16] CCK SIG errors | [15:0] SFD errors"),
    (0x83081020, "PHY_CNT_OFDMERR", "[31:16] OFDM SIG errors | [15:0] TAG errors"),
    (0x83081024, "PHY_CNT_FCSERR", "[31:16] OFDM | [15:0] CCK FCS errors"),
    (0x83081044, "PHY_TXGEN_CTRL", "ContiTx writes 0x10 then 0x30; stop writes 0"),
    (0x83082004, "PHY_STSCNT_CTRL", "counter enable: [11:9] STSCNT_EN (0 -> 5 edge clears), [2:0]"),
    (0x83086010, "PHY_SFO_CONST", "[27:16]/[11:0] = round(0x190000 / f_MHz), /2 for '10 MHz', /4 for '5 MHz' "
     "(MT7996 BN_6188)"),
    (0x83088014, "PHY_DEMOD_TIMING", "[31:24] stock 0xea; 0x75 revives SIGNAL decoding behind the class-4/5 /2 "
     "(20 MHz frames only)"),
    (0x83088230, "PHY_STSCNT_RST", "[29] counter reset pulse"),
    (0x83088234, "PHY_STSCNT_EN2", "|= 0x30000 enables the stage counters"),
    (0x8308a004, "PHY_SIGGEN_START", "band test-signal generator: 0x80000000 = run (ContiTx), 0 = stop; MT7996: "
     "(n + 0x30400000) << 2"),
    (0x8308a008, "PHY_SIGGEN_CFG0", "generator configuration words 0x8308a008..a054 (ContiTx copies the TX-vector "
     "latch into a008..a024 and a040..a04c)"),
    (0x8308a830, "PHY_TXV_LATCH0", "latch of the last TX vector (0x8308a830..85c, 12 words): word0 [10:8] BW, [15:12] "
     "mode (1 OFDM, 2 HT-MF, 3 HT-GF), word4 symbol count, word8 legacy rate"),
    (0x8308b000, "PHY_TXTEST_CTRL0", "TX test control: 0x44000000 ContiTx start, 0xcc000000 stop"),
    (0x8308b004, "PHY_TXTEST_CTRL1", "0x30000000 during ContiTx"),
    (0x8308b020, "PHY_TXTEST_SEL", "0x100000 when the ContiTx rate has no mode bits"),
    (0x8308b024, "PHY_TXTEST_MODE", "0x10 for OFDM ContiTx; table value for HT/VHT modes"),
    (0x8308f060, "PHY_RXFE_60", "RX front end word (AFIFO side)"),
    (0x8308f064, "PHY_RXFE_CTRL", "[7:4] code, [1:0]; stock 0x01010000; no narrow decode with any code (notes/03)"),
    (0x8308f068, "PHY_RXFE_01", "WF_RXFE_01: [1:0] MAN_DOWNSAMP, [3] AFIFO_CK320M, [6:3] DLPF manual, [16] ADC480M, "
     "[15:12] WB_SEL, [27:26] RXFDIQ_TAP_NUM; live 0x08000000"),
    (0x8308fc00, "PHY_TONE_CTRL", "band tone generator control (bins, enables)"),
    (0x8308fc04, "PHY_TONE_CTRL1", "tone generator (0x100 at stop)"),
    (0x8308fc08, "PHY_TONE_DC_IQ", "DC tone I [11:0] / Q [27:16]: a live linear IQ DAC (MCU stores 238 ns each, "
     "4.2 M/s)"),
    (0x8308fc0c, "PHY_TONE_LATCH", "[0] latch pulse: knocks the generator off the carrier, do not use"),
    (0x8308fc20, "PHY_TONE_FC20", "tone generator word (0 at stop)"),
    (0x83091000, "PHY_BN1_MODE", "band 1 BN_MODE (ContiTx/tone with DBDC = 1)"),
    (0x8309a004, "PHY_BN1_SIGGEN_START", "band 1 test-signal generator start"),
    (0x8309b000, "PHY_BN1_TXTEST_CTRL0", "band 1 TX test control"),
    # per chain (0x8300xxxx WF0, 0x8301xxxx WF1)
    (0x830000dc, "PHY_WF0_AGC_PORT", "AGC table write port: 0x80 | idx<<16 | v (48 entries)"),
    (0x83007254, "PHY_WF0_TRX_CLK", "[5:4] TX-DAC clock on, [7:6] RX-ADC clock on (enables only, no rate)"),
    (0x8300c000, "PHY_WF0_TSSI_CTRL", "TSSI/power-control word; bit 31 cleared by ContiTx/tone, stock 0xc0c80806"),
    (0x8300c508, "PHY_WF0_TONE_PWR", "[31] tone power override, [23:16] power"),
    (0x8300d008, "PHY_WF0_TXPATH", "antenna/TX path select (ContiTx |= 0xc0; 0x1d10 after stop, 0x1d00 after tone stop)"),
    (0x8300d06c, "PHY_WF0_DEMOD_CLASS", "[6:3] one-hot demodulator class (bit 5 at 20 MHz); bits 3/4 never set"),
    (0x8300d0ac, "PHY_WF0_SFO_CFO", "[25:8] SFO-from-CFO constant round(0x1400000 / f_MHz) << s"),
    (0x8301c000, "PHY_WF1_TSSI_CTRL", "path 1 twin of PHY_WF0_TSSI_CTRL"),
    (0x8301c508, "PHY_WF1_TONE_PWR", "path 1 twin of PHY_WF0_TONE_PWR"),
    (0x8301d008, "PHY_WF1_TXPATH", "path 1 twin of PHY_WF0_TXPATH"),
    # PHY top / WF_COMM
    (0x830a0080, "PHY_WFCOMM_PRIMCH", "[7:4] 2.4 GHz channel index"),
    (0x830ac004, "PHY_TXTEST_TOP", "0x16011400 during ContiTx, 0x16151400 after"),
    (0x830ac054, "PHY_TXTEST_TOP54", "0x10 (band 0) / 0x30 (band 1) during ContiTx"),
    (0x830ac058, "PHY_TXTEST_TOP58", "1 during ContiTx"),
    (0x830ad4d8, "PHY_TXGEN_GAIN", "0x101f1f10 during ContiTx; 0x1010101 after tone stop"),
    # WFSYS_ON
    (0x820b00a0, "WFSYS_TICKCR0", "[1:0] WLAN_CLK_MODE, [4:2] LMAC_1X_MODE: 0 / 0x5 / 0xa at 20 / 10 / 5 MHz. MAC "
     "tick (TX end, slot, SIFS), not the PHY clock. Only TICKCR0 [4:0] may be written in WFSYS_ON"),
    (0x820b00a8, "WFSYS_PWR_SEQ", "[11:10] = 3 wake / 2 sleep (wfsysPowerSeq). HAZARD: do not write"),
    # LMAC
    (0x820e3000, "ARB_SCR", "[8] MAC TX off, [9] MAC RX off"),
    (0x820e2004, "AGG_BWCR", "[3:2] MAC bandwidth class (MT7996 RATE BWCR role)"),
    (0x820e4008, "TMAC_BANDPARAM", "[8:0] = 0xbe (2.4 GHz) / 4 (5 GHz)"),
    (0x820e40c8, "TMAC_CDTR", "[31:16] / [15:0] (ROM FUN_8288d0)"),
    (0x820e40cc, "TMAC_ODTR", "[31:16] / [15:0] (ROM FUN_8288d0)"),
    # MCU / CONN
    (0x89020068, "MCU_US_TIMER", "free-running 1 MHz counter"),
    (0x81026008, "MCU_DVFS", "MCU/bus DVFS divider set (dvfsSwitch)"),
    (0x88009004, "PSE_ICAP_GRANT", "[27:24] = 0xf lends SRAM banks to ICAP. HAZARD: never write [23:0]"),
    (0x7c003004, "CONN_AFE_XTALMODE", "per-crystal codes: [1:0] = 2 and [7:6] = 1 for 24 and 40 MHz, 3/3 for 25, "
     "3/1 for 26; [3:2] = [17:16] = 3. Not a divider. HAZARD: do not write (clock-tree root)"),
    (0x7c0030f4, "CONN_AFE_XTALCNT", "crystal-cycle counts: [14:0] = 50.5 x f_MHz, [30:16] = 30.3 x f_MHz"),
    (0x7c005004, "CONN_WF_PWRCTL", "WF power handshake control (wfsysPowerSeq). HAZARD: leave alone"),
    (0x7c005008, "CONN_WF_PWRSTS", "WF power handshake status bits polled by wfsysPowerSeq"),
]
R += [
    # ICAP (0x80021000, MT7996 ICAPCR0..12, notes/08)
    (0x80021090, "ICAP_CR0", "control: [0] EN, [1] CAPTURE, [3:2] TIME_ADJ, [7:4] 0xf, [10] SW_RST_B, [13] RING, "
     "[17] WRAP, [22:20] SRC_SEL, [23] SWITCH (0x24f3 ring start, 0x4f0 stop)"),
    (0x80021098, "ICAP_CR2", "ADDR0_START (4-byte units)"),
    (0x8002109c, "ICAP_CR3", "ADDR0_END (4-byte units, inclusive)"),
    (0x800210a4, "ICAP_CR5", "stop cycle"),
    (0x800210b0, "ICAP_CR8", "[0] band, [27] FPGA_LOWRATE"),
    (0x800210b4, "ICAP_CR9", "live write pointer"),
    (0x800210b8, "ICAP_CR10", "[19:16] input packers (2 = uniform 10 Msps, 8 = 30 Msps, 0 = 40 Msps); [28:26], [31] "
     "SRAM-interface selects"),
    (0x800210bc, "ICAP_CR11", "SYSRAM_OFFSET (zeroed before every capture)"),
    (0x800210c0, "ICAP_CR12", "PSE_END_ADDR (zeroed before every capture)"),
]
for ch in range(4):
    b = 0x80010000 + 0x100 * ch
    R += [
        (b + 0x00, f"AXI_DMA{ch}_STS", "[0] busy/enable status (ROM AXI DMA driver, mcu/driver/axi_dma.c; unused by the "
         "RAM firmware)"),
        (b + 0x18, f"AXI_DMA{ch}_CTRL", "control word: [29:28], [25:24] 2-bit fields, [20], [18:16], [15], [14:5] "
         "count, [1], [0] (axiDmaConfig)"),
        (b + 0x1c, f"AXI_DMA{ch}_ADDR0", "first address (axiDmaConfig cfg[1])"),
        (b + 0x20, f"AXI_DMA{ch}_ADDR1", "second address (axiDmaConfig cfg[2])"),
        (b + 0x24, f"AXI_DMA{ch}_LEN", "length (axiDmaConfig cfg[3])"),
    ]

import sys
which = sys.argv[1] if len(sys.argv) > 1 else "ram"
if which == "regs":
    for a, n, c in R:
        print(f"{a:#010x}\t{REG}\t{n}\t{' '.join(c.split())}")
else:
    for a, k, n, c in E:
        print(f"{a:#010x}\t{k}\t{n}\t{c}")
