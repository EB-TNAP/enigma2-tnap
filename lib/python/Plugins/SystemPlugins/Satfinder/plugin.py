from enigma import eDVBResourceManager, eDVBFrontendParametersSatellite, eDVBFrontendParametersTerrestrial, eTimer, ePoint
from Screens.ScanSetup import ScanSetup, buildTerTransponder
from Screens.ServiceScan import ServiceScan
from Screens.MessageBox import MessageBox
from Screens.ChoiceBox import ChoiceBox
import xml.etree.ElementTree as ET
from Plugins.Plugin import PluginDescriptor
from Components.Sources.FrontendStatus import FrontendStatus
from Components.ActionMap import ActionMap
from Components.NimManager import nimmanager, getConfigSatlist
from Components.config import config, ConfigSelection, ConfigSubsection, configfile, ACTIONKEY_RIGHT
from Components.TuneTest import Tuner
from Tools.Transponder import getChannelNumber, channel2frequency
from Tools.BoundFunction import boundFunction
from Screens.Screen import Screen # for services found class
from Components.Sources.StaticText import StaticText
from Components.ProgressBar import ProgressBar  # live AGC bar (raw ioctl driven)
from Components.Label import Label              # live AGC value labels
from Tools.Directories import fileExists   # Extra Import
import os  # Extra Import
import struct  # AGCReader ioctl buffers
import fcntl   # AGCReader ioctl calls
import select  # NIT network-name reader demux polling
import errno   # NIT network-name reader demux read errors
import threading  # Use threading instead of _thread
import traceback  # worker-thread failure reporting (see _runGuarded)
import time
import datetime

# Global flag to indicate if threads should continue running
THREAD_RUNNING = True

try: # for reading the current transport stream (SatfinderExtra)
	from Plugins.SystemPlugins.Satfinder import dvbreader
	dvbreader_available = True
	# Fix chmod octal value
	os.chmod("/usr/lib/enigma2/python/Plugins/SystemPlugins/Satfinder/dvbreader.so", 0o755)
except ImportError:
	print("[Satfinder] import dvbreader not available")
	dvbreader_available = False

if dvbreader_available:
	from skin import parameters
	from Components.ScrollLabel import ScrollLabel
	from Components.Label import Label
	from Tools.Hex2strColor import Hex2strColor

# Audible signal tone. Optional in every sense: the module is imported
# defensively, it reports its own backend availability, and it defaults to off.
# Level/mode naming, cycling and config come from the shared module too, so
# this screen and PositionerSetup read and write the SAME setting instead of
# each keeping its own - set it once, both screens honour it.
try:
	from Tools.SignalTone import (SignalTone, toneAvailable, toneConfig,
	                              levelName as _toneLevelName, noLockName as _noLockName,
	                              cycleLevel as _cycleToneLevel, cycleNoLock as _cycleNoLock)
	SIGNALTONE_AVAILABLE = toneAvailable()
	if not SIGNALTONE_AVAILABLE:
		print("[Satfinder] no ALSA player on this box -- signal tone disabled")
except Exception as e:
	print("[Satfinder] signaltone unavailable: %s" % e)
	SignalTone = None
	SIGNALTONE_AVAILABLE = False
	toneConfig = None

# Canvas is used for the live signal-trend graph at the bottom right. It ships
# with enigma2 (Components/Renderer/Canvas.py) but is optional here: if the
# import fails the trend block is dropped from the skin and the tuning list
# grows to fill the column instead, so the screen degrades cleanly on a build
# that does not have it.
try:
	from Components.Sources.CanvasSource import CanvasSource
	TREND_AVAILABLE = True
except ImportError:
	print("[Satfinder] CanvasSource not available -- signal trend disabled")
	TREND_AVAILABLE = False

# Box model detection
BOX_MODEL = ""
BOX_NAME = ""
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
		if BOX_NAME.startswith("et"):
			BOX_MODEL = "xtrend"
		elif BOX_NAME.startswith("os"):
			BOX_MODEL = "edision"
		elif BOX_NAME.startswith("sf"):
			BOX_MODEL = "octagon"
			if p == 1:
				BOX_NAME = "sf8008-Supreme"
		nimfile.close()
	except:
		pass

# ---------------------------------------------------------------------------
# Raw AGC reader -- signal strength below lock
# ---------------------------------------------------------------------------
# Why this exists: the skin's AGC widgets used to be driven by the
# FrontendInfo converter, but eDVBFrontend::readFrontendData(signalPower)
# in the enigma2 core is gated on m_state == stateLock, so AGC blanked to
# 0/N-A whenever the tuner was not locked -- i.e. exactly when you are
# swinging the dish and need it most (the Sonicview pre-lock "S" reading).
#
# FE_READ_SIGNAL_STRENGTH is NOT lock-gated at the driver level: both the
# Octagon (HiSilicon blob) and Edision (open-source AVL) drivers return the
# live AGC register regardless of lock state, on a 0-65535 relative scale.
# This was hardware-verified with fe-monitor / dish_monitor and is the same
# in-process ioctl approach already used by FESignalReader in ServiceScan.py.
#
# The frontend device allows additional O_RDONLY opens while enigma2 holds
# its O_RDWR handle, and FE_READ_* ioctls are permitted on read-only fds,
# so this side-channel never interferes with tuning. strength == 0 is a
# valid reading (deep null / no carrier); only an ioctl/open failure means
# "no value present".

FE_READ_SIGNAL_STRENGTH = 0x80026F47  # _IOR('o', 71, __u16)


class AGCReader:
	"""Side read-only fd on the frontend device for ungated AGC reads."""

	def __init__(self, feid):
		self.feid = feid
		self._fd = -1
		self._open()

	def _open(self):
		for path in ("/dev/dvb/adapter0/frontend%d" % self.feid,
					"/dev/dvb/adapter%d/frontend0" % self.feid):
			if os.path.exists(path):
				try:
					self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
					return
				except OSError as e:
					print("[Satfinder][AGCReader] open %s failed: %s" % (path, e))
		self._fd = -1

	def read(self):
		"""Return raw strength 0-65535, or None if no value is available."""
		if self._fd < 0:
			self._open()  # device may appear late (e.g. tuner switch)
			if self._fd < 0:
				return None
		try:
			buf = bytearray(2)
			fcntl.ioctl(self._fd, FE_READ_SIGNAL_STRENGTH, buf)
			return struct.unpack("H", bytes(buf))[0]
		except (OSError, IOError):
			return None

	def close(self):
		if self._fd >= 0:
			try:
				os.close(self._fd)
			except OSError:
				pass
			self._fd = -1


# ---------------------------------------------------------------------------
# NIT network-name reader -- pure-Python demux section filter
# ---------------------------------------------------------------------------
# Why this exists: dvbreader.read_nit() only returns entries from the NIT's
# second loop (the transport-stream loop carrying the delivery-system
# descriptors), so the network-level FIRST descriptor loop -- where the
# network_name_descriptor (tag 0x40) lives -- never reaches Python. Feed
# transponders frequently broadcast a NIT with a network name but no
# satellite_delivery_system_descriptor at all, in which case the POS field
# can only say "No NIT data" even though the NIT identifies the uplinker
# (hardware-verified on the sf8008 with enigma2_nit_dump.py: network_name
# "Ericsson", no 0x43 descriptor in the section).
#
# Each open() on a demux node is an independent Linux-DVB section filter, so
# this reader coexists with the dvbreader fds on the same demux device
# without disturbing them. DMX_CHECK_CRC makes the kernel verify the section
# CRC_32 before delivery; the kernel-side timeout is disabled and the
# reader's own deadline plus the plugin's thread controls bound the capture.

_DMX_FILTER_SIZE = 16
_DMX_CHECK_CRC = 1
_DMX_IMMEDIATE_START = 4

# Native C layout of struct dmx_sct_filter_params:
#   __u16 pid; __u8 filter[16]; __u8 mask[16]; __u8 mode[16];
#   __u32 timeout; __u32 flags;
_DMX_SCT_STRUCT = "@H16s16s16sII"

