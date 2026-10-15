// SPDX-License-Identifier: GPL-2.0
/*
 * mt7921dbg: host side of the instrumented MT7961 firmware debug command (CE cid 0xcb, fw/src/dbgcmd.c).
 *
 * Works on top of any loaded mt76/mt7921 stack without rebuilding it: the target device is found through the
 * mt76 debugfs "regval" file of a phy, whose i_private is that device's struct mt76_dev *.
 *
 *   /sys/kernel/debug/mt7921dbg/target   write "phyN"
 *   /sys/kernel/debug/mt7921dbg/cmd      write a binary request, read back the raw MCU event (rxd + payload)
 *   /sys/kernel/debug/mt7921dbg/raw      write u32 mt76 cmd id (MCU_*_CMD encoding) + payload; sends it as-is
 *                                        and waits for the response (read it back), unless bit 31 of the
 *                                        cmd id is set (RAW_NOWAIT): then it is fire-and-forget
 *   /sys/kernel/debug/mt7921dbg/mem      write "<addr> <len> [chunk]", then read: len bytes of device memory or
 *                                        registers from addr through the mt792x register read request
 *                                        (MT_VEND_READ_EXT, 0x1f vendor type), chunk bytes per control transfer:
 *                                        4 (default, like regval) or up to 64. Multi-word transfers of the ICAP
 *                                        ring after a capture stall the USB register path and the card.
 *   /sys/kernel/debug/mt7921dbg/rxbw     RX-vector bandwidth histogram (P-RXV FRAME_MODE [14:12]) of every frame
 *                                        the target card's mt76 parses (frames of other cards are only counted
 *                                        in total), plus the last raw RX vector with a value >= 4. mt76 drops
 *                                        frames whose value is not 20/40/80/160 (-EINVAL); with module parameter
 *                                        rxbw_fix=1 (default) values 4..7 (the PHY's narrow bandwidth codes) are
 *                                        rewritten to 20 MHz before parsing so those frames reach monitor
 *                                        interfaces. Write anything to reset the counters.
 *   /sys/kernel/debug/mt7921dbg/iqbind   write "<slot> <phy>": route that card's IQ stream (fw/src/iqstream.c) to
 *                                        slot 0 or 1 ("<slot> -" unbinds); read: the bindings
 *   /sys/kernel/debug/mt7921dbg/iq0, iq1 the slot's 32 MiB ring: every unsolicited MCU event of the bound card
 *                                        carrying the "QIQQ" marker and eid iq_eid (module parameter, default
 *                                        0xcc) is appended as a record: u32 "QIQR", u32 sequence, u32 absolute
 *                                        sample position, u32 payload bytes, then the payload (raw ICAP ring words:
 *                                        Q [13:0], I [29:16], 14-bit two's complement). Reads consume the ring
 *   /sys/kernel/debug/mt7921dbg/iqstat0, iqstat1  packets, bytes, sequence gaps, ring-full drops, last sequence
 *                                        number; write anything to empty the ring and reset the counters
 *
 * Module parameter tx_count (writable at runtime, default 0 = mt76's own value): transmission attempts written into
 * the TXD (DW3 REM_TX_COUNT) of every injected frame, through a kretprobe on mt76_connac2_mac_write_txwi. mt76 always
 * asks for 15; the ÷8 (2.5 MHz) transmitter ends every PPDU 3 symbols early, the MAC takes that as a failure and
 * sends each frame ~11-15 times, so narrow-band nodes set 1.
 */
#include <linux/module.h>
#include <linux/debugfs.h>
#include <linux/fs.h>
#include <linux/skbuff.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/usb.h>
#include <linux/kprobes.h>
#include <net/mac80211.h>
#include <linux/bitfield.h>
#include <linux/unaligned.h>
#include <linux/vmalloc.h>
#include <linux/spinlock.h>

struct mt76_dev;
int mt76_mcu_send_and_get_msg(struct mt76_dev *dev, int cmd, const void *data, int len, bool wait_resp,
			      struct sk_buff **ret);
