from enigma import eDVBResourceManager, eDVBFrontendParametersSatellite, eDVBFrontendParametersTerrestrial, eTimer
from Screens.ScanSetup import ScanSetup, buildTerTransponder
from Screens.ServiceScan import ServiceScan
from Screens.MessageBox import MessageBox
from Screens.ChoiceBox import ChoiceBox
import xml.etree.ElementTree as ET
from Plugins.Plugin import PluginDescriptor
from Components.Sources.FrontendStatus import FrontendStatus
from Components.ActionMap import ActionMap
from Components.NimManager import nimmanager, getConfigSatlist
from Components.config import config, ConfigSelection, ACTIONKEY_RIGHT
from Components.TuneTest import Tuner
from Tools.Transponder import getChannelNumber, channel2frequency
from Tools.BoundFunction import boundFunction
from Screens.Screen import Screen # for services found class
from Components.Sources.StaticText import StaticText
from Tools.Directories import fileExists   # Extra Import
import os  # Extra Import
import threading  # Use threading instead of _thread
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
# 0x00 alpha byte = opaque, 0xFF = transparent). The only external pixmap is
# infobar/bar_big.png, which lives in skin_default and therefore resolves on
# every skin via the standard GUI-skin search path. Designed at 1920x1080;
# resolution="1920,1080" lets forks that support it auto-scale on other panels.
#
# Layout coordinates match the PLi-FullNightHD Satfinder so the on-screen
# result is the one shown in the reference screenshot.

# Shared building blocks ----------------------------------------------------

# Absolute path to our own gradient bar, derived from where the plugin is
# installed so it always resolves (an embedded skin has no owning skin dir, so
# a relative "infobar/bar_big.png" wrongly falls back to skin_default's white
# bar). If the PNG is ever missing, the Progress widgets below carry a green
# foregroundColor fallback, so the bar is never an unreadable white block.
PLUGIN_PATH = os.path.dirname(os.path.realpath(__file__))
_BAR_PIXMAP = os.path.join(PLUGIN_PATH, "signalbar.png")

_SAT_SKIN_HEADER = """
	<eLabel position="0,0" size="1920,1080" backgroundColor="#00000000" zPosition="-2"/>
	<widget source="Title" render="Label" position="30,22" size="1500,66" font="Regular;46" foregroundColor="#00f0f0f0" transparent="1" valign="center" halign="left" noWrap="1"/>
	<widget source="global.CurrentTime" render="Label" position="1430,18" size="460,56" font="Regular;46" foregroundColor="#00f0f0f0" transparent="1" halign="right" valign="center">
		<convert type="ClockToText">Format:%H:%M</convert>
	</widget>
	<widget source="global.CurrentTime" render="Label" position="1230,78" size="660,40" font="Regular;30" foregroundColor="#00b6b6b6" transparent="1" halign="right" valign="center">
		<convert type="ClockToText">Date</convert>
	</widget>
	<eLabel position="0,124" size="1920,2" backgroundColor="#00303030" zPosition="-1"/>
"""

