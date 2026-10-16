//! The node's network interface toward the host's IP stack: a TUN (layer 3: IP packets) or a TAP (layer 2:
//! Ethernet frames) device.
//!
//! - **tap**: every Ethernet frame the kernel sends goes on the air with its own source and destination MAC
//!   (addr2 / addr3), and received frames come back as Ethernet frames. This is a shared Ethernet segment: ARP,
//!   bridging and layer-2 mesh routing (batman-adv) or IP routing daemons (babeld, OLSR) run on top, exactly as on
//!   nbrxd's TAP, with which it interoperates.
//! - **tun**: IP packets only, sent from the card's MAC address to broadcast. All nodes share one IP subnet and
//!   each receiver keeps the packets addressed to it (its own `--addr` addresses, broadcast or multicast). There
//!   are no link-layer addresses, so multi-hop routing over a TUN needs a tap.

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};
use std::os::unix::io::AsRawFd;

use crate::card::{other, parse_mac, run};
use crate::sys;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Tun,
    Tap,
}

impl std::str::FromStr for Mode {
    type Err = String;
    fn from_str(s: &str) -> Result<Mode, String> {
        match s {
            "tun" => Ok(Mode::Tun),
            "tap" => Ok(Mode::Tap),
            _ => Err(format!("mode must be tun or tap, not {s:?}")),
        }
    }
}

pub struct NetDev {
    file: File,
    pub name: String,
    pub mode: Mode,
    /// TAP: the interface's MAC address (the source of the frames it sends).
    pub mac: Option<[u8; 6]>,
    /// TUN: the interface addresses (`--addr`), for the receive filter.
    pub addrs: Vec<(IpAddr, u8)>,
}

impl NetDev {
    /// Create the interface, set its MTU and addresses and bring it up (with `ip`). With `netns`, the interface is
    /// moved into that network namespace first (two nodes on one host).
    pub fn open(name: &str, mode: Mode, mtu: usize, addrs: &[String], netns: Option<&str>) -> io::Result<NetDev> {
        let file = OpenOptions::new().read(true).write(true).open("/dev/net/tun")?;
        let flags = match mode {
            Mode::Tun => sys::IFF_TUN,
            Mode::Tap => sys::IFF_TAP,
        } | sys::IFF_NO_PI;
        sys::tun_set_iff(file.as_raw_fd(), name, flags)?;
        let mut ip: Vec<String> = vec!["ip".into()];
        if let Some(ns) = netns {
            run(&["ip", "netns", "add", ns], false)?; // exists already: fine
            run(&["ip", "link", "set", name, "netns", ns], true)?;
            ip.extend(["-n".into(), ns.into()]);
        }
        let ip_cmd = |args: &[&str], check: bool| {
            let mut v: Vec<&str> = ip.iter().map(|s| s.as_str()).collect();
            v.extend_from_slice(args);
            run(&v, check)
        };
        let mtu_s = mtu.to_string();
        ip_cmd(&["link", "set", name, "mtu", &mtu_s], true)?;
        let mut parsed = Vec::new();
        for a in addrs {
            ip_cmd(&["addr", "add", a, "dev", name], true)?;
            parsed.push(parse_cidr(a).ok_or_else(|| other(format!("bad address {a:?} (want ADDR/PREFIX)")))?);
        }
        ip_cmd(&["link", "set", name, "up"], true)?;
        let mac = if mode == Mode::Tap { Some(read_mac(name, netns)?) } else { None };
        Ok(NetDev { file, name: name.to_string(), mode, mac, addrs: parsed })
    }

    /// A second handle on the same device (the reader thread blocks in `read`).
    pub fn try_clone(&self) -> io::Result<NetDev> {
        Ok(NetDev {
            file: self.file.try_clone()?,
            name: self.name.clone(),
            mode: self.mode,
            mac: self.mac,
            addrs: self.addrs.clone(),
        })
    }

