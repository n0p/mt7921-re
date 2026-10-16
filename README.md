# MT7921AU narrowband: firmware RE, IQ capture, 5 / 2.5 MHz mesh and 802.11p nodes

This project turns an MT7921AU USB Wi-Fi card (MT7961 D-die + MT7972 A-die, NDS32 WM MCU) into:
- a narrowband (5 or 2.5 MHz) long-range mesh node, for 2.4 GHz ISM spectrum or the amateur bands;
- an 802.11p receiver.

It covers reverse engineering and instrumenting the Wi-Fi firmware, a firmware IQ streamer, software OFDM
receivers, and the node programs. Details are in [notes/](notes/); this page is the summary. Status as of 2026-10-16.

## Hardware capabilities (what we found)
**Firmware**
- The WM RAM firmware can be modified. There is no signature, only a per-region LFSR download checksum, which
  `tools/mtfw_patch.py` fixes (notes/02).
- C hooks run from free SRAM. A debug command gives MCU-side reads, writes and calls of anything (notes/03).
- Never load a 2025+ ROM patch (eFuse gate).

**Tuning**
- A frequency override in the driver's CHANNEL_SWITCH command tunes the synthesizer anywhere in 2300–2500,
  4900–5925 and 5925–7125 MHz (notes/04).
- Checked by RF readback at 2350, 2450, 5180, 5890 and 5900 MHz. 6 GHz is untested.

**Transmit**
- The card transmits narrow OFDM with hardware clock dividers: **÷4 = 5 MHz** and **÷8 = 2.5 MHz** PPDUs
  (`PHY_BN_MODE 0x83081000 [30:28]`).
- **There is no ÷2, so the card cannot transmit 10 MHz (802.11p/bd).** It cannot play back IQ either: the tone
  generator's DC word works as a slow DAC, up to 1.45 Msps, and collapses unpredictably (notes/07, 09).
- **The MAC tick `WFSYS_TICKCR0` decides when a PPDU ends**:
  - 0xa (5 MHz timing) is needed for whole 5 MHz frames. The channel switch leaves 0x5, which cuts frames
    longer than ~260 bytes.
  - No tick is slow enough for ÷8: a 2.5 MHz PPDU of N symbols keeps ~30 + N/2 of them (notes/10 §5).