_SAT_SKIN_METERS = """
	<widget source="Frontend" render="Progress" pixmap="%(bar)s" position="30,150" size="1860,75" borderWidth="1" borderColor="#00808888" foregroundColor="#0056c856">
		<convert type="FrontendInfo">SNR</convert>
	</widget>
	<eLabel text="SNR:" position="37,150" size="150,75" valign="center" transparent="1" foregroundColor="#00f0f0f0" font="Regular;52" zPosition="2"/>
	<widget source="Frontend" render="Label" position="1552,150" size="330,75" halign="right" valign="center" transparent="1" foregroundColor="#00f0f0f0" font="Regular;52" zPosition="2">
		<convert type="FrontendInfo">SNR</convert>
	</widget>

	<widget source="Frontend" render="Progress" pixmap="%(bar)s" position="30,240" size="1860,75" borderWidth="1" borderColor="#00808888" foregroundColor="#0056c856">
		<convert type="FrontendInfo">AGC</convert>
	</widget>
	<eLabel text="AGC:" position="37,240" size="150,75" valign="center" transparent="1" foregroundColor="#00f0f0f0" font="Regular;52" zPosition="2"/>
	<widget source="Frontend" render="Label" position="1552,240" size="330,75" halign="right" valign="center" transparent="1" foregroundColor="#00f0f0f0" font="Regular;52" zPosition="2">
		<convert type="FrontendInfo">AGC</convert>
	</widget>

	<eLabel text="SNR:" position="30,360" size="180,30" transparent="1" zPosition="5" font="Regular;27"/>
	<widget source="Frontend" render="Label" position="30,390" size="450,112" font="Regular;108" halign="left" transparent="1">
		<convert type="FrontendInfo">SNRdB</convert>
	</widget>
	<eLabel text="AGC:" position="30,540" size="180,30" transparent="1" zPosition="5" font="Regular;27"/>
	<widget source="Frontend" render="Label" position="30,570" size="450,112" font="Regular;108" halign="left" transparent="1">
		<convert type="FrontendInfo">AGC</convert>
	</widget>
	<eLabel text="BER:" position="30,720" size="180,30" transparent="1" zPosition="5" font="Regular;27"/>
	<widget source="Frontend" render="Label" position="30,750" size="450,112" font="Regular;108" halign="left" transparent="1">
		<convert type="FrontendInfo">BER</convert>
	</widget>
	<widget text="LOCK" source="Frontend" render="FixedLabel" position="30,895" size="465,120" font="Regular;108" halign="left" foregroundColor="#0056c856" transparent="1">
		<convert type="FrontendInfo">LOCK</convert>
		<convert type="ConditionalShowHide"/>
	</widget>

	<widget name="config" valueFont="Regular;28" position="450,360" size="1440,643" itemHeight="49" font="Regular;40" transparent="1" enableWrapAround="1" scrollbarMode="showOnDemand"/>
""" % {"bar": _BAR_PIXMAP}

# ONID / TSID / POS row -- only present on SatfinderExtra (needs dvbreader)
_SAT_SKIN_DVBROW = """
	<eLabel text="ONID:" position="452,320" size="160,40" font="Regular;32" transparent="1" foregroundColor="#00b6b6b6" halign="right" valign="center"/>
	<eLabel position="618,317" size="230,46" backgroundColor="#25333333" zPosition="1"/>
	<widget source="onid" render="Label" position="620,319" size="226,42" font="Regular;32" foregroundColor="#00ffc000" backgroundColor="#25333333" halign="center" valign="center" zPosition="2"/>

	<eLabel text="TSID:" position="870,320" size="160,40" font="Regular;32" transparent="1" foregroundColor="#00b6b6b6" halign="right" valign="center"/>
	<eLabel position="1036,317" size="230,46" backgroundColor="#25333333" zPosition="1"/>
	<widget source="tsid" render="Label" position="1038,319" size="226,42" font="Regular;32" foregroundColor="#00ffc000" backgroundColor="#25333333" halign="center" valign="center" zPosition="2"/>

	<eLabel text="POS:" position="1290,320" size="130,40" font="Regular;32" transparent="1" foregroundColor="#00b6b6b6" halign="right" valign="center"/>
	<eLabel position="1426,317" size="434,46" backgroundColor="#25333333" zPosition="1"/>
	<widget source="pos" render="Label" position="1428,319" size="430,42" font="Regular;32" foregroundColor="#00ffc000" backgroundColor="#25333333" halign="center" valign="center" zPosition="2"/>
"""

# Bottom colour-key bar. Red/Green/Blue chips are always present. The Yellow
# key only exists on SatfinderExtra, so it lives in its own block and is
# rendered as conditional coloured text -- visible only when the plugin sets
# key_yellow ("Service list") and hidden otherwise.
_SAT_SKIN_BUTTONS_RGB = """
	<eLabel position="190,1035" size="30,30" backgroundColor="#00ff4a3c" zPosition="2"/>
	<widget source="key_red" render="Label" position="235,1030" size="320,40" font="Regular;34" foregroundColor="#00f0f0f0" transparent="1" valign="center" halign="left"/>

	<eLabel position="620,1035" size="30,30" backgroundColor="#0056c856" zPosition="2"/>
	<widget source="key_green" render="Label" position="665,1030" size="320,40" font="Regular;34" foregroundColor="#00f0f0f0" transparent="1" valign="center" halign="left"/>
"""

