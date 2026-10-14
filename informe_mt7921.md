# mt76 / MT7921-MT7961 (connac2-3): superficie de control real, 5/10 MHz, OCB y ruta legal

Verificado el 2026-10-14 sobre `master` de torvalds/linux y `main` de linux-firmware. Todo lo citable esta en **HECHOS**; mi lectura esta en **INFERENCIAS**.

---

## 1) Testmode nl80211 de mt76

### La API comun (existe, pero MT7921 no la usa)

- `drivers/net/wireless/mediatek/mt76/testmode.c` define los dos handlers: `mt76_testmode_cmd()` (linea 407) y `mt76_testmode_dump()` (linea 570). El alta de la API es el commit `f0efa8621550` "mt76: add API for testmode support" (21-jul-2020).
  https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/plain/drivers/net/wireless/mediatek/mt76/testmode.c
- Set completo de atributos en `testmode.h`: `RESET, STATE, MTD_PART, MTD_OFFSET, TX_COUNT, TX_LENGTH, TX_RATE_MODE, TX_RATE_NSS, TX_RATE_IDX, TX_RATE_SGI, TX_RATE_LDPC, TX_RATE_STBC, TX_LTF, TX_ANTENNA, TX_POWER_CONTROL, TX_POWER, FREQ_OFFSET, STATS, TX_SPE_IDX, TX_DUTY_CYCLE, TX_IPG, TX_TIME, DRV_DATA, MAC_ADDRS`. **No hay** atributo de lectura/escritura de registros RF, ni de escritura de EEPROM/flash, ni Txbf por testmode. El token `dac_dig_load` no aparece en ninguno de los cinco ficheros de testmode del arbol.
- Estados: `OFF, IDLE, TX_FRAMES, RX_FRAMES, TX_CONT, ON`. Modos de tasa: `CCK, OFDM, HT, VHT, HE_SU, HE_EXT_SU, HE_TB, HE_MU`.
- Quien registra la API comun son **solo mt7915 y mt7615**: `CFG80211_TESTMODE_CMD(mt76_testmode_cmd)` en `mt7915/main.c:1826` y `mt7615/main.c:1361`. Handlers concretos de mt7915: `mt7915_tm_set_tx_power`, `mt7915_tm_set_freq_offset`, `mt7915_tm_set_trx`, `mt7915_tm_set_slot_time`, `mt7915_tm_set_tam_arb`, `mt7915_tm_set_wmm_qid`, `mt7915_tm_set_ipg_params`, `mt7915_tm_set_tx_len`, `mt7915_tm_set_tx_frames`, `mt7915_tm_set_rx_frames`, `mt7915_tm_set_tx_cont`, `mt7915_tm_rf_switch_mode` (`RF_OPER_NORMAL/RF_TEST/ICAP/ICAP_OVERLAP/WIFI_SPECTRUM`) y `mt7915_tm_reg_backup_restore`, que guarda/restaura registros WF incluidos `WF_RFCR` y `WF_RFCR1`. En mt7615: `mt7615_tm_set_tx_power` (reenvia un corte completo de EEPROM con `MCU_EXT_CMD(SET_TX_POWER_CTRL)`), `mt7615_tm_set_tx_antenna` (writes directos a `MT_ANT_SWITCH_CON*`), `mt7615_tm_update_params` (`MCU_ATE_SET_TX_POWER_CONTROL`, `MCU_ATE_SET_FREQ_OFFSET`).
- **MT7921 no pasa por ahi.** `mt7921/main.c:1588-1589` registra `CFG80211_TESTMODE_CMD(mt7921_testmode_cmd)`, y ese handler solo acepta `MT76_TM_ATTR_DRV_DATA` → `MT7921_TM_ATTR_SET` / `MT7921_TM_ATTR_QUERY`; cualquier otra cosa devuelve `-EINVAL`. Exige `MT76_STATE_RUNNING` y `IEEE80211_CONF_MONITOR`.
  https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/plain/drivers/net/wireless/mediatek/mt76/mt7921/testmode.c
