/* Trampoline for the UNI SNIFFER config TLV handler (FUN_923ca0): pass a u16 "outband" frequency in MHz,
 * taken from the TLV pad bytes (+14), as rlmPhySetChannel's outband argument (stack slot +0x14), which the
 * stock code hard-codes to 0. Linked at 0x96dc00 (region 0 trailer pad, within jal range of the caller).
 * In: s1 ($r7) = TLV pointer (callee-saved, set by the handler), args a0..a5 + stack set up for the call.
 * Value 0 (stock driver) keeps stock behaviour: rlmPhySetChannel ignores outband 0.
 *
 * @link 0x0096dc00
 * @defsym rlmPhySetChannel=0x00963f6c
 */
	.text
	.global sniffer_outband_tramp
sniffer_outband_tramp:
	lhi	$r15, [$r7 + 14]
	swi	$r15, [$sp + 0x14]
	j	rlmPhySetChannel
