/* Hook code lives in free SRAM right after RAM region 3 (0xe0270000..0xe027cad0), appended to that region. */
INCLUDE fwsyms.ld

SECTIONS
{
	. = HOOK_BASE;
	.text : {
		*(.text.entry)
		*(.text .text.*)
		*(.rodata .rodata.*)
		*(.data .data.*)
		*(.sdata .sdata.*)
		*(.bss .bss.* .sbss .sbss.* COMMON)
		. = ALIGN(4);
	}
	/DISCARD/ : { *(.comment) *(.note*) *(.nds32*) }
}
