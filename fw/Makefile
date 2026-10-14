# Instrumented MT7961 (MT7921) WM firmware build.
#   make            -> out/WIFI_RAM_CODE_MT7961_1.bin (stock image + hooks, checksums fixed)
#   make install    -> copy to the test guest and re-probe the cards
#   make restore    -> put the stock image back in the guest
#   make verify     -> check region PDA checksums and file crc of the output
#   make OUTBAND=1  -> also apply patches/outband.def (sniffer "outband" MHz override, notes/04-2g3-tuning.md)

STOCK      ?= ../work/fw/WIFI_RAM_CODE_MT7961_1.bin
STOCK_SHA  := b94217a951518a9c14095765f367bc5dd7698f2dc033941d6f18fc2ebd6a2ab9
HOOK_BASE  := 0xe027cb00
GUEST      ?= ../tools/g.sh

CROSS      := nds32le-elf-
CC         := $(CROSS)gcc
LD         := $(CROSS)ld
OBJCOPY    := $(CROSS)objcopy
NM         := $(CROSS)nm
PY         := python3 -I

# Match the firmware ABI: NDS32 v3, reduced register set. Large code model so hooks in 0xe027xxxx can call
# firmware code at 0x9xxxxx (out of jal range); never use $fp as gp (the firmware keeps fp == gp).
CFLAGS     := -Os -march=v3 -mreduced-regs -mcmodel=large -mno-ext-fpu-sp -mno-ext-fpu-dp \
              -fno-pic -ffreestanding -fno-builtin -fno-common -mno-fp-as-gp -Wall -Wextra -Iinclude
LDFLAGS    := --no-relax -T hooks.ld -L . --defsym=HOOK_BASE=$(HOOK_BASE)

SRCS       := $(wildcard src/*.c)
OBJS       := $(SRCS:src/%.c=build/%.o)
OUT        := out/WIFI_RAM_CODE_MT7961_1.bin

# Assembled in-place patches (asm/*.S). Each source declares its link address and symbols in comments:
#   @link 0xADDR   @defsym name=0xADDR
AS         := $(CROSS)as
ASFLAGS    := -EL -march=v3 -mreduced-regs -mno-all-relax
EXTRA_DEFS := $(if $(filter 1,$(OUTBAND)),patches/outband.def)
EXTRA_BINS := $(if $(EXTRA_DEFS),$(patsubst asm/%.S,build/asm/%.bin,$(wildcard asm/*.S)))

all: $(OUT)

build/%.o: src/%.c include/fw.h | build
	$(CC) $(CFLAGS) -c $< -o $@

build/hooks.elf: $(OBJS) hooks.ld fwsyms.ld
	$(LD) $(LDFLAGS) -o $@ $(OBJS)
	@$(NM) $@ | grep -q " U " && { echo "undefined symbols:"; $(NM) -u $@; exit 1; } || true

build/hooks.bin: build/hooks.elf
	$(OBJCOPY) -O binary $< $@
	@echo "hook blob: $$(stat -c %s $@) bytes at $(HOOK_BASE)"

build/hooks.nm: build/hooks.elf
	$(NM) $< > $@

build/asm/%.bin: asm/%.S | build
	@mkdir -p build/asm
	$(AS) $(ASFLAGS) -o build/asm/$*.o $<
	$(LD) --no-relax -e 0 -Ttext=$$(sed -n 's/.*@link *//p' $<) \
		$$(sed -n 's/.*@defsym */--defsym=/p' $<) -o build/asm/$*.elf build/asm/$*.o
	$(OBJCOPY) -O binary build/asm/$*.elf $@

$(OUT): $(STOCK) build/hooks.bin build/hooks.nm hooks.def $(EXTRA_DEFS) $(EXTRA_BINS) ../tools/mtfw_patch.py FORCE | out
	@echo "$(STOCK_SHA)  $(STOCK)" | sha256sum -c --quiet
	$(PY) ../tools/mtfw_patch.py $(STOCK) $@ blob:$(HOOK_BASE)=build/hooks.bin \
		$$($(PY) ../tools/hookargs.py hooks.def build/hooks.nm $(EXTRA_DEFS))

verify: $(OUT)
	$(PY) ../tools/mtfw_patch.py --verify $(OUT)

install: $(OUT)
	$(GUEST) 'mkdir -p /root/fwre && cat > /root/fwre/WIFI_RAM_CODE_MT7961_1.instr.bin' < $(OUT)
	$(GUEST) '/root/fwre/fwswap.sh /root/fwre/WIFI_RAM_CODE_MT7961_1.instr.bin'

restore:
	$(GUEST) '/root/fwre/fwswap.sh --restore'

build out:
	mkdir -p $@

clean:
	rm -rf build out

# FORCE: the output depends on OUTBAND, which make cannot track as a file
FORCE:

.PHONY: all verify install restore clean FORCE
