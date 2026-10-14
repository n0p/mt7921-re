/* Replaces `jal rlmPhySetChannel` at 0x923d12 in FUN_923ca0 with a call through sniffer_outband_tramp.
 * @link 0x00923d12
 * @defsym sniffer_outband_tramp=0x0096dc00
 */
	.text
	jal	sniffer_outband_tramp
