/* Packet pool layout: make FUN_94bb3e (pool setup at init) always take its ICAP-mode branch, so the boot reserves
 * MediaTek's own ICAP partition (128 banks = 256 KiB at 0xe0200000) and places PLE / PSE above it (PLE 0xe0240000,
 * PSE 0xe0245000..0xe025ffff). The streamer's capture ring (units 0..0xfffc) then never overwrites the datapath.
 * Replaces `beqz38 s1, 0x94bb8e` (normal-mode branch) with `movi55 s1, 2`; the following `bnec s1, 2` falls through.
 * s1 is reloaded at 0x94bbc8, and the boot mode itself stays normal. (notes/08-icap-ring-and-rate.md, Fix A)
 * @link 0x0094bb66
 */
	.text
	movi55	$r7, 2