- La superficie real es un tunel crudo: `struct mt7921_tm_cmd { u8 action; u32 param0; u32 param1; }` → `mt7921_rftest_cmd` → `mt76_mcu_send_msg(MCU_CE_CMD(TEST_CTRL))`. Acciones (`mt7921/mcu.h`): `TM_SWITCH_MODE`, `TM_SET_AT_CMD`, `TM_QUERY_AT_CMD`. Modos: `MT7921_TM_NORMAL`, `TESTMODE`, `ICAP`, `ICAP_OVERLAP`, `WIFISPECTRUM`. Al entrar en testmode apaga el power management (`pm->enable = false`, cancela `ps_work`/`wake_work`).
- MT7925 igual pero con payload ATE explícito: `MCU_UNI_CMD(TESTMODE_CTRL)`, `enum ENUM_CMD_TEST_CTRL_ACT { SWITCH_MODE=0, SET_AT=1, GET_AT=2, SET_AT_ENG=3, GET_AT_ENG=4 }`, modos `NORMAL/RF_TEST/ICAP`, y `union testmode_data { op_mode; channel_freq; u8 rf_at_info[84]; }` / `testmode_evt { ... u8 rf_at_info[1024]; }`. Es decir: consola ATE del firmware accesible desde usuariospace.

### EEPROM / flash

- El kernel **no escribe EEPROM**. Lo unico que hace es publicar `MT76_TM_ATTR_MTD_PART` y `MTD_OFFSET` en el dump, y eso solo si `mt76_get_of_data_from_mtd()` (`mt76/eeprom.c`) lo llenó desde el arbol de dispositivos bajo `#ifdef CONFIG_NL80211_TESTMODE`. En un MT7921 PCIe no hay MTD ni OF: el atributo no existe.
- La herramienta `mt76-test` de openwrt/mt76 lo confirma: el `eeprom set` hace `mmap()` sobre `/dev/mtd…` en usuariospace, y su README describe la herramienta como "similar al set de comandos ATE del driver SDK", con solo `state, tx_count, tx_length, tx_rate_mode, tx_rate_idx, tx_rate_sgi, tx_rate_ldpc, tx_power_control, tx_power, tx_antenna, freq_offset`.
  https://github.com/openwrt/mt76/blob/master/tools/README.md

### Si se pueden escribir registros WF / RF: por debugfs, no por testmode

- `mt76_register_debugfs_fops()` (`mt76/debugfs.c`) crea `regidx` (u32, 0600) y `regval` (0600) con `mt76_reg_get/mt76_reg_set` → `__mt76_rr/__mt76_wr`. **Lectura y escritura arbitraria del espacio de registros del chip.** `eeprom` y `otp` se exponen como `debugfs_create_blob(..., 0400, ...)`: solo lectura.
- `mt7921/debugfs.c` registra `fops_regval` con wrappers `mt7921_reg_get/set` que toman `mt792x_mutex_acquire()` alrededor de `mt76_wr/mt76_rr`. Anade `fw_debug`, `runtime-pm`, `idle-timeout`, `chip_reset`, `deep-sleep`, `txpower_sku` (lectura), `tx_stats`, `acq`.
- Escritura de **RF** por registro solo existe en mt7915 y mt7615: `mt7915/debugfs.c` crea `rf_regval` → `mt7915_mcu_rf_regval(dev, reg, &val, write)` (via MCU), y `mt7615/debugfs.c` crea `rf_wfidx` + `rf_regidx` + `rf_regval`. **El arbol mt7921/mt7925 no tiene `rf_regval`.**
- MT7921 ademas comparte `MT_WF_RFCR` y compañía del espacio WF, alcanzables por `regidx/regval`, pero no hay un path de "RF register write" semantico como en mt7915.

**Respuesta corta:** si — se puede escribir el espacio WF/SMU/MMIO de un MT7921 con `regidx/regval` y se puede hablar con la consola ATE del firmware por el tunel `TEST_CTRL`; no — el testmode mainline no expone rf read/write, eeprom write, flash write, txbf ni `dac_dig_load`, y esas cifras son especificas de mt7915/mt7615.

---

## 2) 5 y 10 MHz (NL80211_CHAN_WIDTH_5 / _10)

**El driver SI lo traduce; el nucleo ya no lo deja pasar.**

