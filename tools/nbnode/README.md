# nbnode: narrow-band 802.11 nodes for MT7921AU cards

The crate is Rust with std only and no dependencies, so it builds for an embedded host next to the card. It
contains:

| binary | what it is |
|---|---|
| `nbmesh` | a 5 or 2.5 MHz mesh node on one card, serving a **TUN or TAP** interface (`--mode tun\|tap`), with packet-level Reed-Solomon FEC |
| `p11node` | an IEEE 802.11p receiving node (10 MHz OCB, ITS channels 172–184 at 5.9 GHz): frames go to a TAP/TUN interface, a pcap or stdout |
| `rxfile` | the offline decoder: `rxfile <capture.cf32\|.cs16>`, the counterpart of `tools/sdr/rx80211.py <file>` |

Both nodes need, on the card's host:
- the instrumented firmware: `fw/`, `make install` (the debug command and the `hk_iqstream` hook);
- the mt7921dbg module: `tools/guest/mt7921dbg` (TXD overrides, IQ ring);
- `/root/fwre/hooks.nm` (the hook addresses of the installed build);
- `ip` and `iw`.

The Python counterpart for preparing a card by hand is `tools/guest/nbprep.py`.

**Transmitting:**
- `nbmesh` transmits on its `--freq` in any band the card tunes: ISM spectrum, or amateur allocations for a licensed
  operator. It enforces no band rules, so choosing a frequency, width and power that are allowed where it runs is up
  to the operator. `--rx-only` gives a node that never transmits.
- `p11node` never transmits.

