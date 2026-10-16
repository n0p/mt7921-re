# ROM register-field layer (GCI) and the WLAN/LMAC clock-mode register (static analysis, 2026-10-15)

## The layer exists (same design as MT7996's "GCI")
- Selector = module << 16 | group << 5 | field. Each module has a GetCRControlParam function that bounds-checks the
  group (`slti a4, N`), reads an 8-byte group record {u32 field_table, u16 reg_offset, u8 n_fields, u8 pad} and a
  field table of {u8 lsb, u8 msb}, then adds the module's base (chosen by module id: band 0/1, path 0/1).
- Module interface array: DLM `0x02014f04 + 4*module` (0x5c modules), filled by ROM `FUN_00826aac(module, &ifc)`
  from per-module init functions. ifc = {GetCRControlParam, aux}.
- Accessor table (MT7996 "FIT" equivalent): DLM `0x02013800..0x0201388c`, filled by ROM `FUN_0082693a`:

  | slot | fn | role |
  |---|---|---|
  | +0x00 / +0x04 / +0x08 | 0x826c7e / 0x826c90 / 0x826ca2 | Get byte / word / dword (sel, *out), via FUN_00826c22 |
  | +0x0c | 0x826b70 | Set(sel, val): one read-modify-write |
  | +0x1c | 0x826cde | VaArgs triples (n % 3 == 0) |
  | +0x20 | 0x826b7a | VaArgs pairs (n, sel, val, ...): one read-modify-write per pair (MT7996 "+0x20 non-transaction setter") |
  | +0x28 | 0x826bb0 | transaction VaArgs set |
  | +0x84 | 0x826860 | GetChipModuleIfc(module) = *(gp+73476 + 4*module) |

- Full dump: `data/crmap/crmap.json`, `data/crmap/crmap.txt` (36 tables, 1114 registers, 3923 fields; table
  0x84aa6c..0x84f388, functions 0x826e0e..0x83afca). All 280 GCI calls in the 2026 RAM firmware, with decoded
  selectors: `data/crmap/ram_gci_calls.txt`.
- Module bases (band 0 / band 1 or path 0 / path 1):
  - LMAC: ARB 0x00/01 → 0x820e3000/0x820f3000; AGG 0x02/03 → 0x820e2000/0x820f2000; WF_DMA 0x04/05;
    ETBF 0x0a/0b; WF_CFG 0x0c/0d → 0x820e0000/0x820f0000; RMAC 0x12/13; TMAC 0x17/18; MIB 0x1a/1b;
    LPON 0x1d/1e; WF_INT 0x1f/20; WTBLOFF 0x28/29.
  - Other MAC-side blocks: WF_PF 0x11; PLE/PSE 0x14/15; **WFSYS_ON 0x1c → 0x820b0000**; 0x22 → 0x820ca000;
    MDP 0x23 → 0x820cd000.
  - PHY: 0x24/25 → 0x83080000/0x83090000; 0x26/0x27 → 0x830a0000; per-chain 0x2a..0x31 → 0x83000000/0x83010000;
    0x3a..0x47 → 0x830a0000/0x830b0000; 0x48..0x59 → 0x83080000/0x83090000.
  - Top/infra: ICAP 0x5a → 0x80021000; WF_TOP_MISC_ON 0x21 → 0x81021000 (only OSC_MASK 0x81021104 and
    CONN_SLP_CTL 0x81021000[0]); CONN_INFRA 0x5b → 0x7c00a000.
- Unregistered ids: 6–9, 0x0e–0x10, 0x16, 0x19, 0x32–0x39, 0x42–0x45, 0x50, 0x51.
- Sanity checks: 0x83081000 is module 0x48, group 30, with 13 fields: [0] [1] **[30:28]** [27:24] [6] [31]
  [11:8] [3:2] [15:12] [5:4] [7] [22:20] [18:16]. 0x80021090 (ICAP control) is module 0x5a, group 0, with 21 fields.

