//! The few Linux system calls the card and network code needs that Rust's std does not expose, declared by hand so
//! the crate keeps zero dependencies (std already links the C library that provides them).
//!
//! Each wrapper turns the C error convention (-1 + errno) into `std::io::Error`.

use std::ffi::CString;
use std::io;
use std::os::raw::{c_char, c_int, c_short, c_uint, c_ulong, c_ushort, c_void};
use std::sync::atomic::{AtomicBool, Ordering};

extern "C" {
    fn flock(fd: c_int, operation: c_int) -> c_int;
    fn ioctl(fd: c_int, request: c_ulong, ...) -> c_int;
    fn socket(domain: c_int, ty: c_int, protocol: c_int) -> c_int;
    fn bind(fd: c_int, addr: *const c_void, len: c_uint) -> c_int;
    fn send(fd: c_int, buf: *const c_void, len: usize, flags: c_int) -> isize;
    fn close(fd: c_int) -> c_int;
    fn if_nametoindex(name: *const c_char) -> c_uint;
    fn signal(signum: c_int, handler: usize) -> usize;
    fn sched_getaffinity(pid: c_int, cpusetsize: usize, mask: *mut u8) -> c_int;
    fn sched_setaffinity(pid: c_int, cpusetsize: usize, mask: *const u8) -> c_int;
}

/// Set by SIGINT / SIGTERM once `catch_stop_signals` has run: the daemons poll it to end their capture slice and
/// release the card cleanly (a slice left running keeps streaming into a ring nobody reads).
pub static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn on_stop_signal(_sig: c_int) {
    STOP.store(true, Ordering::SeqCst); // an atomic store is async-signal-safe
}

pub fn catch_stop_signals() {
    const SIGINT: c_int = 2;
    const SIGTERM: c_int = 15;
    let handler = on_stop_signal as extern "C" fn(c_int) as usize;
    unsafe {
        signal(SIGINT, handler);
        signal(SIGTERM, handler);
    }
}

pub fn stop_requested() -> bool {
    STOP.load(Ordering::SeqCst)
}

/// flock(2) operation: exclusive lock (blocks until granted).
const LOCK_EX: c_int = 2;
/// flock(2) operation: release.
const LOCK_UN: c_int = 8;