- `mt76_connac.h`: `enum { CMD_CBW_20MHZ = IEEE80211_STA_RX_BW_20 /*0*/, 40MHZ /*1*/, 80MHZ /*2*/, 160MHZ /*3*/, CMD_CBW_10MHZ /*4*/, CMD_CBW_5MHZ /*5*/, 8080MHZ /*6*/, 320MHZ /*7*/ }`, y el inline `mt76_connac_chan_bw()` mapea `NL80211_CHAN_WIDTH_5 → CMD_CBW_5MHZ` y `_10 → CMD_CBW_10MHZ`.
- Usos en el path de MT7921 sin rechazo explicito: `mt7921_mcu_set_chan_info()` con `.bw = mt76_connac_chan_bw(chandef)` (`mt7921/mcu.c` ~913) y `mt76_connac_mcu_uni_set_chctx()` (TLV `UNI_BSS_INFO_RLM`), con `case NL80211_CHAN_WIDTH_5: rlm_req.rlm.bw = CMD_CBW_5MHZ;` y `_10` analogo, justo antes del `default: 20MHZ`. `mt7915_tm_set_tx_cont()` tiene los mismos casos para el comando `RF_TEST` (`TX_CONT_START 0x05` / `TX_CONT_STOP 0x06`).
- El bloqueo esta en cfg80211 y es reciente: commit `1a15bf9708ba3bf80410065e113aa17cd6a18dcf`, **"wifi: cfg80211: remove 5/10 MHz channel support"**, Johannes Berg, 29-may-2026, 8 ficheros +11/-49. Borra `WIPHY_FLAG_SUPPORTS_5_10_MHZ` y deja `_nl80211_parse_chandef()` rechazando siempre con extack `"5/10 MHz not supported any more"`. Toca ath5k, ath9k, mac80211_hwsim, `include/net/cfg80211.h`, `net/wireless/{chan,core,nl80211,util}.c`.
  Justificacion literal: "We contemplated this back in early 2023 and didn't do it yet, but nobody stepped up to maintain it. It's already _mostly_ dead code since it can really only be used for AP and maybe IBSS and monitor, but not on a client since there's no way to scan".
- Esta en **v7.2** (el fichero del tag v7.2 contiene el texto nuevo); **no** esta en v7.1; si en v7.3-rc1.
- Y ojo: mt76 **nunca** puso `WIPHY_FLAG_SUPPORTS_5_10_MHZ`, asi que incluso en kernels viejos `iw` devolvia "5/10 MHz not supported" en un phy mt76. Los valores del enum UAPI siguen ahi por ABI.
- mac80211 tampoco lo representa: `enum ieee80211_sta_rx_bandwidth` solo tiene 20/40/80/160/320 y `ieee80211_chan_width_to_rx_bw()` hace `WARN_ON_ONCE(1)` y cae a 20 MHz.

**Consecuencia:** el camino limpio `iw … 10MHz` → cfg80211 → TLV del MCU esta cerrado desde v7.2 (y nunca estuvo abierto para mt76). Lo que queda vivo es lo que no pasa por el UAPI: el tunel ATE/`TEST_CTRL` y la escritura de registros.

---

## 3) 802.11p / OCB en mainline

- **Mergeado desde v3.19.** Commit `6e0bd6c35b02` "cfg80211: 802.11p OCB mode handling", Rostislav Lisovy (CVUT), 3-nov-2014. `net/wireless/ocb.c` y `net/mac80211/ocb.c` existen ya en el tag v3.19; `NL80211_CMD_JOIN_OCB` aparece en el UAPI de v3.19 y no en v3.18.
- El mensaje original fija la semantica: "A mandatory parameters during the ocb_join operation are 'center frequency' and 'channel width (5/10 MHz)'" — es decir, OCB nacio dependiendo justo del ancho que cfg80211 acaba de eliminar.
- Estado hoy: `NL80211_IFTYPE_OCB`, `NL80211_CMD_JOIN_OCB/LEAVE_OCB`, `nl80211_join_ocb()` (~15261) llamando a `nl80211_parse_chandef()` → **rechazo de 5/10 desde v7.2**. mac80211 conserva `ieee80211_ocb_join()`, `BSS_CHANGED_OCB`, EDCA propio (`util.c`: "Use another EDCA parameters if dot11OCBActivated=true") y la restriccion "Only OCB and monitor mode may coexist" (`iface.c`, `-EBUSY` si no).
- **Drivers que anuncian OCB: ath9k y mac80211_hwsim.** ath9k en `init.c` (`interface_modes |= BIT(NL80211_IFTYPE_OCB)`) y lo gestiona en `main.c` (`iter_data.nocbs → ah->opmode = NL80211_IFTYPE_OCB`). ath10k, ath12k e iwlwifi: cero referencias. **mt76: cero referencias a OCB en los 217 ficheros `.c/.h` del directorio**; `mt792x_core.c` fija `interface_modes = STATION|AP|P2P_CLIENT|P2P_GO|P2P_DEVICE` (+NAN solo si el firmware declara `MT792x_FW_CAP_CNM` y `MT792x_FW_CAP_NAN`).
- Proyectos DSRC/ITS reales y su chip — todos Atheros:
  - **OpenC2X** (CCS Labs / TU Berlin): kernel 3.18 con OCB, tarjetas ath9k (AR9220, AR9280, AR9382); interoperabilidad verificada contra el equipo comercial **Cohda Wireless MK5**. https://www.ccs-labs.org/bib/laux2016openc2x/laux2016openc2x.pdf
  - **OpenWrt-V2X** (francescoraves483), OpenWrt 21.02.1 parcheado para DSRC, NICs ath5k/ath9k (UNEX DHXA-222, AR9642); arranque con `iw dev wlan0 ocb join 5890 10MHz`; documenta que ciertos data rates dan `Invalid argument (-22)` segun chipset.
  - **car2x-capture** (WernersGit): Pi 5 + Atheros AR9300 mPCIe, parche `11p-on-linux` de HPI Potsdam sobre `rpi-6.12.y`, OCB a 5900 MHz / 10 MHz.
  - Tesis 802.11p de jfpastrana (UVa): exige tarjetas con driver open source.
  - Ruta emergente fuera de mac80211: **ESP32-C5** (`Boslx/esp32-its-tap`, fork de `TheEnbyperor/esp32-c-its`) con la ruta PHY interna reingenierizada de Espressif, con aviso explicito de que transmitir en 5.9 GHz puede requerir licencia.