int mt76u_vendor_request(struct mt76_dev *dev, u8 req, u8 req_type, u16 val, u16 offset, void *buf, size_t len);

#define MCU_CE_QUERY_ID(id)	((id) | BIT(16) | BIT(18))	/* __MCU_CMD_FIELD_QUERY | __MCU_CMD_FIELD_CE */
#define DBG_CID			0xcb
#define DBG_MAX			1024
#define RAW_NOWAIT		BIT(31)
#define MEM_MAX			(512 * 1024)
#define MEM_CHUNK_MAX		64		/* larger MT_VEND_READ_EXT requests fail */
#define MT_VEND_READ_EXT	0x63
#define MT_USB_TYPE_VENDOR	(USB_TYPE_VENDOR | 0x1f)

static struct dentry *dbg_dir;
static DEFINE_MUTEX(dbg_lock);
static char target[16] = "phy0";
static u8 rsp[DBG_MAX + 64];
static size_t rsp_len;
static u32 mem_addr, mem_len, mem_chunk = 4;

static bool rxbw_fix = true;
module_param(rxbw_fix, bool, 0644);
MODULE_PARM_DESC(rxbw_fix, "rewrite RX-vector bandwidth codes 4..7 to 20 MHz so mt76 does not drop the frames");
#define PRXV_FRAME_MODE		GENMASK(14, 12)
static atomic_long_t rxbw_count[8], rxbw_other;
static u32 rxbw_last[4];
static bool rxbw_registered;
static struct mt76_dev *rxbw_dev;	/* count only the target device (set when "target" is written) */

/* Entry of mt76_connac2_mac_fill_rx_rate(dev, status, sband, rxv, mode): rxv is the 4th argument (rcx). */
static int rxbw_pre(struct kprobe *p, struct pt_regs *regs)
{
	__le32 *rxv = (__le32 *)regs->cx;
	u32 v0;
	unsigned int bw;

	if (!rxv)
		return 0;
	if ((struct mt76_dev *)regs->di != READ_ONCE(rxbw_dev)) {
		atomic_long_inc(&rxbw_other);
		/* still let narrow frames of other cards through */
		v0 = le32_to_cpu(rxv[0]);
		if (rxbw_fix && FIELD_GET(PRXV_FRAME_MODE, v0) >= 4)
			rxv[0] = cpu_to_le32(v0 & ~PRXV_FRAME_MODE);
		return 0;
	}
	v0 = le32_to_cpu(rxv[0]);
	bw = FIELD_GET(PRXV_FRAME_MODE, v0);
	atomic_long_inc(&rxbw_count[bw]);
	if (bw >= 4) {
		memcpy(rxbw_last, rxv, sizeof(rxbw_last));
		if (rxbw_fix)
			rxv[0] = cpu_to_le32(v0 & ~PRXV_FRAME_MODE);
	}
	return 0;
}

static struct kprobe rxbw_kp = {
	.symbol_name = "mt76_connac2_mac_fill_rx_rate",
	.pre_handler = rxbw_pre,
};

static unsigned int tx_count;
module_param(tx_count, uint, 0644);
MODULE_PARM_DESC(tx_count, "TXD transmission attempts of injected frames (1-31; 0 = leave mt76's 15)");
#define MT_TXD3_REM_TX_COUNT	GENMASK(15, 11)
static bool txc_registered;

struct txc_data {
	__le32 *txwi;
	struct sk_buff *skb;
};

/* mt76_connac2_mac_write_txwi(dev, txwi, skb, wcid, key, pid, qid, changed): txwi in rsi, skb in rdx. */
static int txc_entry(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct txc_data *d = (struct txc_data *)ri->data;

	d->txwi = (__le32 *)regs->si;
	d->skb = (struct sk_buff *)regs->dx;
	return 0;
}

