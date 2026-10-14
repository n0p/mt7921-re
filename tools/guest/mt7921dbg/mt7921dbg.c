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
 */
#include <linux/module.h>
#include <linux/debugfs.h>
#include <linux/fs.h>
#include <linux/skbuff.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/usb.h>
#include <linux/unaligned.h>

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

static struct mt76_dev *dbg_lookup(void)
{
	char path[96];
	struct mt76_dev *dev;
	struct file *f;

	snprintf(path, sizeof(path), "/sys/kernel/debug/ieee80211/%s/mt76/regval", target);
	f = filp_open(path, O_RDONLY, 0);
	if (IS_ERR(f))
		return NULL;
	dev = file_inode(f)->i_private;
	filp_close(f, NULL);
	return dev;
}

static ssize_t target_write(struct file *file, const char __user *ubuf, size_t count, loff_t *ppos)
{
	char buf[16] = {};

	if (count >= sizeof(buf) || copy_from_user(buf, ubuf, count))
		return -EINVAL;
	mutex_lock(&dbg_lock);
	strscpy(target, strim(buf), sizeof(target));
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
	dbg_dir = debugfs_create_dir("mt7921dbg", NULL);
	debugfs_create_file("target", 0600, dbg_dir, NULL, &target_fops);
	debugfs_create_file("cmd", 0600, dbg_dir, NULL, &cmd_fops);
	debugfs_create_file("raw", 0600, dbg_dir, NULL, &raw_fops);
	debugfs_create_file("mem", 0600, dbg_dir, NULL, &mem_fops);
	return 0;
}

static void __exit mt7921dbg_exit(void)
{
	debugfs_remove_recursive(dbg_dir);
}

module_init(mt7921dbg_init);
module_exit(mt7921dbg_exit);
MODULE_LICENSE("GPL");
MODULE_SOFTDEP("pre: mt76");
MODULE_DESCRIPTION("Host side of the instrumented MT7961 firmware debug command");