# _IOW('o', 43, struct dmx_sct_filter_params) -- computed from the pack
# format so the encoded size always matches the struct actually sent.
_DMX_SET_FILTER = ((1 << 30)
	| (struct.calcsize(_DMX_SCT_STRUCT) << 16)
	| (ord("o") << 8)
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
	"""Strip control codes for OSD display.

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
	"""Practical DVB text decoder (EN 300 468 annex A subset).

	Handles plain ASCII/ISO-6937 defaults plus the common explicitly
	signalled character sets; latin-1 is the readable fallback for the rest.
	"""
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

	Section-filter reads normally deliver one complete section, but this also
	copes with concatenated or partially accumulated reads. Returns
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


def _nitNetworkNames(section):
	"""Return (network_name, multilingual_fallback) from one NIT section.

	Walks only the network-level (first) descriptor loop; either value is
	None when the corresponding descriptor is absent or empty."""
	if len(section) < 12 or section[0] != _NIT_ACTUAL_TABLE_ID:
		return None, None
	section_length = ((section[1] & 0x0F) << 8) | section[2]
	crc_start = min(3 + section_length, len(section)) - 4
	loop_length = ((section[8] & 0x0F) << 8) | section[9]
	offset = 10
	loop_end = min(offset + loop_length, crc_start)
	name = ml_name = None
	while offset + 2 <= loop_end:
		tag = section[offset]
		length = section[offset + 1]
		if offset + 2 + length > loop_end:
			break  # truncated/malformed descriptor -- stop walking
		body = section[offset + 2:offset + 2 + length]
		if tag == 0x40 and name is None:
			name = _decodeDvbText(body) or None
		elif tag == 0x5B and ml_name is None and len(body) >= 4:
			# multilingual_network_name_descriptor: first language entry.
			name_length = body[3]
			if 4 + name_length <= len(body):
				ml_name = _decodeDvbText(body[4:4 + name_length]) or None
		offset += 2 + length
	return name, ml_name


# ---------------------------------------------------------------------------
# TNAP embedded Signal-finder skin
# ---------------------------------------------------------------------------
# Rationale: every skin ships its own <screen name="Satfinder"> and most of them
# omit the widgets this plugin relies on (onid/tsid/pos, the "Load Blindscan"
# blue key, the large readouts). By giving the screen a UNIQUE skinName that no
# installed skin defines, enigma2's readSkin() never finds a match in the loaded
# skins and falls back to the embedded self.skin below -- so our layout always
# wins, regardless of the active skin. Same pattern used for BlindscanState.
#
# The skin is fully self-contained: no <panel> includes, no skin-defined color
# names, no skin-private pixmaps. Colours are inlined as #AARRGGBB (enigma2:
# 0x00 alpha byte = opaque, 0xFF = transparent) and every one of them comes from
# the _CLR palette below, so the whole look can be retuned in one place. The
# only external pixmap is our own signalbar.png. Designed at 1920x1080;
# resolution="1920,1080" lets forks that support it auto-scale on other panels.
#
# Layout is a two-column instrument panel:
#
#   header band          title + accent rule / clock + date / NIT network name
#   meter block          SNR and AGC as caption cell | gradient track | value cell
#   stream row           Services / ONID / TSID / POS chips (SatfinderExtra only)
#   left column          SNR dB, AGC, BER stat cards + LOCK pill + peak hold
#   right column         tuning config list + 60 s signal trend graph
#   footer band          colour-key legend
#
# Everything is aligned to a 30px outer margin and a shared 1016px content
# baseline so the two columns bottom out together. Widget NAMES are unchanged
# from the previous layout -- this is a restyle, not a rewrite of the bindings.

# Shared building blocks ----------------------------------------------------

# Absolute path to our own gradient bar, derived from where the plugin is
# installed so it always resolves (an embedded skin has no owning skin dir, so
# a relative "infobar/bar_big.png" wrongly falls back to skin_default's white
# bar). If the PNG is ever missing, the Progress widgets below carry a green
# foregroundColor fallback, so the bar is never an unreadable white block.
PLUGIN_PATH = os.path.dirname(os.path.realpath(__file__))
_BAR_PIXMAP = os.path.join(PLUGIN_PATH, "signalbar.png")

# Palette. Single source of truth for the whole screen -- change a value here
# and every widget that uses it follows.
_CLR = {
	"bar":      _BAR_PIXMAP,
	"screen":   "#00080809",  # page background
	"chrome":   "#000d0e12",  # header / footer bands
	"panel":    "#0012141a",  # card surface, meter trough, chip background
	"panel2":   "#001a1d26",  # raised cell (meter caption / meter value)
	"line":     "#001d2029",  # hairlines, separators, graph grid
	"text":     "#00f0f0f0",
	"dim":      "#008c93a1",  # captions and secondary text
	"accent":   "#00ffc000",  # TNAP amber -- decoded stream values
	"green":    "#0043c95a",
	"greenink": "#00061006",  # text drawn on top of a green fill
	"red":      "#008c1f22",
	"amber":    "#00ffb020",
	"peak":     "#00d8dde6",  # peak-hold needle
}

# Geometry the Python side also needs: the meter tracks (peak-hold needles are
# moved along them at runtime) and the trend canvas. Keep these in step with
# the XML below -- they are the same numbers, named once.
_BAR_X = 196
_BAR_W = 1320
_BAR_H = 66
_SNR_BAR_Y = 152
_AGC_BAR_Y = 238
_PEAK_W = 4

_TREND_W = 1430
_TREND_H = 144
_TREND_TOP = 34          # caption band kept clear of the plot
_TREND_COL = 10          # px per sample -> 143 samples across
_TREND_SAMPLES = _TREND_W // _TREND_COL

_CFG_TOP = 384
_CONTENT_BOTTOM = 1016

_SAT_SKIN_HEADER = """
	<eLabel position="0,0" size="1920,1080" backgroundColor="%(screen)s" zPosition="-3"/>
	<eLabel position="0,0" size="1920,124" backgroundColor="%(chrome)s" zPosition="-2"/>
	<eLabel position="0,124" size="1920,2" backgroundColor="%(line)s" zPosition="-1"/>
	<eLabel position="30,28" size="6,64" backgroundColor="%(accent)s" zPosition="1"/>
	<widget source="Title" render="Label" position="54,24" size="1140,46" font="Regular;40" foregroundColor="%(text)s" transparent="1" valign="center" halign="left" noWrap="1"/>
	<widget source="global.CurrentTime" render="Label" position="1430,20" size="460,52" font="Regular;44" foregroundColor="%(text)s" transparent="1" halign="right" valign="center">
		<convert type="ClockToText">Format:%%H:%%M</convert>
	</widget>
	<widget source="global.CurrentTime" render="Label" position="1230,74" size="660,34" font="Regular;26" foregroundColor="%(dim)s" transparent="1" halign="right" valign="center">
		<convert type="ClockToText">Date</convert>
	</widget>
""" % _CLR

# Meter block. Each row is caption cell | gradient track | value cell instead of
# text floating on top of the gradient: white-on-red at the left end of the bar
# was the worst contrast on the old screen, and the percentage used to sit half
# on the fill and half on the background depending on the reading. The value now
# has a fixed cell of its own, so it never moves and never fights the gradient.
#
# snr_peak / agc_peak are 4px needles parked at the highest reading seen since
# the last retune (see _updateInstruments). They are Labels with font size 1 and
# no text, so all they ever paint is their own background -- the same trick the
# conditional colour-key chips below use, and no extra pixmap.
_SAT_SKIN_METERS = """
	<eLabel position="30,146" size="160,78" backgroundColor="%(panel2)s"/>
	<eLabel text="SNR" position="30,146" size="160,78" font="Regular;38" halign="center" valign="center" transparent="1" foregroundColor="%(text)s" zPosition="2"/>
	<widget source="Frontend" render="Progress" pixmap="%(bar)s" position="196,152" size="1320,66" backgroundColor="%(panel)s" foregroundColor="%(green)s">
		<convert type="FrontendInfo">SNR</convert>
	</widget>
	<widget name="snr_peak" position="196,152" size="4,66" backgroundColor="%(peak)s" font="Regular;1" zPosition="3"/>
	<eLabel position="1524,146" size="366,78" backgroundColor="%(panel2)s"/>
	<widget source="Frontend" render="Label" position="1524,146" size="342,78" halign="right" valign="center" transparent="1" foregroundColor="%(text)s" font="Regular;46" zPosition="2">
		<convert type="FrontendInfo">SNR</convert>
	</widget>

	<eLabel position="30,232" size="160,78" backgroundColor="%(panel2)s"/>
	<eLabel text="AGC" position="30,232" size="160,78" font="Regular;38" halign="center" valign="center" transparent="1" foregroundColor="%(text)s" zPosition="2"/>
	<widget name="agc_bar" pixmap="%(bar)s" position="196,238" size="1320,66" backgroundColor="%(panel)s" foregroundColor="%(green)s"/>
	<widget name="agc_peak" position="196,238" size="4,66" backgroundColor="%(peak)s" font="Regular;1" zPosition="3"/>
	<eLabel position="1524,232" size="366,78" backgroundColor="%(panel2)s"/>
	<widget name="agc_value" position="1524,232" size="342,78" halign="right" valign="center" transparent="1" foregroundColor="%(text)s" font="Regular;46" zPosition="2"/>
""" % _CLR

# Left column: three stat cards, a LOCK pill and the peak-hold strip. Cards are
# a flat panel with a 5px coloured edge and a small dim caption above the value,
# which gives the readouts a visible hierarchy the old bare "SNR:" / big number
# stack did not have. Font drops from 108 to 80 -- still legible from the dish,
# but it stops the numbers from crowding the card and lets BER share the column.
_SAT_SKIN_READOUTS = """
	<eLabel position="30,384" size="400,150" backgroundColor="%(panel)s"/>
	<eLabel position="30,384" size="5,150" backgroundColor="%(green)s"/>
	<eLabel text="SNR" position="52,398" size="240,28" font="Regular;24" transparent="1" foregroundColor="%(dim)s" zPosition="2"/>
	<widget source="Frontend" render="Label" position="52,428" size="360,94" font="Regular;80" halign="left" valign="center" transparent="1" foregroundColor="%(text)s" zPosition="2">
		<convert type="FrontendInfo">SNRdB</convert>
	</widget>

	<eLabel position="30,546" size="400,150" backgroundColor="%(panel)s"/>
	<eLabel position="30,546" size="5,150" backgroundColor="%(amber)s"/>
	<eLabel text="AGC" position="52,560" size="240,28" font="Regular;24" transparent="1" foregroundColor="%(dim)s" zPosition="2"/>
	<widget name="agc_big" position="52,590" size="360,94" font="Regular;80" halign="left" valign="center" transparent="1" foregroundColor="%(text)s" zPosition="2"/>

	<eLabel position="30,708" size="400,150" backgroundColor="%(panel)s"/>
	<eLabel position="30,708" size="5,150" backgroundColor="%(dim)s"/>
	<eLabel text="BER" position="52,722" size="240,28" font="Regular;24" transparent="1" foregroundColor="%(dim)s" zPosition="2"/>
	<widget source="Frontend" render="Label" position="52,752" size="360,94" font="Regular;80" halign="left" valign="center" transparent="1" foregroundColor="%(text)s" zPosition="2">
		<convert type="FrontendInfo">BER</convert>
	</widget>

	<widget text="LOCK" source="Frontend" render="FixedLabel" position="30,870" size="400,76" font="Regular;52" halign="center" valign="center" foregroundColor="%(greenink)s" backgroundColor="%(green)s" zPosition="3">
		<convert type="FrontendInfo">LOCK</convert>
		<convert type="ConditionalShowHide"/>
	</widget>
	<widget text="NO LOCK" source="Frontend" render="FixedLabel" position="30,870" size="400,76" font="Regular;52" halign="center" valign="center" foregroundColor="%(text)s" backgroundColor="%(red)s" zPosition="2">
		<convert type="FrontendInfo">LOCK</convert>
		<convert type="ConditionalShowHide">Invert</convert>
	</widget>

	<eLabel position="30,958" size="400,58" backgroundColor="%(panel)s"/>
	<widget name="peak_text" position="48,958" size="368,58" font="Regular;28" halign="left" valign="center" transparent="1" foregroundColor="%(accent)s" zPosition="2"/>
""" % _CLR


def _satSkinConfig(height):
	"""Tuning list on a panel of its own, inset 16px so the selection
	highlight never touches the panel edge. Height varies: the trend graph
	takes the bottom of the column when Canvas rendering is available.

	NOTE the zPosition on the list. GUISkin.createGUIScreen() instantiates
	every named/source component first and only then attaches the skin's
	additionalWidgets (the raw eLabels), so at equal zPosition an OPAQUE
	eLabel is always painted OVER a widget, whatever the document order says.
	Any widget that sits on one of the panels in this skin therefore needs an
	explicit zPosition above 0 -- the readouts, meters and trend canvas all
	carry one for the same reason."""
	values = dict(_CLR)
	values["panel_h"] = height
	values["list_h"] = height - 28
	return """
	<eLabel position="460,384" size="1430,%(panel_h)d" backgroundColor="%(panel)s"/>
	<widget name="config" position="476,398" size="1398,%(list_h)d" itemHeight="49" font="Regular;36" valueFont="Regular;30" transparent="1" enableWrapAround="1" scrollbarMode="showOnDemand" zPosition="2"/>
""" % values


# Rolling 60-70s trend of AGC (filled, amber) and SNR (line, green). This is the
# part of the screen that actually helps you peak a dish: a single live number
# tells you nothing about whether the last nudge helped, and AGC is readable
# below lock, so the amber trace is useful before the green one exists. Drawn
# with CanvasSource fills only -- no drawLine, no writeText -- so it works on
# any build that ships Components/Renderer/Canvas.py, and the block is omitted
# entirely (config list grows to fill the column) when it does not.
_SAT_SKIN_TREND = """
	<eLabel position="460,872" size="1430,144" backgroundColor="%(panel)s"/>
	<widget source="trend" render="Canvas" position="460,872" size="1430,144" zPosition="2"/>
	<eLabel text="SIGNAL TREND" position="476,878" size="300,26" font="Regular;22" transparent="1" foregroundColor="%(dim)s" zPosition="4"/>
	<widget name="tone_status" position="800,876" size="780,30" font="Regular;22" transparent="1" foregroundColor="%(accent)s" halign="left" valign="center" zPosition="4"/>
	<eLabel position="1610,888" size="20,6" backgroundColor="%(amber)s" zPosition="4"/>
	<eLabel text="AGC" position="1638,876" size="80,30" font="Regular;22" transparent="1" foregroundColor="%(amber)s" zPosition="4"/>
	<eLabel position="1734,888" size="20,6" backgroundColor="%(green)s" zPosition="4"/>
	<eLabel text="SNR" position="1762,876" size="80,30" font="Regular;22" transparent="1" foregroundColor="%(green)s" zPosition="4"/>
""" % _CLR

if TREND_AVAILABLE:
	_SAT_SKIN_MAIN = _satSkinConfig(476) + _SAT_SKIN_TREND
else:
	_SAT_SKIN_MAIN = _satSkinConfig(632)

# Services / ONID / TSID / POS row -- only present on SatfinderExtra (needs
# dvbreader). The network name sits on the header sub-line, mirroring the date
# on the right: full width for long names (up to 255 bytes are legal in the
# NIT) instead of squeezing a fourth box into this row. It lives in this
# block, not _SAT_SKIN_HEADER, because the "network" source only exists on
# SatfinderExtra and a widget bound to a missing source is a skin error.
#
# Every caption/value pair is bound to its StaticText source through
# ConditionalShowHide (same pattern as the blue/yellow keys), so a field that
# has nothing to show disappears entirely instead of painting an empty box.
# Captions are FixedLabels rather than eLabels precisely so they can follow
# their value's visibility -- an eLabel has no source and can never hide. The
# value Label paints its own background, replacing the old separate box
# eLabel for the same reason.
_SAT_SKIN_DVBROW = """
	<widget source="network" render="Label" position="54,72" size="1140,38" font="Regular;28" foregroundColor="%(accent)s" transparent="1" halign="left" valign="center" noWrap="1"/>

	<widget source="services" render="FixedLabel" text="Services" position="30,322" size="150,46" font="Regular;26" transparent="1" foregroundColor="%(dim)s" halign="right" valign="center" zPosition="1">
		<convert type="ConditionalShowHide"/>
	</widget>
	<widget source="services" render="Label" position="190,322" size="140,46" font="Regular;30" foregroundColor="%(accent)s" backgroundColor="%(panel)s" halign="center" valign="center" zPosition="1">
		<convert type="ConditionalShowHide"/>
	</widget>

	<widget source="onid" render="FixedLabel" text="ONID" position="350,322" size="130,46" font="Regular;26" transparent="1" foregroundColor="%(dim)s" halign="right" valign="center" zPosition="1">
		<convert type="ConditionalShowHide"/>
	</widget>
	<widget source="onid" render="Label" position="490,322" size="140,46" font="Regular;30" foregroundColor="%(accent)s" backgroundColor="%(panel)s" halign="center" valign="center" zPosition="1">
		<convert type="ConditionalShowHide"/>
	</widget>

	<widget source="tsid" render="FixedLabel" text="TSID" position="650,322" size="130,46" font="Regular;26" transparent="1" foregroundColor="%(dim)s" halign="right" valign="center" zPosition="1">
		<convert type="ConditionalShowHide"/>
	</widget>
	<widget source="tsid" render="Label" position="790,322" size="140,46" font="Regular;30" foregroundColor="%(accent)s" backgroundColor="%(panel)s" halign="center" valign="center" zPosition="1">
		<convert type="ConditionalShowHide"/>
	</widget>

	<widget source="pos" render="FixedLabel" text="POS" position="950,322" size="110,46" font="Regular;26" transparent="1" foregroundColor="%(dim)s" halign="right" valign="center" zPosition="1">
		<convert type="ConditionalShowHide"/>
	</widget>
	<widget source="pos" render="Label" position="1070,322" size="820,46" font="Regular;30" foregroundColor="%(accent)s" backgroundColor="%(panel)s" halign="center" valign="center" zPosition="1">
		<convert type="ConditionalShowHide"/>
	</widget>
""" % _CLR

# Base Satfinder has no stream row, so a hairline stands in for it and keeps the
# meters visually separated from the content columns below.
_SAT_SKIN_PLAINROW = """
	<eLabel position="30,344" size="1860,2" backgroundColor="%(line)s"/>
""" % _CLR

# Bottom colour-key bar on its own chrome band. Red/Green chips are always
# present. Yellow and Blue only exist in some states, so each is drawn as a
# key-bound Label whose fore/background match (its text is hidden by the colour
# match, leaving a solid square) plus a ConditionalShowHide on both chip and
# caption -- so an unavailable key leaves no orphan chip behind. No extra asset.
_SAT_SKIN_BUTTONS_RGB = """
	<eLabel position="0,1022" size="1920,2" backgroundColor="%(line)s" zPosition="-1"/>
	<eLabel position="0,1024" size="1920,56" backgroundColor="%(chrome)s" zPosition="-2"/>

	<eLabel position="30,1038" size="26,26" backgroundColor="#00ff4a3c" zPosition="2"/>
	<widget source="key_red" render="Label" position="68,1032" size="380,38" font="Regular;30" foregroundColor="%(text)s" transparent="1" valign="center" halign="left"/>

	<eLabel position="490,1038" size="26,26" backgroundColor="%(green)s" zPosition="2"/>
	<widget source="key_green" render="Label" position="528,1032" size="380,38" font="Regular;30" foregroundColor="%(text)s" transparent="1" valign="center" halign="left"/>
""" % _CLR

_SAT_SKIN_BUTTON_YELLOW = """
	<widget source="key_yellow" render="Label" position="950,1038" size="26,26" font="Regular;1" backgroundColor="%(accent)s" foregroundColor="%(accent)s" zPosition="2">
		<convert type="ConditionalShowHide"/>
	</widget>
	<widget source="key_yellow" render="Label" position="988,1032" size="380,38" font="Regular;30" foregroundColor="%(text)s" transparent="1" valign="center" halign="left">
		<convert type="ConditionalShowHide"/>
	</widget>
""" % _CLR

_SAT_SKIN_BUTTON_BLUE = """
	<widget source="key_blue" render="Label" position="1410,1038" size="26,26" font="Regular;1" backgroundColor="#00879ce1" foregroundColor="#00879ce1" zPosition="2">
		<convert type="ConditionalShowHide"/>
	</widget>
	<widget source="key_blue" render="Label" position="1448,1032" size="442,38" font="Regular;30" foregroundColor="%(text)s" transparent="1" valign="center" halign="left">
		<convert type="ConditionalShowHide"/>
	</widget>
""" % _CLR

# Full screens --------------------------------------------------------------

# Base Satfinder (no AutoBouquetsMaker / dvbreader) -- no ONID/TSID/POS row.
SATFINDER_SKIN_BASE = (
	'<screen name="TNAP_Satfinder" position="0,0" size="1920,1080" '
	'title="Signal finder" flags="wfNoBorder" backgroundColor="#00000000" '
	'resolution="1920,1080">'
	+ _SAT_SKIN_HEADER
	+ _SAT_SKIN_METERS
	+ _SAT_SKIN_PLAINROW
	+ _SAT_SKIN_READOUTS
	+ _SAT_SKIN_MAIN
	+ _SAT_SKIN_BUTTONS_RGB
	+ _SAT_SKIN_BUTTON_BLUE
	+ '</screen>'
)

# SatfinderExtra -- includes the ONID/TSID/POS network-info row.
SATFINDER_SKIN_EXTRA = (
	'<screen name="TNAP_Satfinder" position="0,0" size="1920,1080" '
	'title="Signal finder" flags="wfNoBorder" backgroundColor="#00000000" '
	'resolution="1920,1080">'
	+ _SAT_SKIN_HEADER
	+ _SAT_SKIN_DVBROW
	+ _SAT_SKIN_METERS
	+ _SAT_SKIN_READOUTS
	+ _SAT_SKIN_MAIN
	+ _SAT_SKIN_BUTTONS_RGB
	+ _SAT_SKIN_BUTTON_YELLOW
	+ _SAT_SKIN_BUTTON_BLUE
	+ '</screen>'
)


class Satfinder(ScanSetup, ServiceScan):
	"""Inherits StaticText [key_red] and [key_green] properties from ScanSetup"""

	def __init__(self, session):
		# Force our own self-contained layout instead of whatever the active
		# skin ships for "Satfinder". skinName is unique so readSkin() misses
		# every installed skin and falls back to self.skin below. (readSkin runs
		# after __init__ completes, so setting these here is enough; SatfinderExtra
		# overrides them with its ONID/TSID/POS variant.)
		self.skin = SATFINDER_SKIN_BASE
		self.skinName = ["TNAP_Satfinder"]

		self.initcomplete = False
		service = session and session.nav.getCurrentService()
		feinfo = service and service.frontendInfo()
		self.frontendData = feinfo and feinfo.getAll(True)
		del feinfo
		del service

		# Initialize member variables
		self.typeOfTuningEntry = None
		self.systemEntry = None
		self.systemEntryATSC = None
		self.satfinderTunerEntry = None
		self.satEntry = None
		self.typeOfInputEntry = None
		self.DVB_TypeEntry = None
		self.systemEntryTerr = None
		self.preDefTransponderEntry = None
		self.preDefTransponderCableEntry = None
		self.preDefTransponderTerrEntry = None
		self.preDefTransponderAtscEntry = None
		self.frontend = None
		self.is_id_boolEntry = None
		self.t2mi_plp_id_boolEntry = None
		self.raw_channel = None
		self.transponder = None
		self.tuner = None
		self.blindscan_transponders = None  # None = satellites.xml; list = blindscan loaded
		self._blindscan_orbpos = None       # orbital position the loaded blindscan belongs to
		
		# Initialize memory variables to prevent potential errors
		self.is_id_memory = -1
		self.pls_mode_memory = eDVBFrontendParametersSatellite.PLS_Gold
		self.pls_code_memory = eDVBFrontendParametersSatellite.PLS_Default_Gold_Code
		self.t2mi_plp_id_memory = -1
		self.t2mi_pid_memory = eDVBFrontendParametersSatellite.T2MI_Default_Pid
		
		self.timer = eTimer()
		self.timer.callback.append(self.updateFrontendStatus)

		ScanSetup.__init__(self, session)
		self.entryChanged = self.newConfig
		self.setTitle(_("Signal finder") + " for " + BOX_MODEL + " " + BOX_NAME)
		self["Frontend"] = FrontendStatus(frontend_source=lambda: self.frontend, update_interval=100)
		self["key_blue"] = StaticText("")

		# Live AGC -- raw ioctl, NOT the lock-gated FrontendInfo path, so a
		# value is shown at all times the driver reports one (i.e. below
		# lock too, for dish alignment). See AGCReader above. 250ms poll is
		# plenty: the drivers refresh the AGC register at only 0.6-1.3 Hz.
		self["agc_bar"] = ProgressBar()
		self["agc_value"] = Label("")
		self["agc_big"] = Label("")
		self._agc_reader = None
		self.agc_timer = eTimer()
		self.agc_timer.callback.append(self._updateMeters)
		self.agc_timer.start(250)

		# Peak hold + signal trend. Both are fed from the same 250ms tick as
		# the AGC bar (see _updateInstruments) and both reset on every retune,
		# so a peak always refers to the transponder currently on screen.
		self["snr_peak"] = Label("")
		self["agc_peak"] = Label("")
		self["peak_text"] = Label("")
		self["tone_status"] = Label("")
		if TREND_AVAILABLE:
			self["trend"] = CanvasSource()
		self._peak_snr_pct = 0
		self._peak_agc_pct = 0
		self._peak_snr_db = None
		self._needle_x = {"snr_peak": None, "agc_peak": None}
		self._trend = []
		self._trend_tick = 0
		self._tone_locked = False
		self._instr_sig = None

		self["actions"] = ActionMap(["SetupActions", "ColorActions"],
		{
			"save": self.keyGoScan,
			"ok": self.keyOK,
			"cancel": self.keyCancel,
			"blue": self.keyBlue,
		}, -3)

		# MENU cycles the sound level. Its own ActionMap because "menu" is not
		# in SetupActions/ColorActions, and MenuActions is present in every
		# keymap.xml. Deliberately NOT a number key: the config list needs
		# those for direct frequency entry.
		self["tone_actions"] = ActionMap(["MenuActions"],
		{
			"menu": self.keyToneCycle,
		}, -2)

		# AUDIO toggles what you hear with no lock. Its own key rather than a
		# second axis on MENU, because both need to be one blind press while
		# you are at the dish.
		self["tone_actions2"] = ActionMap(["InfobarAudioSelectionActions"],
		{
			"audioSelection": self.keyNoLockCycle,
		}, -2)
		self._tone = None
		self._tone_error = None
		self._tone_shown = None

		self.initcomplete = True
		self.session.postScanService = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		self.session.nav.stopService()
		self.onClose.append(self.__onClose)
		self.onLayoutFinish.append(self._initInstruments)
		self.onShow.append(self.prepareFrontend)
		# Mute whenever the screen loses focus -- a ChoiceBox or MessageBox
		# opening over the top would otherwise leave the tone whining
		# underneath it. pause/resume rather than stop/start, so a dialog does
		# not cost a process restart.
		self.onShow.append(self._toneApply)
		self.onHide.append(self._tonePause)
		# Hide the blue "Load Blindscan" key unless a blindscan file is present.
		self._updateBlueButton()

	def openFrontend(self):
		try:
			res_mgr = eDVBResourceManager.getInstance()
			if res_mgr:
				self.raw_channel = res_mgr.allocateRawChannel(self.feid)
				if self.raw_channel:
					self.frontend = self.raw_channel.getFrontend()
					if self.frontend:
						return True
			return False
		except Exception as e:
			print(f"Error opening frontend: {e}")
			return False

	def prepareFrontend(self):
		if getattr(self, '_frontend_error', False):
			return  # MessageBox already shown; wait for callback before proceeding
		# Clean up existing resources first
		self.frontend = None
		if hasattr(self, 'raw_channel') and self.raw_channel:
			del self.raw_channel
			self.raw_channel = None

		if not self.openFrontend():
			self.session.nav.stopService()
			if not self.openFrontend():
				if self.session.pipshown:
					from Screens.InfoBar import InfoBar
					if InfoBar.instance and hasattr(InfoBar.instance, "showPiP"):
						InfoBar.instance.showPiP()
						if not self.openFrontend():
							self._showFrontendError(_("All tuners are in use. Cannot start signal finder."))
							return
				else:
					self._showFrontendError(_("Failed to open frontend. All tuners might be in use."))
					return

		self.tuner = Tuner(self.frontend)
		self.retune()

	def _showFrontendError(self, message):
		self._frontend_error = True
		self.session.openWithCallback(self._frontendErrorClosed, MessageBox, message, MessageBox.TYPE_ERROR)

	def _frontendErrorClosed(self, answer=None):
		self._frontend_error = False
		self.close(False)

	def showError(self, message):
		"""Display an error message to the user"""
		self.session.open(MessageBox, message, MessageBox.TYPE_ERROR)

	def updateFrontendStatus(self):
		if not hasattr(self, 'frontend') or not self.frontend:
			return
			
		try:
			dict = {}
			self.frontend.getFrontendStatus(dict)
			if dict["tuner_state"] == "FAILED" or dict["tuner_state"] == "LOSTLOCK":
				self.retune()
			else:
				self.timer.start(500, True)
		except Exception as e:
			print(f"Error updating frontend status: {e}")
			self.timer.start(1000, True)  # Retry after a longer delay

	def _updateMeters(self):
		"""Single 250ms tick: AGC bar/labels, then peak hold and trend."""
		agc_pct = self._updateAGC()
		self._updateInstruments(agc_pct)

	def _updateAGC(self):
		"""Poll raw signal strength and paint the AGC bar/labels.

		Runs on the main thread via eTimer, so touching GUI components here
		is safe. Independent of lock state and of the retune cycle: the side
		fd keeps reading the AGC register even while updateFrontendStatus()
		is busy re-tuning after FAILED/LOSTLOCK.

		Returns the percentage shown, or None when no value is available.
		"""
		feid = getattr(self, "feid", None)
		if feid is None:
			self._setAGC(None)
			return None
		# (Re)open the reader on first use or after a tuner switch.
		if self._agc_reader is None or self._agc_reader.feid != feid:
			if self._agc_reader is not None:
				self._agc_reader.close()
			self._agc_reader = AGCReader(feid)
		strength = self._agc_reader.read()
		if strength is None:
			self._setAGC(None)  # no value present (ioctl/open failure only)
			return None
		# 0 is a valid reading (deep null) -- show it, don't blank it.
		pct = strength * 100 // 65535
		self._setAGC(pct)
		return pct

	def _setAGC(self, pct):
		if pct is None:
			self["agc_bar"].setValue(0)
			self["agc_value"].setText("---")
			self["agc_big"].setText("---")
		else:
			text = "%d %%" % pct  # match the FrontendInfo SNR label format
			self["agc_bar"].setValue(pct)
			self["agc_value"].setText(text)
			self["agc_big"].setText(text)

	# -----------------------------------------------------------------------
	# Peak hold and signal trend
	# -----------------------------------------------------------------------
	# Why these exist: a live number tells you the signal right now, but not
	# whether the last nudge of the dish made it better or worse -- which is
	# the only question that matters while you are on the roof. Peak hold
	# remembers the best reading since the current transponder was tuned, and
	# the trend graph keeps roughly the last minute on screen. AGC is drawn as
	# well as SNR because AGC is readable below lock (see AGCReader), so the
	# amber trace is the useful one while you are still hunting for the bird.
	#
	# Both are driven from the existing 250ms AGC tick. The graph is redrawn at
	# 1Hz from samples taken at 2Hz: the drivers only refresh their registers
	# at roughly 1Hz anyway, and this keeps the canvas to about 300 fills per
	# second on a box that also has a tuner and a demux to service.

	# -----------------------------------------------------------------------
	# Audible tone
	# -----------------------------------------------------------------------

	def keyToneCycle(self):
		"""MENU: step Off -> Low -> Medium -> High -> Off and persist it."""
		if toneConfig is None:
			return
		cfg = toneConfig().level
		cfg.value = _cycleToneLevel(cfg.value)
		cfg.save()
		configfile.save()
		self._tone_error = None
		self._toneApply()

	def keyNoLockCycle(self):
		"""AUDIO: switch the no-lock sound between the search pulse and silence."""
		if toneConfig is None:
			return
		cfg = toneConfig().nolock
		cfg.value = _cycleNoLock(cfg.value)
		cfg.save()
		configfile.save()
		if self._tone is not None:
			self._tone.setSearchEnabled(cfg.value == "search")
		self._updateToneStatus()

	def _toneApply(self):
		"""Bring the tone into line with the config value."""
		level = 0
		if toneConfig is not None:
			try:
				level = int(toneConfig().level.value)
			except (TypeError, ValueError):
				level = 0
		if level <= 0 or not SIGNALTONE_AVAILABLE:
			self._toneStop()
		elif self._tone is None:
			tone = SignalTone(volume=level / 100.0)
			tone.setSearchEnabled(toneConfig().nolock.value == "search")
			if tone.start():
				self._tone = tone
			else:
				self._tone_error = tone.error
				print("[Satfinder] signal tone failed to start: %s" % tone.error)
		else:
			self._tone.setVolume(level / 100.0)
			self._tone.setSearchEnabled(toneConfig().nolock.value == "search")
			self._tone.resume()
		self._updateToneStatus()

	def _tonePause(self):
		if self._tone is not None:
			self._tone.pause()

	def _toneStop(self):
		tone, self._tone = self._tone, None
		if tone is not None:
			tone.stop()

	def _updateToneStatus(self):
		"""One short line in the trend panel caption band. Also the only
		discoverability hint for the MENU key, so it says something when the
		tone is off rather than nothing at all."""
		level = toneConfig().level.value if toneConfig is not None else "0"
		if not SIGNALTONE_AVAILABLE:
			text = _("Sound: not available on this box")
		elif self._tone_error:
			text = _("Sound: failed (%s)") % self._tone_error
		elif level == "0" or self._tone is None:
			text = _("MENU: sound off")
		else:
			nolock = toneConfig().nolock.value
			if self._tone_locked:
				now = _("SNR tone")
			elif nolock == "search":
				now = _("AGC search pulse")
			else:
				now = _("silent, no lock")
			text = "%s: %s   |   %s: %s   |   %s" % (
				_("Sound"), _toneLevelName(level),
				_("No lock"), _noLockName(nolock), now)
		if text != self._tone_shown:
			self._tone_shown = text
			self["tone_status"].setText(text)

	def _initInstruments(self):
		"""Park the peak needles out of sight and clear the graph (post-layout)."""
		for name in ("snr_peak", "agc_peak"):
			inst = getattr(self[name], "instance", None)
			if inst is not None:
				inst.hide()
		self._updatePeakText()
		self._drawTrend()

	def _checkInstrumentReset(self):
		"""Reset peaks/history only when the tuned transponder or tuner changed.

		Peak hold describes a TRANSPONDER, not a tune attempt. This used to
		reset on every retune() -- but updateFrontendStatus() calls retune() on
		every FAILED/LOSTLOCK, which on a motorised dish means every single
		time the rotor moves. A 16dB peak would quietly become 12dB as you
		stepped the dish, which is exactly backwards: peak hold earns its keep
		when you have swung PAST the best position and want to know what the
		best was. Momentary loss of lock now keeps the peak; changing satellite,
		transponder or tuner clears it.
		"""
		if not hasattr(self, "_trend"):
			return  # retune() can fire before __init__ finished
		try:
			signature = (getattr(self, "feid", None), repr(self.transponder))
		except Exception:
			return
		if signature != self._instr_sig:
			self._instr_sig = signature
			self._resetInstruments()

	def _resetInstruments(self):
		"""Drop peaks and history. Only reached via _checkInstrumentReset."""
		if not hasattr(self, "_trend"):
			return  # retune() can fire before __init__ finished
		self._peak_snr_pct = 0
		self._peak_agc_pct = 0
		self._peak_snr_db = None
		del self._trend[:]
		self._trend_tick = 0
		for name in ("snr_peak", "agc_peak"):
			self._needle_x[name] = None
			inst = getattr(self[name], "instance", None)
			if inst is not None:
				inst.hide()
		self._updatePeakText()
		self._drawTrend()

	def _readSnr(self):
		"""Return (snr_pct, snr_db or None, locked) straight from the frontend.

		Same normalisation the FrontendInfo converter uses (0-65535 -> percent,
		hundredths of a dB -> dB) so the graph and the readouts never disagree.
		Values outside a sane dB range are treated as 'not reported' -- some
		drivers park a sentinel there when they have no dB estimate.

		Both values are reported as zero/None unless the tuner is LOCKED. SNR
		is meaningless without lock, and the AVL62X1 returns a full-scale
		0xFFFF for a tick or two while it is still acquiring -- which used to
		be enough to strand the peak needle at 99 %% for the whole session.
		AGC is deliberately NOT gated this way (see AGCReader): a pre-lock AGC
		reading is the whole point of this screen.
		"""
		frontend = getattr(self, "frontend", None)
		if frontend is None:
			return 0, None, False
		status = {}
		try:
			frontend.getFrontendStatus(status)
		except Exception:
			return 0, None, False
		if status.get("tuner_state") != "LOCKED":
			return 0, None, False
		raw = status.get("tuner_signal_quality") or 0
		snr_pct = min(100, int(raw) * 100 // 65536)
		raw_db = status.get("tuner_signal_quality_db")
		snr_db = None
		if raw_db is not None and 0 < raw_db <= 10000:
			snr_db = raw_db / 100.0
		return snr_pct, snr_db, True

	def _updateInstruments(self, agc_pct):
		snr_pct, snr_db, locked = self._readSnr()
		self._tone_locked = locked
		if self._tone is not None:
			self._tone.update(agc_pct, snr_pct, locked)
		self._trend_tick += 1
		# One second of settling before anything can set a peak: a retune or a
		# tuner switch throws transients on both readings.
		settled = self._trend_tick > 4

		if settled:
			if agc_pct is not None and agc_pct > self._peak_agc_pct:
				self._peak_agc_pct = agc_pct
			if snr_pct > self._peak_snr_pct:
				self._peak_snr_pct = snr_pct
			if snr_db is not None and (self._peak_snr_db is None or snr_db > self._peak_snr_db):
				self._peak_snr_db = snr_db

		self._moveNeedle("snr_peak", _SNR_BAR_Y, self._peak_snr_pct)
		self._moveNeedle("agc_peak", _AGC_BAR_Y, self._peak_agc_pct)
		self._updatePeakText()

		if self._trend_tick % 2 == 0:
			self._trend.append((agc_pct or 0, snr_pct))
			if len(self._trend) > _TREND_SAMPLES:
				del self._trend[0:len(self._trend) - _TREND_SAMPLES]
		if self._trend_tick % 4 == 0:
			self._drawTrend()
			self._updateToneStatus()

	def _moveNeedle(self, name, y, pct):
		"""Slide a peak-hold needle along its track; hide it until there is a
		peak to show. Only moves when the pixel position actually changed, so
		a steady signal costs no repaints."""
		inst = getattr(self[name], "instance", None)
		if inst is None:
			return
		if pct <= 0:
			if self._needle_x[name] is not None:
				self._needle_x[name] = None
				inst.hide()
			return
		x = _BAR_X + int((_BAR_W - _PEAK_W) * min(pct, 100) / 100.0)
		if x == self._needle_x[name]:
			return
		self._needle_x[name] = x
		inst.move(ePoint(x, y))
		inst.show()

	def _updatePeakText(self):
		if self._peak_snr_db is not None:
			text = _("Peak") + "   %.1f dB" % self._peak_snr_db
		elif self._peak_agc_pct:
			text = _("Peak") + "   %d %% AGC" % self._peak_agc_pct
		else:
			text = _("Peak") + "   ---"
		self["peak_text"].setText(text)

	def _drawTrend(self):
		"""Repaint the trend graph, newest sample flush to the right edge.

		Fills only: one column for the AGC area, a 2px cap on top of it, and a
		3px mark for SNR. drawLine/writeText are deliberately avoided so this
		works on any build that has the Canvas renderer at all.
		"""
		if not TREND_AVAILABLE or "trend" not in self:
			return
		canvas = self["trend"]
		plot_h = _TREND_H - _TREND_TOP
		try:
			canvas.fill(0, 0, _TREND_W, _TREND_H, 0x0012141a)
			for frac in (0.25, 0.5, 0.75):
				canvas.fill(0, _TREND_H - int(frac * plot_h), _TREND_W, 1, 0x001d2029)
			canvas.fill(0, _TREND_H - 1, _TREND_W, 1, 0x001d2029)
			count = len(self._trend)
			for i in range(count):
				agc, snr = self._trend[count - 1 - i]
				x = _TREND_W - (i + 1) * _TREND_COL
				if x < 0:
					break
				# Full-width segments (no 1px gap) so the traces read as
				# continuous lines rather than a hatched block, and the wash
				# under AGC stays barely above the panel colour -- AGC pins at
				# 100 %% on a good dish, and a bright fill there just turns the
				# whole graph into a slab.
				if agc > 0:
					height = max(3, int(agc * plot_h / 100))
					canvas.fill(x, _TREND_H - height, _TREND_COL, height, 0x00161206)
					canvas.fill(x, _TREND_H - height, _TREND_COL, 3, 0x00ffb020)
				if snr > 0:
					top = _TREND_H - max(3, int(snr * plot_h / 100))
					canvas.fill(x, top, _TREND_COL, 3, 0x0043c95a)
			canvas.flush()
		except Exception as e:
			print("[Satfinder][trend] draw failed: %s" % e)

	def __onClose(self):
		try:
			# Kill the tone first. SignalTone is referenced by its own writer
			# thread, so it survives Screen.doClose() clearing this object's
			# __dict__ -- without this it would keep playing to an empty room
			# and holding a pipe fd.
			self._toneStop()
			if hasattr(self, 'timer') and self.timer:
				self.timer.stop()
			if hasattr(self, 'agc_timer') and self.agc_timer:
				self.agc_timer.stop()
			if getattr(self, '_agc_reader', None) is not None:
				self._agc_reader.close()
				self._agc_reader = None
			if hasattr(self, 'frontend'):
				self.frontend = None
			if hasattr(self, 'raw_channel') and self.raw_channel:
				del self.raw_channel
				self.raw_channel = None
			self.session.nav.playService(self.session.postScanService)
		except Exception as e:
			print(f"Error during close: {e}")


	def newConfig(self):
		cur = self["config"].getCurrent()
		if cur in (
					self.typeOfTuningEntry,
					self.systemEntry,
					self.typeOfInputEntry,
					self.systemEntryATSC,
					self.DVB_TypeEntry,
					self.systemEntryTerr,
					):  # update screen and retune
			self.createSetup()
			self.retune()

		elif cur == self.satEntry:  # satellite changed — auto-switch blindscan if active
			if self.blindscan_transponders is not None:
				self._updateBlindscanForSat()
			self.createSetup()
			self.retune()

		elif cur == self.satfinderTunerEntry: # switching tuners, update screen, get frontend, and retune (in prepareFrontend())
			self.feid = int(self.satfinder_scan_nims.value)
			self.createSetup()
			self.prepareFrontend()
			if self.frontend is None:
				msg = _("Tuner not available.")
				if self.session.nav.RecordTimer.isRecording():
					msg += _("\nRecording in progress.")
				self.session.open(MessageBox, msg, MessageBox.TYPE_ERROR)

		elif cur in (self.preDefTransponderEntry, self.preDefTransponderCableEntry, self.preDefTransponderTerrEntry, self.preDefTransponderAtscEntry): # retune only
			self.retune()
		elif cur == self.is_id_boolEntry:
			if self.is_id_boolEntry[1].value:
				self.scan_sat.is_id.value = 0 if self.is_id_memory < 0 else self.is_id_memory
				self.scan_sat.pls_mode.value = self.pls_mode_memory
				self.scan_sat.pls_code.value = self.pls_code_memory
			else:
				self.is_id_memory = self.scan_sat.is_id.value
				self.pls_mode_memory = self.scan_sat.pls_mode.value
				self.pls_code_memory = self.scan_sat.pls_code.value
				self.scan_sat.is_id.value = eDVBFrontendParametersSatellite.No_Stream_Id_Filter
				self.scan_sat.pls_mode.value = eDVBFrontendParametersSatellite.PLS_Gold
				self.scan_sat.pls_code.value = eDVBFrontendParametersSatellite.PLS_Default_Gold_Code
			self.createSetup()
			self.retune()
		elif cur == self.t2mi_plp_id_boolEntry:
			if self.t2mi_plp_id_boolEntry[1].value:
				self.scan_sat.t2mi_plp_id.value = 0 if self.t2mi_plp_id_memory < 0 else self.t2mi_plp_id_memory
				self.scan_sat.t2mi_pid.value = self.t2mi_pid_memory
			else:
				self.t2mi_plp_id_memory = self.scan_sat.t2mi_plp_id.value
				self.t2mi_pid_memory = self.scan_sat.t2mi_pid.value
				self.scan_sat.t2mi_plp_id.value = eDVBFrontendParametersSatellite.No_T2MI_PLP_Id
				self.scan_sat.t2mi_pid.value = eDVBFrontendParametersSatellite.T2MI_Default_Pid
			self.createSetup()
			self.retune()

	def createSetup(self):
		self.list = []
		indent = "  "
		self.satfinderTunerEntry = (_("Tuner"), self.satfinder_scan_nims)
		self.list.append(self.satfinderTunerEntry)
		self.DVB_type = self.nim_type_dict[int(self.satfinder_scan_nims.value)]["selection"]
		self.DVB_TypeEntry = (_("DVB type"), self.DVB_type) # multitype?
		if len(self.nim_type_dict[int(self.satfinder_scan_nims.value)]["modes"]) > 1:
			self.list.append(self.DVB_TypeEntry)
		if self.DVB_type.value == "DVB-S":
			self.tuning_sat = self.scan_satselection[self.getSelectedSatIndex(self.feid)]
			self.satEntry = (_('Satellite'), self.tuning_sat)
			self.list.append(self.satEntry)
			# Sync tuning_type value with blindscan state for this satellite
			try:
				current_orbpos = int(self.tuning_sat.value)
			except (ValueError, TypeError):
				current_orbpos = None
			blindscan_here = (self.blindscan_transponders is not None and current_orbpos == self._blindscan_orbpos)
			if blindscan_here and self.tuning_type.value != "blindscan_transponder":
				self.tuning_type.value = "blindscan_transponder"
			elif not blindscan_here and self.tuning_type.value == "blindscan_transponder":
				self.tuning_type.value = "predefined_transponder"
			self.typeOfTuningEntry = (_('Tune'), self.tuning_type)
			has_tps = (len(nimmanager.getTransponders(int(self.tuning_sat.value), self.feid)) > 0
					   or blindscan_here)
			if not has_tps:
				self.tuning_type.value = "single_transponder"
			else:
				self.list.append(self.typeOfTuningEntry)

			nim = nimmanager.nim_slots[self.feid]

			if self.tuning_type.value == "single_transponder":
				if nim.canBeCompatible("DVB-S2"):
					self.systemEntry = (_('System'), self.scan_sat.system)
					self.list.append(self.systemEntry)
				else:
					# downgrade to dvb-s, in case a -s2 config was active
					self.scan_sat.system.value = eDVBFrontendParametersSatellite.System_DVB_S
				self.list.append((_('Frequency'), self.scan_sat.frequency))
				self.list.append((_('Polarization'), self.scan_sat.polarization))
				self.list.append((_('Symbol rate'), self.scan_sat.symbolrate))
				self.list.append((_('Inversion'), self.scan_sat.inversion))
				if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S:
					self.list.append((_("FEC"), self.scan_sat.fec))
				elif self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S2:
					self.list.append((_("FEC"), self.scan_sat.fec_s2))
					self.modulationEntry = (_('Modulation'), self.scan_sat.modulation)
					self.list.append(self.modulationEntry)
					self.list.append((_('Roll-off'), self.scan_sat.rolloff))
					self.list.append((_('Pilot'), self.scan_sat.pilot))
					if nim.isMultistream():
						self.is_id_boolEntry = (_('Transport Stream Type'), self.scan_sat.is_id_bool)
						self.list.append(self.is_id_boolEntry)
						if self.scan_sat.is_id_bool.value:
							self.list.append(("%s%s" % (indent, _('Input Stream ID')), self.scan_sat.is_id))
							self.list.append(("%s%s" % (indent, _('PLS Mode')), self.scan_sat.pls_mode))
							self.list.append(("%s%s" % (indent, _('PLS Code')), self.scan_sat.pls_code))
					else:
						self.scan_sat.is_id.value = eDVBFrontendParametersSatellite.No_Stream_Id_Filter
						self.scan_sat.pls_mode.value = eDVBFrontendParametersSatellite.PLS_Gold
						self.scan_sat.pls_code.value = eDVBFrontendParametersSatellite.PLS_Default_Gold_Code
					if nim.isT2MI():
						self.t2mi_plp_id_boolEntry = (_('T2MI PLP'), self.scan_sat.t2mi_plp_id_bool)
						self.list.append(self.t2mi_plp_id_boolEntry)
						if self.scan_sat.t2mi_plp_id_bool.value:
							self.list.append(("%s%s" % (indent, _('T2MI PLP ID')), self.scan_sat.t2mi_plp_id))
							self.list.append(("%s%s" % (indent, _('T2MI PID')), self.scan_sat.t2mi_pid))
					else:
						self.scan_sat.t2mi_plp_id.value = eDVBFrontendParametersSatellite.No_T2MI_PLP_Id
						self.scan_sat.t2mi_pid.value = eDVBFrontendParametersSatellite.T2MI_Default_Pid
			elif self.tuning_type.value in ("predefined_transponder", "blindscan_transponder"):
				self.scan_nims.value = self.satfinder_scan_nims.value
				self.updatePreDefTransponders()
				self.preDefTransponderEntry = (_("Transponder"), self.preDefTransponders)
				self.list.append(self.preDefTransponderEntry)
		elif self.DVB_type.value == "DVB-C":
			if self.tuning_type.value == "blindscan_transponder":
				self.tuning_type.value = "predefined_transponder"
			self.typeOfTuningEntry = (_('Tune'), self.tuning_type)
			if config.Nims[self.feid].cable.scan_type.value != "provider" or len(nimmanager.getTranspondersCable(int(self.satfinder_scan_nims.value))) < 1: # only show 'predefined transponder' if in provider mode and transponders exist
				self.tuning_type.value = "single_transponder"
			else:
				self.list.append(self.typeOfTuningEntry)
			if self.tuning_type.value == "single_transponder":
				self.list.append((_("Frequency"), self.scan_cab.frequency))
				self.list.append((_("Inversion"), self.scan_cab.inversion))
				self.list.append((_("Symbol rate"), self.scan_cab.symbolrate))
				self.list.append((_("Modulation"), self.scan_cab.modulation))
				self.list.append((_("FEC"), self.scan_cab.fec))
			elif self.tuning_type.value == "predefined_transponder":
				self.scan_nims.value = self.satfinder_scan_nims.value
				self.predefinedCabTranspondersList()
				self.preDefTransponderCableEntry = (_("Transponder"), self.CableTransponders)
				self.list.append(self.preDefTransponderCableEntry)
		elif self.DVB_type.value == "DVB-T":
			if self.tuning_type.value == "blindscan_transponder":
				self.tuning_type.value = "predefined_transponder"
			self.typeOfTuningEntry = (_('Tune'), self.tuning_type)
			region = nimmanager.getTerrestrialDescription(int(self.satfinder_scan_nims.value))
			if len(nimmanager.getTranspondersTerrestrial(region)) < 1: # Only offer 'predefined transponder' if some transponders exist
				self.tuning_type.value = "single_transponder"
			else:
				self.list.append(self.typeOfTuningEntry)
			if self.tuning_type.value == "single_transponder":
				if nimmanager.nim_slots[int(self.satfinder_scan_nims.value)].canBeCompatible("DVB-T2"):
					self.systemEntryTerr = (_('System'), self.scan_ter.system)
					self.list.append(self.systemEntryTerr)
				else:
					self.scan_ter.system.value = eDVBFrontendParametersTerrestrial.System_DVB_T
				self.typeOfInputEntry = (_("Use frequency or channel"), self.scan_input_as)
				if self.ter_channel_input:
					self.list.append(self.typeOfInputEntry)
				else:
					self.scan_input_as.value = self.scan_input_as.choices[0]
				if self.ter_channel_input and self.scan_input_as.value == "channel":
					channel = getChannelNumber(self.scan_ter.frequency.floatint * 1000, self.ter_tnumber)
					if channel:
						self.scan_ter.channel.value = int(channel.replace("+", "").replace("-", ""))
					self.list.append((_("Channel"), self.scan_ter.channel))
				else:
					prev_val = self.scan_ter.frequency.floatint
					self.scan_ter.frequency.floatint = channel2frequency(self.scan_ter.channel.value, self.ter_tnumber) / 1000
					if self.scan_ter.frequency.floatint == 474000:
						self.scan_ter.frequency.floatint = prev_val
					self.list.append((_("Frequency"), self.scan_ter.frequency))
				self.list.append((_("Inversion"), self.scan_ter.inversion))
				self.list.append((_("Bandwidth"), self.scan_ter.bandwidth))
				self.list.append((_("Code rate HP"), self.scan_ter.fechigh))
				self.list.append((_("Code rate LP"), self.scan_ter.feclow))
				self.list.append((_("Modulation"), self.scan_ter.modulation))
				self.list.append((_("Transmission mode"), self.scan_ter.transmission))
				self.list.append((_("Guard interval"), self.scan_ter.guard))
				self.list.append((_("Hierarchy info"), self.scan_ter.hierarchy))
				if self.scan_ter.system.value == eDVBFrontendParametersTerrestrial.System_DVB_T2:
					self.list.append((_('PLP ID'), self.scan_ter.plp_id))
			elif self.tuning_type.value == "predefined_transponder":
				self.scan_nims.value = self.satfinder_scan_nims.value
				self.predefinedTerrTranspondersList()
				self.preDefTransponderTerrEntry = (_('Transponder'), self.TerrestrialTransponders)
				self.list.append(self.preDefTransponderTerrEntry)
		elif self.DVB_type.value == "ATSC":
			if self.tuning_type.value == "blindscan_transponder":
				self.tuning_type.value = "predefined_transponder"
			self.typeOfTuningEntry = (_('Tune'), self.tuning_type)
			if len(nimmanager.getTranspondersATSC(int(self.satfinder_scan_nims.value))) < 1: # only show 'predefined transponder' if transponders exist
				self.tuning_type.value = "single_transponder"
			else:
				self.list.append(self.typeOfTuningEntry)
			if self.tuning_type.value == "single_transponder":
				self.systemEntryATSC = (_("System"), self.scan_ats.system)
				self.list.append(self.systemEntryATSC)
				self.list.append((_("Frequency"), self.scan_ats.frequency))
				self.list.append((_("Inversion"), self.scan_ats.inversion))
				self.list.append((_("Modulation"), self.scan_ats.modulation))
			elif self.tuning_type.value == "predefined_transponder":
				#FIXME add region
				self.scan_nims.value = self.satfinder_scan_nims.value
				self.predefinedATSCTranspondersList()
				self.preDefTransponderAtscEntry = (_('Transponder'), self.ATSCTransponders)
				self.list.append(self.preDefTransponderAtscEntry)
		self["config"].list = self.list
		self["config"].l.setList(self.list)

	def createConfig(self, foo):
		self.tuning_type = ConfigSelection(default="predefined_transponder", choices=[
			("single_transponder",   _("User defined transponder")),
			("predefined_transponder", _("Predefined transponder")),
			("blindscan_transponder",  _("Blindscan transponder")),
		])
		self.orbital_position = 192
		if self.frontendData and 'orbital_position' in self.frontendData:
			self.orbital_position = self.frontendData['orbital_position']
		ScanSetup.createConfig(self, self.frontendData)

		# The following are updated in self.newConfig(). Do not add here.
		# self.scan_sat.system, self.tuning_type, self.scan_input_as, self.scan_ats.system, self.DVB_type, self.scan_ter.system, self.satfinder_scan_nims, self.tuning_sat
		for x in (self.scan_sat.frequency,
			self.scan_sat.inversion, self.scan_sat.symbolrate,
			self.scan_sat.polarization, self.scan_sat.fec, self.scan_sat.pilot,
			self.scan_sat.fec_s2, self.scan_sat.fec, self.scan_sat.modulation,
			self.scan_sat.rolloff,
			self.scan_sat.is_id, self.scan_sat.pls_mode, self.scan_sat.pls_code,
			self.scan_sat.t2mi_plp_id, self.scan_sat.t2mi_pid,
			self.scan_ter.channel, self.scan_ter.frequency, self.scan_ter.inversion,
			self.scan_ter.bandwidth, self.scan_ter.fechigh, self.scan_ter.feclow,
			self.scan_ter.modulation, self.scan_ter.transmission,
			self.scan_ter.guard, self.scan_ter.hierarchy, self.scan_ter.plp_id,
			self.scan_cab.frequency, self.scan_cab.inversion, self.scan_cab.symbolrate,
			self.scan_cab.modulation, self.scan_cab.fec,
			self.scan_ats.frequency, self.scan_ats.modulation, self.scan_ats.inversion):
			x.addNotifier(self.retune, initial_call=False)

		satfinder_nim_list = []
		for n in nimmanager.nim_slots:
			if not any([n.isCompatible(x) for x in ("DVB-S", "DVB-T", "DVB-C", "ATSC")]):
				continue
			if n.config_mode in ("loopthrough", "satposdepends", "nothing"):
				continue
			if n.isCompatible("DVB-S") and n.config_mode in ("simple", "equal", "advanced") and len(nimmanager.getSatListForNim(n.slot)) < 1:
				continue
			satfinder_nim_list.append((str(n.slot), n.friendly_full_description))
		self.satfinder_scan_nims = ConfigSelection(choices=satfinder_nim_list)
		if self.frontendData is not None and len(satfinder_nim_list) > 0: # open the plugin with the currently active NIM as default
			self.satfinder_scan_nims.setValue(str(self.frontendData.get("tuner_number", satfinder_nim_list[0][0])))

		self.feid = int(self.satfinder_scan_nims.value)

		self.satList = []
		self.scan_satselection = []
		for slot in nimmanager.nim_slots:
			if slot.isCompatible("DVB-S"):
				self.satList.append(nimmanager.getSatListForNim(slot.slot))
				self.scan_satselection.append(getConfigSatlist(self.orbital_position, self.satList[slot.slot]))
			else:
				self.satList.append(None)

		if self.frontendData:
			ttype = self.frontendData.get("tuner_type", "UNKNOWN")
			if ttype == "DVB-S" and self.predefinedTranspondersList(self.getSelectedSatIndex(self.feid)) is None and len(nimmanager.getTransponders(self.getSelectedSatIndex(self.feid), self.feid)) > 0:
				self.tuning_type.value = "single_transponder"
			elif ttype == "DVB-T" and self.predefinedTerrTranspondersList() is None and len(nimmanager.getTranspondersTerrestrial(nimmanager.getTerrestrialDescription(self.feid))) > 0:
				self.tuning_type.value = "single_transponder"
			elif ttype == "DVB-C" and self.predefinedCabTranspondersList() is None and len(nimmanager.getTranspondersCable(self.feid)) > 0:
				self.tuning_type.value = "single_transponder"
			elif ttype == "ATSC" and self.predefinedATSCTranspondersList() is None and len(nimmanager.getTranspondersATSC(self.feid)) > 0:
				self.tuning_type.value = "single_transponder"

	def getSelectedSatIndex(self, v):
		index = 0
		none_cnt = 0
		for n in self.satList:
			if self.satList[index] is None:
				none_cnt += 1
			if index == int(v):
				return index - none_cnt
			index += 1
		return -1

	def updatePreDefTransponders(self):
		if self.tuning_type.value == "blindscan_transponder":
			self._buildPreDefFromBlindscan()
		else:
			ScanSetup.predefinedTranspondersList(self, self.tuning_sat.orbital_position)

	def _hasBlindscanFiles(self):
		"""Cheap presence check (no XML parsing): any blindscan_*.xml in /tmp."""
		try:
			return any(f.startswith("blindscan_") and f.endswith(".xml")
					   for f in os.listdir("/tmp"))
		except OSError:
			return False

	def _updateBlueButton(self):
		"""Show the blue key only when there's something to load or clear.
		Empty text -> the skin's ConditionalShowHide hides the key entirely."""
		if self.blindscan_transponders is not None:
			self["key_blue"].setText(_("Clear Blindscan"))
		elif self._hasBlindscanFiles():
			self["key_blue"].setText(_("Load Blindscan"))
		else:
			self["key_blue"].setText("")

	def keyBlue(self):
		if self.blindscan_transponders is not None:
			# Clear blindscan — revert to satellites.xml transponders
			self.blindscan_transponders = None
			self._blindscan_orbpos = None
			self._updateBlueButton()
			self.tuning_type.value = "predefined_transponder"
			self.createSetup()
			self.retune()
			return

		# Only show blindscan files that match the current satellite
		try:
			orbpos = int(self.tuning_sat.value)
		except (ValueError, TypeError):
			self.session.open(MessageBox, _("No satellite selected."), MessageBox.TYPE_INFO)
			return

		matching = self._findAllBlindscanForOrbpos(orbpos)
		if not matching:
			inventory = self._getBlindscanInventory()
			if not inventory:
				self.session.open(MessageBox,
					_("No blindscan files found in /tmp.\nRun a blindscan first."),
					MessageBox.TYPE_INFO)
			else:
				self.session.open(ServicesFound,
					inventory,
					_("No blindscan for current satellite.\nAvailable in /tmp:"),
					show_scan=False)
			return

		if len(matching) == 1:
			self._loadBlindscanFile(matching[0])
		else:
			choices = [(os.path.basename(f), f) for f in matching]
			self.session.openWithCallback(self._blindscanFileChosen, ChoiceBox,
				title=_("Select blindscan file:"), list=choices)

	def keyRight(self):
		# Mirror left-arrow behaviour: toggle between user-defined and predefined
		# transponders when the cursor is on the Tune/type row.  For all other
		# rows fall back to the normal right-arrow (cycle forward) action.
		if self.getCurrentItem() is self.tuning_type:
			self.keyLeft()
		else:
			self["config"].handleKey(ACTIONKEY_RIGHT, self.entryChanged)

	def keyOK(self):
		"""OK on the Satellite or Transponder rows opens a deferred-tune picker.

		Left/right on these rows retunes on every step (newConfig fires per
		keypress), so on a motorised dish the rotor starts driving toward
		every satellite the cursor merely passes on the way to the one the
		user actually wants. The ChoiceBox lets the whole list be browsed
		with zero tuner/rotor side effects; the single tune (and rotor move)
		happens only when a choice is confirmed. On every other row OK keeps
		its original meaning and starts the scan, same as the green key.
		"""
		cur = self["config"].getCurrent()
		if cur is not None and cur in (
					getattr(self, "satEntry", None),
					getattr(self, "preDefTransponderEntry", None),
					getattr(self, "preDefTransponderCableEntry", None),
					getattr(self, "preDefTransponderTerrEntry", None),
					getattr(self, "preDefTransponderAtscEntry", None),
				):
			self._openDeferredPicker(cur)
		else:
			self.keyGoScan()

	def _openDeferredPicker(self, entry):
		"""Open a ChoiceBox over a ConfigSelection's choices without touching
		the config element (and therefore without tuning) until confirmed."""
		label, cfg = entry[0], entry[1]
		choices = cfg.choices.choices
		if isinstance(choices, dict):
			pairs = list(choices.items())
		else:
			# ConfigSelection/ConfigSatlist choices: (value, description)
			# tuples, or bare strings acting as both.
			pairs = [c if isinstance(c, tuple) else (c, c) for c in choices]
		menu = [(str(desc), value) for value, desc in pairs]
		if not menu:
			return
		try:
			selection = cfg.index  # preselect the current value
		except (ValueError, AttributeError):
			selection = 0
		self._picker_config = cfg
		self.session.openWithCallback(self._deferredPickerChosen, ChoiceBox,
			title=_("Select %s — tuning starts after OK") % label.strip(),
			list=menu, selection=selection)

	def _deferredPickerChosen(self, answer):
		cfg = getattr(self, "_picker_config", None)
		self._picker_config = None
		if answer is None or cfg is None:
			return  # cancelled — nothing was tuned, dish never moved
		value = answer[1]
		if cfg.value == value:
			return  # same choice reconfirmed — don't kick off a redundant retune
		# tuning_sat and the predefined-transponder ConfigSelections have no
		# retune notifiers (see createConfig); newConfig() owns their retune.
		# The cursor is still on the picked row, so newConfig() takes the
		# right branch: satellite -> blindscan sync + createSetup + retune,
		# transponder -> retune only. Exactly one tune / rotor command.
		cfg.value = value
		self["config"].invalidateCurrent()
		self.newConfig()

	def _blindscanFileChosen(self, answer):
		if answer is None:
			return
		self._loadBlindscanFile(answer[1])

	def _loadBlindscanFile(self, filepath, show_message=True):
		try:
			tree = ET.parse(filepath)
			root = tree.getroot()
		except Exception as e:
			self.session.open(MessageBox,
				_("Failed to parse blindscan file:\n%s") % str(e),
				MessageBox.TYPE_ERROR)
			return

		# Try to match the <sat> element by orbital position
		try:
			current_orbpos = int(self.tuning_sat.value)
		except (ValueError, TypeError):
			current_orbpos = None
		sat_elem = None
		for sat in root.findall("sat"):
			try:
				if current_orbpos is None or int(sat.get("position", "")) == current_orbpos:
					sat_elem = sat
					break
			except ValueError:
				continue
		if sat_elem is None:
			all_sats = root.findall("sat")
			if all_sats:
				sat_elem = all_sats[0]  # fallback: first satellite in file

		if sat_elem is None:
			self.session.open(MessageBox,
				_("No transponders found in blindscan file."),
				MessageBox.TYPE_INFO)
			return

		tps = []
		for tp_elem in sat_elem.findall("transponder"):
			try:
				tps.append((
					0,                                                                  # [0] DVB-S type
					int(tp_elem.get("frequency", "0")),                                 # [1] frequency kHz
					int(tp_elem.get("symbol_rate", "0")),                               # [2] symbol_rate sps
					int(tp_elem.get("polarization", "0")),                              # [3] polarization
					int(tp_elem.get("fec_inner", "0")),                                 # [4] fec
					int(tp_elem.get("system", "0")),                                    # [5] system
					int(tp_elem.get("modulation", "1")),                                # [6] modulation
					0,                                                                  # [7] orbital_position (unused)
					eDVBFrontendParametersSatellite.RollOff_auto,                       # [8] rolloff
					eDVBFrontendParametersSatellite.Pilot_Unknown,                      # [9] pilot
					eDVBFrontendParametersSatellite.No_Stream_Id_Filter,                # [10] is_id
					eDVBFrontendParametersSatellite.PLS_Gold,                           # [11] pls_mode
					eDVBFrontendParametersSatellite.PLS_Default_Gold_Code,              # [12] pls_code
					eDVBFrontendParametersSatellite.No_T2MI_PLP_Id,                     # [13] t2mi_plp_id
					eDVBFrontendParametersSatellite.T2MI_Default_Pid,                   # [14] t2mi_pid
				))
			except (ValueError, TypeError):
				continue

		if not tps:
			self.session.open(MessageBox,
				_("No transponders found in blindscan file."),
				MessageBox.TYPE_INFO)
			return

		self.blindscan_transponders = tps
		try:
			self._blindscan_orbpos = int(self.tuning_sat.value)
		except (ValueError, TypeError):
			self._blindscan_orbpos = None
		self._updateBlueButton()
		self.tuning_type.value = "blindscan_transponder"

		if show_message:
			# Caller is keyBlue — we own the full UI update
			self.createSetup()
			self.retune()
			sat_name = sat_elem.get("name", os.path.basename(filepath))
			self.session.open(MessageBox,
				_("Loaded %d transponders\n%s") % (len(tps), sat_name),
				MessageBox.TYPE_INFO, timeout=3)
		# When show_message=False the caller (_updateBlindscanForSat via newConfig)
		# will call createSetup() and retune() immediately after returning.

	def _buildPreDefFromBlindscan(self):
		choices = []
		for i, tp in enumerate(self.blindscan_transponders):
			choices.append((str(i), self.humanReadableTransponder(tp)))
		if not choices:
			choices = [("0", _("No transponders"))]
		self.preDefTransponders = ConfigSelection(choices=choices, default="0")

	def _findAllBlindscanForOrbpos(self, orbpos):
		"""Return list of /tmp/blindscan_*.xml paths (newest first) that contain a <sat> for orbpos."""
		blindscan_dir = "/tmp"
		try:
			files = sorted(
				[f for f in os.listdir(blindscan_dir) if f.startswith("blindscan_") and f.endswith(".xml")],
				reverse=True
			)
		except OSError:
			return []
		matches = []
		for filename in files:
			filepath = os.path.join(blindscan_dir, filename)
			try:
				tree = ET.parse(filepath)
				for sat in tree.getroot().findall("sat"):
					if int(sat.get("position", "x")) == orbpos:
						matches.append(filepath)
						break
			except Exception:
				continue
		return matches

	def _findBlindscanForOrbpos(self, orbpos):
		"""Return the newest /tmp/blindscan_*.xml path containing a <sat> for orbpos, or None."""
		result = self._findAllBlindscanForOrbpos(orbpos)
		return result[0] if result else None

	def _getBlindscanInventory(self):
		"""Return a formatted string listing all blindscan files in /tmp with satellite details."""
		blindscan_dir = "/tmp"
		try:
			files = sorted(
				[f for f in os.listdir(blindscan_dir) if f.startswith("blindscan_") and f.endswith(".xml")],
				reverse=True
			)
		except OSError:
			return ""
		if not files:
			return ""
		lines = []
		for filename in files:
			# Extract date from filename: blindscan_97W_DD-MM-YYYY_HH-MM-SS.xml
			parts = filename.replace(".xml", "").split("_")
			date_str = parts[2] if len(parts) > 2 else ""
			filepath = os.path.join(blindscan_dir, filename)
			try:
				tree = ET.parse(filepath)
				for sat in tree.getroot().findall("sat"):
					name = sat.get("name", "Unknown")
					count = len(sat.findall("transponder"))
					lines.append("%s  [%d TP]  %s" % (name, count, date_str))
			except Exception:
				lines.append("%s  (unreadable)" % filename)
		return "\n".join(lines)

	def _updateBlindscanForSat(self):
		"""Called when satellite changes while blindscan mode is active.
		Auto-loads the matching blindscan file, or reverts to satellites.xml."""
		try:
			orbpos = int(self.tuning_sat.value)
		except (ValueError, TypeError):
			return
		match = self._findBlindscanForOrbpos(orbpos)
		if match:
			self._loadBlindscanFile(match, show_message=False)
		else:
			# No blindscan for this satellite — fall back to satellites.xml
			self.blindscan_transponders = None
			self._blindscan_orbpos = None
			self._updateBlueButton()

	def retuneCab(self):
		if not self.initcomplete:
			return
		if self.tuning_type.value == "single_transponder":
			transponder = (
				self.scan_cab.frequency.floatint,
				self.scan_cab.symbolrate.value * 1000,
				self.scan_cab.modulation.value,
				self.scan_cab.fec.value,
				self.scan_cab.inversion.value
			)
			self.tuner.tuneCab(transponder)
			self.transponder = transponder
		elif self.tuning_type.value == "predefined_transponder":
			tps = nimmanager.getTranspondersCable(int(self.satfinder_scan_nims.value))
			if len(tps) > self.CableTransponders.index:
				tp = tps[self.CableTransponders.index]
				# tp = 0 transponder type, 1 freq, 2 sym, 3 mod, 4 fec, 5 inv, 6 sys
				transponder = (tp[1], tp[2], tp[3], tp[4], tp[5])
				self.tuner.tuneCab(transponder)
				self.transponder = transponder

	def retuneTerr(self):
		if not self.initcomplete:
			return
		if self.scan_input_as.value == "channel":
			frequency = channel2frequency(self.scan_ter.channel.value, self.ter_tnumber)
		else:
			frequency = self.scan_ter.frequency.floatint * 1000
		if self.tuning_type.value == "single_transponder":
			transponder = [
				2, #TERRESTRIAL
				frequency,
				self.scan_ter.bandwidth.value,
				self.scan_ter.modulation.value,
				self.scan_ter.fechigh.value,
				self.scan_ter.feclow.value,
				self.scan_ter.guard.value,
				self.scan_ter.transmission.value,
				self.scan_ter.hierarchy.value,
				self.scan_ter.inversion.value,
				self.scan_ter.system.value,
				self.scan_ter.plp_id.value]
			self.tuner.tuneTerr(transponder[1], transponder[9], transponder[2], transponder[4], transponder[5], transponder[3], transponder[7], transponder[6], transponder[8], transponder[10], transponder[11])
			self.transponder = transponder
		elif self.tuning_type.value == "predefined_transponder":
			region = nimmanager.getTerrestrialDescription(int(self.satfinder_scan_nims.value))
			tps = nimmanager.getTranspondersTerrestrial(region)
			if len(tps) > self.TerrestrialTransponders.index:
				transponder = tps[self.TerrestrialTransponders.index]
				# frequency 1, inversion 9, bandwidth 2, fechigh 4, feclow 5, modulation 3, transmission 7, guard 6, hierarchy 8, system 10, plp_id 11
				self.tuner.tuneTerr(transponder[1], transponder[9], transponder[2], transponder[4], transponder[5], transponder[3], transponder[7], transponder[6], transponder[8], transponder[10], transponder[11])
				self.transponder = transponder

	def retuneATSC(self):
		if not self.initcomplete:
			return
		if self.tuning_type.value == "single_transponder":
			transponder = (
				self.scan_ats.frequency.floatint * 1000,
				self.scan_ats.modulation.value,
				self.scan_ats.inversion.value,
				self.scan_ats.system.value,
			)
			self.tuner.tuneATSC(transponder)
			self.transponder = transponder
		elif self.tuning_type.value == "predefined_transponder":
			tps = nimmanager.getTranspondersATSC(int(self.satfinder_scan_nims.value))
			if tps and len(tps) > self.ATSCTransponders.index:
				tp = tps[self.ATSCTransponders.index]
				transponder = (tp[1], tp[2], tp[3], tp[4])
				self.tuner.tuneATSC(transponder)
				self.transponder = transponder

	def retuneSat(self): #satellite
		if not self.tuning_sat.value:
			return
		satpos = int(self.tuning_sat.value)
		if self.tuning_type.value == "single_transponder":
			if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S2:
				fec = self.scan_sat.fec_s2.value
			else:
				fec = self.scan_sat.fec.value
			transponder = (
				self.scan_sat.frequency.floatint / 1000.0,
				self.scan_sat.symbolrate.value,
				self.scan_sat.polarization.value,
				fec,
				self.scan_sat.inversion.value,
				satpos,
				self.scan_sat.system.value,
				self.scan_sat.modulation.value,
				self.scan_sat.rolloff.value,
				self.scan_sat.pilot.value,
				self.scan_sat.is_id.value,
				self.scan_sat.pls_mode.value,
				self.scan_sat.pls_code.value,
				self.scan_sat.t2mi_plp_id.value,
				self.scan_sat.t2mi_pid.value)
			if self.initcomplete:
				self.tuner.tune(transponder)
			self.transponder = transponder
		elif self.tuning_type.value in ("predefined_transponder", "blindscan_transponder"):
			tps = (self.blindscan_transponders if self.tuning_type.value == "blindscan_transponder"
				   else nimmanager.getTransponders(satpos, int(self.satfinder_scan_nims.value)))
			if len(tps) > self.preDefTransponders.index:
				tp = tps[self.preDefTransponders.index]
				transponder = (tp[1] / 1000.0, tp[2] // 1000,
					tp[3], tp[4], 2, satpos, tp[5], tp[6], tp[8], tp[9], tp[10], tp[11], tp[12], tp[13], tp[14])
				if self.initcomplete:
					self.tuner.tune(transponder)
				self.transponder = transponder

	def retune(self, configElement=None):
		if not hasattr(self, 'DVB_type'):
			print("Warning: DVB_type not properly initialized")
			return

		try:
			if self.DVB_type.value == "DVB-S":
				self.retuneSat()
			elif self.DVB_type.value == "DVB-T":
				self.retuneTerr()
			elif self.DVB_type.value == "DVB-C":
				self.retuneCab()
			elif self.DVB_type.value == "ATSC":
				self.retuneATSC()
			else:
				print(f"Unknown DVB type: {self.DVB_type.value}")
				return

			# Clear peaks/history only if the tuning actually CHANGED. This has
			# to run after the retune*() call, once self.transponder has been
			# rebuilt. See _checkInstrumentReset for why it is not simply done
			# on every retune.
			self._checkInstrumentReset()
			self.timer.start(500, True)
		except Exception as e:
			print(f"Error during retune: {e}")
			self.timer.start(3000, True)  # Retry after longer delay on error

	def keyGoScan(self):
		if not hasattr(self, 'transponder') or not self.transponder:
			self.showError(_("No transponder configured"))
			return
			
		self.frontend = None
		if hasattr(self, 'raw_channel') and self.raw_channel:
			del self.raw_channel
			self.raw_channel = None
			
		tlist = []
		
		try:
			# Build the transponder list based on DVB type
			if self.DVB_type.value == "DVB-S":
				self.addSatTransponder(tlist,
					self.transponder[0], # frequency
					self.transponder[1], # sr
					self.transponder[2], # pol
					self.transponder[3], # fec
					self.transponder[4], # inversion
					self.tuning_sat.orbital_position,
					self.transponder[6], # system
					self.transponder[7], # modulation
					self.transponder[8], # rolloff
					self.transponder[9], # pilot
					self.transponder[10],# input stream id
					self.transponder[11],# pls mode
					self.transponder[12],# pls code
					self.transponder[13],# t2mi_plp_id
					self.transponder[14] # t2mi_pid
				)
			elif self.DVB_type.value == "DVB-T":
				parm = buildTerTransponder(
					self.transponder[1],  # frequency
					self.transponder[9],  # inversion
					self.transponder[2],  # bandwidth
					self.transponder[4],  # fechigh
					self.transponder[5],  # feclow
					self.transponder[3],  # modulation
					self.transponder[7],  # transmission
					self.transponder[6],  # guard
					self.transponder[8],  # hierarchy
					self.transponder[10], # system
					self.transponder[11]  # plp_id
				)
				tlist.append(parm)
			elif self.DVB_type.value == "DVB-C":
				self.addCabTransponder(tlist,
					self.transponder[0], # frequency
					self.transponder[1], # sr
					self.transponder[2], # modulation
					self.transponder[3], # fec_inner
					self.transponder[4]  # inversion
				)
			elif self.DVB_type.value == "ATSC":
				self.addATSCTransponder(tlist,
					self.transponder[0], # frequency
					self.transponder[1], # modulation
					self.transponder[2], # inversion
					self.transponder[3]  # system
				)
				
			# Start the scan after the transponder list is completely built
			self.startScan(tlist, self.feid)
			
		except Exception as e:
			print(f"Error during scan initiation: {e}")
			self.showError(_("Failed to start scan"))

	def startScan(self, tlist, feid):
		flags = 0
		networkid = 0
		self.session.openWithCallback(self.startScanCallback, ServiceScan, [{"transponders": tlist, "feid": feid, "flags": flags, "networkid": networkid}])

	def startScanCallback(self, answer=None):
		if answer:
			self.doCloseRecursive()

	def keyCancel(self):
		try:
			if hasattr(self, 'timer') and self.timer:
				self.timer.stop()
			if self.session.postScanService and self.frontend:
				self.frontend = None
				if hasattr(self, 'raw_channel') and self.raw_channel:
					del self.raw_channel
					self.raw_channel = None
			self.close(False)
		except Exception as e:
			print(f"Error during cancel: {e}")
			self.close(False)

	def doCloseRecursive(self):
		try:
			if hasattr(self, 'timer') and self.timer:
				self.timer.stop()
			if self.session.postScanService and self.frontend:
				self.frontend = None
				if hasattr(self, 'raw_channel') and self.raw_channel:
					del self.raw_channel
					self.raw_channel = None
			self.close(True)
		except Exception as e:
			print(f"Error during close: {e}")
			self.close(True)

class SatfinderExtra(Satfinder):
	# This class requires AutoBouquetsMaker to be installed.
	def __init__(self, session):
		# Keep existing init code
		Satfinder.__init__(self, session)
		# Override with the variant that adds the ONID/TSID/POS network-info row.
		# Unique skinName -> no installed skin matches -> our embedded self.skin wins.
		self.skin = SATFINDER_SKIN_EXTRA
		self.skinName = ["TNAP_Satfinder"]

		# Add thread control
		global THREAD_RUNNING
		THREAD_RUNNING = True
		self.threadLock = threading.Lock()
		self.threadEvents = {}
		self.threadpool = []
		# Shared closed-flag. A LIST, not a bool, so a worker thread can be
		# handed the object itself and keep reading it after this screen's
		# __dict__ has been cleared out from under it -- see _runGuarded.
		self._closed = [False]

		self["key_yellow"] = StaticText("")

		self["actions2"] = ActionMap(["ColorActions"],
		{
			"yellow": self.keyReadServices,
		}, -3)
		self["actions2"].setEnabled(False)

		# DVB stream info
		self.serviceList = []
		self["services"] = StaticText("")
		self["tsid"] = StaticText("")
		self["onid"] = StaticText("")
		self["pos"] = StaticText("")
		self["network"] = StaticText("")  # NIT network_name, header sub-line

		# Register our close handler — must be done explicitly because Python
		# name-mangling (__onClose → _SatfinderExtra__onClose) means the parent's
		# self.onClose.append(self.__onClose) only registered _Satfinder__onClose.
		# insert(0), not append: handlers run in list order and the parent's is
		# already registered, so appending would have released the frontend
		# BEFORE the reader threads were told to stop -- the opposite of what
		# _extraOnClose is for.
		self.onClose.insert(0, self._extraOnClose)
	def start_thread(self, target, args=(), name=None):
		"""Safely start and track a new thread"""
		if name not in self.threadEvents:
			self.threadEvents[name] = threading.Event()
		else:
			self.threadEvents[name].clear()  # reset event for reuse

		# Prune finished threads before adding a new one
		self.threadpool = [t for t in self.threadpool if t.is_alive()]

		# Enter through _runGuarded, and hand it the closed-flag object rather
		# than a route back through self -- see _runGuarded for why.
		thread = threading.Thread(target=self._runGuarded, args=(target, args, name, self._closed))
		thread.daemon = True  # Set thread as daemon so it exits when main thread exits
		thread.name = name if name else f"Thread-{len(self.threadpool)}"
		self.threadpool.append(thread)
		thread.start()
		return thread

	def _runGuarded(self, target, args, name, closed):
		"""Thread entry point that survives the screen closing underneath it.

		Screen.doClose() calls self.__dict__.clear() to break reference cycles,
		so every attribute on this object disappears the instant the screen is
		gone. stop_all_threads() only join()s for one second, and a reader can
		be parked far longer than that inside dvbreader.read_sdt/read_nit or an
		os.read() on a demux fd. It then wakes up, correctly leaves its loop on
		the THREAD_RUNNING check -- that one is a module global, so it is still
		readable -- and dies on the first self.<anything> that follows the loop.
		That is the AttributeError on self.threadLock in getOrbPosFromNit: the
		"No NIT data" write sits immediately after the read loop.

		Rather than sprinkle guards over every late UI write in four different
		readers, catch it once here. The closed flag arrives as an argument, so
		this frame holds its own reference and can still be read after the
		clear; a genuine bug raised while the screen is alive still gets its
		full traceback.
		"""
		try:
			target(*args)
		except Exception as e:
			if closed[0] or not THREAD_RUNNING:
				print("[Satfinder][%s] abandoned after screen close: %s: %s" % (
					name or "thread", e.__class__.__name__, e))
			else:
				print("[Satfinder][%s] thread failed:" % (name or "thread"))
				traceback.print_exc()

	def stop_all_threads(self):
		"""Signal all threads to stop and wait for them"""
		global THREAD_RUNNING
		THREAD_RUNNING = False
		
		# Set all thread events
		for event in self.threadEvents.values():
			event.set()
		
		# Wait for all threads to finish (with timeout)
		for thread in self.threadpool:
			if thread.is_alive():
				thread.join(1.0)  # Wait up to 1 second for each thread

	def should_continue(self, name=None):
		"""Check if thread should continue running"""
		global THREAD_RUNNING
		if not THREAD_RUNNING:
			return False
		
		if name and name in self.threadEvents:
			return not self.threadEvents[name].is_set()
		
		return True

	def _extraOnClose(self):
		"""Stop all threads before the parent close handler releases the frontend."""
		self._closed[0] = True
		self.stop_all_threads()

	def retune(self, configElement=None):
		Satfinder.retune(self)
		self.dvb_read_stream()

	def openFrontend(self):
		if Satfinder.openFrontend(self):
			self.demux = self.raw_channel.reserveDemux() # used for keyReadServices()
			return True
		return False

	def prepareFrontend(self):
		self.demux = -1 # used for keyReadServices()
		Satfinder.prepareFrontend(self)

	def dvb_read_stream(self):
		print("[satfinder][dvb_read_stream] starting")
		# Use our thread management instead of raw thread
		self.start_thread(self.getCurrentTsidOnid, (True,), "tsid_onid_reader")

	def getCurrentTsidOnid(self, from_retune=False):
		self.currentProcess = currentProcess = datetime.datetime.now()
		
		with self.threadLock:
			# Reset UI elements
			self["services"].setText("")
			self["tsid"].setText("")
			self["onid"].setText("")
			self["pos"].setText("")
			self["network"].setText("")
			self["key_yellow"].setText("")
			self["actions2"].setEnabled(False)
			self.serviceList = []

		if not dvbreader_available or self.frontend is None or self.demux < 0:
			return

		if from_retune:  # give the tuner a chance to retune
			time.sleep(1.0)
			if self.currentProcess != currentProcess:
				# A newer retune has already started; let it own the demux.
				return

		if not self.tunerLock() and not self.waitTunerLock(currentProcess):
			# Don't even try to read the transport stream if tuner is not locked
			return

		# Start tuner lock monitor in a separate thread
		self.start_thread(self.monitorTunerLock, (currentProcess,), "lock_monitor")

		# The network name only needs a locked tuner and the NIT -- not the
		# SDT -- so read it in parallel with the SDT/service pass below and
		# independently of getOrbPosFromNit (a NIT can carry a name without
		# any delivery descriptor). ATSC has no DVB NIT (PSIP uses the VCT),
		# so skip it there.
		if self.DVB_type.value.startswith("DVB"):
			self.start_thread(self.getNetworkNameFromNit, (currentProcess,), "nit_name_reader")

		adapter = 0
		demuxer_device = "/dev/dvb/adapter%d/demux%d" % (adapter, self.demux)

		sdt_pid = 0x11
		sdt_current_table_id = 0x42
		mask = 0xff
		tsidOnidTimeout = 60  # maximum time allowed to read the service descriptor table (seconds)
		self.tsid = None
		self.onid = None

		sdt_current_version_number = -1
		sdt_current_sections_read = []
		sdt_current_sections_count = 0
		sdt_current_content = []
		sdt_current_completed = False

		# SDT must repeat every <=2s (EN 300 468); 8s is generous for any compliant mux.
		dvbreader.set_timeouts(1000, 8000)
		fd = dvbreader.open(demuxer_device, sdt_pid, sdt_current_table_id, mask, self.feid)
		if fd < 0:
			dvbreader.set_timeouts(1000, 15000)
			print("[Satfinder][getCurrentTsidOnid] Cannot open the demuxer")
			return None

		timeout = datetime.datetime.now()
		timeout += datetime.timedelta(0, tsidOnidTimeout)

		try:
			while self.should_continue("tsid_onid_reader"):
				if datetime.datetime.now() > timeout:
					print("[Satfinder][getCurrentTsidOnid] Timed out")
					break

				if self.currentProcess != currentProcess or not self.tunerLock():
					break

				section = dvbreader.read_sdt(fd, sdt_current_table_id, 0x00)
				if section is None:
					time.sleep(0.1)  # no data.. so we wait a bit
					continue

				table_id = section["header"]["table_id"]

				if table_id == 0x00:
					# PAT fallback — no SDT on this transponder; accept immediately
					sdt_current_content = section["content"]
					sdt_current_completed = True

				elif table_id == sdt_current_table_id and not sdt_current_completed:
					if section["header"]["version_number"] != sdt_current_version_number:
						sdt_current_version_number = section["header"]["version_number"]
						sdt_current_sections_read = []
						sdt_current_sections_count = section["header"]["last_section_number"] + 1
						sdt_current_content = []

					if section["header"]["section_number"] not in sdt_current_sections_read:
						sdt_current_sections_read.append(section["header"]["section_number"])
						sdt_current_content += section["content"]

						# Update UI with thread safety
						if self.tsid is None or self.onid is None:
							self.tsid = section["header"]["transport_stream_id"]
							self.onid = section["header"]["original_network_id"]

							with self.threadLock:
								self["tsid"].setText("%d" % (section["header"]["transport_stream_id"]))
								self["onid"].setText("%d" % (section["header"]["original_network_id"]))

							print("[Satfinder][getCurrentTsidOnid] tsid %d, onid %d" % (
								section["header"]["transport_stream_id"],
								section["header"]["original_network_id"]
							))

						if len(sdt_current_sections_read) == sdt_current_sections_count:
							sdt_current_completed = True

				if sdt_current_completed:
					break
		finally:
			# Ensure demuxer is closed
			dvbreader.close(fd)
			dvbreader.set_timeouts(1000, 15000)

		if not sdt_current_content:
			print("[Satfinder][getCurrentTsidOnid] no services found on transponder")
			return

		# If the content came from PAT fallback, enrich each entry via PMT.
		if sdt_current_content and sdt_current_content[0].get("from_pat"):
			sdt_current_content = self._enrich_pat_from_pmt(sdt_current_content, demuxer_device)

		# Process service data
		for i in range(len(sdt_current_content)):
			if not sdt_current_content[i]["service_name"]:  # if service name is empty use SID
				sdt_current_content[i]["service_name"] = "0x%x" % sdt_current_content[i]["service_id"]

		with self.threadLock:
			self.serviceList = sorted(sdt_current_content, key=lambda listItem: listItem["service_name"])
			if self.serviceList:
				self["key_yellow"].setText(_("Service list"))
				self["actions2"].setEnabled(True)
				# Service readout: listing SIDs is pointless on a busy mux
				# ("716 +11" tells you nothing), so show a plain count of the
				# distinct services found instead. The full list -- names,
				# SIDs, types, CA status -- stays one press away on the
				# yellow key.
				self["services"].setText("%d" % len(set(s["service_id"] for s in self.serviceList)))

		# Get orbital position for satellite
		if self.tsid is not None and self.onid is not None:
			self.start_thread(self.getOrbPosFromNit, (currentProcess,), "nit_reader")

	def _enrich_pat_from_pmt(self, services, demuxer_device):
		"""Read PMT for each PAT-derived service and update service_type/free_ca.
		Services that return no PMT (ghost PAT entries) are dropped."""
		VIDEO_TYPES = {0x01, 0x02, 0x1b, 0x24}
		result = []
		dvbreader.set_timeouts(1000, 2000)
		for svc in services:
			pmt_pid = svc.get("pmt_pid")
			if pmt_pid is None:
				result.append(svc)
				continue
			fd = dvbreader.open(demuxer_device, pmt_pid, 0x02, 0xff, 0)
			if fd < 0:
				result.append(svc)
				continue
			pmt = dvbreader.read_pmt(fd)
			dvbreader.close(fd)
			if pmt is None:
				print("[Satfinder] no PMT for service_id=%d pmt_pid=0x%04x — skipping" % (
				      svc["service_id"], pmt_pid))
				continue
			info = pmt[0]
			svc["free_ca"] = 0 if info["encrypted"] == 0 else 1
			has_video = any(es["stream_type"] in VIDEO_TYPES for es in pmt[1:])
			svc["service_type"] = 1 if has_video else 2
			result.append(svc)
		dvbreader.set_timeouts(1000, 15000)
		return result

	def getNetworkNameFromNit(self, currentProcess):
		"""Read the network_name_descriptor from NIT-actual and display it.

		Runs in its own thread on its own demux section filter (see the
		module-level reader notes); a NIT that carries a name but no
		delivery descriptor -- common on occasional-use feeds -- still
		identifies the network even when POS has nothing to show.
		"""
		if not dvbreader_available or self.frontend is None or self.demux < 0:
			return

		demuxer_device = "/dev/dvb/adapter0/demux%d" % self.demux

		try:
			fd = os.open(demuxer_device, os.O_RDWR | os.O_NONBLOCK)
		except OSError as e:
			print("[Satfinder][getNetworkNameFromNit] open %s failed: %s" % (demuxer_device, e))
			return

		# NIT-actual must repeat at least every 10s (EN 300 468 sec. 5.1.4);
		# 30s tolerates a couple of missed/CRC-failed repetitions on a
		# marginal signal without holding the thread for the full 60s the
		# position reader allows itself.
		deadline = time.monotonic() + 30
		carry = b""
		fallback = None      # multilingual name, used only if 0x40 never appears
		sections_seen = set()
		version = None

		try:
			fcntl.ioctl(fd, _DMX_SET_FILTER, _nitActualFilterParams())

			poller = select.poll()
			poller.register(fd, select.POLLIN | select.POLLPRI)

			while self.should_continue("nit_name_reader"):
				if time.monotonic() > deadline:
					print("[Satfinder][getNetworkNameFromNit] timed out")
					break
				if self.currentProcess != currentProcess or not self.tunerLock():
					return

				if not poller.poll(500):
					continue
				try:
					chunk = os.read(fd, 4096)
				except OSError as e:
					if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EOVERFLOW):
						continue
					print("[Satfinder][getNetworkNameFromNit] read failed: %s" % e)
					break
				if not chunk:
					continue

				sections, carry = _splitNitSections(carry + chunk)
				for section in sections:
					name, ml_name = _nitNetworkNames(section)
					if name:
						self._setNetworkName(name, currentProcess)
						return
					if ml_name and fallback is None:
						fallback = ml_name

					# Track completeness so we can stop as soon as every
					# section of the current NIT version was inspected.
					section_version = (section[5] >> 1) & 0x1F
					if section_version != version:
						version = section_version
						sections_seen = set()
					sections_seen.add(section[6])
					if len(sections_seen) >= section[7] + 1:
						if fallback:
							self._setNetworkName(fallback, currentProcess)
						else:
							print("[Satfinder][getNetworkNameFromNit] NIT carries no network name")
						return
		finally:
			os.close(fd)

		# Timed out before seeing every section; better a multilingual name
		# than none at all.
		if fallback:
			self._setNetworkName(fallback, currentProcess)

	def _setNetworkName(self, name, currentProcess):
		name = name.strip()
		if not name or self.currentProcess != currentProcess:
			return
		print("[Satfinder][getNetworkNameFromNit] network name: %s" % name)
		with self.threadLock:
			self["network"].setText(_("Network: %s") % name)

	def getOrbPosFromNit(self, currentProcess):
		"""Get orbital position information from NIT"""
		if self.DVB_type.value != "DVB-S" or not dvbreader_available or self.frontend is None or self.demux < 0:
			return

		adapter = 0
		demuxer_device = "/dev/dvb/adapter%d/demux%d" % (adapter, self.demux)

		nit_current_pid = 0x10
		nit_current_table_id = 0x40
		nit_other_table_id = 0x00  # don't read other table
		if nit_other_table_id == 0x00:
			mask = 0xff
		else:
			mask = nit_current_table_id ^ nit_other_table_id ^ 0xff
		nit_current_timeout = 60  # maximum time in seconds

		nit_current_version_number = -1
		nit_current_sections_read = []
		nit_current_sections_count = 0
		nit_current_content = []
		nit_current_completed = False

		if self.currentProcess != currentProcess:
			return

		with self.threadLock:
			self["pos"].setText(_("Reading NIT..."))

		fd = dvbreader.open(demuxer_device, nit_current_pid, nit_current_table_id, mask, self.feid)
		if fd < 0:
			print("[Satfinder][getOrbPosFromNit] Cannot open the demuxer")
			with self.threadLock:
				self["pos"].setText(_("NIT: demuxer error"))
			return

		timeout = datetime.datetime.now()
		timeout += datetime.timedelta(0, nit_current_timeout)

		try:
			while self.should_continue("nit_reader"):
				if datetime.datetime.now() > timeout:
					print("[Satfinder][getOrbPosFromNit] Timed out reading NIT")
					break

				if self.currentProcess != currentProcess or not self.tunerLock():
					break

				section = dvbreader.read_nit(fd, nit_current_table_id, nit_other_table_id)
				if section is None:
					time.sleep(0.1)  # no data.. so we wait a bit
					continue

				if section["header"]["table_id"] == nit_current_table_id and not nit_current_completed:
					if section["header"]["version_number"] != nit_current_version_number:
						nit_current_version_number = section["header"]["version_number"]
						nit_current_sections_read = []
						nit_current_sections_count = section["header"]["last_section_number"] + 1
						nit_current_content = []

					if section["header"]["section_number"] not in nit_current_sections_read:
						nit_current_sections_read.append(section["header"]["section_number"])
						nit_current_content += section["content"]

						if len(nit_current_sections_read) == nit_current_sections_count:
							nit_current_completed = True

				if nit_current_completed:
					break
		finally:
			dvbreader.close(fd)

		if not nit_current_content:
			print("[Satfinder][getOrbPosFromNit] current transponder not found")
			with self.threadLock:
				self["pos"].setText(_("No NIT data"))
			return

		# Find the transponder with matching ONID and TSID
		transponders = [t for t in nit_current_content if "descriptor_tag" in t and t["descriptor_tag"] == 0x43
					   and t["original_network_id"] == self.onid and t["transport_stream_id"] == self.tsid]

		# If not found, try with just TSID
		transponders2 = [t for t in nit_current_content if "descriptor_tag" in t and t["descriptor_tag"] == 0x43
						and t["transport_stream_id"] == self.tsid]

		if transponders and "orbital_position" in transponders[0]:
			entry = transponders[0]
			tentative = False
		elif transponders2 and "orbital_position" in transponders2[0]:
			entry = transponders2[0]
			tentative = True
			print("[satfinder][getOrbPosFromNit] tentative, tsid match, onid mismatch between NIT and SDT")
		else:
			print("[satfinder][getOrbPosFromNit] no orbital position found")
			with self.threadLock:
				self["pos"].setText(_("NIT: no position"))
			return

		verdict = self.buildNitVerdict(entry["orbital_position"], entry["west_east_flag"], tentative)
		print("[satfinder][getOrbPosFromNit]", verdict)
		with self.threadLock:
			self["pos"].setText(verdict)

	def getOrbitalPositionValue(self, bcd, w_e_flag=1):
		# Same decoding as getOrbitalPosition but returns the position as an
		# integer in enigma's 0..3600 convention (west = 3600 - x) for comparison
		# against the satellite selected in the tuner configuration.
		op = 0
		for i in range(4):
			op += ((bcd >> 4 * i) & 0x0F) * 10**i
		if op > 1800:
			op = (3600 - op) * -1
		if w_e_flag == 0:
			op *= -1
		return op % 3600

	def nitSatName(self, pos):
		try:
			name = str(nimmanager.getSatDescription(pos))
			if name:
				return name[:26]
		except Exception:
			pass
		return ""

	def buildNitVerdict(self, bcd, w_e_flag, tentative):
		# The satellite broadcasts its own orbital position in the NIT; comparing
		# it with the position the tuner is configured for verifies whether this
		# transponder really belongs to the selected satellite.
		pos_text = self.getOrbitalPosition(bcd, w_e_flag)
		pos_value = self.getOrbitalPositionValue(bcd, w_e_flag)
		name = self.nitSatName(pos_value)
		text = pos_text
		if name:
			text += " " + name
		if tentative:
			text += " ?"
		expected = None
		try:
			if self.DVB_type.value == "DVB-S":
				expected = int(self.tuning_sat.value)
		except Exception:
			expected = None
		if expected is None:
			return text
		diff = abs(pos_value - expected)
		if diff > 1800:
			diff = 3600 - diff
		if diff <= 5: # 0.5 degree tolerance for nominal-position fuzz, e.g. 0.8W vs 1.0W
			return text + " - " + _("verified")
		expected_text = "{:0.1f}{}".format((3600 - expected) / 10. if expected > 1800 else expected / 10., "W" if expected > 1800 else "E")
		return text + " - " + _("(Not %s)") % expected_text

	def getOrbitalPosition(self, bcd, w_e_flag=1):
		# 4 bit BCD (binary coded decimal)
		# w_e_flag, 0 == west, 1 == east
		op = 0
		bits = 4
		for i in range(bits):
			op += ((bcd >> 4 * i) & 0x0F) * 10**i
		if op > 1800:
			op = (3600 - op) * -1
		if w_e_flag == 0:
			op *= -1
		return "{:0.1f}{}".format(abs(op) / 10., "W" if op < 0 else "E")

	def tunerLock(self):
		try:
			frontendStatus = {}
			self.frontend.getFrontendStatus(frontendStatus)
			return frontendStatus["tuner_state"] == "LOCKED"
		except:
			pass

	def waitTunerLock(self, currentProcess):
		"""Wait for tuner to acquire lock, with timeout"""
		lock_timeout = 120  # seconds

		timeout = datetime.datetime.now()
		timeout += datetime.timedelta(0, lock_timeout)

		while self.should_continue():
			try:
				if datetime.datetime.now() > timeout:
					print("[Satfinder][waitTunerLock] tuner lock timeout reached, seconds:", lock_timeout)
					return False

				if self.currentProcess != currentProcess:
					return False

				if not self.frontend:
					return False

				frontendStatus = {}
				self.frontend.getFrontendStatus(frontendStatus)
				if frontendStatus["tuner_state"] == "FAILED":
					print("[Satfinder][waitTunerLock] TUNING FAILED FATAL")  # enigma2 cpp code has given up trying
					return False

				if frontendStatus["tuner_state"] != "LOCKED":
					time.sleep(0.25)
					continue

				return True
			except Exception as e:
				print(f"[Satfinder][waitTunerLock] Error: {e}")
				time.sleep(0.5)
		
		return False


	def monitorTunerLock(self, currentProcess):
		"""Monitor if tuner maintains lock, restart scanning if lock is lost"""
		# Check every second if tuner is still locked
		while self.should_continue("lock_monitor"):
			try:
				if self.currentProcess != currentProcess:
					return
					
				frontendStatus = {}
				if self.frontend:
					self.frontend.getFrontendStatus(frontendStatus)
					if frontendStatus["tuner_state"] != "LOCKED":
						print("[monitorTunerLock] Lock lost, restarting scan")
						# Start a new thread for scanning to avoid blocking
						self.start_thread(self.getCurrentTsidOnid, (False,), "restart_scan")
						return
				else:
					# Frontend is gone, exit thread
					return
					
				time.sleep(1.0)
			except Exception as e:
				print(f"[monitorTunerLock] Error: {e}")
				# Sleep a bit longer on error
				time.sleep(2.0)


	def keyReadServices(self):
		if not self.serviceList:
			return
		tv = [1, 17, 22, 25, 31]
		radio = [2, 10]
		colors = parameters.get("SatfinderExtraColors", (0x0088FF88, 0x00FF8888, 0x00FFFF00, 0x007799FF, 0x00FFFFFF)) # "FTA", "encrypted", "data", "radio", "default" colors
		fta_color = Hex2strColor(colors[0])
		encrypted_color = Hex2strColor(colors[1])
		data_color = Hex2strColor(colors[2])
		radio_color = Hex2strColor(colors[3])
		default_color = Hex2strColor(colors[4])
		out = []
		legend = "{}{}{}:  {}{}{}  {}{}{}  {}{}{}  {}{}{}\n\n{}{}{}\n".format(default_color, _("Key"), default_color, fta_color, _("FTA TV"), default_color, encrypted_color, _("Encrypted TV"), default_color, radio_color, _("Radio"), default_color, data_color, _("Other"), default_color, default_color, _("Channels"), default_color)
		for service in self.serviceList:
			fta = "free_ca" in service and service["free_ca"] == 0
			if service["service_type"] in radio:
				color = radio_color
			elif service["service_type"] not in tv: # data/interactive/etc
				color = data_color
			elif fta:
				color = fta_color
			else:
				color = encrypted_color
			# SID shown per service (dim, after the name) so the "+N" hint in
			# the info row expands to the full SID list on the yellow key.
			out.append("- {}{}{}  (SID {})".format(color, service["service_name"], default_color, service["service_id"]))

		self.session.openWithCallback(self._servicesFoundCallback, ServicesFound, "\n".join(out), legend)

	def _servicesFoundCallback(self, answer=None):
		if answer == "scan":
			self.keyGoScan()