static int txc_ret(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct txc_data *d = (struct txc_data *)ri->data;
	unsigned int n = READ_ONCE(tx_count);
	u32 v;

	if (!n || !d->txwi || !d->skb || !(IEEE80211_SKB_CB(d->skb)->flags & IEEE80211_TX_CTL_INJECTED))
		return 0;
	v = le32_to_cpu(d->txwi[3]) & ~MT_TXD3_REM_TX_COUNT;
	d->txwi[3] = cpu_to_le32(v | FIELD_PREP(MT_TXD3_REM_TX_COUNT, min(n, 31u)));
	return 0;
}

static struct kretprobe txc_krp = {
	.kp.symbol_name = "mt76_connac2_mac_write_txwi",
	.entry_handler = txc_entry,
	.handler = txc_ret,
	.data_size = sizeof(struct txc_data),
	.maxactive = 16,
};

/* mt76_dev of a phy, through the i_private of its mt76 debugfs "regval" file. */
static struct mt76_dev *phy_dev(const char *phy)
{
	char path[96];
	struct mt76_dev *dev;
	struct file *f;

	snprintf(path, sizeof(path), "/sys/kernel/debug/ieee80211/%s/mt76/regval", phy);
	f = filp_open(path, O_RDONLY, 0);
	if (IS_ERR(f))
		return NULL;
	dev = file_inode(f)->i_private;
	filp_close(f, NULL);
	return dev;
}

/* IQ stream collector. fw/src/iqstream.c sends 48-byte headers (rxd[0] PKT_TYPE 7, eid at +28, sequence number at
 * +36, marker at +40, absolute sample position at +44) followed by raw ICAP ring words; mt7921 treats them as
 * unsolicited events and frees them. Each streaming card is bound to a slot ("iqbind": write "<slot> <phy>") with its
 * own ring (iqN) and counters (iqstatN), so both cards can stream at once. */
#define IQ_HDR			48
#define IQ_MARK			0x51514951
#define IQ_REC			0x52514951	/* "QIQR" record header in the ring */
#define IQ_SLOTS		2
#define IQ_RING_BYTES		(32u << 20)
static uint iq_eid = 0xcc;
module_param(iq_eid, uint, 0644);
MODULE_PARM_DESC(iq_eid, "MCU event id of the firmware IQ stream");

struct iq_slot {
	struct mt76_dev *dev;
	char phy[16];
	u8 *buf;
	size_t wr, rd;		/* byte positions, modulo IQ_RING_BYTES when indexing */
	spinlock_t lock;
	atomic_long_t pkts, bytes, gaps, drops;
	u32 next, last;
	bool seen;
};

static struct iq_slot iqs[IQ_SLOTS];
static atomic_long_t iq_unbound;
static bool iq_registered;

static void iq_reset(struct iq_slot *q)
{
	unsigned long flags;

	spin_lock_irqsave(&q->lock, flags);
	q->rd = q->wr = 0;
	q->seen = false;
	q->last = 0;
	spin_unlock_irqrestore(&q->lock, flags);
	atomic_long_set(&q->pkts, 0);
	atomic_long_set(&q->bytes, 0);
	atomic_long_set(&q->gaps, 0);
	atomic_long_set(&q->drops, 0);
}

