# -*- coding: utf-8 -*-
"""
TNAP: identify the demodulator and tuner (RF front-end) chips behind each NIM,
so users can look up what is actually doing the receiving and scanning.

enigma2 only knows the NIM name the driver reports in /proc/bus/nim_sockets,
which is sometimes the demodulator ("AVL62X1"), sometimes a product name
("Vuplus DVB-S NIM(SI2166)", "GIGA DVB-S2X NIM (TS3L10)") and never the RF
tuner. This module fills the gap, most reliable source first:

  1. KNOWN   - receiver/NIM combinations verified from the vendor drivers.
  2. Kernel  - chip names the drivers print while probing (dmesg), e.g. the
               Silicon Labs "Si2166-D60 Part running 'FW_6_1b4'" banner.
  3. Modules - open-source frontend/tuner drivers that are loaded
               (/proc/modules: m88ds3103, ts2020, stv090x, av201x, ...).
  4. NIM     - the chip named in the NIM name itself.

Anything that cannot be identified is reported as None, never guessed.
Results are cached in memory, and kernel-log hits are also written to
/etc/enigma2/tunerchips.json, because the boot messages scroll out of the
kernel ring buffer on a receiver that has been up for a while.

Public API:
  getTunerChips(slot, nimName)  -> {"demod": str|None, "tuner": str|None, "source": str}
  formatTunerChips(slot, nimName) -> "Si2166 (Silicon Labs) / RT710 (Rafael Micro)" or ""
"""
from json import dump, load
from os.path import isfile
from re import IGNORECASE, compile as reCompile
from subprocess import PIPE, Popen

CACHE_FILE = "/etc/enigma2/tunerchips.json"

# --- 1. Verified receiver / NIM combinations ---------------------------------
# (box type prefix, substring of the NIM name, demodulator, tuner)
# Only add entries confirmed from the vendor driver or the hardware.
KNOWN = (
	# Vu+ Zero 4K: dvb_base.ko si2166_rt710_* (Select_SAT_Tuner 0x710), Si2183 core, ROM 6.0.
	("vuzero4k", "SI2166", "Si2166 (Silicon Labs, Si2183 core)", "RT710 (Rafael Micro)"),
	# Octagon SF8008: hi-dvb.ko - RDA5815M is the only SiLabs SAT tuner driver built in (Si2166D path);
	# the AVL62X1 path drives the Airoha AV201x driver.
	("sf8008", "Si2166", "Si2166-D (Silicon Labs, Si2183 core)", "RDA5815M (RDA Microelectronics)"),
	("sf8008", "AVL62X1", "AVL62X1 (Availink)", "AV201x (Airoha)"),
	# Edision: edision-open dvb-frontends-avl62x1 bundles the av201x tuner driver.
	("os", "AVL6261", "AVL6261 (Availink, AVL62X1 family)", "AV201x (Airoha)"),
	("os", "AVL62X1", "AVL62X1 (Availink)", "AV201x (Airoha)"),
)