- **No he encontrado ni un solo proyecto DSRC/ITS documentado sobre mt76 / MT7921.**

---

## 4) Ruta legal MediaTek

**Driver: abierto. Firmware: blob, siempre.**

- El codigo que MediaTek aporta a mt76 esta marcado `SPDX-License-Identifier: BSD-3-Clause-Clear` con "Copyright (C) MediaTek Inc." (`mt76_connac_mcu.c/.h`, `mt7615/testmode.c`, `mt7915/testmode.h`, `mt7921/init.c`).
- `WHENCE` de linux-firmware: 27 bloques MediaTek; 25 dicen "Redistributable. See LICENCE.mediatek for details" y 2 la variante `LICENCE.ralink_a_mediatek_company_firmware`. **Cero bloques MediaTek tienen linea `Source:`** — el campo que el repo usa para vincular fuentes, y que si tienen carl9170fw, dsp56k, av7110, keyspan_pda, usbdux, isci o cis/src (18 lineas `Source:` en todo el WHENCE, ninguna de MediaTek).
- MT7921 hoy: `mediatek/WIFI_MT7961_patch_mcu_1_2_hdr.bin` (Version 20260224110909a) + `mediatek/WIFI_RAM_CODE_MT7961_1.bin` (Version 20260224110949), "Licence: Redistributable. See LICENCE.mediatek", `SPDX-License-Identifier: LicenseRef-LICENCE.mediatek`. Nota: MT7961 es el silicio del MT7921 (`mt792x.h`: `MT7921_FIRMWARE_WM "mediatek/WIFI_RAM_CODE_MT7961_1.bin"`); MT7920 usa las variantes `1a`.
- **Casos que preguntabas, sin fuente:** MT7601U (`mediatek/mt7601u.bin`, version 34, licencia ralink_a_mediatek_company_firmware), MT76x0/MT7610E (`mt7610u.bin`, `mt7610e.bin`, `mt7650e.bin`), MT7615 (`mt7615_n9.bin`, `mt7615_cr4.bin`, `mt7615_rom_patch.bin`), MT7915 (`mt7915_wm.bin`, `mt7915_wa.bin`, `mt7915_rom_patch.bin`) — todos binarios, sin `Source:`. Los ficheros `mt7915_eeprom.bin` / `mt7915_eeprom_dbdc.bin` / `*_mt7976_dbdc.bin` son **datos de calibracion EEPROM**, no fuentes. `WIFI_RE6` no aparece en WHENCE. **Lo digo claro: no hay ni un firmware WiFi de MediaTek publicado con fuentes, y no he encontrado ningun repo "linux-master" de MTK que cambie eso.**
- `LICENSES/LICENCE.mediatek`, texto integro: "MediaTek Inc. grants permission to use and redistribute aforementioned firmware files for the use with devices containing MediaTek chipsets, but not as part of the Linux kernel or in any other form which would require these files themselves to be covered by the terms of the GNU General Public License or the GNU Lesser General Public License. These firmware files are distributed in the hope that they will be useful, but are provided WITHOUT ANY WARRANTY…". **No prohibe ingenieria inversa.**
- La antigua `LICENCE.ralink_a_mediatek_company_firmware` —que aun cubre `mt7601u.bin` y los `mt7662*`— **si** prohibe: "Redistribution and use in binary form, **without modification**, are permitted…" y "**No reverse engineering, decompilation, or disassembly of this software is permitted.**"
- Criterio del proyecto (`LICENSE-CRITERIA.md`): "Firmware may be binary-only. Source is welcome whenever the firmware's owner can provide it, but a binary-only image is acceptable as long as its license meets the criteria", exigiendo redistribucion irrestricta "with no signatures, registration, click-through, or payment required".
- **NDA:** no he encontrado **ningun** documento publico de MediaTek que enuncie un NDA como condicion para obtener fuentes de firmware, ni repos publicos de MediaTek con esas fuentes (la unica organizacion `linux-mediatek` de GitHub tiene 2 repos, de kernel para SoCs moviles). No lo doy como cierto ni como falso: **no hay evidencia**.
- La unica via de "fuentes" documentada por la comunidad son las liberaciones GPL downstream de kernels de producto: "the downstream sources for several MediaTek SoC kernels had header and C files that described the structures in the firmware binaries" (codigo en `drivers/misc/mediatek/connectivity/wlan`, `gen2/gen3/include/wlan_lib.h`). Eso describe el contenedor del firmware, **no** el codigo del MCU.
- Soporte inexistente: la bitacora de Linaro del upstreaming del MT7927/Filogic 380 registra que el issue `openwrt/mt76#927` acumulo 89 comentarios y 46 upvotes y "**No MediaTek developer ever replied to any of them**"; el trabajo salio de ingenieria inversa del paquete Windows (el firmware Bluetooth estaba oculto dentro del contenedor de firmware WiFi) + 18 parches WiFi + 2 BT + un envio a linux-firmware.
  https://www.linaro.org/blog/from-replace-it-with-intel-to-upstream-bringing-mediatek-bluetooth-wifi-7-to-linux