/* Entry of mt7921_mcu_rx_event(dev, skb): dev in rdi, skb in rsi (not yet linearized). */
static int iq_pre(struct kprobe *p, struct pt_regs *regs)
{
	struct sk_buff *skb = (struct sk_buff *)regs->si;
	struct iq_slot *q = NULL;
	u32 h[IQ_HDR / 4], rec[4];
	unsigned long flags;
	size_t len, pos, n1;
	int i;

	if (skb->len < IQ_HDR || skb_copy_bits(skb, 0, h, IQ_HDR) || h[10] != IQ_MARK || (h[7] & 0xff) != iq_eid)
		return 0;
	for (i = 0; i < IQ_SLOTS; i++)
		if (iqs[i].buf && READ_ONCE(iqs[i].dev) == (struct mt76_dev *)regs->di)
			q = &iqs[i];
	if (!q) {
		atomic_long_inc(&iq_unbound);
		return 0;
	}
	len = skb->len - IQ_HDR;
	rec[0] = IQ_REC;
	rec[1] = h[9];
	rec[2] = h[11];
	rec[3] = len;
	spin_lock_irqsave(&q->lock, flags);
	if (q->seen && h[9] != q->next)
		atomic_long_add((u32)(h[9] - q->next), &q->gaps);
	q->next = h[9] + 1;
	q->last = h[9];
	q->seen = true;
	if (sizeof(rec) + len > IQ_RING_BYTES - (q->wr - q->rd)) {
		spin_unlock_irqrestore(&q->lock, flags);
		atomic_long_inc(&q->drops);
		return 0;
	}
	/* records are multiples of 16 bytes and the ring size a multiple of 16, so the header never wraps */
	memcpy(q->buf + q->wr % IQ_RING_BYTES, rec, sizeof(rec));
	q->wr += sizeof(rec);
	pos = q->wr % IQ_RING_BYTES;
	n1 = min_t(size_t, len, IQ_RING_BYTES - pos);
	skb_copy_bits(skb, IQ_HDR, q->buf + pos, n1);
	if (n1 < len)
		skb_copy_bits(skb, IQ_HDR + n1, q->buf, len - n1);
	q->wr += len;
	spin_unlock_irqrestore(&q->lock, flags);
	atomic_long_inc(&q->pkts);
	atomic_long_add(len, &q->bytes);
	return 0;
}

static struct kprobe iq_kp = {
	.symbol_name = "mt7921_mcu_rx_event",
	.pre_handler = iq_pre,
};

/* Single reader per slot: the producer never touches unread bytes, so copy outside the lock. */
static ssize_t iq_read(struct file *file, char __user *ubuf, size_t count, loff_t *ppos)
{
	struct iq_slot *q = file->private_data;
	unsigned long flags;
	size_t rd, avail, pos, n1;

	spin_lock_irqsave(&q->lock, flags);
	rd = q->rd;
	avail = q->wr - q->rd;
	spin_unlock_irqrestore(&q->lock, flags);
	count = min(count, avail);
	pos = rd % IQ_RING_BYTES;
	n1 = min_t(size_t, count, IQ_RING_BYTES - pos);
	if (copy_to_user(ubuf, q->buf + pos, n1) || copy_to_user(ubuf + n1, q->buf, count - n1))
		return -EFAULT;
	spin_lock_irqsave(&q->lock, flags);
	q->rd += count;
	spin_unlock_irqrestore(&q->lock, flags);
	return count;
}

static ssize_t iqstat_read(struct file *file, char __user *ubuf, size_t count, loff_t *ppos)
{
	struct iq_slot *q = file->private_data;
	char buf[224];
	int n = scnprintf(buf, sizeof(buf),
			  "pkts %ld bytes %ld gaps %ld drops %ld last %u queued %zu probe %d eid %#x phy %s unbound %ld\n",
			  atomic_long_read(&q->pkts), atomic_long_read(&q->bytes), atomic_long_read(&q->gaps),
			  atomic_long_read(&q->drops), q->last, q->wr - q->rd, iq_registered, iq_eid,
			  q->dev ? q->phy : "-", atomic_long_read(&iq_unbound));

	return simple_read_from_buffer(ubuf, count, ppos, buf, n);
}

static ssize_t iqstat_write(struct file *file, const char __user *ubuf, size_t count, loff_t *ppos)
{
	iq_reset(file->private_data);
	return count;
}