/// Take an exclusive advisory lock on an open file (released by `unlock` or when the file is closed).
pub fn lock_exclusive(fd: c_int) -> io::Result<()> {
    if unsafe { flock(fd, LOCK_EX) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub fn unlock(fd: c_int) {
    unsafe { flock(fd, LOCK_UN) };
}

/// TUNSETIFF (include/uapi/linux/if_tun.h): attach an open /dev/net/tun file to a named TUN or TAP interface.
pub const TUNSETIFF: c_ulong = 0x4004_54ca;
pub const IFF_TUN: c_short = 0x0001;
pub const IFF_TAP: c_short = 0x0002;
/// No 4-byte packet-information prefix: reads and writes carry the bare IP packet (TUN) or Ethernet frame (TAP).
pub const IFF_NO_PI: c_short = 0x1000;

/// struct ifreq as TUNSETIFF reads it: the interface name, then the flags, padded to the 40 bytes of the union.
#[repr(C)]
struct IfReq {
    name: [u8; 16],
    flags: c_short,
    _pad: [u8; 22],
}

/// Attach `fd` (an open /dev/net/tun) to interface `name` with `flags` (IFF_TUN or IFF_TAP, plus IFF_NO_PI).
pub fn tun_set_iff(fd: c_int, name: &str, flags: c_short) -> io::Result<()> {
    let mut req = IfReq { name: [0; 16], flags, _pad: [0; 22] };
    let b = name.as_bytes();
    if b.len() >= 16 {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "interface name longer than 15 bytes"));
    }
    req.name[..b.len()].copy_from_slice(b);
    if unsafe { ioctl(fd, TUNSETIFF, &mut req as *mut IfReq) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// AF_PACKET, SOCK_RAW (include/linux/socket.h, net.h).
const AF_PACKET: c_int = 17;
const SOCK_RAW: c_int = 3;

/// struct sockaddr_ll (include/uapi/linux/if_packet.h).
#[repr(C)]
struct SockaddrLl {
    family: c_ushort,
    protocol: c_ushort,
    ifindex: c_int,
    hatype: c_ushort,
    pkttype: u8,
    halen: u8,
    addr: [u8; 8],
}

/// A raw packet socket bound to one interface, for sending complete link-layer frames. On a monitor interface a
/// frame is radiotap + 802.11, and mac80211 hands it to the driver for transmission ("injection").
pub struct PacketSocket {
    fd: c_int,
}

impl PacketSocket {
    /// Protocol 0: the socket receives nothing, it only sends.
    pub fn bind_send_only(ifname: &str) -> io::Result<PacketSocket> {
        let cname = CString::new(ifname).map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "bad name"))?;
        let ifindex = unsafe { if_nametoindex(cname.as_ptr()) };
        if ifindex == 0 {
            return Err(io::Error::last_os_error());
        }
        let fd = unsafe { socket(AF_PACKET, SOCK_RAW, 0) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let sa = SockaddrLl {
            family: AF_PACKET as c_ushort,
            protocol: 0,
            ifindex: ifindex as c_int,
            hatype: 0,
            pkttype: 0,
            halen: 0,
            addr: [0; 8],
        };
        if unsafe { bind(fd, &sa as *const SockaddrLl as *const c_void, std::mem::size_of::<SockaddrLl>() as c_uint) }
            != 0
        {
            let e = io::Error::last_os_error();
            unsafe { close(fd) };
            return Err(e);
        }
        Ok(PacketSocket { fd })
    }

    pub fn send(&self, frame: &[u8]) -> io::Result<()> {
        let n = unsafe { send(self.fd, frame.as_ptr() as *const c_void, frame.len(), 0) };
        if n < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}

impl Drop for PacketSocket {
    fn drop(&mut self) {
        unsafe { close(self.fd) };
    }
}

/// Keep this process (and the threads it starts afterwards, which inherit the mask) off the CPUs that take the xHCI
/// interrupts. Streaming 80 MB/s keeps such a CPU ~98% busy, and a decoder scheduled there delays the USB completions
/// until the card's DMA stalls and its register requests time out (notes/09, nbrxd keep_off_usb_irq_cpus). Returns
/// the CPUs avoided. Call it before starting threads.
pub fn keep_off_usb_irq_cpus() -> Vec<usize> {
    let mut irq_cpus = Vec::new();
    let Ok(interrupts) = std::fs::read_to_string("/proc/interrupts") else { return irq_cpus };
    for line in interrupts.lines().filter(|l| l.contains("xhci_hcd")) {
        let irq = line.split(':').next().unwrap_or("").trim();
        let Ok(list) = std::fs::read_to_string(format!("/proc/irq/{irq}/effective_affinity_list")) else { continue };
        for part in list.trim().split(',') {
            let (lo, hi) = part.split_once('-').unwrap_or((part, part));
            if let (Ok(lo), Ok(hi)) = (lo.parse::<usize>(), hi.parse::<usize>()) {
                irq_cpus.extend(lo..=hi);
            }
        }
    }
    // cpu_set_t: a 1024-bit mask
    let mut mask = [0u8; 128];
    if unsafe { sched_getaffinity(0, mask.len(), mask.as_mut_ptr()) } != 0 {
        return Vec::new();
    }
    let mut avoided = Vec::new();
    let mut left = mask;
    for &c in &irq_cpus {
        if c < 1024 && left[c / 8] & (1 << (c % 8)) != 0 {
            left[c / 8] &= !(1 << (c % 8));
            avoided.push(c);
        }
    }
    // only if some CPU is left to run on
    if left.iter().any(|&b| b != 0) && !avoided.is_empty() {
        unsafe { sched_setaffinity(0, left.len(), left.as_ptr()) };
    } else {
        avoided.clear();
    }
    avoided
}