    /// Next packet (TUN: an IP packet; TAP: an Ethernet frame) the kernel sends; blocks.
    pub fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.file.read(buf)
    }

    /// Hand a received packet (TUN: an IP packet; TAP: an Ethernet frame) to the kernel.
    pub fn write(&mut self, pkt: &[u8]) -> io::Result<()> {
        self.file.write(pkt).map(|_| ())
    }

    /// TUN receive filter: an IP packet for one of our addresses, a broadcast (limited or of our subnets) or
    /// multicast. Without `--addr` everything passes (the kernel decides).
    pub fn wants(&self, ip_packet: &[u8]) -> bool {
        if self.addrs.is_empty() {
            return true;
        }
        match ip_dst(ip_packet) {
            Some(IpAddr::V4(d)) => {
                d.is_multicast()
                    || d.is_broadcast()
                    || self.addrs.iter().any(|&(a, plen)| match a {
                        IpAddr::V4(a) => d == a || d == v4_broadcast(a, plen),
                        _ => false,
                    })
            }
            Some(IpAddr::V6(d)) => d.is_multicast() || self.addrs.iter().any(|&(a, _)| a == IpAddr::V6(d)),
            None => false,
        }
    }
}

/// Ethertype of an IP packet from its version nibble (TUN mode has no Ethernet header).
pub fn ip_ethertype(ip_packet: &[u8]) -> Option<u16> {
    match ip_packet.first()? >> 4 {
        4 => Some(0x0800),
        6 => Some(0x86DD),
        _ => None,
    }
}

fn ip_dst(p: &[u8]) -> Option<IpAddr> {
    match p.first()? >> 4 {
        4 => Some(IpAddr::V4(Ipv4Addr::from(<[u8; 4]>::try_from(p.get(16..20)?).ok()?))),
        6 => Some(IpAddr::V6(Ipv6Addr::from(<[u8; 16]>::try_from(p.get(24..40)?).ok()?))),
        _ => None,
    }
}

fn v4_broadcast(a: Ipv4Addr, plen: u8) -> Ipv4Addr {
    let mask = if plen == 0 { 0 } else { u32::MAX << (32 - plen as u32) };
    Ipv4Addr::from(u32::from(a) | !mask)
}

fn parse_cidr(s: &str) -> Option<(IpAddr, u8)> {
    let (a, p) = s.split_once('/')?;
    Some((a.parse().ok()?, p.parse().ok()?))
}

fn read_mac(name: &str, netns: Option<&str>) -> io::Result<[u8; 6]> {
    // /sys/class/net shows the namespace of the reader, so a moved interface is read through `ip`
    let out = match netns {
        None => std::fs::read_to_string(format!("/sys/class/net/{name}/address"))?,
        Some(ns) => {
            let o = std::process::Command::new("ip").args(["-n", ns, "-o", "link", "show", name]).output()?;
            let s = String::from_utf8_lossy(&o.stdout).to_string();
            s.split_whitespace().skip_while(|w| *w != "link/ether").nth(1).unwrap_or("").to_string()
        }
    };
    parse_mac(out.trim()).ok_or_else(|| other(format!("{name}: no MAC address")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tun_filter() {
        let d = NetDev {
            file: File::open("/dev/null").unwrap(),
            name: "t".into(),
            mode: Mode::Tun,
            mac: None,
            addrs: vec![(IpAddr::V4(Ipv4Addr::new(10, 42, 0, 1)), 24)],
        };
        let mut p = vec![0x45u8; 20];
        p[16..20].copy_from_slice(&[10, 42, 0, 1]);
        assert!(d.wants(&p));
        p[16..20].copy_from_slice(&[10, 42, 0, 255]);
        assert!(d.wants(&p));
        p[16..20].copy_from_slice(&[10, 42, 0, 2]);
        assert!(!d.wants(&p));
        p[16..20].copy_from_slice(&[224, 0, 0, 251]);
        assert!(d.wants(&p));
    }
}