/* "<slot> <phy>": route that card's IQ packets to slot (ring emptied, counters reset); "<slot> -" unbinds. */
static ssize_t iqbind_write(struct file *file, const char __user *ubuf, size_t count, loff_t *ppos)
{
	char buf[32] = {}, phy[16] = {};
	unsigned int slot;

	if (count >= sizeof(buf) || copy_from_user(buf, ubuf, count))
		return -EINVAL;
	if (sscanf(buf, "%u %15s", &slot, phy) != 2 || slot >= IQ_SLOTS)
		return -EINVAL;
	WRITE_ONCE(iqs[slot].dev, strcmp(phy, "-") ? phy_dev(phy) : NULL);
	if (strcmp(phy, "-") && !iqs[slot].dev)
		return -ENODEV;
	strscpy(iqs[slot].phy, phy, sizeof(iqs[slot].phy));
	iq_reset(&iqs[slot]);
	return count;
}

static ssize_t iqbind_read(struct file *file, char __user *ubuf, size_t count, loff_t *ppos)
{
	char buf[96];
	int i, n = 0;

	for (i = 0; i < IQ_SLOTS; i++)
		n += scnprintf(buf + n, sizeof(buf) - n, "%d %s\n", i, iqs[i].dev ? iqs[i].phy : "-");
	return simple_read_from_buffer(ubuf, count, ppos, buf, n);
}

static const struct file_operations iq_fops = {
	.owner = THIS_MODULE,
	.open = simple_open,
	.read = iq_read,
};

static const struct file_operations iqstat_fops = {
	.owner = THIS_MODULE,
	.open = simple_open,
	.read = iqstat_read,
	.write = iqstat_write,
};

static const struct file_operations iqbind_fops = {
	.owner = THIS_MODULE,
	.read = iqbind_read,
	.write = iqbind_write,
};

static struct mt76_dev *dbg_lookup(void)
{
	return phy_dev(target);
}

static ssize_t target_write(struct file *file, const char __user *ubuf, size_t count, loff_t *ppos)
{
	char buf[16] = {};

	if (count >= sizeof(buf) || copy_from_user(buf, ubuf, count))
		return -EINVAL;
	mutex_lock(&dbg_lock);
	strscpy(target, strim(buf), sizeof(target));
	WRITE_ONCE(rxbw_dev, dbg_lookup());
	mutex_unlock(&dbg_lock);
	return count;
}

static ssize_t target_read(struct file *file, char __user *ubuf, size_t count, loff_t *ppos)
{
	char buf[20];
	int n = scnprintf(buf, sizeof(buf), "%s\n", target);

	return simple_read_from_buffer(ubuf, count, ppos, buf, n);
}

static ssize_t cmd_write(struct file *file, const char __user *ubuf, size_t count, loff_t *ppos)
{
	struct sk_buff *skb = NULL;
	struct mt76_dev *dev;
	void *req;
	int ret;

	if (count < 28 || count > DBG_MAX)
		return -EINVAL;
	req = memdup_user(ubuf, count);
	if (IS_ERR(req))
		return PTR_ERR(req);

	mutex_lock(&dbg_lock);
	rsp_len = 0;
	dev = dbg_lookup();
	if (!dev) {
		ret = -ENODEV;
		goto out;
	}
	ret = mt76_mcu_send_and_get_msg(dev, MCU_CE_QUERY_ID(DBG_CID), req, count, true, &skb);
	if (ret < 0)
		goto out;
	if (skb) {
		rsp_len = min_t(size_t, skb->len, sizeof(rsp));
		memcpy(rsp, skb->data, rsp_len);
		dev_kfree_skb(skb);
	}
	ret = count;
out:
	mutex_unlock(&dbg_lock);
	kfree(req);
	return ret;
}

