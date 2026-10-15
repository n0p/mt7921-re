# MT7921AU narrowband: firmware RE, IQ capture, and a 5 / 2.5 MHz node

Goal: use an MT7921AU USB card (MT7961 + MT7972, NDS32 WM MCU) as a narrowband long-range mesh node and as an
802.11p / bd-style modem in the 2.3–2.4 GHz amateur band.

This work covers:
- reverse engineering of the Wi-Fi firmware, then patching and instrumenting it;
- a firmware IQ streamer;
- a software OFDM receiver;
- a single-card half-duplex node that carries IP.

Details live in [notes/](notes/); this page is the summary. Status as of 2026-10-15.

## What runs on the card and what runs as IQ
The card's own PHY can **transmit** narrow OFDM (hardware clock dividers) but cannot **demodulate** it: its
demodulator stays at 20 MHz timing in every configuration (notes/03, 06).

So the **receive side of every narrow mode is IQ**:
- the card's capture block (ICAP) records the front end's samples;
- an instrumented-firmware hook streams them over USB;
- software on the host demodulates them.

| Function | Runs on | How |
|---|---|---|
| Normal 20/40/80 MHz Wi-Fi | **card** (stock PHY + MAC) | unchanged |
| Tuning to 2300–2400 MHz | **card** firmware | CHANNEL_SWITCH frequency override (notes/04); the RF readback confirms 2350.000 MHz |
| **5 MHz OFDM TX** (802.11a timing ÷4) | **card** hardware | BN_MODE `0x83081000 [30:28] = 5` (native cbw 4). Frames are injected on a monitor interface and the card's MAC/PHY sends them. 1.5–13.5 Mbps on air |
| **2.5 MHz OFDM TX** (÷8) | **card** hardware + host framing | `[30:28] = 7`. The PPDU always loses its last 3 OFDM symbols, so the sender adds its own CRC and padding, and mt7921dbg `tx_count=1` stops the MAC's retries (notes/09 §4). 0.75–6.75 Mbps |
| 10 MHz OFDM TX (802.11p / bd) | **not possible** | no ÷2 divider exists; there is no IQ playback either (notes/07) |
| **5 / 2.5 / 10 MHz OFDM RX** | **IQ** → host software | ICAP capture → firmware hook → USB → `tools/sdr/rx80211.py` |
| IQ capture rate | **card** hardware | ICAP packer `0x800210b8 [19:16]`: 0 = 40 Msps (gated only), 8 = 30 Msps, 2 = 10 Msps; the last two stream continuously |
| IQ streaming | **card** firmware hook | `fw/src/iqstream.c` lends the idle MCU WFDMA TX ring 0 to USB bulk IN 0x84; `tools/guest/mt7921dbg` collects it in the host kernel |
| Energy gate (optional) | **card** firmware | the same hook streams only busy periods (40 Msps mode) |
| Preamble detection, CFO, FFT, Viterbi, FCS | **host** | `rx80211.py` (Viterbi in C: `vit.c`) |
| Node logic (half-duplex, IP over TAP) | **host** | `tools/guest/nbrxd.py` |
| Tone generator as an IQ DAC | **card** (the MCU writes a DC I/Q word) | experimental: it modulates the carrier, but the output collapses unpredictably (notes/09 §5b) |

**Per mode:**
- **5 MHz:** TX on the card, RX as 10 Msps IQ.
- **2.5 MHz:** TX on the card (with the framing workaround), RX as 10 Msps IQ.
- **802.11p 10 MHz:** RX only, as 30 Msps IQ.

## How a node works (tools/guest/nbrxd.py)
```
RX:  air -> RF front end (band-limited to +-5 MHz in native cbw 4) -> ICAP packer (10 or 30 Msps)
         -> SRAM ring (dedicated ICAP partition, fw/asm/icap_pool_layout.S) -> fw hook hk_iqstream
         -> USB bulk IN 0x84 -> mt7921dbg kprobe collector -> nbrxd: 100 ms blocks -> rx80211 (3 worker processes)
         -> 802.11 frames -> TAP interface (IP)
TX:  TAP -> nbrxd -> 802.11 data frame (OCB-style addressing, LLC/SNAP) -> monitor injection
         -> card MAC/PHY at /4 (5 MHz) or /8 (2.5 MHz) -> air
```
- **Half-duplex.** The card receives in continuous slices. When the kernel queues frames, nbrxd ends the slice
  (one register write), opens a TX window, and restarts capture.
- **TX window (default "window" mode):** MAC TX goes on; the frames go out; MAC TX goes off only once the device's
  queues are empty.
- **Firmware protocol.** No MCU commands are sent while a slice streams. Slice status is read through the USB
  register path.

## Results (all measured on the rig at 2350 MHz)
| Test | Result |
|---|---|
| X410 → card A, 5 MHz frames, continuous 10 Msps, real time | 368/372 (losses only in two ~0.1 s slice restarts) |
| Narrowed capture vs. full 40 Msps, X410 5 MHz frames | 98/98 at 10 Msps and at 30 Msps, the same SNR as 40 Msps; 0 overruns |
| **802.11p 10 MHz RX**, X410 frames, continuous 30 Msps | **296/300** with valid FCS, no false decodes |
| UDP card → card, 5 MHz, one way | 97/100, 98/100; 49/50 from a half-duplex sender |
| UDP both ways, two half-duplex nodes, Poisson ~5/s each way, 30 s | 81% / 74%, median latency 0.2 s |
| Same, 4/s each way, 120 s, window TX | 54% / 52%, latency p50 0.23 s, p90 0.37 s (no MAC: turnaround and collisions) |
| Ping between two nodes | 9/10 in the best run |
| **2.5 MHz OFDM TX**, one way (UDP) | 49/50, one transmission per frame |
| 2.5 MHz TX from a half-duplex node (card A), seen by the X410 | 23/23 frames in the windows captured; 28 cycles, no failure |
| Card A stability: 5 min continuous RX | 23.7 GB, 0 drops, 0 errors |
| Card A stability: ~800 TX-then-RX cycles | 0 failures |
| Software receiver speed at 10 Msps | 1.2 CPU-seconds per second (guest), 1.0 (laptop) |

