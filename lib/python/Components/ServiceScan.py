# TNAP modifications to Components/ServiceScan.py:
#
# 1. FESignalReader — in-process DVB frontend reader (replaces the external
#    dvbstat binary).  Opens the frontend O_RDONLY so it is safe alongside an
#    active scan.  Handles two silicon families:
#      - Octagon SF8008 blob: no DVB API5 stats; FE_READ_SNR is in 0.01 dB
#        units with garbage sentinel values that must be filtered out.
#      - Edision / proper drivers: DVB API5 DTV_STAT_CNR in real dB.
#    The reader probes API5 first and falls back to the legacy ioctl path.
#
# 2. pollScanSignal / _takeTpSignal — a 300 ms eTimer samples SNR while the
#    frontend is locked on each transponder so the scan report captures a
#    reading taken during the actual lock rather than after the scanner has
#    already retuned to the next transponder.
#
# 3. _updateLiveSignal — drives the on-screen SNR/AGC bars in the TNAP skin.
#    No-ops when the optional slider/text widgets were not supplied, so the
#    component remains compatible with vanilla callers.
#
# 4. onScanComplete callback list — fired once in execEnd() when all scan runs
#    finish.  The Screen (Screens/ServiceScan.py) appends _scanComplete to this
#    list to trigger the keep/discard dialog at the right moment.

from enigma import eComponentScan, iDVBFrontend, eTimer
from Components.NimManager import nimmanager as nimmgr
from Components.About import about
from Components.TunerInfo import TunerInfo   # (Extra Import)
from Components.TuneTest import Tuner    # (Extra Import)
from Components.Sources.FrontendStatus import FrontendStatus  # (Extra Import)
from Tools.Transponder import getChannelNumber
from Tools.Directories import fileExists
from time import strftime, time, gmtime, localtime
import os
import pwd
import grp
import ctypes
import fcntl
import struct  # NIT capture demux filter parameters
import errno   # NIT capture non-blocking read handling


BOX_MODEL = ""
BOX_NAME = ""
if fileExists("/proc/stb/info/vumodel") and not fileExists("/proc/stb/info/hwmodel") and not fileExists("/proc/stb/info/boxtype"):
	try:
		l = open("/proc/stb/info/vumodel")
		model = l.read().strip()
		l.close()
		BOX_NAME = str(model.lower())
		BOX_MODEL = "vuplus"
	except:
		pass
if fileExists("/proc/stb/info/boxtype") and not fileExists("/proc/stb/info/hwmodel") and not fileExists("/proc/stb/info/gbmodel"):
	try:
		l = open("/proc/stb/info/boxtype")
		model = l.read().strip()
		l.close()
		BOX_NAME = str(model.lower())
		if BOX_NAME.startswith("et"):
			BOX_MODEL = "xtrend"
		elif BOX_NAME.startswith("os"):
			BOX_MODEL = "edision"
	except:
		pass
if fileExists("/proc/stb/info/boxtype") and not fileExists("/proc/stb/info/hwmodel") and not fileExists("/proc/stb/info/gbmodel"):
	try:
		p = 0
		nimfile = open("/proc/bus/nim_sockets")
		for line in nimfile:
			line = line.strip()
			if line.endswith("AVL62X1"):
				p = 1
		l = open("/proc/stb/info/boxtype")
		model = l.read().strip()
		l.close()
		BOX_NAME = str(model.lower())
		if p == 1:
			BOX_NAME = "SF8008-Supreme"
		if BOX_NAME.startswith("et"):
			BOX_MODEL = "xtrend"
		elif BOX_NAME.startswith("os"):
			BOX_MODEL = "edision"
		elif BOX_NAME.startswith("sf"):
			BOX_MODEL = "octagon"
		if p == 1 and BOX_NAME.startswith("sf"):
			BOX_NAME = "SF8008-Supreme"
		nimfile.close()
	except:
		pass

# In-process DVB frontend signal reader (replaces the external dvbstat
# binary). Opens the frontend read-only, so it can safely run alongside
# an active scan without disturbing the tune. Same ioctl path as the
# fe-monitor.py diagnostic tool.
#
# Driver differences (verified on real hardware, both AVL6261 silicon):
#  - Octagon SF8008 blob: no DVB API5 stats; legacy FE_READ_SNR is in
#    0.01 dB units with garbage sentinel values at 55536 / ~65500.
#  - Edision osmio4k/osmio4kplus/osmini4k driver: proper API5
#    DTV_STAT_CNR in decibels; legacy FE_READ_SNR is relative 0-65535.
# The reader probes API5 first and only applies the sentinel filter and
# the 0.01 dB interpretation when API5 stats are unavailable.

FE_HAS_LOCK = 0x10
SNR_GARBAGE_THRESHOLD = 15536	# legacy-path sentinel filter (Octagon blob)
SNR_DB_FULL_SCALE = 20.0	# dB value mapped to 100% on the live SNR bar
DTV_STAT_CNR = 63
FE_SCALE_DECIBEL = 1
_MAX_DTV_STATS = 4


def _fe_ior(nr, size):	# _IOR('o', nr, size) for the DVB frontend ioctls
	return (2 << 30) | (size << 16) | (0x6F << 8) | nr


FE_READ_STATUS = _fe_ior(69, 4)
FE_READ_SIGNAL_STRENGTH = _fe_ior(71, 2)
FE_READ_SNR = _fe_ior(72, 2)


class _DtvStats(ctypes.Structure):
	_pack_ = 1
	_layout_ = "ms"
	_fields_ = [("scale", ctypes.c_uint8), ("svalue", ctypes.c_int64)]


class _DtvFeStats(ctypes.Structure):
	_pack_ = 1
	_layout_ = "ms"
	_fields_ = [("len", ctypes.c_uint8), ("stat", _DtvStats * _MAX_DTV_STATS)]


class _PropBuffer(ctypes.Structure):
	_pack_ = 1
	_layout_ = "ms"
	_fields_ = [("data", ctypes.c_uint8 * 32), ("len", ctypes.c_uint32),
		("reserved1", ctypes.c_uint32 * 3), ("reserved2", ctypes.c_void_p)]


class _PropUnion(ctypes.Union):
	_pack_ = 1
	_layout_ = "ms"
	_fields_ = [("data", ctypes.c_uint32), ("st", _DtvFeStats), ("buffer", _PropBuffer)]


class _DtvProperty(ctypes.Structure):
	_pack_ = 1
	_layout_ = "ms"
	_fields_ = [("cmd", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 3),
		("u", _PropUnion), ("result", ctypes.c_int)]


class _DtvProperties(ctypes.Structure):
	_fields_ = [("num", ctypes.c_uint32), ("props", ctypes.POINTER(_DtvProperty))]


FE_GET_PROPERTY = _fe_ior(83, ctypes.sizeof(_DtvProperties))


class FESignalReader:
	def __init__(self, feid=0):
		self.fd = -1
		self.api5_seen = False	# sticky: driver has produced API5 CNR stats
		for dev in ("/dev/dvb/adapter0/frontend%d" % feid,
				"/dev/dvb/adapter%d/frontend0" % feid,
				"/dev/dvb/adapter0/frontend0"):
			try:
				self.fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
				break
			except OSError:
				continue

	def _ioctl(self, request, ctype):
		buf = ctype(0)
		try:
			fcntl.ioctl(self.fd, request, buf)
			return buf.value
		except OSError:
			return None

	def _read_cnr_db(self):
		# DVB API 5.10 DTV_STAT_CNR in dB, or None when the driver does
		# not provide it (e.g. the Octagon SF8008 blob)
		props = (_DtvProperty * 1)()
		props[0].cmd = DTV_STAT_CNR
		wrapper = _DtvProperties(num=1, props=ctypes.cast(props, ctypes.POINTER(_DtvProperty)))
		try:
			fcntl.ioctl(self.fd, FE_GET_PROPERTY, wrapper)
		except OSError:
			return None
		st = props[0].u.st
		for i in range(min(st.len, _MAX_DTV_STATS)):
			if st.stat[i].scale == FE_SCALE_DECIBEL:
				return st.stat[i].svalue / 1000.0
		return None

	def read(self):
		# returns (status, snr_raw, strength_raw, cnr_db); None on failure
		if self.fd < 0:
			return None, None, None, None
		return (self._ioctl(FE_READ_STATUS, ctypes.c_uint32),
			self._ioctl(FE_READ_SNR, ctypes.c_uint16),
			self._ioctl(FE_READ_SIGNAL_STRENGTH, ctypes.c_uint16),
			self._read_cnr_db())

	def close(self):
		if self.fd >= 0:
			try:
				os.close(self.fd)
			except OSError:
				pass
			self.fd = -1