## Why the host does so much
- **Receive:** the MT7961 has no narrow demodulator (fixed 20 MHz numerology, notes/03 and 07). The card streams its
  receive IQ over USB (`fw/src/iqstream.c` → mt7921dbg's `iqN` ring), and this crate demodulates it. The decoder is
  a port of `tools/sdr/rx80211.py` and is byte-identical to it on every test vector.
- **Transmit:** the card's own transmitter makes the narrow PPDUs: ÷4 = 5 MHz, ÷8 = 2.5 MHz, no ÷2. It also applies
  the PHY coding (BCC/LDPC, STBC, DCM, …: `--profile`, notes/10 §1). The host adds what the card cannot:
  - packet erasure FEC (`--fec K,M`): no MAC acknowledgement or retransmission exists at narrow widths;
  - the ÷8 inner CRC + padding: the ÷8 transmitter ends every PPDU early (notes/10 §5);
  - the half-duplex schedule: the card cannot capture and transmit at once.

## Build and deploy
```sh
cd tools/nbnode
cargo build --release            # target/release/{nbmesh,p11node,rxfile}
cargo test --release             # 35 unit tests + 15 test-vector tests against the Python receiver
scp target/release/{nbmesh,p11node} root@guest:/root/fwre/
```
A binary built on a newer glibc needs at most GLIBC_2.39 (checked with `objdump -T`). For another architecture, build
on the target or use `cargo build --target …`; there are no crates to fetch.

## nbmesh
```sh
# card A at the default 2450 MHz (2.4 GHz ISM), 5 MHz, TUN with an address (defaults: legacy:6, --fec 4,2)
./nbmesh wlx00c0cab7962a --addr 10.42.0.1/24
# card B as a TAP in its own namespace (ARP, bridging, batman-adv, babeld … run on top), here in the 13 cm amateur
# band (for a licensed operator)
./nbmesh wlx00c0cab79628 --freq 2350 --mode tap --dev nbb --netns nsb --addr 10.42.0.2/24 --slot 1 --monitor nbmon1
```
What it does:
1. **Card setup**, in nbprep's order:
   - TXD overrides: rate code, coding fields, one attempt per frame;
   - a monitor interface (MTU 2304) for injection;
   - channel switch at cbw 4 with the frequency override, then `WFSYS_TICKCR0 = 0xa` (`--tickcr0`; see
     below), and the ÷8 divider for 2.5 MHz;
   - receive class 4, ICAP registers, manual gain.
2. **Receive:**
   - capture slices of `--slice-ms`. While one runs, only host-path register accesses are made;
   - 100 ms decode blocks that overlap by more than the longest frame, decoded in `--workers` threads;
   - repeats removed by ring position; frames delivered to the interface.
3. **Transmit:** packets from the interface wait (at most `--queue`). A slice with packets waiting ends after
   `--min-rx-ms` plus 0–20 ms of random jitter, then a TX window opens:
   - receive class 0 and MAC TX on;
   - injection;
   - wait until the device queues are empty;
   - MAC TX off and class 4 again.

   A window whose queue does not drain within `--stall-ms` has stalled. The node then tunes again, which clears it.
4. **Recovery:**
   - on errors it sets the card up again;
   - a card that the kernel reset and probed again (new phy) is attached again automatically;
   - after 5 failures in a row it exits and points to `tools/rigrecover.sh`.

| option | default | meaning |
|---|---|---|
| `--freq MHz` | 2450 | 2300–2500, 4900–5925, 5925–7125 (the card's RF bands). 2450 is the 2.4 GHz ISM band's nominal centre, between Wi-Fi channels 6 and 11 and clear of the BLE advertising channels |
| `--width 5\|2.5` | 5 | ÷4 or ÷8 transmitter |
| `--mode tun\|tap` | tun | IP packets on one subnet, or Ethernet frames |
| `--txpower DBM`, `--rx-only` | 5, off | transmit power; a node that never transmits |
| `--fec K,M\|off` | 4,2 | K data + M parity frames per block. A short block of j packets gets ceil(M·j/K) parity frames, so a lone packet is sent twice |
| `--profile P` | legacy:6 | the card's PHY coding, nbprep `--fec` syntax. Our receivers decode legacy 6–24/48 only |
| `--gain L,F,D` | 3,4,6 | manual receive gain. **Use 0,0,0 for cards 10 cm apart** (near-field clipping) |
| `--tickcr0 V` | 0xa | MAC timing; it decides when a TX PPDU ends |
| `--stall-ms N`, `--no-retune` | 60, off | stall detection and its re-tune |
| `--pcap FILE`, `--show-bad`, `--quiet`, `--status-s N` | | diagnostics |

The other options (interface name and addresses, MTU, namespace, IQ slot, slice and block lengths, TX window
budget, queue length, CFO correction, decoder threads, `--regain`) are listed by `nbmesh --help`.

On-air format:
- every frame is radiotap + 802.11 data (FC 0x0008, addr1 broadcast, addr2 source, addr3 destination) + LLC/SNAP;
- without FEC the ethertype is the packet's own, as nbrxd.py sends;
- with FEC, every frame has ethertype 0x88b5 and carries an 8-byte shard header plus a data unit or a parity
  shard (`src/fec.rs`);
- at 2.5 MHz the body is followed by an inner CRC32 and padding of `profile::div8_pad_bytes`.

## p11node
```sh
./p11node wlx00c0cab7962a                        # ITS channel 180 (5900 MHz, ETSI G5-CCH), TAP "p11"
./p11node wlx00c0cab7962a --channel 178 --pcap its.pcap --no-dev
./p11node wlx00c0cab7962a --test-freq 2350       # lab test with an SDR sending 802.11p at 2350 MHz
```
The card is tuned in its native 10 MHz mode (front end ±5 MHz) and streams 30 Msps (ICAP packer 8), which the
host resamples to 10 Msps. Decoded frames are delivered as follows:
- TAP: Ethernet frames, any ethertype (GeoNetworking 0x8947, WSMP 0x88dc, …), e.g. for Vanetza;
- TUN: IP packets only;
- pcap: radiotap with the half-rate channel flag.

**It cannot transmit.** No transmitter of this silicon makes 10 MHz PPDUs (notes/07). The card's 5 MHz PPDUs are
not 802.11p, and ITS stations would not receive them.

## Results (2026-10-16, cards A and B 10 cm apart, 2350 MHz; X410 on RX1)
| test | result |
|---|---|
| X410 → card A, 5 MHz frames, nbmesh receive | 4775 of 4775 frames decoded in 12 s (no dropped blocks, no overruns) |
| card A → X410, nbmesh transmit | frames decoded at 34–39 dB SNR, FEC shard headers as designed |
| two nodes, ping 56 B, 5 MHz, FEC 4,2 | 54/60 round trips (90%), RTT 207–425 ms; FEC rebuilt 22 packets |
| same, FEC off | 48/60 (80%) |
| two nodes, ping 1300 B, 5 MHz, FEC 4,2 | 59/60 (98%), RTT mean 365 ms (needs TICKCR0 = 0xa) |
| two nodes, TAP mode | 36/40, ARP over the link |
| card A → card B, 2.5 MHz, PSDUs up to 1608 B | 26 of 27 frames received, every inner CRC intact |
| X410 802.11p (10 MHz, GeoNetworking) → card A, p11node | 6702/6702 frames in 16 s; delivered to the TAP (tcpdump: `GeoNet`) |
| p11node on ITS channels 180, 178 | tuned, no traffic near the lab |

Decoder speed (one laptop core, 100 ms blocks, nbrxd settings):

| stream | per core |
|---|---|
| dense 10 Msps (all 8 rates, 85% airtime) | 1.6× real time |
| dense 5 Msps | 3.2× real time |
| noise only | ~20× real time at 10 Msps |
| 30 Msps front end alone | ~3× real time |

The guest ran both nodes and p11node with no dropped blocks.

## Known limits
- **Near-field stalls:** after the other card's bursts at 10 cm, a card's MAC can defer its own TX until the PHY is
  reset (the queue for WCID 19 stays non-empty). nbmesh detects this and tunes again (~50 ms); see notes/10 §6.
- **2.5 MHz:**
  - the ÷8 transmitter keeps only ~30 + N/2 of a PPDU's N symbols, so long frames take nearly twice their airtime
    in padding;
  - PPDUs longer than ~17.5 ms were never seen to leave the card;
  - nbmesh therefore lowers the MTU to fit (about 830). Below 1280 the kernel runs no IPv6 on the interface.
- **Profiles:** the host receivers decode legacy 6–24/48 Mbps only. HT/HE profiles are for SDR measurements until the
  receiver learns them (notes/10 §4).
- **Multi-hop:** TUN mode is a single subnet; use TAP for multi-hop routing.

## Modules
| module | role |
|---|---|
| `card` | every access to the card (debug command, host register path, EXT commands, mt7921dbg parameters, injection), the nbprep setup steps, capture slices, TX windows |
| `iqring` | mt7921dbg's IQ ring: records, sample unpacking (ICAP packers 2 and 8) |
| `node` | the shared receive engine: slices, blocks with overlap, decoder threads, repeat removal |
| `frame` | 802.11 framing (nbrxd-compatible), the ÷8 inner CRC, radiotap pcap |
| `fec` | GF(2^8) Cauchy Reed-Solomon erasure code, shard header, block reassembly |
| `profile` | the card's PHY coding profiles, airtime, the ÷8 padding model |
| `netdev` | TUN/TAP creation and configuration, the TUN receive filter |
| `sys` | the few libc calls std lacks: flock, TUNSETIFF, AF_PACKET, signals, CPU affinity |
| `rx80211`, `resample`, `viterbi`, `fft`, `complex`, `iqfile` | the receiver (port of tools/sdr/rx80211.py, vit.c, scipy resample_poly) |