---

## 5) Firmware MT7921/MT7961 MODIFICADO en hardware real

**Si existe evidencia publica, pero no de lo que buscamos.**

- Repo `MtkWifiRev/MtkWifiTools` (GitHub, BSD-3-Clause, creado 2025-01-23, ultimo push 2025-10-18). Su propio README documenta:
  - `MT7961Merge.c` — "convert the ram + rom patch of the mt7921 (aka mt7961) hardware into an elf file".
  - `PatchIncorporator.c` — "Manipulate the format of the ROM patch by merging it with the RAM firmware **for bypassing the ROM crc32 integrity check**".
  - `ramreader` — "read the disassembled NDS32 instructions in the live RAM **by abusing the regidx/regval feature**".
  - `barepatcher` — parcheador de RAM en vivo: escribes C en `./code_output/nds32_code.c`, se compila con NDS32 gcc y se inyectan los bytes; "**tested with mt7922 and permitted to bypass the firmware encryption**".
  - `mapperremapper` — volcado ELF del espacio mapeado por PCIe; "It helped a lot for **breaking mt7922's AES encryption**". Funciona con mt7915/7916/7981/7986/**mt7921**/mt7922/mt7925.
  - `MASS` — scraper que descarga todos los `WIFI_RAM_CODE` de `dumps.tadiphone.dev`.