def get_signal_data(adapter=0):
	# Compatibility wrapper with the old dvbstat output contract, plus
	# 'snr_raw'. dB comes from API5 DTV_STAT_CNR when the driver provides
	# it (Edision); otherwise legacy raw is interpreted as 0.01 dB units
	# with the sentinel filter applied (Octagon blob).
	reader = FESignalReader(adapter)
	status, snr, strength, cnr_db = reader.read()
	reader.close()
	if status is None:
		return {'snr': 0, 'snr_raw': 0, 'status': 0, 'strength': 0}
	if cnr_db:
		db = round(cnr_db, 2)
		raw = snr or 0
	elif status & 0x0F:
		# full status bits = relative-scale legacy register, dB unknown
		db = 0.0
		raw = snr or 0
	elif snr and snr < SNR_GARBAGE_THRESHOLD:
		db = round(snr / 100.0, 2)
		raw = snr
	else:
		db = 0.0
		raw = 0
	return {'snr': db, 'snr_raw': raw,
		'status': status & 0x1F,
		'strength': round((strength or 0) * 100.0 / 65535.0, 1)}


# ---------------------------------------------------------------------------
# NIT capture for the scan report -- network name + NIT orbital position
# ---------------------------------------------------------------------------
# The C++ eDVBScan already dwells several seconds on every transponder to
# read PAT/PMT/SDT, so instead of pausing the scan to read the NIT (a
# standalone capture needs ~5s of dedicated wait for the table to repeat),
# a non-blocking section filter is armed when the scanner arrives on a
# transponder and drained from the existing 300ms signalpolltimer. The
# capture rides entirely under the scanner's own dwell time: when the NIT
# arrived in time its line goes into the report, when the scanner moved on
# first the line is simply omitted. Added scan time: zero.
#
# Python never learns which demux the C++ scan reserved, so filters are
# opened on every adapter0 demux (each open() on a demux node is an
# independent Linux-DVB section filter; idle demuxes deliver nothing). A
# capture is only trusted ("verified") when the NIT's transport-stream loop
# contains a satellite delivery descriptor matching the frequency of the
# transponder being scanned -- this both disambiguates the demux (a
# recording on another tuner delivers that OTHER mux's NIT into its demux)
# and directly yields the NIT orbital position for the report. NITs that
# carry no delivery descriptor at all (typical for uplinker feeds, e.g.
# Ericsson) are accepted only when exactly one demux delivered NIT data,
# and are marked unverified ('?') in the report.
#
# Everything here runs from eTimer / C++ signal-callback context, so no
# method of NitScanCapture may ever raise; all entry points are guarded.

_DMX_FILTER_SIZE = 16
_DMX_CHECK_CRC = 1
_DMX_IMMEDIATE_START = 4

# Native C layout of struct dmx_sct_filter_params:
#   __u16 pid; __u8 filter[16]; __u8 mask[16]; __u8 mode[16];
#   __u32 timeout; __u32 flags;
_DMX_SCT_STRUCT = "@H16s16s16sII"

# _IOW('o', 43, struct dmx_sct_filter_params) -- direction 1 = write,
# computed from the pack format so the encoded size always matches the
# struct actually sent (60 bytes on both 32-bit ARM and x86-64).
_DMX_SET_FILTER = ((1 << 30)
	| (struct.calcsize(_DMX_SCT_STRUCT) << 16)
	| (0x6F << 8)
	| 43)

_NIT_PID = 0x10
_NIT_ACTUAL_TABLE_ID = 0x40


def _nitActualFilterParams():
	"""Section-filter parameters: PID 0x0010, table_id 0x40 (NIT actual)."""
	filter_bytes = bytearray(_DMX_FILTER_SIZE)
	mask_bytes = bytearray(_DMX_FILTER_SIZE)
	filter_bytes[0] = _NIT_ACTUAL_TABLE_ID
	mask_bytes[0] = 0xFF
	return struct.pack(_DMX_SCT_STRUCT, _NIT_PID,
		bytes(filter_bytes), bytes(mask_bytes), b"\x00" * _DMX_FILTER_SIZE,
		0, _DMX_CHECK_CRC | _DMX_IMMEDIATE_START)


def _cleanDvbText(text):
	"""Strip control codes for report/OSD display.

	Besides C0 controls this also handles the DVB single-byte control codes
	(EN 300 468 annex A.1): 0x8A (and its two-byte-table twin U+E08A) is a
	mandated CR/LF and becomes a space; the remaining 0x80-0x9F/U+E080-E09F
	codes (character emphasis on/off etc.) are dropped entirely."""
	out = []
	for ch in text:
		code = ord(ch)
		if code in (0x8A, 0xE08A):
			out.append(" ")
		elif code < 0x20 or 0x80 <= code <= 0x9F or 0xE080 <= code <= 0xE09F:
			if ch == "\t":
				out.append(" ")
		else:
			out.append(ch)
	return "".join(out).strip()


def _decodeDvbText(data):
	"""Practical DVB text decoder (EN 300 468 annex A subset)."""
	if not data:
		return ""
	try:
		first = data[0]
		if first == 0x15:                        # UTF-8
			return _cleanDvbText(data[1:].decode("utf-8", "replace"))
		if first == 0x11:                        # ISO/IEC 10646-1, UCS-2
			return _cleanDvbText(data[1:].decode("utf-16-be", "replace"))
		if 0x01 <= first <= 0x0B:                # ISO-8859-5 .. -15
			return _cleanDvbText(data[1:].decode("iso8859_%d" % (first + 4), "replace"))
		if first == 0x10 and len(data) >= 3 and data[1] == 0x00 and 1 <= data[2] <= 15:
			return _cleanDvbText(data[3:].decode("iso8859_%d" % data[2], "replace"))
		return _cleanDvbText(data.decode("latin-1", "replace"))
	except (LookupError, UnicodeError):
		return _cleanDvbText(data.decode("latin-1", "replace"))


def _splitNitSections(buf):
	"""Extract complete table-0x40 sections from accumulated demux reads.

	Section-filter reads normally deliver one complete section, but this
	also copes with concatenated or partially accumulated reads. Returns
	(sections, remainder)."""
	sections = []
	offset = 0
	while offset + 3 <= len(buf):
		if buf[offset] != _NIT_ACTUAL_TABLE_ID:
			offset += 1
			continue
		total_length = 3 + (((buf[offset + 1] & 0x0F) << 8) | buf[offset + 2])
		if total_length < 12 or total_length > 1024:
			offset += 1
			continue
		if offset + total_length > len(buf):
			break
		sections.append(buf[offset:offset + total_length])
		offset += total_length
	return sections, buf[offset:]


def _iterNitDescriptors(data):
	"""Yield (tag, body) pairs; stops at the first malformed descriptor."""
	offset = 0
	while offset + 2 <= len(data):
		tag = data[offset]
		length = data[offset + 1]
		if offset + 2 + length > len(data):
			return
		yield tag, data[offset + 2:offset + 2 + length]
		offset += 2 + length


def _bcdInt(data):
	"""Decode packed BCD to int, or None if any nibble is not a digit."""
	value = 0
	for byte in data:
		hi = (byte >> 4) & 0x0F
		lo = byte & 0x0F
		if hi > 9 or lo > 9:
			return None
		value = value * 100 + hi * 10 + lo
	return value


def _parseSatDelivery(body):
	"""Decode satellite_delivery_system_descriptor (0x43) essentials."""
	if len(body) != 11:
		return None
	freq = _bcdInt(body[0:4])	# 8 BCD digits, GHz with 5 decimals
	orb = _bcdInt(body[4:6])	# 4 BCD digits, tenths of a degree
	if freq is None or orb is None:
		return None
	return {
		"frequency_khz": freq * 10,
		"orbital": orb,
		"east": bool(body[6] & 0x80),
	}


