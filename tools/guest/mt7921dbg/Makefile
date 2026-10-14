# Out-of-tree build against the running guest kernel. mt76.symvers carries the modversions CRC of the
# loaded mt76.ko's mt76_mcu_send_and_get_msg (read from mt7921-common.ko's __versions section) and of
# mt76-usb.ko's mt76u_vendor_request (from mt792x-usb.ko's __versions).
obj-m := mt7921dbg.o
KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) KBUILD_EXTRA_SYMBOLS=$(CURDIR)/mt76.symvers modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