## MT7921's WFSYS_ON is at 0x820b0000 (not 0x81030000), and TICKCR0 is 0x820b00a0
- The module 0x1c field layout matches MT7996 WFSYS_ON bit for bit:
  - 0x820b0000 / 0x820b0010: LPCR status [31,29,28,27,(26)];
  - 0x820b0004/08 and 0x820b0014/18: LPCR_EN/DIS [31] B0_VALID, [30] LP0_OWN, [1] DIGCK, [0] DSLEEP_REQ_B,
    [4] PLL_CKEN, [21] SX_ZPS_MODE;
  - selector band stride 0x60 (3 groups). ROM low-power code polls 0x820b0000 [30]/[29]/[27] and sets 0x820b0008 [30].
- **0x820b00a0 (module 0x1c, group 10)**:
  - field 0 = [4:2] LMAC_1X_MODE (selector 0x1c0140);
  - field 1 = [1:0] WLAN_CLK_MODE (selector 0x1c0141).
  - Only a band-0 copy exists (the MT7996 layout repeats it per band).
- Writer `FUN_00941cfa(clk, lmac)` (2026; 2022: FUN_0093f332; same code also in the ROM image): when clk < 3 and
  lmac < 5, it calls slot +0x20 (4, 0x1c0141, clk, 0x1c0140, lmac), i.e. WLAN_CLK_MODE first, then LMAC_1X_MODE.
  This is the same order and slot as MT7996.
- Caller `FUN_00941d22(freq, ch, bw, p4, band)` is the channel-set MAC-word routine (MT7996 V:e00e549a equivalent):
  - `v = bw==4 ? 2 : bw==5 ? 1 : 0` then `FUN_00941cfa(v, v)` → **TICKCR0 = 0xa for internal bw 4 (5 MHz),
    0x5 for internal bw 5 (10 MHz), 0 otherwise**;
  - AGG 0x820e2004[3:2] (selector 0x20162 + band<<16, FUN_00941ce6) = 2 for bw 1 (80), 3 for bw 2/3 (160, 80+80),
    else (p4 != 0). This is a MAC bandwidth class (MT7996 writes RATE BWCR = 1 "as 40" for 10 MHz, 0 for 5 MHz);
    MT7921 writes 0/1 from p4 for narrow;
  - RMAC 0x820e5090[7:0] = control channel (selector 0x1200c1 + band<<16), as MT7996's CHFREQ.
- Callers of FUN_00941d22: rlmPhySetChannel 0x963f6c (inside `if (FUN_00942672(...) == 2)`, i.e. on a real
  channel change) and FUN_0096314e. Both pass the internal bw from FUN_0094252e(3, band).
  **So our CHANNEL_SWITCH cbw 4/5 tests most likely already ran with TICKCR0 = 0x5/0xa.** Not yet verified on
  hardware: 0x820b0000 was never in a snapshot page.
- Other firmware writes in the block:
  - FUN_009397ea: power/PLL sequence (0x7c005008 handshakes, A-die 0xbc, 0x820b00a8[11:10] = 3 or 2 by mode,
    0x820b0024/28/34/38);
  - FUN_00939e60: 0x820b00ac[2];
  - FUN_e027b034: 0x820b002c bits 21/23/27/29;
  - ROM FUN_0082c2d6: low-power timers 0x820b00a4[11:0], 0x820b00c0..d4 from a config struct.
  - None of these is bandwidth dependent.

## Channel-set GCI writes (2026 RAM, reachable from rlmPhySetChannel / FUN_0096314e)
- TICKCR0 0x820b00a0 (above).
- AGG 0x820e2004[3:2] (bandwidth class).
- RMAC 0x820e5090[7:0] (channel).
- TMAC 0x820e4008[8:0] = 0xbe (2.4 GHz) / 4 (5 GHz), via FUN_0094c754.
- ARB 0x820e3000 fields 0/1 (TX/RX enables, FUN_00944072).

Every other bandwidth-dependent PHY write (0x83081000 bits 30:28/27:24, 0x83086010, 0x8308xxxx, 0x8300d0ac, A-die
0x160/0xb00/0xb04) is a direct store in FUN_0093dbd4 / FUN_0093c8xx, not GCI. Of the registers that differ between
20/40/80 MHz (data/phydump/diffbw.txt), only 0x83081000, 0x820e3150 and 0x820ed02c are in the GCI tables.