def _parseNitSection(section):
	"""Parse one NIT-actual section into names + satellite TS-loop entries.

	Returns None for unparseable input; truncated descriptor loops yield
	whatever decoded cleanly before the damage."""
	if len(section) < 12 or section[0] != _NIT_ACTUAL_TABLE_ID:
		return None
	section_length = ((section[1] & 0x0F) << 8) | section[2]
	crc_start = min(3 + section_length, len(section)) - 4
	result = {
		"network_id": (section[3] << 8) | section[4],
		"version": (section[5] >> 1) & 0x1F,
		"section_number": section[6],
		"last_section_number": section[7],
		"name": None,
		"ml_name": None,		# multilingual fallback
		"sat_entries": [],
	}
	loop_length = ((section[8] & 0x0F) << 8) | section[9]
	offset = 10
	loop_end = min(offset + loop_length, crc_start)
	for tag, body in _iterNitDescriptors(section[offset:loop_end]):
		if tag == 0x40 and result["name"] is None:
			result["name"] = _decodeDvbText(body) or None
		elif tag == 0x5B and result["ml_name"] is None and len(body) >= 4:
			name_length = body[3]
			if 4 + name_length <= len(body):
				result["ml_name"] = _decodeDvbText(body[4:4 + name_length]) or None
	offset = loop_end
	if offset + 2 > crc_start:
		return result
	ts_loop_length = ((section[offset] & 0x0F) << 8) | section[offset + 1]
	offset += 2
	ts_loop_end = min(offset + ts_loop_length, crc_start)
	while offset + 6 <= ts_loop_end:
		descriptor_length = ((section[offset + 4] & 0x0F) << 8) | section[offset + 5]
		offset += 6
		descriptor_end = offset + descriptor_length
		if descriptor_end > ts_loop_end:
			break
		for tag, body in _iterNitDescriptors(section[offset:descriptor_end]):
			if tag == 0x43:
				entry = _parseSatDelivery(body)
				if entry:
					result["sat_entries"].append(entry)
		offset = descriptor_end
	return result


