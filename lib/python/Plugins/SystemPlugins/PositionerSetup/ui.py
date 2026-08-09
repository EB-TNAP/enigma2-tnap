from enigma import eTimer, eDVBSatelliteEquipmentControl, eDVBResourceManager, eDVBDiseqcCommand, eDVBFrontendParametersSatellite, iDVBFrontend, ePoint

from Screens.Screen import Screen
from Screens.MessageBox import MessageBox
from Screens.ChoiceBox import ChoiceBox
from Screens.Satconfig import NimSetup
from Screens.InfoBar import InfoBar
from Components.Label import Label
from Components.Button import Button
from Components.Sources.StaticText import StaticText
from Components.Sources.FrontendStatus import FrontendStatus
from Components.ConfigList import ConfigList, ConfigListScreen
from Components.TunerInfo import TunerInfo
from Components.ActionMap import NumberActionMap, ActionMap
from Components.NimManager import nimmanager
from Components.MenuList import MenuList
from Components.ScrollLabel import ScrollLabel
from Components.config import config, ConfigSatlist, ConfigNothing, ConfigSelection, ConfigSubsection, ConfigInteger, ConfigFloat, configfile, KEY_LEFT, KEY_RIGHT, KEY_0, getConfigListEntry, NoSave
from Components.TuneTest import Tuner
from Components.Pixmap import Pixmap
from Tools.Transponder import ConvertToHumanReadable
from skin import parameters
from Tools.Directories import fileExists # Extra Import

from time import sleep, strftime
from operator import mul as mul
from random import SystemRandom as SystemRandom
from threading import Thread as Thread
from threading import Event as Event
import os  # Extra Import
from . import log
from . import rotor_calc

# Live signal-trend graph. Optional -- if this build has no Canvas renderer the
# trend block is dropped from the skin and the panel is simply not drawn.
try:
	from Components.Sources.CanvasSource import CanvasSource
	POS_TREND_AVAILABLE = True
except ImportError:
	print("[PositionerSetup] CanvasSource not available -- signal trend disabled")
	POS_TREND_AVAILABLE = False

# Audible signal tone, shared with the Signal finder so the pitch/dB
# association you learn on one screen holds on the other. Preferred location is
# the shared Tools copy; falls back to the copy inside the Satfinder plugin,
# then to a local one, so it works whichever way the image ships it.
SignalTone = None
POS_TONE_AVAILABLE = False
toneConfig = None
# Level/mode naming and cycling live in the shared module too, so the two
# screens can never drift out of step on the labels or the cycle order.
_tone_level_name = _tone_nolock_name = lambda v: v
_tone_cycle_level = _tone_cycle_nolock = lambda v: v
for _mod in ("Tools.SignalTone",
             "Plugins.SystemPlugins.Satfinder.signaltone",
             "Plugins.SystemPlugins.PositionerSetup.signaltone"):
	try:
		_m = __import__(_mod, fromlist=["SignalTone"])
		SignalTone = _m.SignalTone
		toneConfig = _m.toneConfig
		_tone_level_name = _m.levelName
		_tone_nolock_name = _m.noLockName
		_tone_cycle_level = _m.cycleLevel
		_tone_cycle_nolock = _m.cycleNoLock
		POS_TONE_AVAILABLE = _m.toneAvailable()
		print("[PositionerSetup] signal tone from %s (available=%s)" % (_mod, POS_TONE_AVAILABLE))
		break
	except Exception:
		continue
else:
	print("[PositionerSetup] no signaltone module found -- sound disabled")

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
# TNAP embedded Positioner-Setup skin
# ---------------------------------------------------------------------------
# Same approach used for the Signal finder: a unique skinName that no installed
# skin defines, so enigma2's readSkin() falls back to the embedded self.skin
# below and our layout always wins -- including the gradient signal bars.
#
# Fully self-contained: no <panel> includes, no skin-private colours, no fonts
# beyond "Regular". The only external pixmap is signalbar.png (ship it next to
# this file). The TunerInfo snr_bar/agc_bar/ber_bar eSliders accept a fill
# pixmap exactly like stock skins do; foregroundColor is a green fallback if
# the PNG is ever missing (clean green bar instead of an unreadable white one).
#
# DELIBERATELY NOT the same look as the Signal finder, so nobody drives the
# wrong screen. Three differences, all structural rather than decorative:
#
#   * CYAN accent instead of the Signal finder's amber, on the header rule,
#     card edges, decoded values and the move markers.
#   * THREE thin bars (SNR / AGC / BER) instead of two fat ones, with the
#     captions and values sitting OUTSIDE the track as plain text rather than
#     in boxed cells. Reads as a lab instrument next to Satfinder's panel look.
#   * A POSITIONER badge in the header.
#
# One caveat worth knowing before editing: GUISkin.createGUIScreen() builds
# every named/source component first and only then attaches the skin's
# additionalWidgets (the raw eLabels), so at equal zPosition an OPAQUE eLabel
# paints OVER a widget whatever the document order says. Anything sitting on a
# panel here therefore carries an explicit zPosition above 0.
_POS_PLUGIN_PATH = os.path.dirname(os.path.realpath(__file__))
_POS_BAR_PIXMAP = os.path.join(_POS_PLUGIN_PATH, "signalbar.png")

# Palette. Cooler and bluer than the Signal finder's neutral greys, which is
# half of the "do not confuse the two screens" job on its own.
_PCLR = {
	"bar":      _POS_BAR_PIXMAP,
	"screen":   "#00070a0c",
	"chrome":   "#000c1114",
	"panel":    "#00101a1e",
	"panel2":   "#0017242a",
	"line":     "#001d2c33",
	"text":     "#00f0f0f0",
	"dim":      "#00889aa0",
	"accent":   "#0000c8d4",   # cyan -- the positioner's signature colour
	"green":    "#0043c95a",
	"greenink": "#00061006",
	"red":      "#008c1f22",
	"amber":    "#00ffb020",
	"peak":     "#00d8dde6",
	"msg":      "#00f9c731",   # status/blinking messages, unchanged
}

# Geometry shared with the Python side (peak needles, trend canvas).
_POS_BAR_X = 166
_POS_BAR_W = 1440
_POS_BAR_H = 42
_POS_SNR_BAR_Y = 146
_POS_AGC_BAR_Y = 208
_POS_PEAK_W = 3

_POS_TREND_W = 1420
_POS_TREND_H = 150
_POS_TREND_TOP = 40          # caption/legend band kept clear of the plot
_POS_TREND_COL = 10
_POS_TREND_SAMPLES = _POS_TREND_W // _POS_TREND_COL

_POS_SKIN_BODY = """
	<eLabel position="0,0" size="1920,1080" backgroundColor="%(screen)s" zPosition="-3"/>
	<eLabel position="0,0" size="1920,124" backgroundColor="%(chrome)s" zPosition="-2"/>
	<eLabel position="0,124" size="1920,2" backgroundColor="%(line)s" zPosition="-1"/>
	<eLabel position="30,28" size="6,64" backgroundColor="%(accent)s" zPosition="1"/>
	<widget source="Title" render="Label" position="54,22" size="1000,46" font="Regular;40" foregroundColor="%(text)s" transparent="1" valign="center" halign="left" noWrap="1"/>
	<eLabel text="POSITIONER" position="54,74" size="220,32" backgroundColor="%(accent)s" foregroundColor="%(greenink)s" font="Regular;22" halign="center" valign="center" zPosition="1"/>
	<widget source="global.CurrentTime" render="Label" position="1430,20" size="460,52" font="Regular;44" foregroundColor="%(text)s" transparent="1" halign="right" valign="center">
		<convert type="ClockToText">Format:%%H:%%M</convert>
	</widget>
	<widget source="global.CurrentTime" render="Label" position="1230,74" size="660,34" font="Regular;26" foregroundColor="%(dim)s" transparent="1" halign="right" valign="center">
		<convert type="ClockToText">Date</convert>
	</widget>

	<eLabel text="SNR" position="30,140" size="120,54" font="Regular;32" halign="right" valign="center" transparent="1" foregroundColor="%(dim)s" zPosition="2"/>
	<widget name="snr_bar" position="166,146" size="1440,42" pixmap="%(bar)s" backgroundColor="%(panel2)s" foregroundColor="%(green)s"/>
	<widget name="snr_peak" position="166,146" size="3,42" backgroundColor="%(peak)s" font="Regular;1" zPosition="3"/>
	<widget name="snr_percentage" position="1626,140" size="264,54" font="Regular;36" halign="right" valign="center" transparent="1" foregroundColor="%(text)s" zPosition="2"/>

	<eLabel text="AGC" position="30,202" size="120,54" font="Regular;32" halign="right" valign="center" transparent="1" foregroundColor="%(dim)s" zPosition="2"/>
	<widget name="agc_bar" position="166,208" size="1440,42" pixmap="%(bar)s" backgroundColor="%(panel2)s" foregroundColor="%(green)s"/>
	<widget name="agc_peak" position="166,208" size="3,42" backgroundColor="%(peak)s" font="Regular;1" zPosition="3"/>
	<widget name="agc_percentage" position="1626,202" size="264,54" font="Regular;36" halign="right" valign="center" transparent="1" foregroundColor="%(text)s" zPosition="2"/>

	<eLabel text="BER" position="30,264" size="120,54" font="Regular;32" halign="right" valign="center" transparent="1" foregroundColor="%(dim)s" zPosition="2"/>
	<widget name="ber_bar" position="166,270" size="1440,42" pixmap="%(bar)s" backgroundColor="%(panel2)s" foregroundColor="%(green)s"/>
	<widget name="ber_value" position="1626,264" size="264,54" font="Regular;36" halign="right" valign="center" transparent="1" foregroundColor="%(text)s" zPosition="2"/>

	<eLabel position="30,334" size="420,150" backgroundColor="%(panel)s"/>
	<eLabel position="30,334" size="5,150" backgroundColor="%(green)s"/>
	<eLabel text="SNR" position="52,348" size="240,28" font="Regular;24" transparent="1" foregroundColor="%(dim)s" zPosition="2"/>
	<widget name="snr_db" position="52,378" size="380,94" font="Regular;76" halign="left" valign="center" transparent="1" foregroundColor="%(text)s" zPosition="2"/>

	<widget name="lock_yes" text="LOCK" position="30,494" size="420,76" font="Regular;52" halign="center" valign="center" foregroundColor="%(greenink)s" backgroundColor="%(green)s" zPosition="3"/>
	<widget name="lock_no" text="NO LOCK" position="30,494" size="420,76" font="Regular;52" halign="center" valign="center" foregroundColor="%(text)s" backgroundColor="%(red)s" zPosition="2"/>

	<eLabel position="30,580" size="420,56" backgroundColor="%(panel)s"/>
	<widget name="peak_text" position="48,580" size="388,56" font="Regular;28" halign="left" valign="center" transparent="1" foregroundColor="%(accent)s" zPosition="2"/>

	<eLabel position="30,646" size="420,180" backgroundColor="%(panel)s"/>
	<eLabel text="TRANSPONDER" position="48,658" size="300,26" font="Regular;22" transparent="1" foregroundColor="%(dim)s" zPosition="2"/>
	<eLabel text="Frequency" position="48,694" size="180,34" font="Regular;26" transparent="1" foregroundColor="%(dim)s" halign="left" zPosition="2"/>
	<widget name="frequency_value" position="234,694" size="200,34" font="Regular;26" halign="right" valign="center" transparent="1" foregroundColor="%(accent)s" zPosition="2"/>
	<eLabel text="Symbol rate" position="48,734" size="180,34" font="Regular;26" transparent="1" foregroundColor="%(dim)s" halign="left" zPosition="2"/>
	<widget name="symbolrate_value" position="234,734" size="200,34" font="Regular;26" halign="right" valign="center" transparent="1" foregroundColor="%(accent)s" zPosition="2"/>
	<eLabel text="FEC" position="48,774" size="180,34" font="Regular;26" transparent="1" foregroundColor="%(dim)s" halign="left" zPosition="2"/>
	<widget name="fec_value" position="234,774" size="200,34" font="Regular;26" halign="right" valign="center" transparent="1" foregroundColor="%(accent)s" zPosition="2"/>

	<eLabel position="30,836" size="420,180" backgroundColor="%(panel)s"/>
	<eLabel position="30,836" size="5,180" backgroundColor="%(accent)s"/>
	<eLabel text="ROTOR" position="52,848" size="300,26" font="Regular;22" transparent="1" foregroundColor="%(dim)s" zPosition="2"/>
	<widget name="rotorstatus" position="52,880" size="386,124" font="Regular;26" halign="left" valign="top" transparent="1" foregroundColor="%(text)s" zPosition="2"/>

	<eLabel position="470,334" size="1420,372" backgroundColor="%(panel)s"/>
	<widget name="list" position="486,348" size="1388,344" itemHeight="49" font="Regular;38" valueFont="Regular;32" transparent="1" enableWrapAround="1" scrollbarMode="showOnDemand" zPosition="2"/>

	<eLabel position="470,722" size="1420,150" backgroundColor="%(panel)s"/>
	<widget source="trend" render="Canvas" position="470,722" size="1420,150" zPosition="2"/>
	<eLabel text="SIGNAL TREND" position="486,728" size="280,26" font="Regular;22" transparent="1" foregroundColor="%(dim)s" zPosition="4"/>
	<eLabel position="790,738" size="20,6" backgroundColor="%(amber)s" zPosition="4"/>
	<eLabel text="AGC" position="818,726" size="70,30" font="Regular;22" transparent="1" foregroundColor="%(amber)s" zPosition="4"/>
	<eLabel position="910,738" size="20,6" backgroundColor="%(green)s" zPosition="4"/>
	<eLabel text="SNR" position="938,726" size="70,30" font="Regular;22" transparent="1" foregroundColor="%(green)s" zPosition="4"/>
	<eLabel position="1030,738" size="20,6" backgroundColor="%(accent)s" zPosition="4"/>
	<eLabel text="MOVE" position="1058,726" size="90,30" font="Regular;22" transparent="1" foregroundColor="%(accent)s" zPosition="4"/>
	<widget name="tone_status" position="1160,726" size="714,30" font="Regular;22" transparent="1" foregroundColor="%(accent)s" halign="right" valign="center" zPosition="4"/>

	<eLabel position="470,888" size="1420,128" backgroundColor="%(panel)s"/>
	<widget name="status_bar" position="486,888" size="1388,128" font="Regular;44" halign="center" valign="center" transparent="1" foregroundColor="%(msg)s" zPosition="10"/>

	<eLabel position="0,1022" size="1920,2" backgroundColor="%(line)s" zPosition="-1"/>
	<eLabel position="0,1024" size="1920,56" backgroundColor="%(chrome)s" zPosition="-2"/>
	<eLabel text="MENU" position="30,1038" size="90,28" backgroundColor="%(panel2)s" foregroundColor="%(dim)s" font="Regular;20" halign="center" valign="center"/>
	<eLabel text="INFO" position="130,1038" size="84,28" backgroundColor="%(panel2)s" foregroundColor="%(dim)s" font="Regular;20" halign="center" valign="center"/>
	<eLabel position="240,1038" size="26,26" backgroundColor="#00ff4a3c" zPosition="2"/>
	<widget name="key_red" position="276,1032" size="290,38" font="Regular;30" foregroundColor="%(text)s" transparent="1" valign="center" halign="left"/>
	<eLabel position="580,1038" size="26,26" backgroundColor="%(green)s" zPosition="2"/>
	<widget name="key_green" position="616,1032" size="330,38" font="Regular;30" foregroundColor="%(text)s" transparent="1" valign="center" halign="left"/>
	<eLabel position="960,1038" size="26,26" backgroundColor="%(msg)s" zPosition="2"/>
	<widget name="key_yellow" position="996,1032" size="330,38" font="Regular;30" foregroundColor="%(text)s" transparent="1" valign="center" halign="left"/>
	<eLabel position="1340,1038" size="26,26" backgroundColor="#00879ce1" zPosition="2"/>
	<widget name="key_blue" position="1376,1032" size="514,38" font="Regular;30" foregroundColor="%(text)s" transparent="1" valign="center" halign="left"/>
""" % _PCLR