static ssize_t raw_write(struct file *file, const char __user *ubuf, size_t count, loff_t *ppos)
{
	struct sk_buff *skb = NULL;
	struct mt76_dev *dev;
	u32 cmd;
	u8 *buf;
	int ret;

	if (count < 4 || count > DBG_MAX)
		return -EINVAL;
	buf = memdup_user(ubuf, count);
	if (IS_ERR(buf))
		return PTR_ERR(buf);

	mutex_lock(&dbg_lock);
	rsp_len = 0;
	dev = dbg_lookup();
	if (!dev) {
		ret = -ENODEV;
		goto out;
	}
	cmd = get_unaligned_le32(buf);
	ret = mt76_mcu_send_and_get_msg(dev, cmd & ~RAW_NOWAIT, buf + 4, count - 4, !(cmd & RAW_NOWAIT),
					cmd & RAW_NOWAIT ? NULL : &skb);
	if (ret < 0)
		goto out;
	if (skb) {
		rsp_len = min_t(size_t, skb->len, sizeof(rsp));
		memcpy(rsp, skb->data, rsp_len);
		dev_kfree_skb(skb);
	}
	ret = count;
out:
	mutex_unlock(&dbg_lock);
	kfree(buf);
	return ret;
}

static ssize_t cmd_read(struct file *file, char __user *ubuf, size_t count, loff_t *ppos)
{
	ssize_t ret;

	mutex_lock(&dbg_lock);
	ret = simple_read_from_buffer(ubuf, count, ppos, rsp, rsp_len);
	mutex_unlock(&dbg_lock);
	return ret;
}

static ssize_t mem_write(struct file *file, const char __user *ubuf, size_t count, loff_t *ppos)
{
	char buf[40] = {};
	u32 addr, len, chunk = 4;

	if (count >= sizeof(buf) || copy_from_user(buf, ubuf, count))
		return -EINVAL;
	if (sscanf(buf, "%i %i %i", &addr, &len, &chunk) < 2 || !len || len > MEM_MAX || (addr | len | chunk) & 3 ||
	    !chunk || chunk > MEM_CHUNK_MAX)
		return -EINVAL;
	mutex_lock(&dbg_lock);
	mem_addr = addr;
	mem_len = len;
	mem_chunk = chunk;
	mutex_unlock(&dbg_lock);
	return count;
}

static ssize_t mem_read(struct file *file, char __user *ubuf, size_t count, loff_t *ppos)
{
	struct mt76_dev *dev;
	size_t done = 0;
	ssize_t ret;
	u8 *buf;

	mutex_lock(&dbg_lock);
	if (*ppos >= mem_len) {
		ret = 0;
		goto out;
	}
	dev = dbg_lookup();
	if (!dev) {
		ret = -ENODEV;
		goto out;
	}
	count = min_t(size_t, count, mem_len - *ppos) & ~3;
	buf = kmalloc(MEM_CHUNK_MAX, GFP_KERNEL);	/* usb_control_msg needs a DMA-able buffer */
	if (!buf) {
		ret = -ENOMEM;
		goto out;
	}
	while (done < count) {
		u32 addr = mem_addr + *ppos + done;
		size_t n = min_t(size_t, mem_chunk, count - done);

		ret = mt76u_vendor_request(dev, MT_VEND_READ_EXT, USB_DIR_IN | MT_USB_TYPE_VENDOR, addr >> 16,
					   addr & 0xffff, buf, n);
		if (ret != n)
			break;
		if (copy_to_user(ubuf + done, buf, n))
			break;
		done += n;
	}
	kfree(buf);
	*ppos += done;
	ret = done ? done : -EIO;
out:
	mutex_unlock(&dbg_lock);
	return ret;
}

static ssize_t rxbw_read(struct file *file, char __user *ubuf, size_t count, loff_t *ppos)
{
	char buf[320];
	int i, n = 0;

	for (i = 0; i < 8; i++)
		n += scnprintf(buf + n, sizeof(buf) - n, "bw%d %ld\n", i, atomic_long_read(&rxbw_count[i]));
	n += scnprintf(buf + n, sizeof(buf) - n, "last_narrow_rxv %08x %08x %08x %08x\nother_devices %ld\n"
		       "fix %d probe %d target %s\n", rxbw_last[0], rxbw_last[1], rxbw_last[2], rxbw_last[3],
		       atomic_long_read(&rxbw_other), rxbw_fix, rxbw_registered, rxbw_dev ? target : "-");
	return simple_read_from_buffer(ubuf, count, ppos, buf, n);
}