## What was built
**Firmware RE and patching (notes/01, 02):**
- region layout and the per-region PDA download checksum; `tools/mtfw_patch.py` builds modified images that boot;
- gp, EX9 ITB and command tables recovered; a Ghidra NDS32 extension.

**Instrumented firmware (fw/, notes/03), `make` / `make install`:**
- C hooks linked into SRAM;
- a debug command for reads, writes and calls;
- the IQ streamer (`iqstream.c`);
- `dac.c`;
- in-place assembly patches (`fw/asm/`), including the dedicated ICAP partition (Fix A, notes/08).

**2.3 GHz:** frequency override through CHANNEL_SWITCH (notes/04).

**PHY / clock / bandwidth maps (notes/05, 06).** Static analysis: the demodulator has no narrow mode, and the RX BW
codes 4/5 only decimate.

**TX capabilities (notes/07, 09):**
- ÷4 and ÷8 work;
- no ÷2;
- no IQ playback;
- the tone generator is a live, linear DC I/Q DAC (tones up to at least 62.5 kHz, 1.45 Msps writes).

**IQ capture (notes/08, 09):**
- the capture ring moved out of the packet pool;
- the 40 Msps floor beaten with the ICAP packers;
- the packer-2 beat layout decoded.

**Host software:**
- `tools/sdr/rx80211.py`: 802.11a/p/quarter-rate OFDM receiver; all 8 rates, CFO, SNR and CW-spur gates;
- `iqrec.py`, `iqchunkdec.py`, `iqdec.py`: record parsing and offline decoding;
- `tools/guest/nbrxd.py`: the node daemon;
- the mt7921dbg kernel module: debug command, IQ collector, RX bandwidth fix-up, `tx_count`.

**Test harness:**
- `nbping.sh`, `nblink.sh`, `nbudp.sh`, `iqstream.sh`;
- `txdiv8.sh`, `div8bisect.sh`, `tonedac.sh`, `dacbw.sh`, `dactssi.sh`;
- X410 scripts in `tools/sdr/`;
- `tools/rigrecover.sh`.

## Quick start (on the test rig)
```sh
make -C fw && make -C fw install                     # build and load the instrumented firmware (both cards)
tools/g.sh /root/fwre/rigreset.sh                    # clean rig state
# a node: card A at 2350 MHz, 5 MHz frames, continuous 10 Msps RX, IP on TAP nbA in netns nsA
tools/g.sh 'cd /root/fwre && python3 nbrxd.py A 2350 --pack 2 --tap nbA --netns nsA'
# options: --width 2.5 (the /8 transmitter), --pack 8 --width 10 (802.11p receive), --rx-only, --tx-only,
#          --pcap FILE, --show-bad; two-node tests: PACK=2 ./nbping.sh 2350 20, PACK=2 ./nblink.sh 2350 30 5
tools/rigrecover.sh                                  # after a card wedge: USB reset, VM reset, reload
```

## Limitations and open problems
- **No 10 MHz transmit.** 802.11p/bd interop is receive-only. The card's 5 MHz PPDU is a valid quarter-rate 802.11a
  frame (decoded by our receiver and by the X410). Interop with ath9k's 5 MHz mode is expected but untested.
- **Half-duplex, no MAC protocol.** Delivery drops with load. A random-access / ARQ layer is the next software step.
- **÷8 tail loss** is worked around in software; the MAC timing register behind it has not been found.
- **Tone-DAC collapse:** the power-control loop is involved (notes/09 §5b).
- **Card B (lab rig) wedges its USB control path**, even when only receiving; card A is stable. It looks like card B or
  its host port: being tested.
- The rig is a QEMU guest on the lab host. It now runs 12 vCPUs, 8 GB, one emulated xHCI per card, and pinned
  interrupts (lorien `~/vm23.sh`). One card at 80 MB/s costs about one vCPU of USB emulation.

## Hazards (learned the hard way)
- Never load a 2025+ ROM patch (eFuse gate).
- MCU reads of 0x8103xxxx or of unmapped/granted memory hang the MCU.
- Host reads at or after 0xe0300000 reset the card.
- Don't touch CONN AFE 0x7c003xxx or 0x88009000/04 [23:0]/08.
- Avoid host reads of the PSE.
- Stop the tone generator (`FUN_00969126`) before any rig reset.
- Transmit only in the licensed 2300–2400 MHz band.

## Notes index
- [01](notes/01-firmware-layout-and-bw.md) firmware layout and the BW path
- [02](notes/02-integrity-and-patch-strategy.md) integrity and patching
- [03](notes/03-instrumentation-and-narrowband.md) instrumentation and the narrowband investigation (chronological log)
- [04](notes/04-2g3-tuning.md) 2.3 GHz tuning
- [05](notes/05-crmap-and-clock-blocks.md) register-field layer and clocks
- [06](notes/06-phy-bw-config-map.md) PHY bandwidth map
- [07](notes/07-tx-capabilities.md) TX capabilities
- [08](notes/08-icap-ring-and-rate.md) ICAP ring and rate
- [09](notes/09-continuous-narrowband-node.md) continuous capture, the node, ÷8, the tone DAC, rig limits