# The trend canvas only exists if the Canvas renderer does. A widget bound to a
# missing source is a skin error, so it is dropped and the config panel grows.
if POS_TREND_AVAILABLE:
	POSITIONER_SKIN = ('<screen name="TNAP_PositionerSetup" position="0,0" size="1920,1080" '
		'title="TNAP Positioner Setup" flags="wfNoBorder" backgroundColor="#00000000" '
		'resolution="1920,1080">' + _POS_SKIN_BODY + '</screen>')
else:
	_no_trend = _POS_SKIN_BODY
	for _frag in (
		'\t<widget source="trend" render="Canvas" position="470,722" size="1420,150" zPosition="2"/>\n',
		'\t<eLabel position="470,722" size="1420,150" backgroundColor="%s"/>\n' % _PCLR["panel"],
		'\t<eLabel text="SIGNAL TREND" position="486,728" size="280,26" font="Regular;22" transparent="1" foregroundColor="%s" zPosition="4"/>\n' % _PCLR["dim"],
		'\t<eLabel position="790,738" size="20,6" backgroundColor="%s" zPosition="4"/>\n' % _PCLR["amber"],
		'\t<eLabel text="AGC" position="818,726" size="70,30" font="Regular;22" transparent="1" foregroundColor="%s" zPosition="4"/>\n' % _PCLR["amber"],
		'\t<eLabel position="910,738" size="20,6" backgroundColor="%s" zPosition="4"/>\n' % _PCLR["green"],
		'\t<eLabel text="SNR" position="938,726" size="70,30" font="Regular;22" transparent="1" foregroundColor="%s" zPosition="4"/>\n' % _PCLR["green"],
		'\t<eLabel position="1030,738" size="20,6" backgroundColor="%s" zPosition="4"/>\n' % _PCLR["accent"],
		'\t<eLabel text="MOVE" position="1058,726" size="90,30" font="Regular;22" transparent="1" foregroundColor="%s" zPosition="4"/>\n' % _PCLR["accent"],
	):
		_no_trend = _no_trend.replace(_frag, "")
	# tone_status moves into the freed band so the sound hint stays visible.
	_no_trend = _no_trend.replace(
		'<widget name="tone_status" position="1160,726" size="714,30"',
		'<widget name="tone_status" position="1160,730" size="714,30"')
	POSITIONER_SKIN = ('<screen name="TNAP_PositionerSetup" position="0,0" size="1920,1080" '
		'title="TNAP Positioner Setup" flags="wfNoBorder" backgroundColor="#00000000" '
		'resolution="1920,1080">' + _no_trend + '</screen>')


class PositionerSetup(Screen):

	@staticmethod
	def satposition2metric(position):
		if position > 1800:
			position = 3600 - position
			orientation = "west"
		else:
			orientation = "east"
		return (position, orientation)

	@staticmethod
	def orbital2metric(position, orientation):
		if orientation == "west":
			position = 360 - position
		if orientation == "south":
			position = - position
		return position

	@staticmethod
	def longitude2orbital(position):
		if position >= 180:
			return 360 - position, "west"
		else:
			return position, "east"

	@staticmethod
	def latitude2orbital(position):
		if position >= 0:
			return position, "north"
		else:
			return -position, "south"

	FIRST_UPDATE_INTERVAL = 800	#500		# milliseconds 500
	UPDATE_INTERVAL = 25	#50				# milliseconds  50
	RETUNE_KEEPALIVE_INTERVAL = 3000		# milliseconds: debounce before re-arming a failed tune
####
	STATUS_MSG_TIMEOUT = 2					# seconds
	LOG_SIZE = 16 * 1024					# log buffer size

	def __init__(self, session, feid):
		Screen.__init__(self, session)
		# Force our own self-contained layout (with gradient signal bars) instead
		# of whatever the active skin ships for "PositionerSetup". Unique skinName
		# -> readSkin() misses every installed skin and uses self.skin below.
		self.skin = POSITIONER_SKIN
		self.skinName = ["TNAP_PositionerSetup"]
		self.setTitle(_("TNAP Positioner Setup - " + BOX_NAME))
		self.feid = feid
		self.oldref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		self.oldref_stop = False
		self.rotor_diseqc = True
		self.frontend = None
		self.rotor_pos = config.usage.showdish.value and config.misc.lastrotorposition.value != 9999
		self.tsid = self.onid = self.orb_pos = 0
		self.checkingTsidOnid = False
		self.finesteps = 0
		self.tp =""
		getCurrentTuner = None
		getCurrentSat = None
		self.availablesats = []
		log.open(self.LOG_SIZE)
		if config.Nims[self.feid].configMode.value == 'advanced':
			self.advanced = True
			self.advancedconfig = config.Nims[self.feid].advanced
			self.advancedsats = self.advancedconfig.sat
		else:
			self.advanced = False
		self.availablesats = list(map(lambda x: x[0], nimmanager.getRotorSatListForNim(self.feid)))

		cur = {}
		if not self.openFrontend():
			service = self.session.nav.getCurrentService()
			feInfo = service and service.frontendInfo()
			if feInfo:
				cur_info = feInfo.getTransponderData(True)
				frontendData = feInfo.getAll(True)
				getCurrentTuner = frontendData and frontendData.get("tuner_number", None)
				getCurrentSat = cur_info.get('orbital_position', None)
			del feInfo
			del service
			if self.oldref and getCurrentTuner is not None:
				if self.feid == getCurrentTuner:
					self.oldref_stop = True
				else:
					for n in nimmanager.nim_slots:
						try:
							advanced_satposdepends = n.config_mode == 'advanced' and int(n.config.advanced.sat[3607].lnb.value) != 0
						except:
							advanced_satposdepends = False
						if n.config_mode in ("loopthrough", "satposdepends") or advanced_satposdepends:
							if n.config.connectedTo.value and int(n.config.connectedTo.value) == self.feid:
								self.oldref_stop = True
				if self.oldref_stop:
					self.session.nav.stopService() # try to disable foreground service
					if getCurrentSat is not None and getCurrentSat in self.availablesats:
						cur = cur_info
					else:
						self.rotor_diseqc = False
			getCurrentTuner = None
			getCurrentSat = None
			if not self.openFrontend():
				if hasattr(session, 'pipshown') and session.pipshown: # try to disable pip
					service = self.session.pip.pipservice
					feInfo = service and service.frontendInfo()
					if feInfo:
						cur_pip_info = feInfo.getTransponderData(True)
						frontendData = feInfo.getAll(True)
						getCurrentTuner = frontendData and frontendData.get("tuner_number", None)
						getCurrentSat = cur_pip_info.get('orbital_position', None)
						if getCurrentTuner is not None and self.feid == getCurrentTuner:
							if getCurrentSat is not None and getCurrentSat in self.availablesats:
								cur = cur_pip_info
							else:
								self.rotor_diseqc = False
					del feInfo
					del service
					InfoBar.instance and hasattr(InfoBar.instance, "showPiP") and InfoBar.instance.showPiP()
					if hasattr(session, 'pip'):  # try to disable pip again
						del session.pip
						session.pipshown = False
					if not self.openFrontend():
						self.frontend = None # in normal case this should not happen
						if hasattr(self, 'raw_channel'):
							del self.raw_channel
			if self.frontend is None:
				self.messageTimer = eTimer()
				self.messageTimer.callback.append(self.showMessageBox)
				self.messageTimer.start(2000, True)
		self.frontendStatus = {}
		self.diseqc = Diseqc(self.frontend)
		# True means we dont like that the normal sec stuff sends commands to the rotor!
		self.tuner = Tuner(self.frontend, ignore_rotor=True)

		tp = (cur.get("frequency", 0) / 1000.0,
			cur.get("symbol_rate", 0) // 1000,
			cur.get("polarization", eDVBFrontendParametersSatellite.Polarisation_Horizontal),
			cur.get("fec_inner", eDVBFrontendParametersSatellite.FEC_Auto),
			cur.get("inversion", eDVBFrontendParametersSatellite.Inversion_Unknown),
			cur.get("orbital_position", 0),
			cur.get("system", eDVBFrontendParametersSatellite.System_DVB_S),
			cur.get("modulation", eDVBFrontendParametersSatellite.Modulation_QPSK),
			cur.get("rolloff", eDVBFrontendParametersSatellite.RollOff_alpha_0_35),
			cur.get("pilot", eDVBFrontendParametersSatellite.Pilot_Unknown),
			cur.get("is_id", eDVBFrontendParametersSatellite.No_Stream_Id_Filter),
			cur.get("pls_mode", eDVBFrontendParametersSatellite.PLS_Gold),
			cur.get("pls_code", eDVBFrontendParametersSatellite.PLS_Default_Gold_Code),
			cur.get("t2mi_plp_id", eDVBFrontendParametersSatellite.No_T2MI_PLP_Id),
			cur.get("t2mi_pid", eDVBFrontendParametersSatellite.T2MI_Default_Pid))

		if tp[2] == 3:
			pol = "R"
		if tp[2] == 2:
			pol = "L"
		if tp[2] == 1:
			pol = "V"
		if tp[2] == 0:
			pol = "H"
		print(" **** Sat Positioner Log for TNAP Images ****", file=log )