class ServicesFound(Screen):
	# Self-contained layout under a unique skinName so no installed skin's
	# <screen name="ServicesFound"> overrides it, and wfNoBorder + a solid
	# background remove the skin's default window decoration (the teal/green
	# border that was bleeding through).
	skin = """
		<screen name="TNAP_ServicesFound" position="0,0" size="1920,1080" title="Services found" flags="wfNoBorder" backgroundColor="#00000000" resolution="1920,1080">
			<eLabel position="0,0" size="1920,1080" backgroundColor="#00000000" zPosition="-2"/>
			<widget source="Title" render="Label" position="60,22" size="1500,66" font="Regular;46" foregroundColor="#00f0f0f0" transparent="1" valign="center" halign="left" noWrap="1"/>
			<eLabel position="0,110" size="1920,2" backgroundColor="#00303030"/>
			<widget name="legend" position="60,128" size="1800,56" zPosition="10" font="Regular;30" transparent="1" valign="center"/>
			<widget name="servicesfound" position="60,200" size="1800,790" zPosition="10" font="Regular;32" transparent="1"/>
			<eLabel position="0,1010" size="1920,2" backgroundColor="#00303030"/>
			<eLabel position="60,1035" size="30,30" backgroundColor="#00ff4a3c" zPosition="2"/>
			<widget source="key_red" render="Label" position="105,1030" size="320,40" font="Regular;34" foregroundColor="#00f0f0f0" transparent="1" valign="center" halign="left"/>
			<eLabel position="490,1035" size="30,30" backgroundColor="#0056c856" zPosition="2"/>
			<widget source="key_green" render="Label" position="535,1030" size="320,40" font="Regular;34" foregroundColor="#00f0f0f0" transparent="1" valign="center" halign="left">
				<convert type="ConditionalShowHide"/>
			</widget>
		</screen>"""

	def __init__(self, session, text, legend, show_scan=True):
		Screen.__init__(self, session)
		self.skinName = ["TNAP_ServicesFound"]
		self.setTitle(_("Services found"))

		self["key_red"] = StaticText(_("Close"))
		self["key_green"] = StaticText(_("Scan") if show_scan else "")
		self["legend"] = Label(legend)
		self["servicesfound"] = ScrollLabel(text)

		actions = {
			"back": self.close,
			"red": self.close,
			"up": self.pageUp,
			"down": self.pageDown,
			"left": self.pageUp,
			"right": self.pageDown,
		}
		if show_scan:
			actions["green"] = self.keyScan
		self["actions"] = ActionMap(["WizardActions", "ColorActions"], actions, -2)

	def keyScan(self):
		self.close("scan")

	def pageUp(self):
		self["servicesfound"].pageUp()

	def pageDown(self):
		self["servicesfound"].pageDown()