# --- 2. Kernel log patterns ---------------------------------------------------
# Silicon Labs L1 banner: "Si2166-D60 Part running 'FW_6_1b4'" /
# "Si21831-B60 Part running ..." (printed by every SiLabs-API based driver).
_SILABS_PART = reCompile(r"\b(Si21\d\d\d?)-([A-Z])(\d\d)\b[^\n]*Part running '?FW_(\w+)")
_DEMOD_LOG = (
	(reCompile(r"\bAVL6261\b"), "AVL6261 (Availink, AVL62X1 family)"),
	(reCompile(r"\bAVL62X1\b"), "AVL62X1 (Availink)"),
	(reCompile(r"\bM88DS3103B?\b", IGNORECASE), "M88DS3103 (Montage)"),
	(reCompile(r"\bM88RS6000\b", IGNORECASE), "M88RS6000 (Montage)"),
	(reCompile(r"\bSTV0910\b", IGNORECASE), "STV0910 (STMicroelectronics)"),
	(reCompile(r"\bSTV0903\b", IGNORECASE), "STV0903 (STMicroelectronics)"),
	(reCompile(r"\bSTV0900\b", IGNORECASE), "STV0900 (STMicroelectronics)"),
	(reCompile(r"\bCXD2878\b", IGNORECASE), "CXD2878 (Sony)"),
)
_TUNER_LOG = (
	(reCompile(r"\bRDA5815M?\b", IGNORECASE), "RDA5815M (RDA Microelectronics)"),
	(reCompile(r"\bRDA5816\b", IGNORECASE), "RDA5816 (RDA Microelectronics)"),
	(reCompile(r"\bAV2018\b", IGNORECASE), "AV2018 (Airoha)"),
	(reCompile(r"\bAV2012\b", IGNORECASE), "AV2012 (Airoha)"),
	(reCompile(r"\bAV2011\b", IGNORECASE), "AV2011 (Airoha)"),
	(reCompile(r"\bAV201X\b", IGNORECASE), "AV201x (Airoha)"),
	(reCompile(r"\bRT710\b", IGNORECASE), "RT710 (Rafael Micro)"),
	(reCompile(r"\bRT720\b", IGNORECASE), "RT720 (Rafael Micro)"),
	(reCompile(r"\bTS2022\b", IGNORECASE), "TS2022 (Montage)"),
	(reCompile(r"\bTS2020\b", IGNORECASE), "TS2020 (Montage)"),
	(reCompile(r"\bSTV6120\b", IGNORECASE), "STV6120 (STMicroelectronics)"),
	(reCompile(r"\bSTV6111\b", IGNORECASE), "STV6111 (STMicroelectronics)"),
	(reCompile(r"\bSTV6110A?\b", IGNORECASE), "STV6110 (STMicroelectronics)"),
	(reCompile(r"\bSTB6100\b", IGNORECASE), "STB6100 (STMicroelectronics)"),
	(reCompile(r"\bSi2141\b"), "Si2141 (Silicon Labs, terrestrial/cable)"),
	(reCompile(r"\bSi2157\b"), "Si2157 (Silicon Labs, terrestrial/cable)"),
)

# --- 3. Open-source drivers in /proc/modules -----------------------------------
_DEMOD_MODULES = {
	"m88ds3103": "M88DS3103 (Montage)",
	"stv090x": "STV090x (STMicroelectronics)",
	"stv0910": "STV0910 (STMicroelectronics)",
	"si2168": "Si2168 (Silicon Labs)",
	"cxd2880": "CXD2880 (Sony)",
}
_TUNER_MODULES = {
	"ts2020": "TS2020/TS2022 (Montage)",
	"stb6100": "STB6100 (STMicroelectronics)",
	"stv6110x": "STV6110x (STMicroelectronics)",
	"stv6111": "STV6111 (STMicroelectronics)",
	"av201x": "AV201x (Airoha)",
	"si2157": "Si2157 (Silicon Labs)",
}

# --- 4. Chip named inside the NIM name ------------------------------------------
_NIM_DEMOD = (
	(reCompile(r"Si2166D", IGNORECASE), "Si2166-D (Silicon Labs, Si2183 core)"),
	(reCompile(r"Si2166", IGNORECASE), "Si2166 (Silicon Labs, Si2183 core)"),
	(reCompile(r"Si2183", IGNORECASE), "Si2183 (Silicon Labs)"),
	(reCompile(r"Si2169", IGNORECASE), "Si2169 (Silicon Labs)"),
	(reCompile(r"AVL6261"), "AVL6261 (Availink, AVL62X1 family)"),
	(reCompile(r"AVL62X1"), "AVL62X1 (Availink)"),
	(reCompile(r"AVL6222"), "AVL6222 (Availink)"),
	(reCompile(r"AVL6211"), "AVL6211 (Availink)"),
	(reCompile(r"AVL2108|AVL1208"), "AVL2108 (Availink)"),
	(reCompile(r"45308X?"), "BCM45308 (Broadcom, integrated tuner)"),
	(reCompile(r"45208"), "BCM45208 (Broadcom, integrated tuner)"),
	(reCompile(r"7376"), "BCM7376 (Broadcom, integrated tuner)"),
	(reCompile(r"TS3L10|TS2L08"), "Si2183 family (Silicon Labs, GigaBlue PnP)"),
	(reCompile(r"M88DS3103", IGNORECASE), "M88DS3103 (Montage)"),
	(reCompile(r"TBS-?5925"), "STV0903 (STMicroelectronics)"),
)
# Broadcom FBC chips integrate the RF tuner on the same die.
_INTEGRATED_TUNER = reCompile(r"45308X?|45208|7376")

_cache = {}
_kernelLog = None
_modules = None


def _readKernelLog():
	global _kernelLog
	if _kernelLog is None:
		try:
			_kernelLog = Popen(["dmesg"], stdout=PIPE, stderr=PIPE).communicate()[0].decode("utf-8", "ignore")
		except Exception:
			_kernelLog = ""
	return _kernelLog