#		print("", file=log )
		print("Current Transponder = ", tp[0],pol,"-",tp[1],"Symbol Rate", file=log )
		self.tp = tp[0]
		self.tuner.tune(tp)
		self._lastTp = tp	# remembered so we can re-tune after the Tune editor returns
		self.isMoving = False
		self.stopOnLock = False

		self.red = Button("")
		self["key_red"] = self.red
		self.green = Button("")
		self["key_green"] = self.green
		self.yellow = Button("")
		self["key_yellow"] = self.yellow
		self.blue = Button("")
		self["key_blue"] = self.blue

		self.list = []
		self["list"] = ConfigList(self.list)

		self["snr_db"] = TunerInfo(TunerInfo.SNR_DB, statusDict=self.frontendStatus)
		self["snr_bar"] = TunerInfo(TunerInfo.SNR_BAR, statusDict=self.frontendStatus)
		self["snr_percentage"] = TunerInfo(TunerInfo.SNR_PERCENTAGE, statusDict=self.frontendStatus)
		self["agc_percentage"] = TunerInfo(TunerInfo.AGC_PERCENTAGE, statusDict=self.frontendStatus)
		self["agc_bar"] = TunerInfo(TunerInfo.AGC_BAR, statusDict=self.frontendStatus)
		self["ber_value"] = TunerInfo(TunerInfo.BER_VALUE, statusDict=self.frontendStatus)
		self["ber_bar"] = TunerInfo(TunerInfo.BER_BAR, statusDict=self.frontendStatus)
		self["lock_state"] = TunerInfo(TunerInfo.LOCK_STATE, statusDict=self.frontendStatus)

		# Peak hold, trend and tone. lock_yes/lock_no are a pair of opaque
		# Labels shown one at a time rather than the TunerInfo LOCK_STATE text,
		# so the state reads as a filled pill and "no lock" is stated outright
		# instead of being an empty space you have to interpret.
		self["snr_peak"] = Label("")
		self["agc_peak"] = Label("")
		self["peak_text"] = Label("")
		self["tone_status"] = Label("")
		self["lock_yes"] = Label(_("LOCK"))
		self["lock_no"] = Label(_("NO LOCK"))
		if POS_TREND_AVAILABLE:
			self["trend"] = CanvasSource()
		self._peak_snr_pct = 0
		self._peak_agc_pct = 0
		self._peak_snr_db = None
		self._needle_x = {"snr_peak": None, "agc_peak": None}
		self._trend = []
		self._instr_tick = 0
		self._move_flag = False
		self._tone = None
		self._tone_error = None
		self._tone_locked = False
		self._tone_shown = None
		self._lock_shown = None

		self["rotorstatus"] = Label("")
		self["frequency_value"] = Label("")
		self["symbolrate_value"] = Label("")
		self["fec_value"] = Label("")
		self["status_bar"] = Label("")
		self["SNR"] = Label(_("SNR:"))
		self["BER"] = Label(_("BER:"))
		self["AGC"] = Label(_("AGC:"))
		self["Frequency"] = Label(_("Frequency:"))
		self["Symbolrate"] = Label(_("Symbol rate:"))
		self["FEC"] = Label(_("FEC:"))
		self["Lock"] = Label(_("Lock:"))
		self["lock_off"] = Pixmap()
		self["lock_on"] = Pixmap()
		self["lock_on"].hide()
		if self.rotor_pos:
			if hasattr(eDVBSatelliteEquipmentControl.getInstance(), "getTargetOrbitalPosition"):
				current_pos = eDVBSatelliteEquipmentControl.getInstance().getTargetOrbitalPosition()
				if current_pos in self.availablesats and current_pos != config.misc.lastrotorposition.value:
					config.misc.lastrotorposition.value = current_pos
					config.misc.lastrotorposition.save()
				for x in nimmanager.nim_slots:
					if x.slot == self.feid:
						rotorposition = hasattr(x.config, 'lastsatrotorposition') and x.config.lastsatrotorposition.value or ""
						if rotorposition.isdigit():
							current_pos = int(rotorposition)
							if current_pos != config.misc.lastrotorposition.value:
								config.misc.lastrotorposition.value = current_pos
								config.misc.lastrotorposition.save()
						break
			text = _("Current rotor position: ") + self.OrbToStr(config.misc.lastrotorposition.value)
			self["rotorstatus"].setText(text)
		self.statusMsgTimeoutTicks = 0
		self.statusMsgBlinking = False
		self.statusMsgBlinkCount = 0
		self.statusMsgBlinkRate = 500 / self.UPDATE_INTERVAL	# milliseconds
		self.tuningChangedTo(tp)

		self["actions"] = NumberActionMap(["DirectionActions", "OkCancelActions", "ColorActions", "TimerEditActions", "InputActions", "InfobarMenuActions"],
		{
			"ok": self.keyOK,
			"cancel": self.keyCancel,
			"up": self.keyUp,
			"down": self.keyDown,
			"left": self.keyLeft,
			"right": self.keyRight,
			"red": self.redKey,
			"green": self.greenKey,
			"yellow": self.yellowKey,
			"blue": self.blueKey,
			"log": self.showLog,
			"mainMenu": self.furtherOptions,
			"1": self.keyNumberGlobal,
			"2": self.keyNumberGlobal,
			"3": self.keyNumberGlobal,
			"4": self.keyNumberGlobal,
			"5": self.keyNumberGlobal,
			"6": self.keyNumberGlobal,
			"7": self.keyNumberGlobal,
			"8": self.keyNumberGlobal,
			"9": self.keyNumberGlobal,
			"0": self.keyNumberGlobal
		}, -1)

		# Sound keys. MENU is furtherOptions and INFO is showLog on this screen,
		# and every colour and number key is spoken for, so sound lives on AUDIO
		# and TEXT -- which the Signal finder also accepts, so the two screens
		# share one set of sound keys.
		self["tone_actions"] = ActionMap(["InfobarAudioSelectionActions", "InfobarTeletextActions"],
		{
			"audioSelection": self.keyToneCycle,
			"startTeletext": self.keyNoLockCycle,
		}, -2)

		self.updateColors("tune")
		self.statusTimer = eTimer()
		self.rotorStatusTimer = eTimer()
		self.statusTimer.callback.append(self.updateStatus)
		self.rotorStatusTimer.callback.append(self.startStatusTimer)
		self.collectingStatistics = False
		self.retuneKeepAliveTicks = 0
		self.statusTimer.start(self.FIRST_UPDATE_INTERVAL, True)
		self.dataAvailable = Event()
		self.onClose.append(self.__onClose)
		self.onLayoutFinish.append(self._initInstruments)
		self.onShow.append(self._toneApply)
		self.onHide.append(self._tonePause)
		self.createConfig()
		self.createSetup()

	def __onClose(self):
		# Tone first: SignalTone is referenced by its own writer thread, so it
		# outlives Screen.doClose() clearing this object -- without stopping it
		# here it would keep playing and holding a pipe fd.
		self._toneStop()
		self.statusTimer.stop()
		log.close()
		if self.frontend:
			self.frontend = None
		if hasattr(self, 'raw_channel'):
			del self.raw_channel
		self.session.nav.playService(self.oldref)

	def OrbToStr(self, orbpos):
		if orbpos > 1800:
			orbpos = 3600 - orbpos
			return "%d.%d\xb0 W" % (orbpos / 10, orbpos % 10)
		return "%d.%d\xb0 E" % (orbpos / 10, orbpos % 10)

	def setDishOrbosValue(self):
		if self.getRotorMovingState():
			if self.orb_pos != 0 and self.orb_pos != config.misc.lastrotorposition.value:
				config.misc.lastrotorposition.value = self.orb_pos
				config.misc.lastrotorposition.save()
			text = _("Moving to position") + " " + self.OrbToStr(self.orb_pos)
			self.startStatusTimer()
		else:
			text = _("Current rotor position: ") + self.OrbToStr(config.misc.lastrotorposition.value)
		self["rotorstatus"].setText(text)

	def startStatusTimer(self):
		self.rotorStatusTimer.start(1000, True)

	def getRotorMovingState(self):
		return eDVBSatelliteEquipmentControl.getInstance().isRotorMoving()

	def showMessageBox(self):
		text = _("Sorry, this tuner is in use.")
		if self.session.nav.getRecordings():
			text += "\n"
			text += _("Maybe the reason that recording is currently running. Please stop the recording before trying to configure the positioner.")
		self.session.open(MessageBox, text, MessageBox.TYPE_ERROR)

	def restartPrevService(self, yesno):
		if not yesno:
			self.oldref = None
		self.close(None)

	def keyCancel(self):
		if self.oldref is not None:
			if self.oldref_stop:
				self.session.openWithCallback(self.restartPrevService, MessageBox, _("Zap back to service before positioner setup?"), MessageBox.TYPE_YESNO)
			else:
				self.restartPrevService(True)
		else:
			self.restartPrevService(False)

	def openFrontend(self):
		self.frontend = None
		if hasattr(self, 'raw_channel'):
			del self.raw_channel
		res_mgr = eDVBResourceManager.getInstance()
		if res_mgr:
			self.raw_channel = res_mgr.allocateRawChannel(self.feid)
			if self.raw_channel:
				self.frontend = self.raw_channel.getFrontend()
				if self.frontend:
					return True
				else:
					print("getFrontend failed")
			else:
				print("getRawChannel failed")
		else:
			print("getResourceManager instance failed")
		return False

	def setLNB(self, lnb):
		try:
			self.sitelon = lnb.longitude.float
			self.longitudeOrientation = lnb.longitudeOrientation.value
			self.sitelat = lnb.latitude.float
			self.latitudeOrientation = lnb.latitudeOrientation.value
			self.tuningstepsize = 0.86 # 0.36 
	####
			self.rotorPositions = lnb.rotorPositions.value
			self.turningspeedH = lnb.turningspeedH.float
			self.turningspeedV = lnb.turningspeedV.float
		except: # some reasonable defaults from NimManager
			self.sitelon = 5.1
			self.longitudeOrientation = 'east'
			self.sitelat = 50.767
			self.latitudeOrientation = 'north'
			self.tuningstepsize = 0.86 # 0.36
	####
			self.rotorPositions = 99
			self.turningspeedH = 2.3
			self.turningspeedV = 1.7
		self.sitelat = PositionerSetup.orbital2metric(self.sitelat, self.latitudeOrientation)
		self.sitelon = PositionerSetup.orbital2metric(self.sitelon, self.longitudeOrientation)

	def createConfig(self):
		rotorposition = 1
		orb_pos = 0
		self.printMsg(_("Using tuner %s") % chr(0x41 + self.feid))
		if not self.advanced:
			self.printMsg(_("Configuration mode: %s") % _("simple"))
			nim = config.Nims[self.feid]
			self.sitelon = nim.longitude.float
			self.longitudeOrientation = nim.longitudeOrientation.value
			self.sitelat = nim.latitude.float
			self.latitudeOrientation = nim.latitudeOrientation.value
			self.sitelat = PositionerSetup.orbital2metric(self.sitelat, self.latitudeOrientation)
			self.sitelon = PositionerSetup.orbital2metric(self.sitelon, self.longitudeOrientation)
			self.tuningstepsize = nim.tuningstepsize.float
			self.rotorPositions = nim.rotorPositions.value
			self.turningspeedH = nim.turningspeedH.float
			self.turningspeedV = nim.turningspeedV.float
		else:	# it is advanced
			lnb = None
			self.printMsg(_("Configuration mode: %s") % _("advanced"))
			fe_data = {}
			if self.frontend:
				self.frontend.getFrontendData(fe_data)
				self.frontend.getTransponderData(fe_data, True)
				orb_pos = fe_data.get("orbital_position", None)
				if orb_pos is not None and orb_pos in self.availablesats:
					rotorposition = int(self.advancedsats[orb_pos].rotorposition.value)
				lnb = self.getLNBfromConfig(orb_pos)
			self.setLNB(lnb)
		self.positioner_tune = ConfigNothing()
		self.positioner_move = ConfigNothing()
		self.positioner_finemove = ConfigNothing()
		self.positioner_limits = ConfigNothing()
		self.positioner_storage = ConfigInteger(default=rotorposition, limits=(1, self.rotorPositions))
		self.allocatedIndices = []
		m = PositionerSetup.satposition2metric(orb_pos)
		self.orbitalposition = ConfigFloat(default=[int(m[0] / 10), m[0] % 10], limits=[(0, 180), (0, 9)])
		print("Orbit Position =", self.orbitalposition.float, file=log )
		self.orientation = ConfigSelection([("east", _("East")), ("west", _("West"))], default=m[1])
		for x in (self.positioner_tune, self.positioner_storage, self.orbitalposition):
			x.addNotifier(self.retune, initial_call=False)

	def retune(self, configElement):
		self.createSetup()

	def getUsals(self):
		usals = None
		if self.frontend is not None:
			if self.advanced:
				fe_data = {}
				self.frontend.getFrontendData(fe_data)
				self.frontend.getTransponderData(fe_data, True)
				orb_pos = fe_data.get("orbital_position", -9999)
				try:
					pos = str(PositionerSetup.orbital2metric(self.orbitalposition.float, self.orientation.value))
					orb_val = int(pos.replace('.', ''))
				except:
					orb_val = -9999
				sat = -9999
				if orb_val == orb_pos:
					sat = orb_pos
				elif orb_val != -9999 and orb_val in self.availablesats:
					sat = orb_val
				if sat != -9999 and sat in self.availablesats:
					usals = self.advancedsats[sat].usals.value
					self.rotor_diseqc = True
				return usals
			else:
				self.rotor_diseqc = True
				return True
		return usals

	def getLNBfromConfig(self, orb_pos):
		if orb_pos is None or orb_pos == 0:
			return None
		lnb = None
		if orb_pos in self.availablesats:
			lnbnum = int(self.advancedsats[orb_pos].lnb.value)
			if not lnbnum:
				for allsats in range(3601, 3607):
					lnbnum = int(self.advancedsats[allsats].lnb.value)
					if lnbnum:
						break
			if lnbnum:
				self.printMsg(_("Using LNB %d") % lnbnum)
				lnb = self.advancedconfig.lnb[lnbnum]
		if not lnb:
			self.logMsg(_("Warning: no LNB; using factory defaults."), timeout=118)
		return lnb

	def createSetup(self):
		self.list = []
		self.list.append((_("Tune and focus"), self.positioner_tune, "tune"))
		self.list.append((_("Movement"), self.positioner_move, "move"))
		self.list.append((_("Fine movement"), self.positioner_finemove, "finemove"))
		self.list.append((_("Set limits"), self.positioner_limits, "limits"))
		self.list.append((_("Memory index") + (self.getUsals() and " (USALS)" or ""), self.positioner_storage, "storage"))
		self.list.append((_("Goto"), self.orbitalposition, "goto"))
		self.list.append((" ", self.orientation, "goto"))
		self["list"].l.setList(self.list)

	def keyOK(self):
		entry = self.getCurrentConfigPath()
		if entry == "tune":
			self.redKey()
		elif entry == "finemove":
			self.statusMsg(_("Steps") + self.stepCourse(self.finesteps), timeout=self.STATUS_MSG_TIMEOUT)

	def getCurrentConfigPath(self):
		return self["list"].getCurrent()[2]

	def keyUp(self):
		if not self.isMoving:
			self["list"].instance.moveSelection(self["list"].instance.moveUp)
			self.updateColors(self.getCurrentConfigPath())

	def keyDown(self):
		if not self.isMoving:
			self["list"].instance.moveSelection(self["list"].instance.moveDown)
			self.updateColors(self.getCurrentConfigPath())

	def keyNumberGlobal(self, number):
		if self.frontend is None:
			return
		self["list"].handleKey(KEY_0 + number)

	def keyLeft(self):
		if self.frontend is None:
			return
		self["list"].handleKey(KEY_LEFT)

	def keyRight(self):
		if self.frontend is None:
			return
		self["list"].handleKey(KEY_RIGHT)

	def updateColors(self, entry):
		if self.frontend is None:
			return
		if entry == "tune":
			self.red.setText(_("Tune"))
			self.green.setText(_("Auto focus"))
			self.yellow.setText(_("Calibrate"))
			self.blue.setText(_("Calculate"))
		elif entry == "move":
			if self.isMoving:
				self.red.setText(_("Stop"))
				self.green.setText(_("Stop"))
				self.yellow.setText(_("Stop"))
				self.blue.setText(_("Stop"))
			else:
				self.red.setText(_("Move west"))
				self.green.setText(_("Search west"))
				self.yellow.setText(_("Search east"))
				self.blue.setText(_("Move east"))
		elif entry == "finemove":
			self.red.setText("")
			self.green.setText(_("Step west"))
			self.yellow.setText(_("Step east"))
			self.blue.setText("")
		elif entry == "limits":
			self.red.setText(_("Limits off"))
			self.green.setText(_("Limit west"))
			self.yellow.setText(_("Limit east"))
			self.blue.setText(_("Limits on"))
		elif entry == "storage":
			self.red.setText("")
			if not self.getUsals():
				self.green.setText(_("Store position"))
				self.yellow.setText(_("Goto position"))
			else:
				self.green.setText("")
				self.yellow.setText("")
			if self.advanced and not self.getUsals():
				self.blue.setText(_("Allocate"))
			else:
				self.blue.setText("")
		elif entry == "goto":
			self.red.setText("")
			self.green.setText(_("Goto 0"))
			self.yellow.setText(_("Goto X"))
			self.blue.setText("")
		else:
			self.red.setText("")
			self.green.setText("")
			self.yellow.setText("")
			self.blue.setText("")

	def printMsg(self, msg):
		print(msg)
		print(msg, file=log)

	def stopMoving(self):
		self.printMsg(_("Stop"))
		self.diseqccommand("stop")
		self.isMoving = False
		self.stopOnLock = False
		self.statusMsg(_("Stopped"), timeout=self.STATUS_MSG_TIMEOUT)

	def stepCourse(self, steps):
		def dots(s):
			s = abs(s)
			return int(s / 10) * '.' if s < 100 else 10 * '.'

		dx = 4 * " "
		if steps > 0:
			return dx + ">| %s %d" % (dots(steps), steps) # west
		elif steps < 0:
			return dx + "%d %s |<" % (abs(steps), dots(steps)) # east
		else:
			return dx + ">|<"

	def redKey(self):
		if self.frontend is None:
			return
		entry = self.getCurrentConfigPath()
		if entry != "finemove":
			self.finesteps = 0
		if entry == "move":
			if self.isMoving:
				self.stopMoving()
			else:
				self.printMsg(_("Move west"))
				self.diseqccommand("moveWest", 0)
				self.isMoving = True
				self.statusMsg(_("Moving west..."), blinking=True)
			self.updateColors("move")
		elif entry == "limits":
			self.printMsg(_("Limits off"))
			self.diseqccommand("limitOff")
			self.statusMsg(_("Limits cancelled"), timeout=self.STATUS_MSG_TIMEOUT)
		elif entry == "tune":
			fe_data = {}
			self.frontend.getFrontendData(fe_data)
			self.frontend.getTransponderData(fe_data, True)
			feparm = self.tuner.lastparm.getDVBS()
			fe_data["orbital_position"] = feparm.orbital_position
			self.statusTimer.stop()
			# Release the frontend so the Tune editor can allocate and drive it
			# live; we reclaim it in tune() when the editor closes.
			self.frontend = None
			if hasattr(self, 'raw_channel') and self.raw_channel:
				del self.raw_channel
				self.raw_channel = None
			self.tuner = None
			self.session.openWithCallback(self.tune, TunerScreen, self.feid, fe_data)

	def greenKey(self):
		if self.frontend is None:
			return
		entry = self.getCurrentConfigPath()
		if entry != "finemove":
			self.finesteps = 0
		if entry == "tune":
			# Auto focus
			self.printMsg(_("Auto focus"))
			print((_("Site latitude") + "      : %5.1f %s") % PositionerSetup.latitude2orbital(self.sitelat), file=log)
			print((_("Site longitude") + "     : %5.1f %s") % PositionerSetup.longitude2orbital(self.sitelon), file=log)
			Thread(target=self.autofocus).start()
		elif entry == "move":
			if self.isMoving:
				self.stopMoving()
			else:
				self.printMsg(_("Search west"))
				self.isMoving = True
				self.stopOnLock = True
				self.diseqccommand("moveWest", 0)
				self.statusMsg(_("Searching west..."), blinking=True)
			self.updateColors("move")
		elif entry == "finemove":
			self.finesteps += 1
			self.printMsg(_("Step west"))
			self.diseqccommand("moveWest", 0xFF) # one step
			self.statusMsg(_("Stepped west") + self.stepCourse(self.finesteps), timeout=self.STATUS_MSG_TIMEOUT)
		elif entry == "storage":
			if not self.getUsals():
				menu = [(_("yes"), "yes"), (_("no"), "no")]
				available_orbos = False
				orbos = None
				if self.advanced:
					try:
						orb_pos = str(PositionerSetup.orbital2metric(self.orbitalposition.float, self.orientation.value))
						orbos = int(orb_pos.replace('.', ''))
					except:
						pass
					if orbos is not None and orbos in self.availablesats:
						available_orbos = True
						menu.append((_("Yes (save index in setup tuner)"), "save"))
				index = int(self.positioner_storage.value)
				text = _("Really store at index %2d for current position?") % index

				def saveAction(choice):
					if choice:
						if choice[1] in ("yes", "save"):
							self.printMsg(_("Store at index"))
							self.diseqccommand("store", index)
							self.statusMsg((_("Position stored at index") + " %2d") % index, timeout=self.STATUS_MSG_TIMEOUT)
							if choice[1] == "save" and available_orbos:
								self.advancedsats[orbos].rotorposition.value = index
								self.advancedsats[orbos].rotorposition.save()
				self.session.openWithCallback(saveAction, ChoiceBox, title=text, list=menu)
		elif entry == "limits":
			self.printMsg(_("Limit west"))
			self.diseqccommand("limitWest")
			self.statusMsg(_("West limit set"), timeout=self.STATUS_MSG_TIMEOUT)
		elif entry == "goto":
			self.printMsg(_("Goto 0"))
			self.diseqccommand("moveTo", 0)
			self.statusMsg(_("Moved to position 0"), timeout=self.STATUS_MSG_TIMEOUT)

	def yellowKey(self):
		if self.frontend is None:
			return
		entry = self.getCurrentConfigPath()
		if entry != "finemove":
			self.finesteps = 0
		if entry == "move":
			if self.isMoving:
				self.stopMoving()
			else:
				self.printMsg(_("Move east"))
				self.isMoving = True
				self.stopOnLock = True
				self.diseqccommand("moveEast", 0)
				self.statusMsg(_("Searching east..."), blinking=True)
			self.updateColors("move")
		elif entry == "finemove":
			self.finesteps -= 1
			self.printMsg(_("Step east"))
			self.diseqccommand("moveEast", 0xFF) # one step
			self.statusMsg(_("Stepped east") + self.stepCourse(self.finesteps), timeout=self.STATUS_MSG_TIMEOUT)
		elif entry == "storage":
			if not self.getUsals():
				self.printMsg(_("Goto index position"))
				index = int(self.positioner_storage.value)
				self.diseqccommand("moveTo", index)
				self.statusMsg((_("Moved to position at memory#") + " %2d") % index, timeout=self.STATUS_MSG_TIMEOUT)
		elif entry == "limits":
			self.printMsg(_("Limit east"))
			self.diseqccommand("limitEast")
			self.statusMsg(_("East limit set"), timeout=self.STATUS_MSG_TIMEOUT)
		elif entry == "goto":
			self.printMsg(_("Move to position X"))
			satlon = self.orbitalposition.float
			position = ("%5.1f %s") % (satlon, self.orientation.value)
			print((_("Satellite longitude:") + " %s") % position, file=log)
			satlon = PositionerSetup.orbital2metric(satlon, self.orientation.value)
			self.statusMsg((_("Moving to position") + " %s") % position, timeout=self.STATUS_MSG_TIMEOUT)
			self.gotoX(satlon)
		elif entry == "tune":
			# Start USALS calibration
			self.printMsg(_("USALS calibration"))
			print((_("Site latitude") + "      : %5.1f %s") % PositionerSetup.latitude2orbital(self.sitelat), file=log)
			print((_("Site longitude") + "     : %5.1f %s") % PositionerSetup.longitude2orbital(self.sitelon), file=log)
			Thread(target=self.gotoXcalibration).start()

	def blueKey(self):
		if self.frontend is None:
			return
		entry = self.getCurrentConfigPath()
		if entry != "finemove":
			self.finesteps = 0
		if entry == "move":
			if self.isMoving:
				self.stopMoving()
			else:
				self.printMsg(_("Move east"))
				self.diseqccommand("moveEast", 0)
				self.isMoving = True
				self.statusMsg(_("Moving east..."), blinking=True)
			self.updateColors("move")
		elif entry == "limits":
			self.printMsg(_("Limits on"))
			self.diseqccommand("limitOn")
			self.statusMsg(_("Limits enabled"), timeout=self.STATUS_MSG_TIMEOUT)
		elif entry == "tune":
			# Start (re-)calculate
			self.session.openWithCallback(self.recalcConfirmed, MessageBox, _("This will (re-)calculate all positions of your rotor and may remove previously memorised positions and fine-tuning!\nAre you sure?"), MessageBox.TYPE_YESNO, default=False)
		elif entry == "storage":
			if self.advanced and not self.getUsals():
				self.printMsg(_("Allocate unused memory index"))
				while True:
					if not len(self.allocatedIndices):
						for sat in self.availablesats:
							usals = self.advancedsats[sat].usals.value
							if not usals:
								current_index = int(self.advancedsats[sat].rotorposition.value)
								if current_index not in self.allocatedIndices:
									self.allocatedIndices.append(current_index)
						if len(self.allocatedIndices) == self.rotorPositions:
							self.statusMsg(_("No free index available"), timeout=self.STATUS_MSG_TIMEOUT)
							break
					index = 1
					for i in sorted(self.allocatedIndices):
						if i != index:
							break
						index += 1
					if index <= self.rotorPositions:
						self.positioner_storage.value = index
						self["list"].invalidateCurrent()
						self.allocatedIndices.append(index)
						self.statusMsg((_("Index allocated:") + " %2d") % index, timeout=self.STATUS_MSG_TIMEOUT)
						break
					else:
						self.allocatedIndices = []

	def recalcConfirmed(self, yesno):
		if yesno:
			self.printMsg(_("Calculate all positions"))
			print((_("Site latitude") + "      : %5.1f %s") % PositionerSetup.latitude2orbital(self.sitelat), file=log)
			print((_("Site longitude") + "     : %5.1f %s") % PositionerSetup.longitude2orbital(self.sitelon), file=log)
			lon = self.sitelon
			if lon >= 180:
				lon -= 360
			if lon < -30:	# americas, make unsigned binary west positive polarity
				lon = -lon
			lon = int(round(lon)) & 0xFF
			lat = int(round(self.sitelat)) & 0xFF
			index = int(self.positioner_storage.value) & 0xFF
			self.diseqccommand("calc", (((index << 8) | lon) << 8) | lat)
			self.statusMsg(_("Calculation complete"), timeout=self.STATUS_MSG_TIMEOUT)

	def showLog(self):
		self.session.open(PositionerSetupLog)

	def diseqccommand(self, cmd, param=0):
		# Every rotor command funnels through here, which makes it the one place
		# to mark the trend. The markers are what turn a rolling line into
		# something useful on a positioner: you can see whether the signal rose
		# or fell after each nudge, instead of guessing where a move landed.
		self._move_flag = True
		print("Diseqc(%s, %X)" % (cmd, param), file=log)
		self["rotorstatus"].setText("")
		self.diseqc.command(cmd, param)
		self.tuner.retune()

	def tune(self, transponder):
		# The Tune editor released and drove the frontend; reclaim it.
		if self.frontend is None:
			if self.openFrontend():
				self.diseqc = Diseqc(self.frontend)
				self.tuner = Tuner(self.frontend, ignore_rotor=True)
		# re-start the update timer
		self.statusTimer.start(self.UPDATE_INTERVAL, True)
		self.createSetup()
		if self.frontend is None or self.tuner is None:
			return	# couldn't reclaim a tuner (all in use); avoid touching a stale one
		if transponder is not None:
			self.tuner.tune(transponder)
			self.tuningChangedTo(transponder)
			self._lastTp = transponder
		elif self.frontend and self.tuner and getattr(self, "_lastTp", None) is not None:
			# Cancelled: restore the transponder we were on before the editor.
			self.tuner.tune(self._lastTp)
			self.tuningChangedTo(self._lastTp)
		if self.tuner is None or getattr(self.tuner, "lastparm", None) is None:
			return
		feparm = self.tuner.lastparm.getDVBS()
		orb_pos = feparm.orbital_position
		m = PositionerSetup.satposition2metric(orb_pos)
		self.orbitalposition.value = [int(m[0] / 10), m[0] % 10]
		self.orientation.value = m[1]
		if self.advanced:
			if orb_pos in self.availablesats:
				rotorposition = int(self.advancedsats[orb_pos].rotorposition.value)
				self.positioner_storage.value = rotorposition
				self.allocatedIndices = []
			self.setLNB(self.getLNBfromConfig(orb_pos))

	def furtherOptions(self):
		menu = []
		text = _("Select action")
		if self.session.nav.getCurrentlyPlayingServiceOrGroup() and not self.oldref_stop:
			menu.append((_("Stop live TV service"), self.stopService))
		description = _("Open setup tuner ") + "%s" % chr(0x41 + self.feid)
		menu.append((description, self.openTunerSetup))
		if not self.checkingTsidOnid and self.frontend and self.isLocked() and not self.isMoving:
			menu.append((_("Checking ONID/TSID"), self.openONIDTSIDScreen))

		def openAction(choice):
			if choice:
				choice[1]()
		self.session.openWithCallback(openAction, ChoiceBox, title=text, list=menu)

	def stopService(self):
		self.oldref = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		self.session.nav.stopService()
		self.oldref_stop = True

	def openTunerSetup(self):
		self.session.openWithCallback(self.closeTunerSetup, NimSetup, self.feid)

	def openONIDTSIDScreen(self):
		self.tsid = self.onid = 0
		self.session.openWithCallback(self.startChecktsidonid, ONIDTSIDScreen)

	def startChecktsidonid(self, tsidonid=None):
		if tsidonid is not None:
			self.onid = tsidonid[0]
			self.tsid = tsidonid[1]
			if self.frontend and self.isLocked() and not self.isMoving and hasattr(self, "raw_channel") and self.raw_channel:
				self.checkingTsidOnid = True
				self.raw_channel.receivedTsidOnid.get().append(self.gotTsidOnid)
				self.raw_channel.requestTsidOnid()

	def gotTsidOnid(self, tsid, onid):
		colors = parameters.get("PositionerOnidTsidcolors", (0x0000FF00, 0x00FF0000)) # "valid", "not valid"
		if tsid == self.tsid and onid == self.onid:
			msg = "\\c%08x" % colors[0] + _("This valid ONID/TSID")
		else:
			msg = "\\c%08x" % colors[1] + _("This not valid ONID/TSID")
		self.statusMsg(msg, blinking=True)
		if self.raw_channel:
			self.raw_channel.receivedTsidOnid.get().remove(self.gotTsidOnid)
		self.checkingTsidOnid = False

	def closeTunerSetup(self):
		self.restartPrevService(True)

	def isLocked(self):
		return self.frontendStatus.get("tuner_locked", 0) == 1

	# -----------------------------------------------------------------------
	# Peak hold, signal trend, audible tone
	# -----------------------------------------------------------------------
	# Everything below reads self.frontendStatus, which updateStatus() has
	# already refreshed from the frontend, so nothing here talks to hardware.
	#
	# updateStatus runs every UPDATE_INTERVAL (25ms = 40Hz), far faster than any
	# of this needs, so each job is throttled: tone ~8Hz, trend sampled at 2Hz
	# and redrawn at 1Hz, text only when it actually changes. The drivers only
	# refresh their own registers around 1Hz anyway.

	def _initInstruments(self):
		"""Post-layout: park the peak needles and paint an empty graph."""
		for name in ("snr_peak", "agc_peak"):
			inst = getattr(self[name], "instance", None)
			if inst is not None:
				inst.hide()
		self._updateLockPill()
		self._updatePeakText()
		self._drawTrend()

	def _resetInstruments(self):
		"""Drop peaks and history. Called when the tuning changes -- peak hold
		describes a transponder, so it must not carry across a retune to a
		different one, and equally must NOT be cleared just because the rotor
		broke lock while moving."""
		if not hasattr(self, "_trend"):
			return
		self._peak_snr_pct = 0
		self._peak_agc_pct = 0
		self._peak_snr_db = None
		del self._trend[:]
		self._instr_tick = 0
		for name in ("snr_peak", "agc_peak"):
			self._needle_x[name] = None
			inst = getattr(self[name], "instance", None)
			if inst is not None:
				inst.hide()
		self._updatePeakText()
		self._drawTrend()

	def _readSignal(self):
		"""(snr_pct, snr_db or None, agc_pct, locked) from the cached status.

		Same normalisation TunerInfo uses, so the graph never disagrees with the
		bars. SNR is reported as zero unless locked: it is meaningless below
		lock, and a demod can throw a full-scale reading for a tick or two while
		acquiring, which is enough to strand the peak needle at 99%.
		"""
		status = self.frontendStatus
		agc_raw = status.get("tuner_signal_power") or 0
		agc_pct = min(100, int(agc_raw) * 100 // 65536)
		if not self.isLocked():
			return 0, None, agc_pct, False
		snr_raw = status.get("tuner_signal_quality") or 0
		snr_pct = min(100, int(snr_raw) * 100 // 65536)
		raw_db = status.get("tuner_signal_quality_db")
		snr_db = None
		if raw_db is not None and 0 < raw_db <= 10000:
			snr_db = raw_db / 100.0
		return snr_pct, snr_db, agc_pct, True

	def _updateInstruments(self):
		snr_pct, snr_db, agc_pct, locked = self._readSignal()
		self._tone_locked = locked
		self._instr_tick += 1

		# One second of settling before anything can set a peak: a retune or a
		# rotor move throws transients on both readings.
		if self._instr_tick > 40:
			if agc_pct > self._peak_agc_pct:
				self._peak_agc_pct = agc_pct
			if snr_pct > self._peak_snr_pct:
				self._peak_snr_pct = snr_pct
			if snr_db is not None and (self._peak_snr_db is None or snr_db > self._peak_snr_db):
				self._peak_snr_db = snr_db

		self._moveNeedle("snr_peak", _POS_SNR_BAR_Y, self._peak_snr_pct)
		self._moveNeedle("agc_peak", _POS_AGC_BAR_Y, self._peak_agc_pct)
		self._updateLockPill()

		if self._tone is not None and self._instr_tick % 5 == 0:
			self._tone.update(agc_pct, snr_pct, locked)

		if self._instr_tick % 20 == 0:
			moved = self._move_flag
			self._move_flag = False
			self._trend.append((agc_pct, snr_pct, moved))
			if len(self._trend) > _POS_TREND_SAMPLES:
				del self._trend[0:len(self._trend) - _POS_TREND_SAMPLES]
		if self._instr_tick % 40 == 0:
			self._drawTrend()
			self._updatePeakText()
			self._updateToneStatus()

	def _updateLockPill(self):
		locked = self._tone_locked
		if locked == self._lock_shown:
			return
		self._lock_shown = locked
		for name, want in (("lock_yes", locked), ("lock_no", not locked)):
			inst = getattr(self[name], "instance", None)
			if inst is None:
				continue
			if want:
				inst.show()
			else:
				inst.hide()

	def _moveNeedle(self, name, y, pct):
		"""Slide a peak-hold needle along its track. Only moves when the pixel
		position actually changed, so a steady signal costs no repaints."""
		inst = getattr(self[name], "instance", None)
		if inst is None:
			return
		if pct <= 0:
			if self._needle_x[name] is not None:
				self._needle_x[name] = None
				inst.hide()
			return
		x = _POS_BAR_X + int((_POS_BAR_W - _POS_PEAK_W) * min(pct, 100) / 100.0)
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
		"""Repaint the trend, newest sample flush right, with a cyan tick on any
		sample during which a rotor command was issued."""
		if not POS_TREND_AVAILABLE or "trend" not in self:
			return
		canvas = self["trend"]
		plot_h = _POS_TREND_H - _POS_TREND_TOP
		try:
			canvas.fill(0, 0, _POS_TREND_W, _POS_TREND_H, 0x00101a1e)
			for frac in (0.25, 0.5, 0.75):
				canvas.fill(0, _POS_TREND_H - int(frac * plot_h), _POS_TREND_W, 1, 0x001d2c33)
			canvas.fill(0, _POS_TREND_H - 1, _POS_TREND_W, 1, 0x001d2c33)
			count = len(self._trend)
			for i in range(count):
				agc, snr, moved = self._trend[count - 1 - i]
				x = _POS_TREND_W - (i + 1) * _POS_TREND_COL
				if x < 0:
					break
				if moved:
					canvas.fill(x, _POS_TREND_TOP, 2, plot_h, 0x0000c8d4)
				if agc > 0:
					height = max(3, int(agc * plot_h / 100))
					canvas.fill(x, _POS_TREND_H - height, _POS_TREND_COL, height, 0x00161206)
					canvas.fill(x, _POS_TREND_H - height, _POS_TREND_COL, 3, 0x00ffb020)
				if snr > 0:
					top = _POS_TREND_H - max(3, int(snr * plot_h / 100))
					canvas.fill(x, top, _POS_TREND_COL, 3, 0x0043c95a)
			canvas.flush()
		except Exception as e:
			print("[PositionerSetup][trend] draw failed: %s" % e)

	def keyToneCycle(self):
		"""AUDIO: step the sound level Off -> Low -> Medium -> High -> Off."""
		if toneConfig is None:
			return
		cfg = toneConfig()
		cfg.level.value = _tone_cycle_level(cfg.level.value)
		cfg.level.save()
		configfile.save()
		self._tone_error = None
		self._toneApply()

	def keyNoLockCycle(self):
		"""TEXT: switch the no-lock sound between the search pulse and silence."""
		if toneConfig is None:
			return
		cfg = toneConfig()
		cfg.nolock.value = _tone_cycle_nolock(cfg.nolock.value)
		cfg.nolock.save()
		configfile.save()
		if self._tone is not None:
			self._tone.setSearchEnabled(cfg.nolock.value == "search")
		self._updateToneStatus()

	def _toneApply(self):
		if toneConfig is None:
			self._updateToneStatus()
			return
		cfg = toneConfig()
		try:
			level = int(cfg.level.value)
		except (TypeError, ValueError):
			level = 0
		if level <= 0 or not POS_TONE_AVAILABLE:
			self._toneStop()
		elif self._tone is None:
			tone = SignalTone(volume=level / 100.0)
			tone.setSearchEnabled(cfg.nolock.value == "search")
			if tone.start():
				self._tone = tone
			else:
				self._tone_error = tone.error
				print("[PositionerSetup] signal tone failed to start: %s" % tone.error)
		else:
			self._tone.setVolume(level / 100.0)
			self._tone.setSearchEnabled(cfg.nolock.value == "search")
			self._tone.resume()
		self._updateToneStatus()

	def _tonePause(self):
		if self._tone is not None:
			self._tone.pause()

	def _toneStop(self):
		tone, self._tone = getattr(self, "_tone", None), None
		if tone is not None:
			tone.stop()

	def _updateToneStatus(self):
		"""One line in the trend caption band. Doubles as the only hint that the
		AUDIO and TEXT keys do anything, so it says something when sound is off."""
		if toneConfig is None or not POS_TONE_AVAILABLE:
			text = _("Sound: not available on this box")
		elif self._tone_error:
			text = _("Sound: failed (%s)") % self._tone_error
		else:
			cfg = toneConfig()
			if cfg.level.value == "0" or self._tone is None:
				text = _("AUDIO: sound off")
			else:
				if self._tone_locked:
					now = _("SNR tone")
				elif cfg.nolock.value == "search":
					now = _("AGC search pulse")
				else:
					now = _("silent, no lock")
				text = "%s: %s   |   %s: %s   |   %s" % (
					_("Sound"), _tone_level_name(cfg.level.value),
					_("No lock"), _tone_nolock_name(cfg.nolock.value), now)
		if text != self._tone_shown:
			self._tone_shown = text
			self["tone_status"].setText(text)

	def statusMsg(self, msg, blinking=False, timeout=0):			# timeout in seconds
		self.statusMsgBlinking = blinking
		if not blinking:
			self["status_bar"].visible = True
		self["status_bar"].setText(msg)
		self.statusMsgTimeoutTicks = (timeout * 1000 + self.UPDATE_INTERVAL / 2) / self.UPDATE_INTERVAL

	def retuneKeepAlive(self):
		# Keep the demod's acquisition window alive while unlocked, so
		# below-lock signal readings stay live between manual dish moves.
		# As soon as a tune attempt times out (FAILED) we re-arm it after a
		# short debounce, chaining acquisition windows back to back; the only
		# remaining display gap is the demod's own reset/estimation time.
		if self.frontendStatus.get("tuner_locked", 0) == 1 or self.frontendStatus.get("tuner_state", "") == "TUNING":
			self.retuneKeepAliveTicks = 0
			return
		below_lock = getattr(config.Nims[self.feid], "show_signal_below_lock", None)
		if below_lock is not None and not below_lock.value:
			return	# feature disabled for this NIM, nothing to keep alive
		self.retuneKeepAliveTicks += 1
		if self.retuneKeepAliveTicks * self.UPDATE_INTERVAL >= self.RETUNE_KEEPALIVE_INTERVAL:
			self.retuneKeepAliveTicks = 0
			if getattr(self.tuner, "lastparm", None):
				self.tuner.retune()

	def updateStatus(self):
		self.statusTimer.start(self.UPDATE_INTERVAL, True)
		if self.frontend:
			self.frontend.getFrontendStatus(self.frontendStatus)
			if self.rotor_diseqc and not self.collectingStatistics:
				self.retuneKeepAlive()
		if self.rotor_diseqc:
			self["snr_db"].update()
			self["snr_percentage"].update()
			self["ber_value"].update()
			self["snr_bar"].update()
			self["agc_percentage"].update()
			self["agc_bar"].update()
			self["ber_bar"].update()
			self["lock_state"].update()
			self._updateInstruments()
			if self["lock_state"].getValue(TunerInfo.LOCK):
				self["lock_on"].show()
			else:
				self["lock_on"].hide()
		if self.statusMsgBlinking:
			self.statusMsgBlinkCount += 1
			if self.statusMsgBlinkCount == self.statusMsgBlinkRate:
				self.statusMsgBlinkCount = 0
				self["status_bar"].visible = not self["status_bar"].visible
		if self.statusMsgTimeoutTicks > 0:
			self.statusMsgTimeoutTicks -= 1
			if self.statusMsgTimeoutTicks == 0:
				self["status_bar"].setText("")
				self.statusMsgBlinking = False
				self["status_bar"].visible = True
		if self.isLocked() and self.isMoving and self.stopOnLock:
			self.stopMoving()
			self.updateColors(self.getCurrentConfigPath())
		if self.collectingStatistics:
			self.low_rate_adapter_count += 1
			if self.low_rate_adapter_count == self.MAX_LOW_RATE_ADAPTER_COUNT:
				self.low_rate_adapter_count = 0
				self.snr_percentage += self["snr_percentage"].getValue(TunerInfo.SNR)
				self.lock_count += self["lock_state"].getValue(TunerInfo.LOCK)
				self.stat_count += 1
				if self.stat_count == self.max_count:
					self.collectingStatistics = False
					count = float(self.stat_count)
					self.lock_count /= count
					self.snr_percentage *= 100.0 / 0x10000 / count
					self.dataAvailable.set()

	def tuningChangedTo(self, tp):
		# New transponder: old peaks and history describe something else.
		self._resetInstruments()

		def setLowRateAdapterCount(symbolrate):
			# change the measurement time and update interval in case of low symbol rate,
			# since more time is needed for the front end in that case.
			# It is an heuristic determination without any pretence. For symbol rates
			# of 5000 the interval is multiplied by 3 until 15000 which is seen
			# as a high symbol rate. Linear interpolation elsewhere.
			return max(int(round((3 - 1) * (symbolrate - 7500) / (2500 - 7500) + 1)), 1)
#### 			return max(int(round((3 - 1) * (symbolrate - 15000) / (5000 - 15000) + 1)), 1)
		self.symbolrate = tp[1]
		self.polarisation = tp[2]
		self.MAX_LOW_RATE_ADAPTER_COUNT = setLowRateAdapterCount(self.symbolrate)
		if len(self.tuner.getTransponderData()):
			transponderdata = ConvertToHumanReadable(self.tuner.getTransponderData(), "DVB-S")
		else:
			transponderdata = {}
		polarization_text = ""
		polarization = transponderdata.get("polarization")
		if polarization:
			polarization_text = str(polarization)
			if polarization_text == _("Horizontal"):
				polarization_text = " H"
			elif polarization_text == _("Vertical"):
				polarization_text = " V"
			elif polarization_text == _("Circular right"):
				polarization_text = " R"
			elif polarization_text == _("Circular left"):
				polarization_text = " L"
		frequency_text = ""
		frequency = transponderdata.get("frequency")
		if frequency:
			freq_mhz = frequency / 1000.0
			frequency_text = ("%.3f" % freq_mhz).rstrip("0").rstrip(".") + polarization_text
		self["frequency_value"].setText(frequency_text)
		symbolrate_text = ""
		symbolrate = transponderdata.get("symbol_rate")
		if symbolrate:
			symbolrate_text = str(symbolrate // 1000)
		self["symbolrate_value"].setText(symbolrate_text)
		fec_text = ""
		fec_inner = transponderdata.get("fec_inner")
		if fec_inner:
			if frequency and symbolrate:
				fec_text = str(fec_inner)
		self["fec_value"].setText(fec_text)

	@staticmethod
	def rotorCmd2Step(rotorCmd, stepsize):
		return round(float(rotorCmd & 0xFFF) / 0x10 / stepsize) * (1 - ((rotorCmd & 0x1000) >> 11))

	@staticmethod
	def gotoXcalc(satlon, sitelat, sitelon):
		def azimuth2Rotorcode(angle):
			gotoXtable = (0x00, 0x02, 0x03, 0x05, 0x06, 0x08, 0x0A, 0x0B, 0x0D, 0x0E)
			a = int(round(abs(angle) * 10.0))
			return ((a // 10) << 4) + gotoXtable[a % 10]

		satHourAngle = rotor_calc.calcSatHourangle(satlon, sitelat, sitelon)
		if sitelat >= 0: # Northern Hemisphere
			rotorCmd = azimuth2Rotorcode(180 - satHourAngle)
			if satHourAngle <= 180: # the east
				rotorCmd |= 0xE000
			else:					# west
				rotorCmd |= 0xD000
		else: # Southern Hemisphere
			if satHourAngle <= 180: # the east
				rotorCmd = azimuth2Rotorcode(satHourAngle) | 0xD000
			else: # west
				rotorCmd = azimuth2Rotorcode(360 - satHourAngle) | 0xE000
		return rotorCmd

	def gotoX(self, satlon):
		rotorCmd = PositionerSetup.gotoXcalc(satlon, self.sitelat, self.sitelon)
		self.diseqccommand("gotoX", rotorCmd)
		x = PositionerSetup.rotorCmd2Step(rotorCmd, self.tuningstepsize)
		print((_("Rotor step position:") + " %4d") % x, file=log)
		return x

	def getTurningspeed(self):
		if self.polarisation == eDVBFrontendParametersSatellite.Polarisation_Horizontal:
			turningspeed = self.turningspeedH
		else:
			turningspeed = self.turningspeedV
		return max(turningspeed, 0.1)

	TURNING_START_STOP_DELAY = 1.600	# seconds
	MAX_SEARCH_ANGLE = 12.0				# degrees
	MAX_FOCUS_ANGLE = 6.0				# degrees
	LOCK_LIMIT = 0.1					# ratio
	MEASURING_TIME = .800				# seconds
####

	def measure(self, time=MEASURING_TIME):	# time in seconds
		self.snr_percentage = 0.0
		self.lock_count = 0.0
		self.stat_count = 0
		self.low_rate_adapter_count = 0
		self.max_count = max(int((time * 1000 + self.UPDATE_INTERVAL / 2) / self.UPDATE_INTERVAL), 1)
		self.collectingStatistics = True
		self.dataAvailable.clear()
		self.dataAvailable.wait()

	def logMsg(self, msg, timeout=0):
		self.statusMsg(msg, timeout=timeout)
		self.printMsg(msg)

	def sync(self):
		self.lock_count = 0.0
		n = 0
		while self.lock_count < (1 - self.LOCK_LIMIT) and n < 5:
			self.measure(time=0.500)
			n += 1
		if self.lock_count < (1 - self.LOCK_LIMIT):
			return False
		return True

	randomGenerator = None

	def randomBool(self):
		if self.randomGenerator is None:
			self.randomGenerator = SystemRandom()
		return self.randomGenerator.random() >= 0.5

	def gotoXcalibration(self):

		def move(x):
			z = self.gotoX(x + satlon)
			time = int(abs(x - prev_pos) / turningspeed + 2 * self.TURNING_START_STOP_DELAY)
			sleep(time * self.MAX_LOW_RATE_ADAPTER_COUNT)
			return z

		def reportlevels(pos, level, lock):
			print((_("Signal quality") + " %5.1f" + chr(176) + "   : %6.2f") % (pos, level), file=log)
			print((_("Lock ratio") + "     %5.1f" + chr(176) + "   : %6.2f") % (pos, lock), file=log)

		def optimise(readings):
			xi = [*readings]
			yi = list(map(lambda x: x[0], readings.values()))
			x0 = sum(map(mul, xi, yi)) / sum(yi)
			xm = xi[yi.index(max(yi))]
			return (x0, xm)

		def toGeopos(x):
			if x < 0:
				return _("W")
			else:
				return _("E")

		def toGeoposEx(x):
			if x < 0:
				return _("west")
			else:
				return _("east")

		self.logMsg(_("GotoX calibration"))
		satlon = self.orbitalposition.float
		print((_("Satellite longitude:") + " %5.1f" + chr(176) + " %s") % (satlon, self.orientation.value), file=log)
		satlon = PositionerSetup.orbital2metric(satlon, self.orientation.value)
		prev_pos = 0.0						# previous relative position w.r.t. satlon
		turningspeed = self.getTurningspeed()

		x = 0.0								# relative position w.r.t. satlon
		dir = 1
		if self.randomBool():
			dir = -dir
		while abs(x) < self.MAX_SEARCH_ANGLE:
			if self.sync():
				break
			x += (1.0 * dir)						# one degree east/west
			self.statusMsg((_("Searching") + " " + toGeoposEx(dir) + " %2d" + chr(176)) % abs(x), blinking=True)
			move(x)
			prev_pos = x
		else:
			x = 0.0
			dir = -dir
			while abs(x) < self.MAX_SEARCH_ANGLE:
				x += (1.0 * dir)					# one degree east/west
				self.statusMsg((_("Searching") + " " + toGeoposEx(dir) + " %2d" + chr(176)) % abs(x), blinking=True)
				move(x)
				prev_pos = x
				if self.sync():
					break
			else:
				msg = _("Cannot find any signal ..., aborting !")
				self.printMsg(msg)
				self.statusMsg("")
				self.session.open(MessageBox, msg, MessageBox.TYPE_ERROR)
				return
		x = round(x / self.tuningstepsize) * self.tuningstepsize
		move(x)
		prev_pos = x
		measurements = {}
		self.measure()
		print((_("Initial signal quality") + " %5.1f" + chr(176) + ": %6.2f") % (x, self.snr_percentage), file=log)
		print((_("Initial lock ratio") + "     %5.1f" + chr(176) + ": %6.2f") % (x, self.lock_count), file=log)
		measurements[x] = (self.snr_percentage, self.lock_count)

		start_pos = x
		x = 0.0
		dir = 1
		if self.randomBool():
			dir = -dir
		while x < self.MAX_FOCUS_ANGLE:
			x += self.tuningstepsize * dir					# one step east/west
			self.statusMsg((_("Moving") + " " + toGeoposEx(dir) + " %5.1f" + chr(176)) % abs(x + start_pos), blinking=True)
			move(x + start_pos)
			prev_pos = x + start_pos
			self.measure()
			measurements[x + start_pos] = (self.snr_percentage, self.lock_count)
			reportlevels(x + start_pos, self.snr_percentage, self.lock_count)
			if self.lock_count < self.LOCK_LIMIT:
				break
		else:
			msg = _("Cannot determine") + " " + toGeoposEx(dir) + " " + _("limit ..., aborting !")
			self.printMsg(msg)
			self.statusMsg("")
			self.session.open(MessageBox, msg, MessageBox.TYPE_ERROR)
			return
		x = 0.0
		dir = -dir
		self.statusMsg((_("Moving") + " " + toGeoposEx(dir) + " %5.1f" + chr(176)) % abs(start_pos), blinking=True)
		move(start_pos)
		prev_pos = start_pos
		if not self.sync():
			msg = _("Sync failure moving back to origin !")
			self.printMsg(msg)
			self.statusMsg("")
			self.session.open(MessageBox, msg, MessageBox.TYPE_ERROR)
			return
		while abs(x) < self.MAX_FOCUS_ANGLE:
			x += self.tuningstepsize * dir					# one step west/east
			self.statusMsg((_("Moving") + " " + toGeoposEx(dir) + " %5.1f" + chr(176)) % abs(x + start_pos), blinking=True)
			move(x + start_pos)
			prev_pos = x + start_pos
			self.measure()
			measurements[x + start_pos] = (self.snr_percentage, self.lock_count)
			reportlevels(x + start_pos, self.snr_percentage, self.lock_count)
			if self.lock_count < self.LOCK_LIMIT:
				break
		else:
			msg = _("Cannot determine") + " " + toGeoposEx(dir) + " " + _("limit ..., aborting !")
			self.printMsg(msg)
			self.statusMsg("")
			self.session.open(MessageBox, msg, MessageBox.TYPE_ERROR)
			return
		(x0, xm) = optimise(measurements)
		x = move(x0)
		if satlon > 180:
			satlon -= 360
		x0 += satlon
		xm += satlon
		print((_("Weighted position") + "     : %5.1f" + chr(176) + " %s") % (abs(x0), toGeopos(x0)), file=log)
		print((_("Strongest position") + "    : %5.1f" + chr(176) + " %s") % (abs(xm), toGeopos(xm)), file=log)
		self.logMsg((_("Final position at") + " %5.1f" + chr(176) + " %s / %d; " + _("offset is") + " %4.1f" + chr(176)) % (abs(x0), toGeopos(x0), x, x0 - satlon))



	def autofocus(self):

		def move(x):
			if x > 0:
				self.diseqccommand("moveEast", (-x) & 0xFF)
			elif x < 0:
				self.diseqccommand("moveWest", x & 0xFF)
			if x != 0:
				sleep(.8) #(time * self.MAX_LOW_RATE_ADAPTER_COUNT)
####

		def reportlevels(pos, level, lock):
			print((_("Signal quality") + " [%2d]   : %6.2f") % (pos, level), file=log)
			print((_("Lock ratio") + " [%2d]       : %6.2f") % (pos, lock), file=log)


		def optimise(readings):
			xi = [*readings]
			yi = list(map(lambda x: x[0], readings.values()))
	####
			try:
				x0 = int(round(sum(map(mul, xi, yi)) / sum(yi)))
			except:
				f = open('/tmp/positionersetup.log', 'w')
				f.write(log.getvalue())
				f.close()
				msg = _("Cannot determine") + " " + toGeoposEx(dir) + " " + _("limit ..., aborting !")
				self.printMsg(msg)
				self.statusMsg("")
				self.session.open(MessageBox, msg, MessageBox.TYPE_ERROR)
				return
	####
			xm = xi[yi.index(max(yi))]
			return (x0, xm)

		def toGeoposEx(x):
			if x < 0:
				return _("west")
			else:
				return _("east")

		self.logMsg(_("Auto focus commencing..."))
		measurements = {}
		maxsteps = 200 #max(min(round(self.MAX_FOCUS_ANGLE / self.tuningstepsize), 0x1F), 3)
		self.measure()
		print((_("Initial signal quality:") + " %6.2f") % self.snr_percentage, file=log)
		print((_("Initial lock ratio") + "    : %6.2f") % self.lock_count, file=log)
		if self.lock_count < 1 - self.LOCK_LIMIT:
			msg = _("There is no signal to lock on !")
			self.printMsg(msg)
			self.statusMsg("")
			self.session.open(MessageBox, msg, MessageBox.TYPE_ERROR)
			return
		print(_("Signal OK, proceeding"), file=log)
		x = 0
		dir = 1
		if self.randomBool():
			dir = -dir
		measurements[x] = (self.snr_percentage, self.lock_count)
		nsteps = 0
		while nsteps < maxsteps:
			x += dir
			self.statusMsg((_("Moving") + " " + toGeoposEx(dir) + " %2d") % abs(x), blinking=True)
			move(dir) 		# one step
			self.measure()
			measurements[x] = (self.snr_percentage, self.lock_count)
			reportlevels(x, self.snr_percentage, self.lock_count)
			if self.lock_count < self.LOCK_LIMIT:
				break
			nsteps += 1
		else:
			msg = _("Cannot determine") + " " + toGeoposEx(dir) + " " + _("limit ..., aborting !")
			self.printMsg(msg)
			self.statusMsg("")
			self.session.open(MessageBox, msg, MessageBox.TYPE_ERROR)
			return
		dir = -dir
		self.statusMsg(_("Moving") + " " + toGeoposEx(dir) + "  0", blinking=True)
		move(-x)
		if not self.sync():
			msg = _("Sync failure moving back to origin !")
			self.printMsg(msg)
			self.statusMsg("")
			self.session.open(MessageBox, msg, MessageBox.TYPE_ERROR)
			return
		x = 0
		nsteps = 0
		while nsteps < maxsteps:
			x += dir
			self.statusMsg((_("Moving") + " " + toGeoposEx(dir) + " %2d") % abs(x), blinking=True)
			move(dir) 		# one step
			self.measure()
			measurements[x] = (self.snr_percentage, self.lock_count)
			reportlevels(x, self.snr_percentage, self.lock_count)
			if self.lock_count < self.LOCK_LIMIT:
				break
			nsteps += 1
		else:
			msg = _("Cannot determine") + " " + toGeoposEx(dir) + " " + _("limit ..., aborting !")
			self.printMsg(msg)
			self.statusMsg("")
			self.session.open(MessageBox, msg, MessageBox.TYPE_ERROR)
			return
		(x0, xm) = optimise(measurements)
		print((_("Weighted position") + "     : %2d") % x0, file=log)
		print((_("Strongest position") + "    : %2d") % xm, file=log)
		if x0 == 0:
			self.logMsg(_("Position Calibrated Correctly!"))
		if x0 < 0:
			self.logMsg((_("Final Rotor Position =  %d") + " (West) ") % (x0))
		if x0 > 0:
			self.logMsg((_("Final Rotor Position =  %d") + " (East) ") % (x0))
		move(x0 - x)
####  print(_("Signal OK, proceeding"), file=log)
		print("self.orbitalposition, self.tp =", self.orbitalposition.float, self.tp)		
		try:
			if os.path.exists("/media/usb/positionersetup"):
				xml_dir = "/media/usb/positionersetup"
			if os.path.exists("/media/FTA/positionersetup"):
				xml_dir = "/media/FTA/positionersetup" 
			if os.path.exists("/hdd/positionersetup"):
				xml_dir = "/hdd/positionersetup"
		except:
			xml_dir = "/tmp" 

		location = '%s/positionersetup_%s_%s_%s.log' %(xml_dir, self.orbitalposition.float, self.tp, strftime("%d-%m-%Y_%H-%M-%S"))
		try:
			f = open(location, 'w')
			f.write(log.getvalue())
			f.close()
		except :
			return
####

class Diseqc:
	def __init__(self, frontend):
		self.frontend = frontend

	def command(self, what, param=0):
		if self.frontend:
			cmd = eDVBDiseqcCommand()
			if what == "moveWest":
				string = 'E03169' + ("%02X" % param)
			elif what == "moveEast":
				string = 'E03168' + ("%02X" % param)
			elif what == "moveTo":
				string = 'E0316B' + ("%02X" % param)
			elif what == "store":
				string = 'E0316A' + ("%02X" % param)
			elif what == "gotoX":
				string = 'E0316E' + ("%04X" % param)
			elif what == "calc":
				string = 'E0316F' + ("%06X" % param)
			elif what == "limitOn":
				string = 'E0316A00'
			elif what == "limitOff":
				string = 'E03163'
			elif what == "limitEast":
				string = 'E03166'
			elif what == "limitWest":
				string = 'E03167'
			else:
				string = 'E03160' #positioner stop

			print("diseqc command:", end=' ')
			print(string)
			cmd.setCommandString(string)
			self.frontend.setTone(iDVBFrontend.toneOff)
			sleep(0.015) # wait 15msec after disable tone
			self.frontend.sendDiseqc(cmd)
			if string == 'E03160': #positioner stop
				sleep(0.050)
				self.frontend.sendDiseqc(cmd) # send 2nd time


class PositionerSetupLog(Screen):
	skin = """
		<screen position="center,center" size="560,400" title="Positioner setup log" >
			<ePixmap name="red"    position="0,0"   zPosition="2" size="140,40" pixmap="buttons/red.png" transparent="1" alphatest="on" />
			<ePixmap name="green"  position="230,0" zPosition="2" size="140,40" pixmap="buttons/green.png" transparent="1" alphatest="on" />
			<ePixmap name="blue"   position="420,0" zPosition="2" size="140,40" pixmap="buttons/blue.png" transparent="1" alphatest="on" />

			<widget name="key_red" position="0,0" size="140,40" valign="center" halign="center" zPosition="4"  foregroundColor="white" font="Regular;20" transparent="1" shadowColor="background" shadowOffset="-2,-2" />
			<widget name="key_green" position="230,0" size="140,40" halign="center" valign="center"  zPosition="4"  foregroundColor="white" font="Regular;20" transparent="1" shadowColor="background" shadowOffset="-2,-2" />
			<widget name="key_blue" position="420,0" size="140,40" valign="center" halign="center" zPosition="4"  foregroundColor="white" font="Regular;20" transparent="1" shadowColor="background" shadowOffset="-2,-2" />

			<ePixmap alphatest="on" pixmap="icons/clock.png" position="480,383" size="14,14" zPosition="3"/>
			<widget font="Regular;18" halign="left" position="505,380" render="Label" size="55,20" source="global.CurrentTime" transparent="1" valign="center" zPosition="3">
				<convert type="ClockToText">Default</convert>
			</widget>
			<widget name="list" font="Regular;16" position="10,40" size="540,340" />
		</screen>"""

	def __init__(self, session):
		Screen.__init__(self, session)
		self.setTitle(_("Positioner setup log"))
		self["key_red"] = Button(_("Exit"))
		self["key_green"] = Button(_("Save"))
		self["key_blue"] = Button(_("Clear"))
		self["list"] = ScrollLabel(log.getvalue())
		self["actions"] = ActionMap(["DirectionActions", "OkCancelActions", "ColorActions"],
		{
			"red": self.cancel,
			"green": self.save,
			"save": self.save,
			"blue": self.clear,
			"cancel": self.cancel,
			"ok": self.cancel,
			"left": self["list"].pageUp,
			"right": self["list"].pageDown,
			"up": self["list"].pageUp,
			"down": self["list"].pageDown,
			"pageUp": self["list"].pageUp,
			"pageDown": self["list"].pageDown
		}, -2)

	def save(self):
		try:
			f = open('/tmp/positionersetup.log', 'w')
			f.write(log.getvalue())
			f.close()
			self.session.open(MessageBox, _("Write to /tmp/positionersetup.log"), MessageBox.TYPE_INFO)
		except Exception as e:
			self["list"].setText(_("Failed to write /tmp/positionersetup.log: ") + str(e))
		self.close(True)

	def cancel(self):
		self.close(False)

	def clear(self):
		log.logfile.seek(0)
		log.logfile.truncate()
		self.close(False)


class ONIDTSIDScreen(ConfigListScreen, Screen):
	skin = """
		<screen position="center,center" size="520,250" title="Tune">
			<ePixmap pixmap="buttons/red.png" position="0,0" size="140,40" alphatest="on"/>
			<ePixmap pixmap="buttons/green.png" position="140,0" size="140,40" alphatest="on"/>
			<widget source="key_red" render="Label" position="0,0" zPosition="1" size="140,40" font="Regular;20" halign="center" valign="center" backgroundColor="#9f1313" transparent="1"/>
			<widget source="key_green" render="Label" position="140,0" zPosition="1" size="140,40" font="Regular;20" halign="center" valign="center" backgroundColor="#1f771f" transparent="1"/>
			<widget name="config" position="10,50" size="500,150" scrollbarMode="showOnDemand" />
			<widget name="introduction" position="60,220" size="450,23" halign="left" font="Regular;20" />
		</screen>"""

	def __init__(self, session):
		Screen.__init__(self, session)
		self.skinName = ["ONIDTSIDScreen", "TunerScreen"]
		self.setTitle(_("Enter valid ONID/TSID"))
		ConfigListScreen.__init__(self, None)
		self.transponderTsid = NoSave(ConfigInteger(default=0, limits=(0, 65535)))
		self.transponderOnid = NoSave(ConfigInteger(default=0, limits=(0, 65535)))
		self.createSetup()
		self["actions"] = NumberActionMap(["SetupActions", "ColorActions"],
		{
			"ok": self.keyGo,
			"cancel": self.keyCancel,
			"red": self.keyCancel,
			"green": self.keyGo,
		}, -2)

		self["key_red"] = StaticText(_("Cancel"))
		self["key_green"] = StaticText(_("OK"))
		self["introduction"] = Label(_("Valid ONID/TSID look at www.lyngsat.com..."))

	def createSetup(self):
		self.list = []
		self.list.append(getConfigListEntry(_("ONID"), self.transponderOnid))
		self.list.append(getConfigListEntry(_("TSID"), self.transponderTsid))
		self["config"].list = self.list

	def keyGo(self):
		onid = int(self.transponderOnid.value)
		tsid = int(self.transponderTsid.value)
		if onid == 0 and tsid == 0:
			self.close(None)
		else:
			returnvalue = (onid, tsid)
			self.close(returnvalue)

	def keyCancel(self):
		self.close(None)


class TunerScreen(ConfigListScreen, Screen):
	# Self-contained override (unique skinName) -> no installed skin's "Tune"
	# screen applies, which also removes the duplicated/ghosted title the default
	# window decoration was drawing. wfNoBorder + solid background.
	#
	# Live signal works the same way Satfinder's "user defined transponder" does
	# (replicated here, NOT imported, so there is no dependency on Satfinder
	# being installed): the screen allocates its OWN frontend, drives it with a
	# Tuner, and reads it through a FrontendStatus source feeding the gradient
	# Progress bars. The positioner releases its frontend before opening this
	# editor and reclaims it on return.
	skin = ("""
		<screen name="TNAP_TunerScreen" position="0,0" size="1920,1080" title="Tune" flags="wfNoBorder" backgroundColor="#00000000" resolution="1920,1080">
			<eLabel position="0,0" size="1920,1080" backgroundColor="#00000000" zPosition="-2"/>

			<widget source="Title" render="Label" position="30,22" size="1500,66" font="Regular;46" foregroundColor="#00f0f0f0" transparent="1" valign="center" halign="left" noWrap="1"/>
			<widget source="global.CurrentTime" render="Label" position="1430,18" size="460,56" font="Regular;46" foregroundColor="#00f0f0f0" transparent="1" halign="right" valign="center">
				<convert type="ClockToText">Format:%H:%M</convert>
			</widget>
			<widget source="global.CurrentTime" render="Label" position="1230,78" size="660,40" font="Regular;30" foregroundColor="#00b6b6b6" transparent="1" halign="right" valign="center">
				<convert type="ClockToText">Date</convert>
			</widget>
			<eLabel position="0,124" size="1920,2" backgroundColor="#00303030" zPosition="-1"/>

			<widget source="Frontend" render="Progress" pixmap="__BAR__" position="30,150" size="1860,75" borderWidth="1" borderColor="#00808888" foregroundColor="#0056c856">
				<convert type="FrontendInfo">SNR</convert>
			</widget>
			<eLabel text="SNR:" position="37,150" size="150,75" valign="center" transparent="1" foregroundColor="#00f0f0f0" font="Regular;52" zPosition="2"/>
			<widget source="Frontend" render="Label" position="1552,150" size="330,75" halign="right" valign="center" transparent="1" foregroundColor="#00f0f0f0" font="Regular;52" zPosition="2">
				<convert type="FrontendInfo">SNR</convert>
			</widget>

			<widget source="Frontend" render="Progress" pixmap="__BAR__" position="30,240" size="1860,75" borderWidth="1" borderColor="#00808888" foregroundColor="#0056c856">
				<convert type="FrontendInfo">AGC</convert>
			</widget>
			<eLabel text="AGC:" position="37,240" size="150,75" valign="center" transparent="1" foregroundColor="#00f0f0f0" font="Regular;52" zPosition="2"/>
			<widget source="Frontend" render="Label" position="1552,240" size="330,75" halign="right" valign="center" transparent="1" foregroundColor="#00f0f0f0" font="Regular;52" zPosition="2">
				<convert type="FrontendInfo">AGC</convert>
			</widget>

			<eLabel text="SNR:" position="30,355" size="200,30" transparent="1" zPosition="5" font="Regular;27"/>
			<widget source="Frontend" render="Label" position="30,385" size="410,95" font="Regular;78" halign="left" transparent="1">
				<convert type="FrontendInfo">SNRdB</convert>
			</widget>
			<widget text="LOCK" source="Frontend" render="FixedLabel" position="30,520" size="410,70" font="Regular;56" halign="left" foregroundColor="#0056c856" transparent="1">
				<convert type="FrontendInfo">LOCK</convert>
				<convert type="ConditionalShowHide"/>
			</widget>

			<widget name="config" position="470,360" size="1420,520" itemHeight="49" font="Regular;40" valueFont="Regular;36" transparent="1" enableWrapAround="1" scrollbarMode="showOnDemand"/>
			<widget name="introduction" position="470,905" size="1420,40" font="Regular;30" halign="center" valign="center" transparent="1" foregroundColor="#00F9C731"/>

			<eLabel position="240,1035" size="30,30" backgroundColor="#00ff4a3c" zPosition="2"/>
			<widget source="key_red" render="Label" position="285,1030" size="300,40" font="Regular;34" foregroundColor="#00f0f0f0" transparent="1" valign="center" halign="left"/>
			<eLabel position="620,1035" size="30,30" backgroundColor="#0056c856" zPosition="2"/>
			<widget source="key_green" render="Label" position="665,1030" size="300,40" font="Regular;34" foregroundColor="#00f0f0f0" transparent="1" valign="center" halign="left"/>
		</screen>""".replace("__BAR__", _POS_BAR_PIXMAP))

	STATUS_INTERVAL = 500		# ms, frontend-state poll (for retune-on-failure)
	RETUNE_DEBOUNCE = 300		# ms, settle time after a config edit before retuning

	def __init__(self, session, feid, fe_data):
		self.feid = feid
		self.fe_data = fe_data
		self.frontend = None
		self.raw_channel = None
		self.tuner = None
		Screen.__init__(self, session)
		self.skinName = ["TNAP_TunerScreen"]
		self.setTitle(_("Tune"))
		ConfigListScreen.__init__(self, None)
		self.createConfig(fe_data)
		self.initialSetup()
		self.createSetup()

		# FrontendStatus polls our own frontend and feeds the gradient bars.
		self["Frontend"] = FrontendStatus(frontend_source=lambda: self.frontend, update_interval=100)

		self.statusTimer = eTimer()
		self.statusTimer.callback.append(self._updateStatus)
		self.retuneTimer = eTimer()
		self.retuneTimer.callback.append(self._retune)

		self.tuning.sat.addNotifier(self.tuningSatChanged)
		self.tuning.type.addNotifier(self.tuningTypeChanged)
		self.scan_sat.system.addNotifier(self.systemChanged)
		# Re-tune live whenever a tuning field changes (digit entry doesn't go
		# through keyLeft/keyRight). initial_call=False avoids firing at init.
		for cfg in (self.scan_sat.frequency, self.scan_sat.symbolrate, self.scan_sat.polarization,
					self.scan_sat.fec, self.scan_sat.fec_s2, self.scan_sat.inversion,
					self.scan_sat.modulation):
			cfg.addNotifier(self._onConfigChanged, initial_call=False)

		self["actions"] = NumberActionMap(["SetupActions", "ColorActions"],
		{
			"ok": self.keyGo,
			"cancel": self.keyCancel,
			"red": self.keyCancel,
			"green": self.keyGo,
		}, -2)

		self["key_red"] = StaticText(_("Cancel"))
		self["key_green"] = StaticText(_("OK"))
		self["introduction"] = Label(_("Press OK, save and exit..."))

		self.onClose.append(self._cleanup)
		self.onLayoutFinish.append(self._prepareFrontend)

	def _openFrontend(self):
		try:
			res_mgr = eDVBResourceManager.getInstance()
			if res_mgr:
				self.raw_channel = res_mgr.allocateRawChannel(self.feid)
				if self.raw_channel:
					self.frontend = self.raw_channel.getFrontend()
					if self.frontend:
						return True
		except Exception as e:
			print("[TunerScreen] openFrontend failed:", e)
		return False

	def _prepareFrontend(self):
		if self._openFrontend():
			self.tuner = Tuner(self.frontend, ignore_rotor=True)
			self._retune()
		self.statusTimer.start(self.STATUS_INTERVAL, True)

	def _updateStatus(self):
		# Keep retrying the current transponder if the demod drops/fails, so an
		# edited (and momentarily invalid) transponder re-locks once it's valid.
		if self.frontend is not None:
			try:
				d = {}
				self.frontend.getFrontendStatus(d)
				if d.get("tuner_state") in ("FAILED", "LOSTLOCK"):
					self._retune()
			except Exception:
				pass
		self.statusTimer.start(self.STATUS_INTERVAL, True)

	def _onConfigChanged(self, *args):
		self._scheduleRetune()

	def _scheduleRetune(self):
		if self.frontend is not None:
			self.retuneTimer.start(self.RETUNE_DEBOUNCE, True)

	def _retune(self):
		if self.frontend is None or self.tuner is None:
			return
		try:
			self.tuner.tune(self._buildTransponder())
		except Exception:
			pass	# bad/partial config while editing -- ignore, next edit retries

	def _cleanup(self):
		self.statusTimer.stop()
		self.retuneTimer.stop()
		self.frontend = None
		if self.raw_channel:
			del self.raw_channel
			self.raw_channel = None

	def createConfig(self, frontendData):
		satlist = nimmanager.getRotorSatListForNim(self.feid)
		orb_pos = self.fe_data.get("orbital_position", None)
		self.tuning = ConfigSubsection()
		self.tuning.type = ConfigSelection(
				default="manual_transponder",
				choices={"manual_transponder": _("Manual transponder"),
							"predefined_transponder": _("Predefined transponder")})
		self.tuning.sat = ConfigSatlist(list=satlist)
		if orb_pos is not None:
			orb_pos_str = str(orb_pos)
			for sat in satlist:
				if sat[0] == orb_pos and self.tuning.sat.value != orb_pos_str:
					self.tuning.sat.value = orb_pos_str
		self.updateTransponders()

		defaultSat = {
			"orbpos": 192,
			"system": eDVBFrontendParametersSatellite.System_DVB_S,
			"frequency": 11836,
			"inversion": eDVBFrontendParametersSatellite.Inversion_Unknown,
			"symbolrate": 27500,
			"polarization": eDVBFrontendParametersSatellite.Polarisation_Horizontal,
			"fec": eDVBFrontendParametersSatellite.FEC_Auto,
			"fec_s2": eDVBFrontendParametersSatellite.FEC_9_10,
			"modulation": eDVBFrontendParametersSatellite.Modulation_QPSK,
			"pls_mode": eDVBFrontendParametersSatellite.PLS_Gold,
			"pls_code": eDVBFrontendParametersSatellite.PLS_Default_Gold_Code}
		if frontendData is not None:
			defaultSat["system"] = frontendData.get("system", eDVBFrontendParametersSatellite.System_DVB_S)
			_freq_khz = frontendData.get("frequency", 0)
			defaultSat["frequency"] = _freq_khz // 1000
			defaultSat["frequency_khz"] = _freq_khz % 1000
			defaultSat["inversion"] = frontendData.get("inversion", eDVBFrontendParametersSatellite.Inversion_Unknown)
			defaultSat["symbolrate"] = frontendData.get("symbol_rate", 0) // 1000
			defaultSat["polarization"] = frontendData.get("polarization", eDVBFrontendParametersSatellite.Polarisation_Horizontal)
			if defaultSat["system"] == eDVBFrontendParametersSatellite.System_DVB_S2:
				defaultSat["fec_s2"] = frontendData.get("fec_inner", eDVBFrontendParametersSatellite.FEC_Auto)
				defaultSat["rolloff"] = frontendData.get("rolloff", eDVBFrontendParametersSatellite.RollOff_alpha_0_35)
				defaultSat["pilot"] = frontendData.get("pilot", eDVBFrontendParametersSatellite.Pilot_Unknown)
				defaultSat["is_id"] = frontendData.get("is_id", eDVBFrontendParametersSatellite.No_Stream_Id_Filter)
				defaultSat["pls_mode"] = frontendData.get("pls_mode", eDVBFrontendParametersSatellite.PLS_Gold)
				defaultSat["pls_code"] = frontendData.get("pls_code", eDVBFrontendParametersSatellite.PLS_Default_Gold_Code)
				defaultSat["t2mi_plp_id"] = frontendData.get("t2mi_plp_id", eDVBFrontendParametersSatellite.No_T2MI_PLP_Id)
				defaultSat["t2mi_pid"] = frontendData.get("t2mi_pid", eDVBFrontendParametersSatellite.T2MI_Default_Pid)
			else:
				defaultSat["fec"] = frontendData.get("fec_inner", eDVBFrontendParametersSatellite.FEC_Auto)
			defaultSat["modulation"] = frontendData.get("modulation", eDVBFrontendParametersSatellite.Modulation_QPSK)
			defaultSat["orbpos"] = frontendData.get("orbital_position", 0)

		self.scan_sat = ConfigSubsection()
		self.scan_sat.system = ConfigSelection(default=defaultSat["system"], choices=[
			(eDVBFrontendParametersSatellite.System_DVB_S, "DVB-S"),
			(eDVBFrontendParametersSatellite.System_DVB_S2, "DVB-S2")])
		self.scan_sat.frequency = ConfigFloat(default=[defaultSat["frequency"], defaultSat.get("frequency_khz", 0)], limits=[(1, 99999), (0, 999)])
		self.scan_sat.inversion = ConfigSelection(default=defaultSat["inversion"], choices=[
			(eDVBFrontendParametersSatellite.Inversion_Off, _("Off")),
			(eDVBFrontendParametersSatellite.Inversion_On, _("On")),
			(eDVBFrontendParametersSatellite.Inversion_Unknown, _("Auto"))])
		self.scan_sat.symbolrate = ConfigInteger(default=defaultSat["symbolrate"], limits=(1, 99999))
		self.scan_sat.polarization = ConfigSelection(default=defaultSat["polarization"], choices=[
			(eDVBFrontendParametersSatellite.Polarisation_Horizontal, _("horizontal")),
			(eDVBFrontendParametersSatellite.Polarisation_Vertical, _("vertical")),
			(eDVBFrontendParametersSatellite.Polarisation_CircularLeft, _("circular left")),
			(eDVBFrontendParametersSatellite.Polarisation_CircularRight, _("circular right"))])
		self.scan_sat.fec = ConfigSelection(default=defaultSat["fec"], choices=[
			(eDVBFrontendParametersSatellite.FEC_Auto, _("Auto")),
			(eDVBFrontendParametersSatellite.FEC_1_2, "1/2"),
			(eDVBFrontendParametersSatellite.FEC_2_3, "2/3"),
			(eDVBFrontendParametersSatellite.FEC_3_4, "3/4"),
			(eDVBFrontendParametersSatellite.FEC_5_6, "5/6"),
			(eDVBFrontendParametersSatellite.FEC_7_8, "7/8"),
			(eDVBFrontendParametersSatellite.FEC_None, _("None"))])
		self.scan_sat.fec_s2 = ConfigSelection(default=defaultSat["fec_s2"], choices=[
			(eDVBFrontendParametersSatellite.FEC_1_2, "1/2"),
			(eDVBFrontendParametersSatellite.FEC_2_3, "2/3"),
			(eDVBFrontendParametersSatellite.FEC_3_4, "3/4"),
			(eDVBFrontendParametersSatellite.FEC_3_5, "3/5"),
			(eDVBFrontendParametersSatellite.FEC_4_5, "4/5"),
			(eDVBFrontendParametersSatellite.FEC_5_6, "5/6"),
			(eDVBFrontendParametersSatellite.FEC_7_8, "7/8"),
			(eDVBFrontendParametersSatellite.FEC_8_9, "8/9"),
			(eDVBFrontendParametersSatellite.FEC_9_10, "9/10")])
		self.scan_sat.modulation = ConfigSelection(default=defaultSat["modulation"], choices=[
			(eDVBFrontendParametersSatellite.Modulation_QPSK, "QPSK"),
			(eDVBFrontendParametersSatellite.Modulation_8PSK, "8PSK"),
			(eDVBFrontendParametersSatellite.Modulation_16APSK, "16APSK"),
			(eDVBFrontendParametersSatellite.Modulation_32APSK, "32APSK")])
		self.scan_sat.rolloff = ConfigSelection(default=defaultSat.get("rolloff", eDVBFrontendParametersSatellite.RollOff_alpha_0_35), choices=[
			(eDVBFrontendParametersSatellite.RollOff_alpha_0_35, "0.35"),
			(eDVBFrontendParametersSatellite.RollOff_alpha_0_25, "0.25"),
			(eDVBFrontendParametersSatellite.RollOff_alpha_0_20, "0.20"),
			(eDVBFrontendParametersSatellite.RollOff_auto, _("Auto"))])
		self.scan_sat.pilot = ConfigSelection(default=defaultSat.get("pilot", eDVBFrontendParametersSatellite.Pilot_Unknown), choices=[
			(eDVBFrontendParametersSatellite.Pilot_Off, _("Off")),
			(eDVBFrontendParametersSatellite.Pilot_On, _("On")),
			(eDVBFrontendParametersSatellite.Pilot_Unknown, _("Auto"))])
		self.scan_sat.is_id = ConfigInteger(default=defaultSat.get("is_id", 0), limits=(0, 255))
		self.scan_sat.pls_mode = ConfigSelection(default=defaultSat.get("pls_mode", eDVBFrontendParametersSatellite.PLS_Gold), choices=[
			(eDVBFrontendParametersSatellite.PLS_Root, _("Root")),
			(eDVBFrontendParametersSatellite.PLS_Gold, _("Gold")),
			(eDVBFrontendParametersSatellite.PLS_Combo, _("Combo"))])
		self.scan_sat.pls_code = ConfigInteger(default=defaultSat.get("pls_code", eDVBFrontendParametersSatellite.PLS_Default_Gold_Code), limits=(0, 262142))
		self.scan_sat.t2mi_plp_id = ConfigInteger(default=defaultSat.get("t2mi_plp_id", eDVBFrontendParametersSatellite.No_T2MI_PLP_Id), limits=(0, 255))
		self.scan_sat.t2mi_pid = ConfigInteger(default=defaultSat.get("t2mi_pid", eDVBFrontendParametersSatellite.T2MI_Default_Pid), limits=(0, 8191))

	def initialSetup(self):
		currtp = self.transponderToString([None, self.scan_sat.frequency.floatint // 1000, self.scan_sat.symbolrate.value, self.scan_sat.polarization.value])
		if currtp in self.tuning.transponder.choices:
			self.tuning.type.value = "predefined_transponder"
		else:
			self.tuning.type.value = "manual_transponder"

	def createSetup(self):
		self.list = []
		self.list.append(getConfigListEntry(_('Tune'), self.tuning.type))
		self.list.append(getConfigListEntry(_('Satellite'), self.tuning.sat))
		nim = nimmanager.nim_slots[self.feid]

		if self.tuning.type.value == "manual_transponder":
			if nim.isCompatible("DVB-S2"):
				self.list.append(getConfigListEntry(_('System'), self.scan_sat.system))
			else:
				# downgrade to dvb-s, in case a -s2 config was active
				self.scan_sat.system.value = eDVBFrontendParametersSatellite.System_DVB_S
			self.list.append(getConfigListEntry(_('Frequency'), self.scan_sat.frequency))
			self.list.append(getConfigListEntry(_("Polarisation"), self.scan_sat.polarization))
			self.list.append(getConfigListEntry(_('Symbol rate'), self.scan_sat.symbolrate))
			if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S:
				self.list.append(getConfigListEntry(_("FEC"), self.scan_sat.fec))
				self.list.append(getConfigListEntry(_('Inversion'), self.scan_sat.inversion))
			elif self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S2:
				self.list.append(getConfigListEntry(_("FEC"), self.scan_sat.fec_s2))
				self.list.append(getConfigListEntry(_('Inversion'), self.scan_sat.inversion))
				self.modulationEntry = getConfigListEntry(_('Modulation'), self.scan_sat.modulation)
				self.list.append(self.modulationEntry)
				self.list.append(getConfigListEntry(_('Roll-off'), self.scan_sat.rolloff))
				self.list.append(getConfigListEntry(_('Pilot'), self.scan_sat.pilot))
				if nim.isMultistream():
					self.list.append(getConfigListEntry(_('Input Stream ID'), self.scan_sat.is_id))
					self.list.append(getConfigListEntry(_('PLS Mode'), self.scan_sat.pls_mode))
					self.list.append(getConfigListEntry(_('PLS Code'), self.scan_sat.pls_code))
				if nim.isT2MI():
					self.list.append(getConfigListEntry(_('T2MI PLP ID'), self.scan_sat.t2mi_plp_id))
					self.list.append(getConfigListEntry(_('T2MI PID'), self.scan_sat.t2mi_pid))
		else: # "predefined_transponder"
			self.list.append(getConfigListEntry(_("Transponder"), self.tuning.transponder))
			currtp = self.transponderToString([None, self.scan_sat.frequency.floatint // 1000, self.scan_sat.symbolrate.value, self.scan_sat.polarization.value])
			self.tuning.transponder.setValue(currtp)
		self["config"].list = self.list

	def tuningSatChanged(self, *parm):
		self.updateTransponders()
		self.createSetup()

	def tuningTypeChanged(self, *parm):
		self.createSetup()

	def systemChanged(self, *parm):
		self.createSetup()

	def transponderToString(self, tr, scale=1):
		if tr[3] == 0:
			pol = "H"
		elif tr[3] == 1:
			pol = "V"
		elif tr[3] == 2:
			pol = "CL"
		elif tr[3] == 3:
			pol = "CR"
		else:
			pol = "??"
		return str(tr[1] // scale) + "," + pol + "," + str(tr[2] // scale)

	def updateTransponders(self):
		if len(self.tuning.sat.choices):
			transponderlist = nimmanager.getTransponders(int(self.tuning.sat.value), self.feid)
			tps = []
			for transponder in transponderlist:
				tps.append(self.transponderToString(transponder, scale=1000))
			self.tuning.transponder = ConfigSelection(choices=tps)

	def keyLeft(self):
		ConfigListScreen.keyLeft(self)
		self._scheduleRetune()

	def keyRight(self):
		ConfigListScreen.keyRight(self)
		self._scheduleRetune()

	def _buildTransponder(self):
		satpos = int(self.tuning.sat.value)
		if self.tuning.type.value == "manual_transponder":
			if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S2:
				fec = self.scan_sat.fec_s2.value
			else:
				fec = self.scan_sat.fec.value
			return (
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
		else:	# "predefined_transponder"
			transponder = nimmanager.getTransponders(satpos)[self.tuning.transponder.index]
			return (transponder[1] / 1000.0, transponder[2] // 1000,
				transponder[3], transponder[4], 2, satpos, transponder[5], transponder[6], transponder[8], transponder[9], transponder[10], transponder[11], transponder[12], transponder[13], transponder[14])

	def keyGo(self):
		self.close(self._buildTransponder())

	def keyCancel(self):
		self.close(None)


class RotorNimSelection(Screen):
	skin = """
		<screen position="center,center" size="400,130" title="Select slot">
			<widget name="nimlist" position="20,10" size="360,100" />
		</screen>"""

	def __init__(self, session, nimList):
		Screen.__init__(self, session)
		self.setTitle(_("Select slot"))
		nimMenuList = []
		for nim in nimList:
			nimMenuList.append((nimmanager.nim_slots[nim].friendly_full_description, nim))

		self["nimlist"] = MenuList(nimMenuList)

		self["actions"] = ActionMap(["OkCancelActions"],
		{
			"ok": self.okbuttonClick,
			"cancel": self.close
		}, -1)

	def okbuttonClick(self):
		self.session.openWithCallback(self.close, PositionerSetup, self["nimlist"].getCurrent()[1])