def SatfinderCallback(close, answer):
	if close and answer:
		close(True)


def SatfinderMain(session, close=None, **kwargs):
	nims = nimmanager.nim_slots
	nimList = []
	for n in nims:
		if not any([n.isCompatible(x) for x in ("DVB-S", "DVB-T", "DVB-C", "ATSC")]):
			continue
		if n.config_mode in ("loopthrough", "satposdepends", "nothing"):
			continue
		if n.isCompatible("DVB-S") and n.config_mode in ("advanced", "simple") and len(nimmanager.getSatListForNim(n.slot)) < 1 and len(n.getTunerTypesEnabled()) < 2:
			continue
		nimList.append(n)

	if len(nimList) == 0:
		session.open(MessageBox, _("No satellite, terrestrial or cable tuner is configured. Please check your tuner setup."), MessageBox.TYPE_ERROR)
	else:
		if dvbreader_available:
			session.openWithCallback(boundFunction(SatfinderCallback, close), SatfinderExtra)
		else:
			session.openWithCallback(boundFunction(SatfinderCallback, close), Satfinder)


def SatfinderStart(menuid, **kwargs):
	if menuid == "scan" and nimmanager.somethingConnected():
		return [(_("Signal finder"), SatfinderMain, "satfinder", None)]
	else:
		return []


def Plugins(**kwargs):
	if any([nimmanager.hasNimType(x) for x in ("DVB-S", "DVB-T", "DVB-C", "ATSC")]):
		return PluginDescriptor(name=_("Signal finder"), description=_("Helps setting up your antenna"), where=PluginDescriptor.WHERE_MENU, needsRestart=False, fnc=SatfinderStart)
	else:
		return []