- At ÷4 the card applies every coding it has: legacy BCC; HT with LDPC, short GI and STBC over 2 chains; VHT; HE-SU
  with LDPC, DCM and long GI/LTF; HE-ER-SU (checked through the SIG fields' CRCs, notes/10 §1).
- MediaTek's ContiTx block is only a calibration tone source.

**Receive**
- The demodulator has fixed 20 MHz numerology, so no narrow frame is ever demodulated in hardware (notes/03, 06).
- The capture block (ICAP) records the front-end IQ at 40 Msps. Its sample packers give a continuous **10 or
  30 Msps**.
- A firmware hook streams the capture over USB (80 / 120 MB/s, up to ~150 MB/s), and the host demodulates it
  (notes/08, 09).
- The receive gain must be set by hand: the AGC hunts on noise. A neighbour 10 cm away needs the lowest gain.

**Quirks**
- **Half-duplex only:** the capture overwrites the packet SRAM that carries MCU commands and TX frames, so a node
  transmits only between capture slices.
- A neighbour's near-field bursts can latch a MAC TX deferral that only a re-tune clears (notes/10 §6).
- One rig card (B) sometimes wedges its USB control path (notes/09).

## Main programs
- **`nbmesh`** (`tools/nbnode`, Rust, std only, for embedded hosts): a 5 or 2.5 MHz mesh node on one card. It
  serves a **TUN or TAP** interface (`--mode`):
  - the card transmits, with its own PHY coding (`--profile`);
  - the host receives the IQ stream and decodes it;
  - packets get Reed-Solomon erasure FEC (`--fec K,M`, default 4 data + 2 parity);
  - it alternates capture slices and TX windows, and recovers TX stalls and card resets by itself;
  - the default frequency is **2450 MHz (2.4 GHz ISM)**. It transmits on any `--freq` the card tunes: choosing a
    frequency you may use is up to the operator.
- **`p11node`** (`tools/nbnode`, Rust): an IEEE 802.11p receiver on the ITS channels 172–184 (5.9 GHz).
  - It streams IQ at 30 Msps, decodes at 10 Msps, and delivers frames to a TAP/TUN interface (e.g. GeoNetworking
    for an ITS stack), a pcap and stdout.
  - Receive only, since no transmitter of this chip makes 10 MHz frames.
- **`tools/guest/nbprep.py`**: prepares a card by hand. It tunes, sets the MAC tick and the divider, programs the
  capture and gain, sets the PHY coding profile and creates a monitor interface for injection; it also has
  `--status`, `--release` and `--rx-test`. Default 2450 MHz.
- **Instrumented firmware** (`fw/`):
  - the stock 2026 RAM image with C hooks: the debug command, the IQ streamer, the tone DAC and measurement helpers;
  - assembly patches (a dedicated ICAP SRAM partition; optional out-of-band tuning through the sniffer config);
  - `make install` loads it on both cards.
- **`mt7921dbg`** (`tools/guest/mt7921dbg`), a kernel module next to mt76, provides:
  - debugfs access to the debug command and raw MCU commands;
  - the IQ stream collector (one ring per card);
  - TXD overrides for injected frames (rate, coding fields, one attempt);
  - a narrow RX-vector fix-up.
- **Software receivers:**
  - `tools/sdr/rx80211.py` (numpy, with a C Viterbi in `vit.c`) decodes 802.11a/p OFDM at any width. It is the
    reference.
  - The Rust port in `tools/nbnode` (`rxfile` CLI) is byte-identical to it on every test vector.
- **`tools/guest/nbrxd.py`**: the Python prototype of the node (TAP, pcap, transmit-only and receive-only modes,
  test modes). It is kept for experiments; `nbmesh` replaces it for deployment.
- **Commented decompilation** (`tools/annotate.sh` + `tools/ghidra`):
  - a Ghidra project for the NDS32 firmware, with names, register glossaries and comments from the notes;
  - exported as `work/decomp/all.c` (RAM) and `rom_all.c` (boot ROM); look a function up with
    `tools/fshow.sh 0xADDR`.

## Quick start
```sh
make -C fw && make -C fw install            # build the instrumented firmware and load it on both cards (guest)
tools/g.sh /root/fwre/rigreset.sh           # clean rig state
(cd tools/nbnode && cargo build --release)  # nbmesh, p11node, rxfile; copy them to the card's host (/root/fwre)
# a mesh node at the default 2450 MHz (2.4 GHz ISM), 5 MHz, FEC 4+2, IP on TUN nb0
./nbmesh wlx00c0cab7962a --addr 10.42.0.1/24
# on this rig (cards 10 cm apart; test transmissions at 2350 MHz under the operator's amateur licence)
tools/g.sh '/root/fwre/nbmeshtest.sh 2350 40 "--gain 0,0,0"'      # two nodes, ping A -> B
tools/g.sh 'cd /root/fwre && ./p11node wlx00c0cab7962a --channel 180 --pcap /tmp/its.pcap'
tools/g.sh 'cd /root/fwre && python3 nbprep.py wlx00c0cab7962a --status'
tools/rigrecover.sh                         # after a card wedge: USB reset, VM reset, firmware reload
```
`tools/nbnode/README.md` describes the Rust programs; `nbmesh --help` and `p11node --help` list every option.

## Results (measured on the rig at 2350 MHz unless noted)
| Test | Result |
|---|---|
| X410 → card A, 5 MHz frames, IQ at 10 Msps, nbmesh receiver | 4775/4775 |
| X410 → card A, 802.11p 10 MHz, IQ at 30 Msps, p11node | 6702/6702, delivered to a TAP as GeoNetworking |
| nbmesh, two nodes, 5 MHz, ping | 54/60 with FEC 4,2 (48/60 without); 59/60 with 1300-byte pings |
| nbmesh TAP mode | 36/40, ARP over the link |
| 2.5 MHz frames up to 1608 bytes (length-dependent padding) | 26 of 27 frames arrived, every inner CRC intact |
| Receiver sensitivity, BPSK 1/2 at 10% PER | 4 dB SNR with the default ±1 channel smoothing, 3 dB with ±2 (notes/10 §2) |
| Rust receiver, one laptop core | 1.6× real time on a fully loaded 10 Msps channel, 3.2× at 5 Msps |
| nbrxd prototype: UDP card → card, one way / both ways | 97–98/100 / 81% and 74% (Poisson ~5/s each way) |
| Card A stability | 5 min continuous RX (23.7 GB), ~800 TX/RX cycles: 0 failures |

## Limitations
- **No 10 MHz in either direction:** the silicon has no ÷2 transmitter and no narrow demodulator. 802.11p/bd is
  receive-only through IQ. The card's 5 MHz PPDU is a valid quarter-rate 802.11a frame. Interop with ath9k's 5 MHz
  mode is expected but untested.
- **Half-duplex, no MAC protocol:**
  - delivery drops with load and collisions;
  - the packet FEC repairs losses without retransmission (it halved the round-trip loss);
  - a random-access layer is the next software step.
- **2.5 MHz:**
  - long frames need about twice their airtime in padding;
  - PPDUs longer than ~17.5 ms were never seen to leave the card;
  - nbmesh caps the MTU at ~830, so there is no IPv6 at that width.
- **The receivers decode legacy (BCC) rates only.** HT/HE reception (STBC, LDPC, HE-ER) is the plan in notes/10 §4.
- **Tone-DAC collapse:** the power-control loop is involved (notes/09 §5b).
- **The rig:**
  - a QEMU guest on the lab host: 12 vCPUs, one emulated xHCI per card, pinned interrupts (lorien `~/vm23.sh`);
  - one card at 80 MB/s costs about one vCPU of USB emulation;
  - card B's USB wedges are not explained (notes/09).

## Hazards (learned the hard way)
- Never load a 2025+ ROM patch (eFuse gate).
- MCU reads of 0x8103xxxx or of unmapped/granted memory hang the MCU.
- Host reads at or after 0xe0300000 reset the card.
- Don't touch CONN AFE 0x7c003xxx or 0x88009000/04 [23:0]/08; in WFSYS_ON only TICKCR0 [4:0] may be written.
- Avoid host reads of the PSE.
- Stop the tone generator (`FUN_00969126`) and ContiTx (`FUN_00968e82`) before any rig reset.
- MCU commands during a capture slice, and a TX frame left queued when a slice starts, both break the card's command
  or TX path until a reset.
- Transmitting is the operator's responsibility: the tools impose no band limits. This rig's test transmissions stay
  at 2350 MHz (amateur licence).

## Repository map (what file is what)
Not in git (`.gitignore`): the local `work/` directory, the firmware build outputs `fw/build/` and `fw/out/`,
and binaries of any kind (firmware images, memory dumps, IQ captures). "Rebuilding work/" below says how to
recreate them.

### Top level
| file | what it is |
|---|---|
| `README.md` | this page |
| `informe_mt7921.md` | the previous agent's research report (Spanish); superseded, kept for reference |
| `.gitignore` | what stays out of git (see above) |
| `patches/0001-mt7921-force_cbw-experiment.patch` | mt76 patch adding a `force_cbw` parameter, for experiment 1 (`tools/guest/exp1_narrowbw.sh`) |
| `patches/birriasdr-mt7921-rig.patch` | the rig's BirriaSDR changes (any LO from 2.3 to 2.5 GHz, `BIRRIA_FREQ`, the 0e8d:7961 USB ID), on upstream commit 80ec6fc |

### notes/
| file | topic |
|---|---|
| [01](notes/01-firmware-layout-and-bw.md) | firmware layout and the channel-bandwidth path |
| [02](notes/02-integrity-and-patch-strategy.md) | firmware integrity (download checksum, no signature) and how to load modified firmware |
| [03](notes/03-instrumentation-and-narrowband.md) | instrumented firmware and the narrowband investigation (chronological log) |
| [04](notes/04-2g3-tuning.md) | tuning to 2300–2400 MHz |
| [05](notes/05-crmap-and-clock-blocks.md) | ROM register-field layer (GCI) and the WLAN/LMAC clock-mode register (TICKCR0) |
| [06](notes/06-phy-bw-config-map.md) | PHY bandwidth configuration map |
| [07](notes/07-tx-capabilities.md) | TX capabilities: IQ/waveform transmit, the missing ÷2, the 10 MHz conclusion |
| [08](notes/08-icap-ring-and-rate.md) | ICAP ring placement, SRAM modes, packet-pool layout, capture rate |
| [09](notes/09-continuous-narrowband-node.md) | continuous narrowed capture, the node, the ÷8 transmitter, the tone DAC, rig limits |
| [10](notes/10-fec.md) | FEC: the card's coding options, receiver sensitivity, TX truncation and the MAC tick, nbmesh and p11node on hardware |
| [11](notes/11-phy-clock-tree.md) | static study: the D-die PHY clock tree vs channel width (is a 10 MHz PHY possible?) |
| [12](notes/12-mt7996-narrow-recipe.md) | static study: MediaTek's 5/10 MHz recipe on MT7996 (w1700k) compared with MT7921 |

### data/ (analysis results the notes and tools use)
| file | what it is |
|---|---|
| `crmap/crmap.json`, `crmap/crmap.txt` | the boot ROM's register-field (GCI) tables, decoded: 36 tables, 1114 registers, 3923 fields (notes/05). `tools/gci_comment.py` reads the JSON |
| `crmap/ram_gci_calls.txt` | every GCI call of the RAM firmware with its decoded selector (function, access, register field) |
| `phydump/diff22.txt`, `diff26.txt`, `diffA.txt`, `diffbw.txt`, `diffwide.txt` | PHY/RF/MAC register differences between widths and firmware versions, from register snapshots (`tools/phydiff.py`; notes/03, 05, 06) |
| `phydump/crtableA.csv`, `crtableB.csv` | the firmware's compressed PHY CR tables, decoded (`tools/crtable.py`; notes/06) |
| `phydump/bisect_5180_10.txt`, `txbisect.txt`, `reg83081000.txt` | emission measured per narrow-profile register and per 0x83081000 value (X410; `tools/sdr/bisect.sh`, `regtry.sh`) |
| `phydump/rxsweep1.txt` | a narrow-reception register sweep on card B (`tools/sdr/rxsweep.sh`) |
| `phydump/all_pages.txt` | every 4 KB register page probed for the snapshots |

### fw/ (instrumented firmware)
| file | what it is |
|---|---|
| `Makefile` | builds `out/WIFI_RAM_CODE_MT7961_1.bin` (stock image + hooks, checksums fixed); `install`, `restore`, `verify` |
| `include/fw.h` | firmware types and entry points the hooks call |
| `fwsyms.ld` | firmware symbols (live addresses) for linking the hooks |
| `hooks.ld` | places the hook code in free SRAM after RAM region 3 |
| `hooks.def` | hook installation: pointers patched into the image |
| `src/dbgcmd.c` | the debug command (CE cid 0xcb): read, write, call, call-detached |
| `src/iqstream.c` | the IQ streamer: ICAP ring → MCU WFDMA TX ring 0 → USB bulk IN, with an optional energy gate |
| `src/dac.c` | the tone generator as a slow IQ DAC (`hk_dacplay`) |
| `src/util.c` | measurement helpers (`hk_wrspeed`, …) |
| `asm/icap_pool_layout.S` | packet pool with a dedicated ICAP partition (notes/08 Fix A) |
| `asm/sniffer_outband_call.S`, `asm/sniffer_outband_tramp.S` | opt-in frequency override through the sniffer config (notes/04) |
| `patches/icappool.def`, `patches/outband.def` | patch sets: default, and opt-in (`make OUTBAND=1`) |
| `build/`, `out/` | build outputs, not in git: objects, `hooks.nm` (the hook addresses; `make install` copies it to `/root/fwre/hooks.nm`, which the nodes read), the patched images |

### tools/ (laptop side: rig access, firmware images, decompilation)
| file | what it is |
|---|---|
| `g.sh`, `h.sh` | run a command in the test guest / on the lab host lorien (X410, builds) |
| `rigrecover.sh` | full rig recovery after a card wedge (USB reset, VM reset, stack and firmware reload) |
| `vmpause.sh`, `wedgestats.sh` | freeze the VM for N seconds; wedge statistics over repeated two-card runs |
| `trunctest.sh` | narrow-PPDU TX truncation check: card A transmits, the X410 captures, `sdr/trunccheck.py` counts symbols |
| `mtfw_split.py` | split RAM-code and ROM-patch images into load regions |
| `mtfw_patch.py` | patch a RAM image and fix its integrity words; `--verify` |
| `crc_probe.py` | early probe of the per-region check value with CRC-32 variants (the real algorithm, an LFSR, is in `mtfw_patch.py`) |
| `mkruntime.py` | the DLM region in its runtime layout (.data moved by 0x44c, bss cleared) |
| `find_gp.py` | estimate the NDS32 `$gp` from gp-relative string references |
| `hookargs.py` | turn `fw/hooks.def` into `mtfw_patch.py` arguments |
| `ghidra_build.sh`, `ghidra_script.sh` | rebuild the Ghidra project; run one Ghidra post-script on it |
| `decomp.sh` | decompile functions selected by address or by referenced string |
| `annotate.sh` | apply `ghidra/annotations` and regenerate the commented decompilations |
| `fshow.sh`, `fgrep.sh` | print decompiled functions by address; grep a decompilation with the containing function |
| `gci_comment.py` | comment every GCI register-field access in the decompilation exports |
| `bwmap.py` | call-graph walk collecting PHY/RF register writes |
| `crtable.py` | decode the firmware's compressed PHY CR tables into CSV |
| `phydiff.py` | diff PHY/RF register snapshots |
| `icapview.py` | inspect an ICAP capture: statistics, ASCII spectrum, occupied width |

### tools/ghidra/
| file | what it is |
|---|---|
| `NDS32_MT7961/` | Ghidra extension: NDS32 processor (SLEIGH) with EX9 support, for the MT7961 WM MCU |
| `SetupMt7961.java`, `SeedFunctions.java` | map the firmware regions, set `$gp` and entry points; seed functions at NDS32 prologues |
| `Ex9Gen.java`, `GpRefs.java` | generate the EX9 instruction-table SLEIGH include; create references for gp-relative accesses |
| `LabelCmdTable.java` | label an MCU command dispatch table and create its handlers |
| `Decomp.java`, `DecompAll.java` | decompile selected functions; decompile everything into one file with a register glossary |
| `Annotate.java`, `MmioVolatile.java`, `AutoComment.java` | apply the annotations; keep MMIO blocks volatile; add automatic per-function context |
| `annotations/gen_ram.py`, `annotations/gen_rom.py` | annotation sources (functions, registers, comments) |
| `annotations/ram.tsv`, `rom.tsv`, `regs.tsv` | the tables they generate |

### tools/nbnode/ (Rust crate: `nbmesh`, `p11node`, `rxfile`)
| file | what it is |
|---|---|
| `README.md` | usage, options, on-air format, results, limits |
| `Cargo.toml`, `Cargo.lock` | the crate (no dependencies) |
| `src/bin/nbmesh.rs`, `src/bin/p11node.rs`, `src/bin/rxfile.rs` | the three programs |
| `src/lib.rs` | the crate root: module overview |
| `src/card.rs` | every access to the card (debug command, register path, EXT commands, mt7921dbg parameters, injection), the setup steps, capture slices, TX windows |
| `src/node.rs`, `src/iqring.rs` | the shared receive engine (slices, overlapping blocks, decoder threads); the IQ ring reader and sample unpacking |
| `src/frame.rs`, `src/fec.rs`, `src/profile.rs` | 802.11 framing and the ÷8 inner CRC; the Reed-Solomon packet FEC; PHY coding profiles, airtime, the ÷8 padding model |
| `src/netdev.rs`, `src/sys.rs` | TUN/TAP interfaces; the few libc calls std lacks |
| `src/rx80211.rs`, `resample.rs`, `viterbi.rs`, `fft.rs`, `complex.rs`, `iqfile.rs` | the receiver: port of `rx80211.py`, `vit.c` and scipy `resample_poly` |
| `tests/vectors.rs`, `testdata/` | test vectors with Python reference decodes (`testdata/gen.py` regenerates them) |

### tools/guest/ (runs in the test guest, from /root/fwre)
**Card access**
| file | what it is |
|---|---|
| `mt7921dbg/` | the kernel module (`mt7921dbg.c`), its `Makefile`, and `mt76.symvers` (modversion CRCs of the loaded mt76) |
| `fwdbg.py` | CLI for the debug command: read, write, call on the MCU |
| `dbglock.py` | lock shared by all tools that use mt7921dbg's single target card |
| `memdump.py` | chip-bus reads/writes through mt76 debugfs (USB vendor requests) |
| `chsw.py`, `outband.py` | send CHANNEL_SWITCH directly (any width, frequency override); tune out of band and read back the synthesizer |
| `ate.py`, `aterx.py` | MediaTek ATE (RF test mode) client; its receive-test helper |

**Rig management**
| file | what it is |
|---|---|
| `rigreset.sh` | reload the instrumented firmware on both cards, recreate `mon_a`/`mon_b` |
| `fwswap.sh`, `rig2022.sh` | install a firmware image and re-probe (or restore the stock one); load the stock 2022 firmware |
| `loadstack.sh` | load the mt76 driver stack from `/root/stackok` in dependency order |

**Narrowband nodes**
| file | what it is |
|---|---|
| `nbprep.py` | prepare a card for 2.5 / 5 MHz (see Main programs) |
| `nbrxd.py` | the Python node prototype |
| `nbmeshtest.sh` | two-node nbmesh ping test (cards A and B in network namespaces) |
| `nbping.sh`, `nblink.sh`, `nbudp.sh` | nbrxd two-node tests: ping; Poisson UDP both ways; one-way IP with transmit-only and receive-only nodes |
| `nbset.sh` | monitor interface on a native 5/10 MHz channel through the stock driver path |
| `inject.py`, `inject8.py` | inject broadcast test frames on a monitor interface; the same with ÷8 framing |

**IQ capture and streaming**
| file | what it is |
|---|---|
| `icap.py` | program and run the ICAP capture through host register access (`ICAP_SETUP_ONLY=1` for the nodes) |
| `iqstream.sh`, `iqloop.sh`, `iqdrain.py` | one IQ stream through the firmware hook; back-to-back streams; drain an IQ ring into a file |
| `icapcode.sh`, `icapdiv.sh`, `icapdom.sh`, `icappack.sh`, `icaprate.sh` | capture-rate and format surveys: per BN code, divider, capture domain, packer, mode byte |
| `icapsave.sh`, `icapsurvey.sh`, `tapsurvey.sh`, `tonecap.sh` | save captures per configuration; tap surveys; capture taps while an external tone is on air |
| `sramsnap.py` | snapshot of the 256 KiB packet SRAM |

**TX experiments**
| file | what it is |
|---|---|
| `txdiv8.sh`, `div8bisect.sh` | ÷8 transmit test; which setup step makes the ÷8 transmitter key a bare carrier |
| `txmeter.sh`, `txscan.py`, `txscanbatch.sh` | TX timing meter; fast scan of many card-B register configurations in one card-A capture; the scan in batches |
| `contitx.sh`, `txicap.sh`, `narrow_tx.sh` | ContiTx replay; card B as an SDR for card A's frames; card A transmitting in phases for SDR measurements |
| `tonedac.sh`, `dacbw.sh`, `dactssi.sh`, `busspeed.sh` | the tone generator as an IQ DAC: liveness, bandwidth, collapse; MCU write speed to PHY registers |

**RX experiments (the search for a narrow demodulator)**
| file | what it is |
|---|---|
| `narrow_test.sh`, `c2c.sh`, `icapnb.sh` | card-to-card narrow-reception screens (register profile, candidate writes, through ICAP) |
| `rxcand.sh`, `rxclk.sh`, `rxdivscan.sh`, `rxpair.sh` | receive candidate register tests between the two cards |
| `rxscan10.sh`, `pdscan.py`, `rx10match.py` | 10 MHz demodulator field scan; post-detection stage scanner; match scan windows against reference frames |
| `aterxtest.sh`, `mibdiff.sh`, `exp1_narrowbw.sh` | ATE receive test; MIB counter diff around a burst; does the stock firmware narrow the PHY (experiment 1) |
| `dualtrx.sh` | card-wedge reproducer: both cards as half-duplex nodes |

**Register snapshots**
| file | what it is |
|---|---|
| `snap2022.sh`, `snapA.sh`, `snapbw.sh`, `snapwide.sh` | PHY/RF/MAC register page snapshots (2022 firmware; card A narrow; bandwidth progression; wide set) |
| `wide_pages.txt`, `phy_pages.txt` | the register page lists those snapshots read (`wide_pages.txt` holds only pages safe to read) |
| `narrow_profiles.json` | the 2022 firmware's narrow register profiles (10 / 5 MHz), applied by `narrow_test.sh`, `narrow_tx.sh` and `tools/sdr/measure.sh`, `bisect.sh` |

### tools/sdr/ (X410 SDR on lorien, and offline analysis)
**Receivers and decoders**
| file | what it is |
|---|---|
| `rx80211.py`, `vit.c` | the reference 802.11a/p OFDM receiver (any width, at the 20 Msps-equivalent rate); its C Viterbi |
| `ofdm80211.py` | 802.11a/p PPDU generator (used by the X410 transmit scripts and tests) |
| `sigdec.py` | decode L-SIG, HT-SIG and HE-SIG-A with their CRCs: what a transmitter really sent |
| `iqrec.py`, `iqdec.py`, `iqchunkdec.py` | parse mt7921dbg IQ records; decode record files (gated streams; long continuous ones in blocks) |
| `decmulti.py`, `div8dec.py`, `symcheck.py` | decode under several width hypotheses; decode ÷8 node frames (fixed padding); per-symbol quality |
| `perawgn.py`, `trunccheck.py` | PER vs SNR with added noise on real card bursts; symbols that arrive per narrow PPDU |

**X410 capture and transmit**
| file | what it is |
|---|---|
| `capture.py` | raw capture to a cf32 file (RX1, the in-room antenna, by default) |
| `txloop.py`, `txframes.py`, `txtone.py` | transmit frames continuously (`ETHERTYPE` for ITS-like frames); N frames (whole waveform in RAM: small counts only); a tone |
| `burst.py`, `psd.py`, `burstana.py` | first-burst analysis; burst-gated spectrum and occupied bandwidth; burst structure in a packer-2 dump |
| `txmeter.py`, `txscan.py`, `txscansum.py` | OFDM timing of another card's bursts; the streaming TX timing scan; its summary by time |
| `dacana.py`, `dacsess.py`, `toneana.py`, `tonephase.py` | tone-DAC line analysis per segment and per session; CW phase track; packer sampling instants |

**Card + X410 test scripts**
| file | what it is |
|---|---|
| `measure.sh`, `regtry.sh`, `bisect.sh`, `txprobe.sh`, `capnb.sh` | card A transmits with register changes; the X410 measures width, bursts and OFDM timing |
| `nbtest.sh`, `nbmulti.sh`, `rxsweep.sh`, `rxmatrix.sh`, `codesweep.sh` | card B narrow-reception searches against X410 frames |
| `phycnt.sh`, `pdcnt.sh`, `aterxcnt.sh` | PHY counter readouts around X410 bursts (packet detect, ATE) |
| `streamtest.sh`, `c2cstream.sh` | end-to-end IQ-stream reception: X410 → card; card → card |

## Rebuilding work/ (local data, not in git)
The tools expect a `work/` directory next to `tools/`. It holds MediaTek's code and what is derived from it, plus
large captures and third-party sources, so it stays out of git. Everything in it can be recreated:

| `work/` path | contents | how to get it |
|---|---|---|
| `fw/WIFI_RAM_CODE_MT7961_1.bin` | the stock RAM firmware, build 20260224110949 | linux-firmware `mediatek/WIFI_RAM_CODE_MT7961_1.bin` of that build; `fw/Makefile` checks its SHA-256 (`b94217a9…`) |
| `fw/WIFI_MT7961_patch_mcu_1_2_hdr.bin`, `fw/guest/` | the ROM patch the cards run (2024 build) and the guest's images | linux-firmware, or copied from the guest. **Never load a 2025+ ROM patch** |
| `regions/`, `regions_*` | the images' load regions | `tools/mtfw_split.py` |
| `regions_rt/` | the runtime layout Ghidra loads (.data moved by 0x44c) | `tools/mkruntime.py`, plus `rom/rom_800000.bin` |
| `rom/` | boot-ROM dumps | read from a card: `tools/guest/memdump.py <phy> read 0x800000 0x100000 rom_full_800000.bin` (512 KB of content, mirrored; notes/02) |
| `fw2022_live/` | the 2022 RAM firmware, decrypted | dumped from live memory with the 2022 image loaded (`tools/guest/rig2022.sh`, `memdump.py`; notes/02) |
| `dlm_live_B.bin`, `live_dlm_*.bin`, `sram/` | live DLM and packet-SRAM dumps | `tools/guest/memdump.py`, `fwdbg.py read`, `sramsnap.py` |
| `ghidra/` | Ghidra project: the 2026 RAM image, the ROM patch and the boot ROM | install `tools/ghidra/NDS32_MT7961` into Ghidra's Extensions, then `tools/ghidra_build.sh` (RAM image and ROM patch). The boot ROM program `rom_800000.bin` (language NDS32:LE:32:mt7961rom) is imported by hand (notes/02) |
| `ghidra22/` | Ghidra project of the 2022 RAM image | imported by hand from `fw2022_live/` with the `nds32mt7961_2022` language |
| `decomp/all.c`, `rom_all.c`, `all2022.c` | decompilations: the 2026 ones commented | `tools/annotate.sh` (2026 RAM and boot ROM); `all2022.c`: `DecompAll.java` on the 2022 project; `tools/decomp.sh` for single functions |
| `dis/` | disassembly listings (address, instruction) | exported from the Ghidra projects (no script in the repo) |
| `phydump/*.bin` | raw register snapshots | `tools/guest/snap*.sh`. The diffs derived from them are in `data/phydump/` |
| `icap/` | IQ captures and stream records (~1.4 GB) | `tools/guest/icap*.sh`, `iqstream.sh`, `tools/sdr/capture.py` |
| `ext/birria/` | BirriaSDR (MIT), with the rig's changes | `git clone https://github.com/luigifcruz/BirriaSDR`, check out 80ec6fc, `git apply patches/birriasdr-mt7921-rig.patch` |
| `ext/icap/MtkICAPtool/` | MtkICAPtool | `git clone https://github.com/MtkWifiRev/MtkICAPtool` (commit d829596) |
| `ext/w1700k/` | the W1700K SDR sources (MT7996 SDR on a Gemtek W1700K router); notes use its MT7996 register headers | the W1700K SDR project |
| `ext/nullcon/` | slides: D. Wegemer and E. Mantovani, "Unlock hidden Superpowers in Mediatek Wi-Fi Chips", Nullcon 2025 | the talk's published slides |
| `ext/mt7921-11p-linux/` | the author's earlier OCB/802.11p driver work for these cards | a separate repository |
| `guestmods/`, `debs/`, `src/mt76/`, `patchbuild/`, `out/` | driver module builds, kernel headers, an mt76 header copy, the experiment-1 patch tree, experimental firmware variants | rebuilt when needed |