# Blue "Load/Clear Blindscan" key, shown only when key_blue has text (i.e. a
# blindscan file exists or one is loaded). The chip is a key_blue-bound Label
# with matching fore/background so it paints as a solid blue square (its text is
# hidden by the colour match); ConditionalShowHide then hides the chip and the
# adjacent text label together when key_blue is empty -- no extra asset needed.
_SAT_SKIN_BUTTON_BLUE = """
	<widget source="key_blue" render="Label" position="1480,1035" size="30,30" font="Regular;1" backgroundColor="#00879ce1" foregroundColor="#00879ce1" zPosition="2">
		<convert type="ConditionalShowHide"/>
	</widget>
	<widget source="key_blue" render="Label" position="1525,1030" size="365,40" font="Regular;34" foregroundColor="#00f0f0f0" transparent="1" valign="center" halign="left">
		<convert type="ConditionalShowHide"/>
	</widget>
"""

_SAT_SKIN_BUTTON_YELLOW = """
	<widget source="key_yellow" render="Label" position="1010,1030" size="430,40" font="Regular;34" foregroundColor="#00F9C731" transparent="1" valign="center" halign="left">
		<convert type="ConditionalShowHide"/>
	</widget>
"""

# Full screens --------------------------------------------------------------

# Base Satfinder (no AutoBouquetsMaker / dvbreader) -- no ONID/TSID/POS row.
SATFINDER_SKIN_BASE = (
	'<screen name="TNAP_Satfinder" position="0,0" size="1920,1080" '
	'title="Signal finder" flags="wfNoBorder" backgroundColor="#00000000" '
	'resolution="1920,1080">'
	+ _SAT_SKIN_HEADER
	+ _SAT_SKIN_METERS
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

		self["actions"] = ActionMap(["SetupActions", "ColorActions"],
		{
			"save": self.keyGoScan,
			"ok": self.keyGoScan,
			"cancel": self.keyCancel,
			"blue": self.keyBlue,
		}, -3)

		self.initcomplete = True
		self.session.postScanService = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		self.session.nav.stopService()
		self.onClose.append(self.__onClose)
		self.onShow.append(self.prepareFrontend)
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

	def __onClose(self):
		try:
			if hasattr(self, 'timer') and self.timer:
				self.timer.stop()
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

		self["key_yellow"] = StaticText("")

		self["actions2"] = ActionMap(["ColorActions"],
		{
			"yellow": self.keyReadServices,
		}, -3)
		self["actions2"].setEnabled(False)

		# DVB stream info
		self.serviceList = []
		self["tsid"] = StaticText("")
		self["onid"] = StaticText("")
		self["pos"] = StaticText("")

		# Register our close handler — must be done explicitly because Python
		# name-mangling (__onClose → _SatfinderExtra__onClose) means the parent's
		# self.onClose.append(self.__onClose) only registered _Satfinder__onClose.
		self.onClose.append(self._extraOnClose)
	def start_thread(self, target, args=(), name=None):
		"""Safely start and track a new thread"""
		if name not in self.threadEvents:
			self.threadEvents[name] = threading.Event()
		else:
			self.threadEvents[name].clear()  # reset event for reuse

		# Prune finished threads before adding a new one
		self.threadpool = [t for t in self.threadpool if t.is_alive()]

		thread = threading.Thread(target=target, args=args)
		thread.daemon = True  # Set thread as daemon so it exits when main thread exits
		thread.name = name if name else f"Thread-{len(self.threadpool)}"
		self.threadpool.append(thread)
		thread.start()
		return thread

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
			self["tsid"].setText("")
			self["onid"].setText("")
			self["pos"].setText("")
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
			out.append("- {}{}{}".format(color, service["service_name"], default_color))

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