def _nitOrbitalText(entry):
	orb = entry["orbital"]
	return "%d.%d%s" % (orb // 10, orb % 10, "E" if entry["east"] else "W")


def _nitOrbitalValue(entry):
	"""Enigma 0..3600 orbital convention (west = 3600 - x) so the value can
	be matched against satellites.xml via nimmgr.getSatDescription()."""
	orb = entry["orbital"] % 3600
	return orb if entry["east"] else (3600 - orb) % 3600


class NitScanCapture:
	"""Best-effort per-transponder NIT capture riding under the scan dwell.

	arm() opens fresh section filters (reopening per transponder flushes
	any sections the kernel buffered from the previous mux, so stale data
	can never be attributed to the new one), poll() drains them without
	blocking, harvest() applies the demux-disambiguation rules and returns
	what the report should say. No method raises."""

	MAX_DEMUX = 4
	MAX_READS_PER_POLL = 16		# bound work done inside a GUI timer tick
	FREQ_TOLERANCE_KHZ = 5000	# blindscan centre estimates vs NIT nominals

	def __init__(self):
		self._filters = []	# {fd, carry, nets: {network_id: merged parse}}
		self.tp_freq = None

	def arm(self, tp_freq_khz):
		self.close()
		self.tp_freq = tp_freq_khz
		try:
			params = _nitActualFilterParams()
			base = "/dev/dvb/adapter0"
			names = sorted(n for n in os.listdir(base) if n.startswith("demux"))
			for name in names[:self.MAX_DEMUX]:
				fd = -1
				try:
					fd = os.open(os.path.join(base, name), os.O_RDWR | os.O_NONBLOCK)
					fcntl.ioctl(fd, _DMX_SET_FILTER, params)
					self._filters.append({"fd": fd, "carry": b"", "nets": {}})
				except OSError:
					if fd >= 0:
						try:
							os.close(fd)
						except OSError:
							pass
		except Exception as e:
			print("[ServiceScan][NitScanCapture] arm failed: %s" % e)
			self.close()

	def poll(self):
		try:
			for flt in self._filters:
				for _ in range(self.MAX_READS_PER_POLL):
					try:
						chunk = os.read(flt["fd"], 4096)
					except OSError as e:
						if e.errno == errno.EOVERFLOW:
							continue	# drop, keep draining
						break			# EAGAIN and friends: no more data
					if not chunk:
						break
					self._ingest(flt, chunk)
		except Exception as e:
			print("[ServiceScan][NitScanCapture] poll failed: %s" % e)

	def _ingest(self, flt, chunk):
		sections, flt["carry"] = _splitNitSections(flt["carry"] + chunk)
		for section in sections:
			parsed = _parseNitSection(section)
			if parsed is None:
				continue
			net = flt["nets"].setdefault(parsed["network_id"], {
				"name": None, "ml_name": None, "sat_entries": [],
				"seen": set(), "version": None,
			})
			if net["version"] != parsed["version"]:
				# new NIT version: previous accumulation is obsolete
				net.update({"name": None, "ml_name": None,
					"sat_entries": [], "seen": set(),
					"version": parsed["version"]})
			if parsed["section_number"] in net["seen"]:
				continue
			net["seen"].add(parsed["section_number"])
			if net["name"] is None:
				net["name"] = parsed["name"]
			if net["ml_name"] is None:
				net["ml_name"] = parsed["ml_name"]
			net["sat_entries"] += parsed["sat_entries"]

	def harvest(self):
		"""Return (name, pos_text, pos_value, verified) or None.

		1) Preferred: any network whose TS loop contains a delivery entry
		   matching the scanned frequency -> verified name + exact orbit.
		2) Fallback: exactly one demux delivered NIT data (no concurrent
		   recording in play) -> unverified name; orbit only when every
		   delivery entry in that capture agrees on one position."""
		try:
			for flt in self._filters:
				for net in flt["nets"].values():
					for entry in net["sat_entries"]:
						if self.tp_freq and abs(entry["frequency_khz"] - self.tp_freq) <= self.FREQ_TOLERANCE_KHZ:
							return (net["name"] or net["ml_name"],
								_nitOrbitalText(entry), _nitOrbitalValue(entry), True)
			delivering = [flt for flt in self._filters if flt["nets"]]
			if len(delivering) != 1:
				return None	# nothing arrived, or ambiguous across demuxes
			nets = delivering[0]["nets"]
			name = None
			positions = set()
			sample = None
			for net in nets.values():
				if name is None:
					name = net["name"] or net["ml_name"]
				for entry in net["sat_entries"]:
					positions.add((entry["orbital"], entry["east"]))
					sample = entry
			if len(positions) == 1:
				return (name, _nitOrbitalText(sample), _nitOrbitalValue(sample), False)
			if name:
				return (name, None, None, False)
		except Exception as e:
			print("[ServiceScan][NitScanCapture] harvest failed: %s" % e)
		return None

	def close(self):
		for flt in self._filters:
			try:
				os.close(flt["fd"])
			except OSError:
				pass
		self._filters = []
		self.tp_freq = None


class ScanReport:
	"""Formatter and writer for the scan report.

	One stream of events, two outputs:
	 - a column-aligned plain-text report, appended incrementally exactly
	   like the old pseudo-XML one, so whatever was scanned is on disk
	   even if the box dies mid-scan, and
	 - a self-contained HTML rendering written once at completion from the
	   accumulated model -- the shareable artifact (opens styled in any
	   browser, attaches cleanly to a forum post).
	Report I/O is a nice-to-have running from C++ signal callbacks, so
	every method is guarded and may never take the scan down."""

	WIDTH = 100
	NAME_COL = 42

	def __init__(self):
		self.path = None
		self.title = ""
		self.fields = []
		self.tps = []
		self.summary = []

	@staticmethod
	def _label(label, col):
		return "  " + (str(label) + " ").ljust(col, ".") + " "

	def _append(self, text):
		if not self.path:
			return
		try:
			with open(self.path, "a") as f:
				f.write(text)
		except:
			print("[ServiceScan][ScanReport] could not append to %s" % self.path)

	def begin(self, path, title, fields):
		try:
			self.path = path
			self.title = str(title)
			self.fields = [(l, v) for (l, v) in fields if v not in ("", None)]
			self.tps = []
			self.summary = []
			bar = "=" * self.WIDTH
			lines = [bar, self.title.center(self.WIDTH).rstrip(), bar]
			for label, value in self.fields:
				lines.append(self._label(label, 24) + str(value))
			lines.append(bar)
			with open(self.path, "w") as f:
				f.write("\n".join(lines) + "\n")
		except:
			print("[ServiceScan][ScanReport] could not create %s" % path)
			self.path = None

	def tpStart(self, number, text):
		try:
			self.tps.append({"num": number, "text": text, "services": [],
				"nit": None, "signal": None})
			bar = "-" * self.WIDTH
			self._append("\n\n%s\n  Tp# %-4s %s\n%s\n" % (bar, number, text, bar))
		except:
			pass

	def service(self, index, name, ref, snr_text):
		try:
			if self.tps:
				self.tps[-1]["services"].append((index, name, ref, snr_text))
			snr = ("SNR %s" % snr_text) if snr_text != "" else ""
			self._append("  [%3s]  %-*s %-11s %s\n"
				% (index, self.NAME_COL, name, snr, ref))
		except:
			pass

	def nit(self, name, pos_text, sat_name, verified):
		try:
			if self.tps:
				self.tps[-1]["nit"] = (name, pos_text, sat_name, verified)
			mark = "" if verified else "  ?"
			if name:
				self._append(self._label("NIT network", 15)
					+ "'%s'%s\n" % (name, "" if pos_text else mark))
			if pos_text:
				pos = pos_text + ((" (%s)" % sat_name) if sat_name else "")
				self._append(self._label("NIT position", 15) + pos + mark + "\n")
		except:
			pass

	def tpSignal(self, snr_text, raw, lockstate, strength, timestamp):
		try:
			if self.tps:
				self.tps[-1]["signal"] = (snr_text, raw, lockstate, strength, timestamp)
			self._append(self._label("Signal", 15)
				+ "SNR %s (raw %s)   Strength %s%%   %s\n"
				% (snr_text, raw, strength, lockstate))
			self._append(self._label("Finished", 15) + str(timestamp) + "\n")
		except:
			pass

	def finish(self, channels, tv, radio, scanned, total, time_label, time_text):
		try:
			self.summary = [
				("Channels found", "%s  (TV %s / Radio %s)" % (channels, tv, radio)),
				("Transponders scanned", "%s of %s" % (scanned, total)),
				(time_label, time_text),
			]
			bar = "=" * self.WIDTH
			lines = ["", "", bar, "  SUMMARY", "-" * self.WIDTH]
			for label, value in self.summary:
				lines.append(self._label(label, 24) + str(value))
			lines.append(bar)
			self._append("\n".join(lines) + "\n")
		except:
			pass

	# --- HTML rendering ----------------------------------------------------

	@staticmethod
	def _esc(value):
		return (str(value).replace("&", "&amp;").replace("<", "&lt;")
			.replace(">", "&gt;").replace('"', "&quot;"))

	_HTML_CSS = (
		"body{background:#101418;color:#d8dde2;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
		"margin:0;padding:24px}.wrap{max-width:1100px;margin:0 auto}"
		"h1{color:#ffc000;font-size:22px;letter-spacing:2px;border-bottom:2px solid #2a323a;padding-bottom:10px;margin:0 0 8px}"
		"table.meta{border-collapse:collapse;margin:12px 0 24px}"
		"table.meta td{padding:3px 16px 3px 0;font-size:14px}table.meta td.k{color:#8f9aa5;white-space:nowrap}"
		".tp{background:#171d23;border:1px solid #232b33;border-radius:8px;margin:14px 0;overflow:hidden}"
		".tp h2{margin:0;padding:9px 14px;font-size:15px;background:#1d242b;color:#ffc000;font-weight:600}"
		".tp h2 span.n{color:#8f9aa5;font-weight:400;margin-right:12px}"
		".tags{padding:8px 14px 0}"
		".tag{display:inline-block;font-size:12px;border-radius:4px;padding:2px 9px;margin:0 6px 6px 0;"
		"background:#232b33;color:#c6ced6;border:1px solid #39434d}"
		".tag.ok{color:#56c856;border-color:#2c4c2c}.tag.warn{color:#e0b040;border-color:#55492a}"
		"table.svc{border-collapse:collapse;width:100%;font-size:13px;margin-top:4px}"
		"table.svc th{color:#8f9aa5;text-align:left;font-weight:600;padding:5px 14px;border-bottom:1px solid #232b33}"
		"table.svc td{padding:3px 14px;border-bottom:1px solid #1c232a}"
		"table.svc td.i{color:#7f8b96;width:1%;white-space:nowrap}"
		"table.svc td.r{color:#7f8b96;font-family:Consolas,'DejaVu Sans Mono',monospace;font-size:12px}"
		"table.svc td.s{white-space:nowrap}"
		"table.svc tr:last-child td{border-bottom:none}"
		".sig{padding:9px 14px 11px;font-size:13px;color:#aab4bd}.sig b{color:#d8dde2}"
		".lock{color:#56c856}.nolock{color:#e05050}"
		".sum{background:#171d23;border:1px solid #232b33;border-radius:8px;margin-top:24px;padding:2px 14px 12px}"
		".sum h2{color:#ffc000;font-size:16px}"
		"footer{color:#5c6873;font-size:12px;margin-top:18px;text-align:center}"
	)

	def writeHtml(self, path):
		try:
			esc = self._esc
			out = ["<!DOCTYPE html>", "<html><head><meta charset=\"utf-8\">",
				"<title>%s</title>" % esc(self.title),
				"<style>%s</style></head><body><div class=\"wrap\">" % self._HTML_CSS,
				"<h1>%s</h1>" % esc(self.title), "<table class=\"meta\">"]
			for label, value in self.fields:
				out.append("<tr><td class=\"k\">%s</td><td>%s</td></tr>"
					% (esc(label), esc(value)))
			out.append("</table>")
			for tp in self.tps:
				out.append("<div class=\"tp\"><h2><span class=\"n\">Tp# %s</span>%s</h2>"
					% (esc(tp["num"]), esc(tp["text"])))
				nit = tp["nit"]
				if nit:
					name, pos_text, sat_name, verified = nit
					tags = ["<div class=\"tags\">"]
					if name:
						tags.append("<span class=\"tag\">NIT network: %s</span>" % esc(name))
					if pos_text:
						pos = pos_text + ((" (%s)" % sat_name) if sat_name else "")
						tags.append("<span class=\"tag %s\">NIT position: %s%s</span>"
							% ("ok" if verified else "warn", esc(pos),
							"" if verified else " ?"))
					elif not verified:
						tags.append("<span class=\"tag warn\">unverified</span>")
					tags.append("</div>")
					out.append("".join(tags))
				if tp["services"]:
					out.append("<table class=\"svc\"><tr><th>#</th><th>Service</th>"
						"<th>SNR</th><th>Service reference</th></tr>")
					for index, name, ref, snr_text in tp["services"]:
						out.append("<tr><td class=\"i\">%s</td><td>%s</td>"
							"<td class=\"s\">%s</td><td class=\"r\">%s</td></tr>"
							% (esc(index), esc(name), esc(snr_text), esc(ref)))
					out.append("</table>")
				sig = tp["signal"]
				if sig:
					snr_text, raw, lockstate, strength, timestamp = sig
					lock_cls = "lock" if str(lockstate) == "Locked" else "nolock"
					out.append("<div class=\"sig\">SNR <b>%s</b> (raw %s) &nbsp; "
						"Strength <b>%s%%</b> &nbsp; <span class=\"%s\">%s</span>"
						" &nbsp; <span style=\"color:#5c6873\">%s</span></div>"
						% (esc(snr_text), esc(raw), esc(strength), lock_cls,
						esc(lockstate), esc(timestamp)))
				out.append("</div>")
			if self.summary:
				out.append("<div class=\"sum\"><h2>Summary</h2><table class=\"meta\">")
				for label, value in self.summary:
					out.append("<tr><td class=\"k\">%s</td><td>%s</td></tr>"
						% (esc(label), esc(value)))
				out.append("</table></div>")
			out.append("<footer>Generated by TNAP ServiceScan</footer>")
			out.append("</div></body></html>")
			with open(path, "w") as f:
				f.write("\n".join(out))
		except:
			print("[ServiceScan][ScanReport] could not write HTML report %s" % path)


class ServiceScan:
	Idle = 1
	Running = 2
	Done = 3
	Error = 4
	DonePartially = 5
	Errors = {0: _('error starting scanning'), 
	   1: _('error while scanning'), 
	   2: _('no resource manager'), 
	   3: _('no channel list')}

	def scanStatusChanged(self):
		if self.state == self.Running:
			self.progressbar.setValue(self.scan.getProgress())
			self.lcd_summary and self.lcd_summary.updateProgress(self.scan.getProgress())
			if self.scan.isDone():
				errcode = self.scan.getError()
				if errcode == 0:
					self.state = self.DonePartially
					self.servicelist.listAll()
				else:
					self.state = self.Error
					self.errorcode = errcode
				self.network.setText("")
				self.transponder.setText("")
			else:
				result = self.foundServices + self.scan.getNumServices()
				total = self.tt
				tpnumb = 0 + self.t
				percentage = self.scan.getProgress()
				if percentage > 99:                                                                                                                                                                                                ##
					percentage = 99
				#TRANSLATORS: The stb is performing a channel scan, progress percentage is printed in '%d' (and '%%' will show a single '%' symbol)
				message = ngettext("(%d ", " (%d ", tpnumb) % tpnumb
				message += ngettext(" of %d)" , "of %d)", total) % total
				#TRANSLATORS: Intermediate scanning result, '%d' channel(s) have been found so far
				message += ngettext("  Channels Found = %d", "  Channels Found = %d", result) % result
				if self.l == 1 and tpnumb > 1:
					# NIT line first, then the SNR line: both belong to the
					# transponder just finished, harvested at the moment the
					# scanner reports the next one.
					self._writeNitReportLine()
					tpstatus = self._takeTpSignal()
					self.report.tpSignal(self.signaltp, self.signaltp3, tpstatus,
						self.signaltp2, strftime("%a, %d %b %Y %H:%M:%S", localtime()))
				self.t = self.t+1
				self.text.setText(message)
				transponder = self.scan.getCurrentTransponder()
				network = ""
				global tp_text
				tp_text = ""
				if transponder:
					tp_type = transponder.getSystem()
					if tp_type == iDVBFrontend.feSatellite:
						network = _('Satellite')
						tp = transponder.getDVBS()
						orb_pos = tp.orbital_position
						try:
							sat_name = str(nimmgr.getSatDescription(orb_pos))
						except:
							# Matches the defensive pattern used everywhere else in this
							# function (see newService()'s own comment on why an uncaught
							# exception here is not acceptable): this runs as a C++ signal
							# callback, so anything other than a KeyError escaping this
							# lookup would take all of enigma2 down mid-scan.
							sat_name = ''
						if orb_pos > 1800:
							orb_pos = 3600 - orb_pos
							h = 'W'
						else:
							h = 'E'
						# self.network1 feeds the report FILENAME, so the hemisphere
						# letter must stay untranslated ('E'/'W'); gettext locales
						# translate the letter (e.g. German 'E' -> 'O' for Ost) which
						# made filenames locale-dependent. The translated letter is
						# still used for the on-screen display text below.
						try:
							self.network1 = ("%d.%d%s") % ( orb_pos / 10, orb_pos % 10, h)  ##
						except:
							pass
						if '%d.%d' % (orb_pos / 10, orb_pos % 10) in sat_name:
							network = sat_name
						else:
							network = '%s %d.%d %s' % (sat_name, orb_pos / 10, orb_pos % 10, _(h))
						# Band tag from the ACTUAL downlink frequency, not from the
						# satellite's name text. Only dual-band twin entries in
						# satellites.xml carry "Ku-band"/"C-band" in their names, so
						# the old substring test mislabelled every single-band Ku
						# satellite (e.g. "19.2E Astra 1KR/1L/1M/1N") as C-band.
						# tp.frequency is in kHz and already corrected to real RF by
						# the blindscan path: C-band downlink 3400-4800 MHz, Ku-band
						# 10700-12750 MHz, so a 5 GHz split is unambiguous.
						if tp.frequency < 5000000:
							self.network1 += "_C-band"
						else:
							self.network1 += "_Ku-band"
						tp_text = {tp.System_DVB_S: 'DVB-S', tp.System_DVB_S2: 'DVB-S2'}.get(tp.system, '')
						if tp_text == 'DVB-S2':
							tp_text = '%s %s' % (tp_text,
							 {tp.Modulation_Auto: 'Auto', tp.Modulation_QPSK: 'QPSK', tp.Modulation_8PSK: '8PSK', 
								tp.Modulation_QAM16: 'QAM16', tp.Modulation_16APSK: '16APSK', 
								tp.Modulation_32APSK: '32APSK'}.get(tp.modulation, ''))
						tp_text = '%s %d%c / %d / %s' % (tp_text, tp.frequency / 1000,
						 {tp.Polarisation_Horizontal: 'H', tp.Polarisation_Vertical: 'V', tp.Polarisation_CircularLeft: 'L', tp.Polarisation_CircularRight: 'R'}.get(tp.polarisation, ' '),
						 tp.symbol_rate / 1000,
						 {tp.FEC_Auto: 'AUTO', tp.FEC_1_2: '1/2', tp.FEC_2_3: '2/3', tp.FEC_3_4: '3/4', 
							tp.FEC_3_5: '3/5', tp.FEC_4_5: '4/5', tp.FEC_5_6: '5/6', 
							tp.FEC_6_7: '6/7', tp.FEC_7_8: '7/8', tp.FEC_8_9: '8/9', 
							tp.FEC_9_10: '9/10', tp.FEC_None: 'NONE'}.get(tp.fec, ''))
						if tp.system == tp.System_DVB_S2:
							if tp.is_id > tp.No_Stream_Id_Filter:
								tp_text = '%s MIS %d' % (tp_text, tp.is_id)
							if tp.pls_code > 0:
								tp_text = '%s Gold %d' % (tp_text, tp.pls_code)
							if tp.t2mi_plp_id > tp.No_T2MI_PLP_Id:
								tp_text = '%s T2MI %d PID %d' % (tp_text, tp.t2mi_plp_id, tp.t2mi_pid)
						# Arm the NIT capture for the transponder the scanner
						# just moved onto; drained from the signalpolltimer
						# while the C++ scan does its own SI work, harvested
						# when the next statusChanged (or completion) arrives.
						try:
							self.nitcapture.arm(tp.frequency)
						except:
							pass
					elif tp_type == iDVBFrontend.feCable:
						network = _('Cable')
						self.network1 = "_Cable"
						tp = transponder.getDVBC()
						tp_text = 'DVB-C %s %d / %d / %s' % (
						 {tp.Modulation_Auto: 'AUTO', tp.Modulation_QAM16: 'QAM16', 
							tp.Modulation_QAM32: 'QAM32', tp.Modulation_QAM64: 'QAM64', 
							tp.Modulation_QAM128: 'QAM128', tp.Modulation_QAM256: 'QAM256'}.get(tp.modulation, ''),
						 tp.frequency,
						 tp.symbol_rate / 1000,
						 {tp.FEC_Auto: 'AUTO', tp.FEC_1_2: '1/2', tp.FEC_2_3: '2/3', tp.FEC_3_4: '3/4', 
							tp.FEC_3_5: '3/5', tp.FEC_4_5: '4/5', tp.FEC_5_6: '5/6', 
							tp.FEC_6_7: '6/7', tp.FEC_7_8: '7/8', tp.FEC_8_9: '8/9', 
							tp.FEC_9_10: '9/10', tp.FEC_None: 'NONE'}.get(tp.fec_inner, ''))
					elif tp_type == iDVBFrontend.feTerrestrial:
						network = _('Terrestrial')
						self.network1 = "_Terrestrial"
						tp = transponder.getDVBT()
						channel = getChannelNumber(tp.frequency, self.scanList[self.run]['feid'])
						if channel:
							channel = _('CH') + '%s ' % channel
						freqMHz = '%0.1f MHz' % (tp.frequency / 1000000.0)
						tp_text = '%s %s %s %s' % (
						 {tp.System_DVB_T_T2: 'DVB-T/T2', 
							tp.System_DVB_T: 'DVB-T', 
							tp.System_DVB_T2: 'DVB-T2'}.get(tp.system, ''),
						 {tp.Modulation_QPSK: 'QPSK', 
							tp.Modulation_QAM16: 'QAM16', 
							tp.Modulation_QAM64: 'QAM64', tp.Modulation_Auto: 'AUTO', 
							tp.Modulation_QAM256: 'QAM256'}.get(tp.modulation, ''),
						 '%s%s' % (channel, freqMHz.replace('.0', '')),
						 {tp.Bandwidth_8MHz: 'Bw 8MHz', 
							tp.Bandwidth_7MHz: 'Bw 7MHz', tp.Bandwidth_6MHz: 'Bw 6MHz', tp.Bandwidth_Auto: 'Bw Auto', 
							tp.Bandwidth_5MHz: 'Bw 5MHz', tp.Bandwidth_1_712MHz: 'Bw 1.712MHz', 
							tp.Bandwidth_10MHz: 'Bw 10MHz'}.get(tp.bandwidth, ''))
					elif tp_type == iDVBFrontend.feATSC:
						network = _('ATSC')
						self.network1 ="_ATSC"
						tp = transponder.getATSC()
						freqMHz = '%0.1f MHz' % (tp.frequency / 1000000.0)
						tp_text = '%s %s %s %s' % (
						 {tp.System_ATSC: _('ATSC'), 
							tp.System_DVB_C_ANNEX_B: _('DVB-C ANNEX B')}.get(tp.system, ''),
						 {tp.Modulation_Auto: _('Auto'), 
							tp.Modulation_QAM16: 'QAM16', 
							tp.Modulation_QAM32: 'QAM32', 
							tp.Modulation_QAM64: 'QAM64', 
							tp.Modulation_QAM128: 'QAM128', 
							tp.Modulation_QAM256: 'QAM256', 
							tp.Modulation_VSB_8: '8VSB', 
							tp.Modulation_VSB_16: '16VSB'}.get(tp.modulation, ''),
						 freqMHz.replace('.0', ''),
						 {tp.Inversion_Off: _('Off'), 
							tp.Inversion_On: _('On'), 
							tp.Inversion_Unknown: _('Auto')}.get(tp.inversion, ''))
					else:
						print('unknown transponder type in scanStatusChanged')
					if tp_type != iDVBFrontend.feSatellite:
						# NIT network/orbit lines are satellite-scoped; make sure
						# nothing stale is harvested against a C/T/ATSC transponder.
						try:
							self.nitcapture.close()
						except:
							pass
				if self.l == 0:
					self.xml_dir = "/tmp"
					try:
					    if os.path.exists("/media/usb/ServiceScan"):
						    self.xml_dir = "/media/usb/ServiceScan"
					    if os.path.exists("/media/FTA/ServiceScan"):
						    self.xml_dir = "/media/FTA/ServiceScan"
					    if os.path.exists("/media/hdd/ServiceScan"):
						    self.xml_dir = "/media/hdd/ServiceScan"
					except:
					    self.xml_dir = "/tmp"                                                                                                                                                                                              ##
					self.location = '%s/%s_Scan-Report_%s' %(self.xml_dir, self.network1, strftime("%d-%m-%Y_%H-%M-%S"))
					self.l = 1
					# This method runs as a C++ signal callback: any exception that
					# escapes it brings the whole of enigma2 down (green screen).
					# The scan report is a nice-to-have, so no report I/O may ever
					# propagate an error into the scan itself.
					try:
						fields = [
							("Created", strftime("%A, %B %d, %Y at %H:%M:%S")),
							("Satellite", network),
							("Receiver", ("%s %s" % (BOX_MODEL, BOX_NAME)).strip()),
						]
						try:
							fields.append(("Enigma2 image", about.getImageTypeString()))
						except:
							fields.append(("Enigma2 image", "unknown"))
						try:
							fields.append(("Kernel version", about.getKernelVersionString()))
							fields.append(("DVB driver date", about.getDriverInstalledDate()))
						except:
							pass
						if self.freq1 != "":
							fields.append(("Blindscan frequencies", "%s to %s MHz" % (self.freq1, self.freq2)))
							fields.append(("Blindscan symbol rates", "%s to %s Msps" % (self.symbol1, self.symbol2)))
							fields.append(("Free services only", self.free))
						if self.tuner != "":
							fields.append(("Tuner", self.tuner))
						self.report.begin(self.location, "TNAP  SCAN  REPORT", fields)
					except:
						print("[ServiceScan] could not create scan report %s" % self.location)

				if tpnumb != 0:                                                                                                                                                                                                ##
					self.report.tpStart(tpnumb, tp_text)
				self.network.setText(network)
				self.transponder.setText(tp_text)
		# The C++ scanner can emit further statusChanged events during the
		# 100ms delaytimer window after completion; without this guard the
		# block below would run again, double-counting foundServices and
		# re-trying the report rename (which then flashes "Scan Error!!").
		if self.state == self.DonePartially and not self._runDoneProcessed:
			self._runDoneProcessed = True
			runtime = int(time()) - int(self.start_time)
			runtime = runtime + self.start_time1
			self.foundServices += self.scan.getNumServices()
			T = self.foundServices - self.r

			self._writeNitReportLine()
			try:
				tpstatus = self._takeTpSignal()
				self.report.tpSignal(self.signaltp, self.signaltp3, tpstatus,
					self.signaltp2, strftime("%a, %d %b %Y %H:%M:%S", localtime()))
				time_text = "%d Min. %02d Sec." % (runtime / 60, (runtime % 60))
				# start_time1 > 10 means a blindscan handed over its own start
				# timestamp; anything else is a plain service scan. (The old
				# code tested > 10 and < 10 separately, leaving == 10 writing
				# nothing and flashing "Scan Error!!" -- folded into one test.)
				if self.start_time1 > 10:
					time_label = "Blind scan time"
					self.transponder.setText(_("Blind Scan Time = %d Min.  %02d Sec.")  %( runtime / 60, (runtime % 60)))
				else:
					time_label = "Service scan time"
					self.transponder.setText(_("Service Scan Time = %d Min.  %02d Sec.")  %( runtime / 60, (runtime % 60)))
				self.text.setText(_("%d Channels ( TV = %d  Radio = %d)  %d of %d Transponders Scanned.")  %( self.foundServices, T, self.r, (self.t-1), self.tt)) 
				self.report.finish(self.foundServices, T, self.r, (self.t-1), self.tt, time_label, time_text)
				self.rename = '%s/%s_%sch_%stp_%s.txt' %(self.xml_dir, self.network1, self.foundServices, self.tt, strftime("%m-%d-%Y_%H-%M-%S"))
				os.rename(self.location,self.rename)  # ServiceScan_
				# The HTML twin shares the final basename; written last so it
				# only ever exists for completed scans.
				self.report.writeHtml(self.rename[:-4] + ".html")
			except:
				self.transponder.setText(_("Scan Error!!Press exit to abort"))
		if self.state == self.Error:
			self.text.setText(_('ERROR - failed to scan (%s)!') % self.Errors[self.errorcode])
		if self.state == self.DonePartially or self.state == self.Error:
			self.delaytimer.start(100, True)

			
	def pollScanSignal(self):
		# Drain the per-transponder NIT capture on the same tick; poll() is
		# non-blocking and internally guarded, so it can never stall or
		# break the signal sampling below.
		if self.state == self.Running:
			self.nitcapture.poll()
		# Background sampler (300ms): remembers the last SNR raw value seen
		# while the frontend was locked on the transponder currently being
		# scanned. The report then shows a reading taken during the actual
		# lock instead of a snapshot taken after the scanner has already
		# retuned to the next transponder (the old dvbstat approach).
		if self.state != self.Running or self.fereader is None:
			return
		status, snr, strength, cnr_db = self.fereader.read()
		if cnr_db is not None:
			self.fereader.api5_seen = True
		# Drive the on-screen live meter on every poll, locked or not, so the
		# bars track acquisition. This must run BEFORE the capture lock-gate
		# below (which returns early when unlocked).
		self._updateLiveSignal(status, snr, strength, cnr_db)
		# --- capture-for-report path (unchanged behaviour) ----------------
		if status is None or not status & FE_HAS_LOCK:
			return
		# Zero stats while locked mean the estimator has not converged yet
		# (seen on ultra-narrowband carriers on the Edision driver) - keep
		# polling rather than capturing a meaningless 0.
		if cnr_db:
			# API5 dB is authoritative; raw is the legacy register as-is
			raw, db = (snr or 0), round(cnr_db, 2)
		elif (status & 0x0F) or self.fereader.api5_seen:
			# driver sets the intermediate status bits (Edision style):
			# legacy register is a relative 0-65535 scale, dB unknown
			if not snr:
				return
			raw, db = snr, None
		elif snr and snr < SNR_GARBAGE_THRESHOLD:
			# bare-lock-bit blob (Octagon style): raw is 0.01 dB units
			raw, db = snr, round(snr / 100.0, 2)
		else:
			return
		if db is None and self.tp_locked_db is not None:
			return	# never displace a capture that had a real dB reading
		self.tp_locked_raw = raw
		self.tp_locked_db = db
		if strength:
			self.tp_locked_strength = strength

	def _takeTpSignal(self):
		# Consume the sampled reading for the transponder just finished and
		# reset the capture for the next one. Falls back to an instantaneous
		# read if the sampler never saw a lock on this transponder.
		if self.tp_locked_raw is not None:
			self.signaltp = ("%.2fdb" % self.tp_locked_db) if self.tp_locked_db is not None else "no estimate"
			self.signaltp3 = self.tp_locked_raw
			self.signaltp1 = FE_HAS_LOCK
			self.signaltp2 = round((self.tp_locked_strength or 0) * 100.0 / 65535.0, 1)
		else:
			signal_data = get_signal_data(self.feid)
			self.signaltp = "%.2fdb" % signal_data['snr']
			self.signaltp3 = signal_data['snr_raw']
			self.signaltp1 = signal_data['status']
			self.signaltp2 = signal_data['strength']
		self.tp_locked_raw = None
		self.tp_locked_db = None
		self.tp_locked_strength = None
		return "Locked" if (isinstance(self.signaltp1, int) and self.signaltp1 & FE_HAS_LOCK) else "UnLocked"

	def _writeNitReportLine(self):
		# Harvest the NIT capture for the transponder just finished and
		# append its line to the report. Runs from a C++ signal callback,
		# so -- like every other report writer in this file -- it may never
		# raise; when the scanner outran the NIT repetition interval there
		# is simply no line for this transponder.
		try:
			self.nitcapture.poll()  # final drain before deciding
			result = self.nitcapture.harvest()
			self.nitcapture.close()  # filters are re-armed per transponder
			if not result:
				return
			name, pos_text, pos_value, verified = result
			sat_name = ''
			if pos_text:
				try:
					# Same satellites.xml lookup (and the same defensive
					# wrapping) as the orb_pos handling in scanStatusChanged.
					sat_name = str(nimmgr.getSatDescription(pos_value)) or ''
				except:
					sat_name = ''
			self.report.nit(name, pos_text, sat_name, verified)
		except:
			print('[ServiceScan] could not append NIT info to scan report')

	# ------------------------------------------------------------------
	# TNAP modern ServiceScan: live signal meter + transponder plan.
	# All of these are no-ops when their widgets were not supplied, so the
	# component stays compatible with callers that do not pass them.
	# ------------------------------------------------------------------
	def _setBar(self, slider, percent):
		if slider is None:
			return
		try:
			v = int(percent)
			if v < 0:
				v = 0
			elif v > 100:
				v = 100
			slider.setValue(v)
		except:
			pass

	def _updateLiveSignal(self, status, snr, strength, cnr_db):
		# Translate a raw frontend reading into on-screen SNR/AGC/S bars and
		# text. Mirrors the Octagon-vs-Edision logic used for the report so
		# the meter behaves correctly on both driver styles.
		locked = bool(status is not None and status & FE_HAS_LOCK)

		# AGC / signal strength: raw AGC-derived strength register. Valid the
		# instant there is RF on the LNB feed - no demod lock required - which
		# is the entire point of showing it: it lets you see whether you're
		# even pointed at a satellite before the receiver ever locks anything.
		# Deliberately NOT gated on `locked` - only SNR below is, since a
		# quality/CNR number is genuinely meaningless without a lock to
		# measure against, but raw strength is not.
		#
		# `strength is not None` (not a truthiness check): a real reading of
		# exactly 0 is legitimate and should show "0%", not fall through to
		# "---" as if no reading were available at all.
		agc_pct = None
		if strength is not None:
			agc_pct = strength * 100.0 / 65535.0
		self._setBar(self.agcSlider, agc_pct if agc_pct is not None else 0)
		if self.agcText is not None:
			self.agcText.setText(("AGC %d%%" % int(agc_pct)) if agc_pct is not None else "AGC ---")

		if not locked:
			self._setBar(self.snrSlider, 0)
			if self.snrText is not None:
				self.snrText.setText("SNR ---")
			if self.lockText is not None:
				self.lockText.setText(_("Searching..."))
			return

		# SNR / quality.
		snr_pct = None
		snr_db = None
		if cnr_db:
			# API5 dB is authoritative for the text; bar from the relative
			# register when present, otherwise map dB onto the bar scale.
			snr_db = round(cnr_db, 2)
			if snr:
				snr_pct = snr * 100.0 / 65535.0
			else:
				snr_pct = snr_db * 100.0 / SNR_DB_FULL_SCALE
		elif (status & 0x0F) or self.fereader.api5_seen:
			# Edision-style intermediate path: register is relative 0-65535.
			if snr:
				snr_pct = snr * 100.0 / 65535.0
		elif snr and snr < SNR_GARBAGE_THRESHOLD:
			# Octagon-style bare-lock blob: register is 0.01 dB units.
			snr_db = round(snr / 100.0, 2)
			snr_pct = snr_db * 100.0 / SNR_DB_FULL_SCALE

		self._setBar(self.snrSlider, snr_pct if snr_pct is not None else 0)
		if self.snrText is not None:
			if snr_db is not None:
				self.snrText.setText("SNR %.1f dB" % snr_db)
			elif snr_pct is not None:
				self.snrText.setText("SNR %d%%" % int(snr_pct))
			else:
				self.snrText.setText("SNR ---")

		if self.lockText is not None:
			self.lockText.setText(_("Locked"))


	def __init__(self, progressbar, text, servicelist, passNumber, scanList, network, transponder, frontendInfo, lcd_summary, snrSlider=None, snrText=None, agcSlider=None, agcText=None, lockText=None):
		self.foundServices = 0
		self.progressbar = progressbar
		self.text = text
		self.servicelist = servicelist
		self.passNumber = passNumber
		self.scanList = scanList
		self.frontendInfo = frontendInfo
		self.transponder = transponder
		self.network = network
		# Optional live-signal widgets (TNAP modern ServiceScan). All default
		# to None so older callers / other images keep working unchanged.
		self.snrSlider = snrSlider
		self.snrText = snrText
		self.agcSlider = agcSlider
		self.agcText = agcText
		self.lockText = lockText
		# Callbacks fired once, when the whole scan (all runs) has finished.
		self.onScanComplete = []
		self.run = 0
		self.lcd_summary = lcd_summary
		self.scan = None
		self._runDoneProcessed = False  # completion block ran for current run
		self._tt_counted_run = -1  # last run index counted into self.tt
		self.delaytimer = eTimer()
		self.delaytimer.callback.append(self.execEnd)
		self.t = 0
		self.tt = 0
		self.start_time1 = 0
		self.start_time2 = 0
		self.start_time = time()
		self.name ="" #Name of box or box type
		self.y = 1 #For Channel Numbers
		self.l = 0 #Stops Duplicate Files
		self.location ="" #Creates File Location
		self.rename ="" # Used for renaming created file
		self.xml_dir = "/tmp" # Default location for created file.
		self.r = 0  # Used to count Radio Channels
		self.network1 =""
		self.tuner ="" # Key from blindscan used to identify tuner
		self.freq1 ="" #Blindscan start frequency
		self.freq2 ="" #Blindscan Stop frequency
		self.symbol1 ="" #Blindscan start symbol rate
		self.symbol2 ="" #Blindscan stop symbol rate
		self.free ="" #Blindscan scan only free
		self.signal ="" #return signal in db for found services
		self.signaltp =""  #return signal in db for transponders
		self.signaltp1 =""  #return signal Lock-Status transponders
		self.signaltp2 =""  #return LNB Power
		self.size = 0  #Get value of Edision driver file
		self.signaltp3 = 0  #SNR raw register value for transponders
		self.fereader = None  #in-process frontend signal reader (replaces dvbstat)
		self.tp_locked_raw = None  #last SNR raw sampled while locked on current tp
		self.tp_locked_db = None  #same reading converted to dB
		self.tp_locked_strength = None
		self.signalpolltimer = eTimer()
		self.signalpolltimer.callback.append(self.pollScanSignal)
		self.nitcapture = NitScanCapture()  #per-transponder NIT network/orbit for the report
		self.report = ScanReport()  #text + HTML report writer
		return

	def doRun(self):
		self.scan = eComponentScan()
		self.frontendInfo.frontend_source = lambda : self.scan.getFrontend()
		self.feid = self.scanList[self.run]["feid"]
		self.flags = self.scanList[self.run]["flags"]
		try:
			self.start_time1 = self.scanList[self.run]["start"]
		except:
			pass
		try:
			self.start_time2 = self.scanList[self.run]["start1"]
		except:
			pass	
		try:
			self.name = self.scanList[self.run]["name"]
		except:
			pass	
		try:
			self.tuner = self.scanList[self.run]["tuner"]
		except:
			pass	

		try:  #Add Blindscan frequency 7 symbol rate to scan report
			self.freq1 = self.scanList[self.run]["freq1"]
			self.freq2 = self.scanList[self.run]["freq2"]
			self.symbol1 = self.scanList[self.run]["symbol1"]
			self.symbol2 = self.scanList[self.run]["symbol2"]
			self.free = self.scanList[self.run]["free"]
		except:
			pass

		self.networkid = 0
		if "networkid" in self.scanList[self.run]:
			self.networkid = self.scanList[self.run]["networkid"]
		self.state = self.Idle
		self._runDoneProcessed = False
		self.scanStatusChanged()
		# Only count this run's transponders into the total once: doRun() runs
		# again for the same run when the screen is suspended and resumed.
		count_this_run = self._tt_counted_run != self.run
		for x in self.scanList[self.run]["transponders"]:
			if count_this_run:
				self.tt = self.tt+1
			self.scan.addInitial(x)
		self._tt_counted_run = self.run

	def updatePass(self):
		size = len(self.scanList)
		if size > 1:
			txt = '%s %s/%s (%s)' % (_('pass'), self.run + 1, size, nimmgr.getNim(self.scanList[self.run]['feid']).slot_name)
			self.passNumber.setText(txt)

	def execBegin(self):
		try:
		    self.size = os.path.getsize('/lib/modules/5.15.0/extra/avl6261.ko')
		except: 
		    pass
		self.doRun()
		self.updatePass()
		self.scan.statusChanged.get().append(self.scanStatusChanged)
		self.scan.newService.get().append(self.newService)
		self.servicelist.clear()
		self.state = self.Running
		if self.fereader:
			self.fereader.close()
		self.fereader = FESignalReader(self.feid)
		self.tp_locked_raw = None
		self.tp_locked_db = None
		self.tp_locked_strength = None
		self.signalpolltimer.start(300)
		# Reset the live meter to a neutral "searching" state at scan start.
		self._setBar(self.snrSlider, 0)
		self._setBar(self.agcSlider, 0)
		if self.snrText is not None:
			self.snrText.setText("SNR ---")
		if self.agcText is not None:
			self.agcText.setText("AGC ---")
		if self.lockText is not None:
			self.lockText.setText(_("Searching..."))
		err = self.scan.start(self.feid, self.flags, self.networkid)
		self.frontendInfo.updateFrontendData()
		if err:
			self.state = self.Error
			self.errorcode = 0
		self.scanStatusChanged()

	def execEnd(self):
		self.signalpolltimer.stop()
		try:
			self.nitcapture.close()
		except:
			pass
		if self.fereader:
			self.fereader.close()
			self.fereader = None
		if self.scan is None:
			if not self.isDone():
				print("*** warning *** scan was not finished!")
			return
		self.scan.statusChanged.get().remove(self.scanStatusChanged)
		self.scan.newService.get().remove(self.newService)
		self.scan = None
		if self.state == self.Running:
			# execEnd() arrived while the run was still scanning: the Screen
			# was closed or suspended (a dialog opened on top of it), not the
			# scan finishing. Do not start the next run or declare completion
			# here - that would launch a new scan on a hidden/closing screen.
			self.state = self.Idle
			return
		if self.run != len(self.scanList) - 1:
			self.run += 1
			self.execBegin()
		else:
			self.state = self.Done
			# Scan finished: settle the live meter so it does not show a
			# stale reading from the last transponder.
			self._setBar(self.snrSlider, 0)
			self._setBar(self.agcSlider, 0)
			if self.snrText is not None:
				self.snrText.setText("SNR ---")
			if self.agcText is not None:
				self.agcText.setText("AGC ---")
			if self.lockText is not None:
				self.lockText.setText(_("Done"))
			# Notify the screen that all runs are complete (drives the
			# keep/discard prompt). Guarded so a bad callback can never
			# break the scan teardown.
			for cb in self.onScanComplete:
				try:
					cb()
				except:
					pass
		if self.name != "":
			self.network.setText(_("%s.  (%s)") % (self.network1, self.name) )
		if self.start_time2 > 0:                                                                           
			runtime = int(time()) - int(self.start_time2)
			self.transponder.setText(_("Scan Completed in %d Minutes  %02d Seconds.")  %( runtime / 60, (runtime % 60)))



	def isDone(self):
		return self.state == self.Done or self.state == self.Error

	def newService(self):
		# Runs as a C++ signal callback: an exception escaping here takes all
		# of enigma2 down (green screen). Keep the GUI update unconditional and
		# make sure the report bookkeeping can never raise.
		NoName = "NoName"
		self.signal =""
		# prefer the background sampler's capture for the tp being scanned;
		# instantaneous reads can hit the estimator before it has converged
		if self.tp_locked_db is not None:
			self.signal = self.tp_locked_db
		else:
			signal_data = get_signal_data(self.feid)
			self.signal = signal_data['snr']
		newServiceName = self.scan.getLastServiceName()
		newServiceName = newServiceName.rstrip('\x00')
		newServiceRef = self.scan.getLastServiceRef()
		if newServiceName =="":
			newServiceName = newServiceName + NoName
		# eDVBScan::getLastServiceRef() returns "" when it has no last service,
		# so never index the ref without checking its length first.
		if len(newServiceRef) > 5:
			if newServiceRef[4] >= "2.1":
				if newServiceRef[4] !="B":
				    if newServiceRef[4] !="C":
				        if newServiceRef[4] !="A":
				            newServiceName += " --- UnKnown Service Type %s%s" %(newServiceRef[4],newServiceRef[5] )
			if newServiceRef[4] == "2" or newServiceRef[4] == "A":
				self.r = self.r + 1
				newServiceName += ("   (Radio #%d)" % self.r)
		try:
			try:
				if BOX_MODEL == "edision":
					snr_text = '%.2f' % self.signal
				else:
					snr_text = '%s' % self.signal
			except:
				snr_text = ''
			self.report.service(self.y, newServiceName, newServiceRef, snr_text)
		except:
			print("[ServiceScan] could not append service to scan report")
		self.y = self.y + 1
		self.servicelist.addItem((newServiceName, newServiceRef))
		self.lcd_summary and self.lcd_summary.updateService(newServiceName)



	def destroy(self):
		self.state = self.Idle
		try:
			self.nitcapture.close()
		except:
			pass
		if self.scan is not None:
			self.scan.statusChanged.get().remove(self.scanStatusChanged)
			self.scan.newService.get().remove(self.newService)
			self.scan = None
		return