static ssize_t rxbw_write(struct file *file, const char __user *ubuf, size_t count, loff_t *ppos)
{
	int i;

	for (i = 0; i < 8; i++)
		atomic_long_set(&rxbw_count[i], 0);
	atomic_long_set(&rxbw_other, 0);
	memset(rxbw_last, 0, sizeof(rxbw_last));
	return count;
}

static const struct file_operations rxbw_fops = {
	.owner = THIS_MODULE,
	.read = rxbw_read,
	.write = rxbw_write,
};

static const struct file_operations target_fops = {
	.owner = THIS_MODULE,
	.read = target_read,
	.write = target_write,
};

static const struct file_operations cmd_fops = {
	.owner = THIS_MODULE,
	.read = cmd_read,
	.write = cmd_write,
};

static const struct file_operations raw_fops = {
	.owner = THIS_MODULE,
	.read = cmd_read,
	.write = raw_write,
};

static const struct file_operations mem_fops = {
	.owner = THIS_MODULE,
	.read = mem_read,
	.write = mem_write,
};

static int __init mt7921dbg_init(void)
{
	int i;

	dbg_dir = debugfs_create_dir("mt7921dbg", NULL);
	debugfs_create_file("target", 0600, dbg_dir, NULL, &target_fops);
	debugfs_create_file("cmd", 0600, dbg_dir, NULL, &cmd_fops);
	debugfs_create_file("raw", 0600, dbg_dir, NULL, &raw_fops);
	debugfs_create_file("mem", 0600, dbg_dir, NULL, &mem_fops);
	debugfs_create_file("rxbw", 0600, dbg_dir, NULL, &rxbw_fops);
	for (i = 0; i < IQ_SLOTS; i++) {
		spin_lock_init(&iqs[i].lock);
		iqs[i].buf = vmalloc(IQ_RING_BYTES);
	}
	if (iqs[0].buf && iqs[1].buf && !register_kprobe(&iq_kp)) {
		char name[16];

		iq_registered = true;
		for (i = 0; i < IQ_SLOTS; i++) {
			snprintf(name, sizeof(name), "iq%d", i);
			debugfs_create_file(name, 0400, dbg_dir, &iqs[i], &iq_fops);
			snprintf(name, sizeof(name), "iqstat%d", i);
			debugfs_create_file(name, 0600, dbg_dir, &iqs[i], &iqstat_fops);
		}
		debugfs_create_file("iqbind", 0600, dbg_dir, NULL, &iqbind_fops);
	} else {
		pr_warn("mt7921dbg: IQ collector disabled (kprobe on mt7921_mcu_rx_event or ring allocation failed)\n");
	}
	if (register_kprobe(&rxbw_kp))
		pr_warn("mt7921dbg: kprobe on mt76_connac2_mac_fill_rx_rate failed, rxbw disabled\n");
	else
		rxbw_registered = true;
	if (register_kretprobe(&txc_krp))
		pr_warn("mt7921dbg: kretprobe on mt76_connac2_mac_write_txwi failed, tx_count has no effect\n");
	else
		txc_registered = true;
	return 0;
}

static void __exit mt7921dbg_exit(void)
{
	int i;

	if (rxbw_registered)
		unregister_kprobe(&rxbw_kp);
	if (txc_registered)
		unregister_kretprobe(&txc_krp);
	if (iq_registered)
		unregister_kprobe(&iq_kp);
	debugfs_remove_recursive(dbg_dir);
	for (i = 0; i < IQ_SLOTS; i++)
		vfree(iqs[i].buf);
}

module_init(mt7921dbg_init);
module_exit(mt7921dbg_exit);
MODULE_LICENSE("GPL");
MODULE_SOFTDEP("pre: mt76");
MODULE_DESCRIPTION("Host side of the instrumented MT7961 firmware debug command");