- Charla **nullcon 2026, "Unlock hidden Superpowers in MediaTek Wi-Fi Chips"**: mapa de ISA (802.11b/g = ARM32, n/ac/ax = Andes NDS32, WiFi 7 = RISC-V N45), lectura de memoria del MCU por `regidx/regval`, coredump con backtrace de 32 funciones, y anuncia `romdumper`, `Mtkmerge`, **`Mtkcustomizer`** ("inject an NDS32 binary blob into the RAM or ROM patch as a new region, permits also to completely overwrite regions with your own code"), `RamReader` y `Descrambler` (XOR y AES128 con clave conocida). PDF: https://www.nullcon.net/wp-content/uploads/2026/04/Unlock-hidden-Superpowers-in-MediaTek-WiFi-Chips.pdf
- Sobre integridad: la ruta de carga del driver (`mt76_connac2_load_patch()` / `mt76_connac2_load_ram()`) solo parsea cabeceras (`mt76_connac2_patch_hdr`, `mt76_connac2_fw_trailer`, `mt76_connac2_fw_region`) y envia cada region — **sin ninguna comprobacion criptografica**.Analistas independientes (cyrozap/mediatek-wifi-re, y el registro publico de formato de `zachery.lol/code/zacheryasc/mtk-wifi-fw`) coinciden en: trailer de 36 bytes con `crc`, regions con flag `FW_FEATURE_SET_ENCRYPT` sin usar en las revisiones observadas, y que parte de los parches ALPS vienen cifrados/ofuscados con claves reconstruibles.
- **Lo que NO he encontrado, y lo declaro:** ningun firmware MT7921/MT7961 modificado publicado o testeado por terceros que cambie el ancho de canal a 5/10 MHz, ni un "firmware regional desbloqueado" instalado en hardware real por un usuario final. Los casos documentados son rotura de cifrado, parcheo de RAM y herramientas de analisis (mt7921/mt7922/mt7981), no esa funcionalidad concreta.

---

## INFERENCIAS (mi lectura, no hechos)

1. **"Control total del firmware" es la unica ruta que sobrevive al calendario del kernel.** cfg80211 borro 5/10 MHz en v7.2 y mt76 nunca lo anuncio; el tunel ATE (`TEST_CTRL`) y `regidx/regval` no dependen del UAPI y nadie los mantiene ni los cierra.
2. **Sobre el gate `BW <= 3` que barajais: no lo he podido verificar** — no hay fuentes publicas del firmware, asi que trato eso como hipotesis vuestra. Lo que si es verificable es el encoding del lado del driver: 160 MHz = 3, 10 MHz = 4, 5 MHz = 5, 80+80 = 6, 320 = 7. Un umbral `<=3` bloquea exactamente 10/5 MHz y 80+80/320. Subirlo a `<=5` abriria 10 y 5 sin tocar 80+80; un `<=6` ingenuo habilitaria tambien 8080 en ese path.
3. **Riesgo silencioso de encoding cruzado:** en el MCU connac 10 MHz = 4 y 5 MHz = 5, pero en el UAPI `nl80211_chan_width` WIDTH_5 = 6 y WIDTH_10 = 7. Si la comprobacion se hace sobre el valor que llega por cfg80211 en vez del que sale en el TLV, se valida el numero equivocado. Instrumentad `mt76_connac_chan_bw()` / `mt7921_mcu_set_chan_info()` antes de tocar el blob.
4. **Asimetria legal por licencia:** LICENCE.mediatek (MT7961/MT7921, MT7915, MT7615) no prohibe desensamblar; la variante `ralink_a_mediatek_company_firmware` (mt7601u, mt7662) si prohibe reverse engineering y la redistribucion sin modificacion. Si el trabajo se queda en MT7961, estais en la permisiva — no arrastreis blobs viejos.
5. **"Redistributable" cubre el blob, no un blob modificado:** un `WIFI_RAM_CODE` alterado ya no son "aforementioned firmware files". Publicar el binario parcheado es el punto mas debil; un parche delta o instrucciones de reproceso aplicadas por el usuario es lo que hace MtkWifiTools (publica herramientas, no blobs).
6. **Dos tarjetas vuestras a 5/10 MHz no prueban interoperabilidad ni cumplimiento.** OCB en mainline ya no puede unirse a 10 MHz: montar OCB sobre mt7921 exige parchear el driver (`interface_modes` + manejo MCU) ademas del firmware. Para validar solo el PHY, monitor + `TX_CONT` medido contra un SDR es mas robusto que un enlace OCB auto-construido.
7. **MT7961/MT7921 es la plataforma correcta donde invertir:** es el connac2 mejor cartografiado del arbol, es el unico con herramientas publicas de merge/injection, y el techo es de tiempo de RE en NDS32 (modulo Ghidra incompleto: las extensiones de opcodes de inlining aun no funcionan, segun su propio README), no de NDA ni de firma criptografica.
8. **El coste del byte-patch es acotado y re-localizable:** WHENCE declara las versiones (`20260224110909a` patch / `20260224110949` RAM) y el trailer lleva `build_date` + `crc`, asi que el patron se puede re-encontrar automaticamente en cada actualizacion de linux-firmware en lugar de congelar un offset. Eso es lo que hace sostenible la opcion "parchear" frente a "reescribir el firmware".