def _readModules():
	global _modules
	if _modules is None:
		try:
			with open("/proc/modules") as fd:
				_modules = set(line.split()[0] for line in fd if line.strip())
		except Exception:
			_modules = set()
	return _modules


def _boxType():
	for path in ("/proc/stb/info/vumodel", "/proc/stb/info/boxtype", "/proc/stb/info/gbmodel", "/proc/stb/info/model"):
		if isfile(path):
			try:
				with open(path) as fd:
					name = fd.read().strip().lower()
				if name:
					return ("vu" + name) if path.endswith("vumodel") and not name.startswith("vu") else name
			except Exception:
				pass
	return ""


def _loadDiskCache():
	try:
		with open(CACHE_FILE) as fd:
			data = load(fd)
		return data if isinstance(data, dict) else {}
	except Exception:
		return {}


def _saveDiskCache(key, value):
	data = _loadDiskCache()
	if data.get(key) == value:
		return
	data[key] = value
	try:
		with open(CACHE_FILE, "w") as fd:
			dump(data, fd, indent=1, sort_keys=True)
	except Exception:
		pass


def _firstMatch(patterns, text):
	for pattern, label in patterns:
		if pattern.search(text):
			return label
	return None


def _fromKernelLog(nimName):
	log = _readKernelLog()
	if not log:
		return None, None
	demod = None
	match = _SILABS_PART.search(log)
	if match:
		part, die, rom, firmware = match.groups()
		demod = "%s-%s%s (Silicon Labs, firmware %s)" % (part, die, rom, firmware)
	if demod is None:
		demod = _firstMatch(_DEMOD_LOG, log)
	return demod, _firstMatch(_TUNER_LOG, log)


def _fromModules():
	modules = _readModules()
	demod = next((label for name, label in _DEMOD_MODULES.items() if name in modules), None)
	tuner = next((label for name, label in _TUNER_MODULES.items() if name in modules), None)
	return demod, tuner


def getTunerChips(slot, nimName):
	"""Demodulator and tuner for one NIM: {"demod", "tuner", "source"}."""
	nimName = nimName or ""
	key = "%s|%s" % (slot, nimName)
	if key in _cache:
		return _cache[key]
	box = _boxType()
	result = {"demod": None, "tuner": None, "source": "unknown"}
	for prefix, nimPart, demod, tuner in KNOWN:
		if box.startswith(prefix) and nimPart.lower() in nimName.lower():
			result = {"demod": demod, "tuner": tuner, "source": "known"}
			break
	if result["demod"] is None or result["tuner"] is None:
		# Only trust receiver-wide sources when every NIM has the same name,
		# otherwise a chip found in the log cannot be tied to this slot.
		single = _singleNimType()
		if single:
			cached = _loadDiskCache().get(nimName)
			demod, tuner = _fromKernelLog(nimName)
			if demod or tuner:
				_saveDiskCache(nimName, {"demod": demod, "tuner": tuner})
			elif cached:
				demod, tuner = cached.get("demod"), cached.get("tuner")
			modDemod, modTuner = _fromModules()
			demod = demod or modDemod
			tuner = tuner or modTuner
			if result["demod"] is None and demod:
				result["demod"], result["source"] = demod, "kernel"
			if result["tuner"] is None and tuner:
				result["tuner"], result["source"] = tuner, "kernel"
	if result["demod"] is None:
		demod = _firstMatch(_NIM_DEMOD, nimName)
		if demod:
			result["demod"] = demod
			if result["source"] == "unknown":
				result["source"] = "nim"
	if result["tuner"] is None and _INTEGRATED_TUNER.search(nimName):
		result["tuner"] = "integrated in the demodulator"
	_cache[key] = result
	return result


def _singleNimType():
	"""True when all satellite NIMs report the same name (single chip type)."""
	try:
		names = set()
		with open("/proc/bus/nim_sockets") as fd:
			for line in fd:
				line = line.strip()
				if line.startswith("Name:"):
					names.add(line[5:].strip())
		return len(names) == 1
	except Exception:
		return False


def formatTunerChips(slot, nimName):
	"""One line for display, or "" when nothing could be identified."""
	chips = getTunerChips(slot, nimName)
	parts = []
	if chips["demod"]:
		parts.append(chips["demod"])
	if chips["tuner"]:
		parts.append(chips["tuner"])
	return " / ".join(parts)
