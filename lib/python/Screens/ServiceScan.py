# TNAP modifications to Screens/ServiceScan.py:
#
# 1. Embedded TNAP_ServiceScan skin (1080p, wfNoBorder) with live SNR/AGC
#    gradient bars (signalbar.png), a real-time services-found list on the
#    right pane, and a color-keyed footer.  A unique skinName ensures this
#    layout always wins over whatever skin the user has installed.
#
# 2. ScrollingList — a MenuList that auto-follows the live tail during the
#    scan and pauses following the moment the user scrolls up, so long scans
#    can be browsed mid-run without losing the live view.
#
# 3. Keep / Discard prompt (_scanComplete / _keepResults) — at scan
#    completion eComponentScan (lib/components/scan.cpp line 33) calls
#    db->flush() → saveServicelist() BEFORE Python sees isDone(), so by
#    the time the prompt appears lamedb, lamedb5, and the Last Scanned
#    bouquet are already overwritten on disk.
#      Keep    → delete /tmp snapshots (prevent contamination), then
#                db.reloadBouquets() to refresh the channel list.
#      Discard → delete the three scan-written files from /etc/enigma2,
#                restore the three /tmp snapshots, then
#                db.reloadServicelist() + db.reloadBouquets() — same
#                sequence as LamedbMerger, no enigma2 restart needed.

import os
import shutil
from Screens.Screen import Screen
import Screens.InfoBar
from Components.ServiceScan import ServiceScan as CScan
from Components.ProgressBar import ProgressBar
from Components.Label import Label
from Components.MenuList import MenuList
from Components.ActionMap import ActionMap
from Components.Sources.FrontendInfo import FrontendInfo
from Components.config import config
from enigma import eServiceReference, eDVBDB
from Tools.Directories import fileExists  # extra import

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
		if p == 1 and BOX_NAME.startswith("sf"):
			BOX_NAME = "sf8008-Supreme"
		nimfile.close()
	except:
		pass

# Make sure we always have something to print in the header, regardless of
# which /proc/stb/info node this box exposes. The block above only fills
# BOX_NAME on a narrow set of boxes; fall back to the usual model nodes so a
# posted screenshot always identifies the receiver that ran the scan.
if not BOX_NAME:
	for _bp in ("/proc/stb/info/model", "/proc/stb/info/boxtype", "/proc/stb/info/hwmodel", "/proc/stb/info/gbmodel", "/proc/stb/info/vumodel"):
		try:
			if fileExists(_bp):
				with open(_bp) as _bf:
					_bv = _bf.read().strip()
				if _bv:
					BOX_NAME = _bv
					break
		except:
			pass
BOX_NAME = BOX_NAME.upper()
# XML-escape the value before it goes into the skin string: it comes off a
# /proc node, so an unexpected '&' or quote would otherwise break readSkin.
BOX_NAME_XML = BOX_NAME.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

# Absolute path to the shipped gradient bar, resolved at runtime so it loads
# wherever the plugin/image is installed (see field-guide section 3.5). The
# PNG is the full bar width (850px) so the eSlider's native-width blit fills
# the whole bar instead of cramming the gradient into the left edge (3.6).
_PLUGIN_PATH = os.path.dirname(os.path.realpath(__file__))
_BAR_PIXMAP = os.path.join(_PLUGIN_PATH, "signalbar.png")

# Fully self-contained skin. A unique, prefixed skinName (TNAP_ServiceScan)
# that no installed skin defines guarantees the domScreens lookup misses and
# the engine falls through to this embedded layout - so the screen renders
# identically on every skin and no skin can starve it of widgets. All colors
# are inline #AARRGGBB literals, the background is painted opaque, and there
# are no <panel> includes or skin-private color names. The asset path is
# injected with str.replace() (NOT %-formatting) because the clock/date
# converters contain %H, %a, %B ... which would break % string formatting.
SERVICESCAN_SKIN = """
	<screen name="TNAP_ServiceScan" position="0,0" size="1920,1080" flags="wfNoBorder" backgroundColor="#00000000" resolution="1920,1080">
		<eLabel position="0,0" size="1920,1080" backgroundColor="#00101010" zPosition="-2"/>
		<eLabel position="0,0" size="1920,98" backgroundColor="#00181818" zPosition="-1"/>
		<eLabel position="0,98" size="1920,2" backgroundColor="#00303030" zPosition="-1"/>
		<eLabel position="0,1008" size="1920,2" backgroundColor="#00303030" zPosition="-1"/>

		<widget source="Title" render="Label" position="30,20" size="500,54" font="Regular;44" foregroundColor="#00f0f0f0" transparent="1" horizontalAlignment="left" verticalAlignment="center" noWrap="1"/>
		<eLabel text="__BOX__" position="548,22" size="780,50" font="Regular;40" foregroundColor="#00ffc000" backgroundColor="#00181818" transparent="1" horizontalAlignment="left" verticalAlignment="center"/>
		<widget source="global.CurrentTime" render="Label" position="1560,12" size="330,48" font="Regular;42" foregroundColor="#00f0f0f0" transparent="1" horizontalAlignment="right" verticalAlignment="center">
			<convert type="ClockToText">Default</convert>
		</widget>
		<widget source="global.CurrentTime" render="Label" position="1380,64" size="510,30" font="Regular;26" foregroundColor="#00b8b8b8" transparent="1" horizontalAlignment="right" verticalAlignment="center">
			<convert type="ClockToText">Format:%a %d %B %Y</convert>
		</widget>

		<widget name="network" position="30,120" size="850,92" font="Regular;36" foregroundColor="#00f0f0f0" transparent="1" verticalAlignment="top"/>
		<widget name="transponder" position="30,220" size="850,42" font="Regular;32" foregroundColor="#00d8d8d8" transparent="1" verticalAlignment="center" noWrap="1"/>

		<widget name="scan_progress" position="30,286" size="850,26" borderWidth="1" borderColor="#00606060" backgroundColor="#00202020" foregroundColor="#0056c856"/>
		<widget name="scan_state" position="30,326" size="850,44" font="Regular;32" foregroundColor="#00f0f0f0" transparent="1" verticalAlignment="center"/>
		<widget name="pass" position="30,374" size="850,38" font="Regular;28" foregroundColor="#00b8b8b8" transparent="1" verticalAlignment="center"/>

		<eLabel position="30,430" size="850,2" backgroundColor="#00303030"/>
		<eLabel position="30,444" size="850,28" text="Signal monitor" font="Regular;24" foregroundColor="#00808080" backgroundColor="#00101010" transparent="1" horizontalAlignment="left" verticalAlignment="center"/>

		<widget name="snr_text" position="30,480" size="850,34" font="Regular;30" foregroundColor="#00f0f0f0" transparent="1" verticalAlignment="center"/>
		<widget name="snr_slider" position="30,518" size="850,30" pixmap="__BAR__" borderWidth="1" borderColor="#00606060" backgroundColor="#00202020" foregroundColor="#0056c856"/>
		<widget name="agc_text" position="30,562" size="850,34" font="Regular;30" foregroundColor="#00f0f0f0" transparent="1" verticalAlignment="center"/>
		<widget name="agc_slider" position="30,600" size="850,30" pixmap="__BAR__" borderWidth="1" borderColor="#00606060" backgroundColor="#00202020" foregroundColor="#0056c856"/>
		<widget name="lock_state" position="30,648" size="850,36" font="Regular;30" foregroundColor="#00f0f0f0" transparent="1" verticalAlignment="center"/>

		<widget name="svc_header" position="910,114" size="980,40" font="Regular;34" foregroundColor="#00f0f0f0" transparent="1" verticalAlignment="center"/>
		<eLabel position="910,158" size="980,2" backgroundColor="#00303030"/>
		<widget name="servicelist" position="910,168" size="980,832" itemHeight="40" font="Regular;30" scrollbarMode="showOnDemand" transparent="1" foregroundColor="#00f0f0f0" backgroundColor="#00101010" backgroundColorSelected="#00313a46"/>

		<eLabel position="30,1026" size="36,36" backgroundColor="#0056c856" borderWidth="1" borderColor="#00348a34"/>
		<widget name="key_green" position="80,1022" size="220,44" font="Regular;32" foregroundColor="#00f0f0f0" transparent="1" verticalAlignment="center" noWrap="1"/>
		<eLabel text="OK" position="330,1026" size="70,36" backgroundColor="#00232323" borderWidth="1" borderColor="#00707070" font="Regular;26" foregroundColor="#00d0d0d0" horizontalAlignment="center" verticalAlignment="center"/>
		<widget name="key_ok" position="416,1022" size="320,44" font="Regular;32" foregroundColor="#00f0f0f0" transparent="1" verticalAlignment="center" noWrap="1"/>
		<eLabel position="766,1026" size="36,36" backgroundColor="#00ff4a3c" borderWidth="1" borderColor="#00a3312a"/>
		<widget name="key_red" position="816,1022" size="240,44" font="Regular;32" foregroundColor="#00f0f0f0" transparent="1" verticalAlignment="center" noWrap="1"/>
	</screen>""".replace("__BAR__", _BAR_PIXMAP).replace("__BOX__", BOX_NAME_XML)


class ScrollingList(MenuList):
	"""A MenuList that keeps every item and stays scrollable while it grows.

	It auto-"follows" the live end (newest service / current transponder) so the
	screen tracks the scan in real time, but the moment the user scrolls up it
	stops following and holds position. Scrolling back down to the bottom resumes
	following. Used for both the found-services list and the transponder plan, so
	long scans (hundreds/thousands of services, 60-80 transponders) can be browsed
	mid-scan without losing the live view.
	"""

	def __init__(self):
		MenuList.__init__(self, [], enableWrapAround=False)
		self.follow = True

	def addItem(self, item):
		self.list.append(item)
		if self.follow:
			self.l.setList(self.list)
			self._toIndex(len(self.list) - 1)
		else:
			idx = self.getSelectionIndex()
			self.l.setList(self.list)
			self._toIndex(idx)

	def setItems(self, items):
		self.list = list(items)
		self.l.setList(self.list)
		self.follow = True
		self._toIndex(0)

	def clear(self):
		del self.list[:]
		self.l.setList(self.list)
		self.follow = True

	def listAll(self):
		# scan finished: show everything and let the user pick a service
		self.l.setList(self.list)
		self.selectionEnabled(True)

	def getCurrentSelection(self):
		return self.list and self.getCurrent() or None

	def followTo(self, idx):
		# move the live cursor only while still following (current transponder)
		if self.follow:
			self._toIndex(idx)

	def _toIndex(self, idx):
		if self.list:
			self.moveToIndex(max(0, min(idx, len(self.list) - 1)))

	# --- user navigation: pause follow; resume when back at the live tail ---
	def up(self):
		self.follow = False
		MenuList.up(self)

	def pageUp(self):
		self.follow = False
		MenuList.pageUp(self)

	def down(self):
		MenuList.down(self)
		self.follow = self.getSelectionIndex() >= len(self.list) - 1

	def pageDown(self):
		MenuList.pageDown(self)
		self.follow = self.getSelectionIndex() >= len(self.list) - 1

	def toLatest(self):
		self.follow = True
		self._toIndex(len(self.list) - 1)


class ServiceScanSummary(Screen):
	skin = """
	<screen position="0,0" size="132,64">
		<widget name="Title" position="6,4" size="120,42" font="Regular;16" transparent="1" />
		<widget name="scan_progress" position="6,50" zPosition="1" borderWidth="1" size="56,12" backgroundColor="dark" />
		<widget name="Service" position="6,22" size="120,26" font="Regular;12" transparent="1" />
	</screen>"""

	def __init__(self, session, parent, showStepSlider=True):
		Screen.__init__(self, session, parent)

		self["Title"] = Label(parent.title or _("Service scan"))
		self["Service"] = Label(_("No service"))
		self["scan_progress"] = ProgressBar()

	def updateProgress(self, value):
		self["scan_progress"].setValue(value)

	def updateService(self, name):
		self["Service"].setText(name)


class ServiceScan(Screen):

	def _doKeep(self):
		if self._awaitingDecision:
			self._keepResults(True)

	def _doDiscard(self):
		if self._awaitingDecision:
			self._keepResults(False)
		else:
			self.cancel()

	def ok(self):
		# At the keep/discard prompt, OK means "keep these results AND jump to the
		# highlighted service". GREEN still keeps-and-closes, RED still discards.
		# We must keep before we can zap: zapping needs reloadBouquets() to have
		# made the "Last Scanned" bouquet live so enterUserbouquet() can find it.
		if self._awaitingDecision:
			self._keepResults(True, zap=True)
			return
		if self["scan"].isDone():
			self._zapToSelection()

	def _zapToSelection(self):
		# Tune the running InfoBar to the service highlighted in the found-services
		# list, then close recursively so we land on that channel. Assumes the
		# "Last Scanned" bouquet is already in memory (reloadBouquets() has run).
		# Any path that cannot zap falls back to a plain close.
		if self.currentInfobar is None or self.currentInfobar.__class__.__name__ != "InfoBar":
			self.cancel()
			return
		selectedService = self["servicelist"].getCurrentSelection()
		if not selectedService or self.currentServiceList is None:
			self.cancel()
			return
		self.currentServiceList.setTvMode()
		bouquets = self.currentServiceList.getBouquetList()
		last_scanned_bouquet = bouquets and next((x[1] for x in bouquets if x[0] == "Last Scanned"), None)
		if not last_scanned_bouquet:
			self.cancel()
			return
		self.currentServiceList.enterUserbouquet(last_scanned_bouquet)
		self.currentServiceList.setCurrentSelection(eServiceReference(selectedService[1]))
		service = self.currentServiceList.getCurrentSelection()
		if not self.session.postScanService or service != self.session.postScanService:
			self.session.postScanService = service
			self.currentServiceList.addToHistory(service)
		config.servicelist.lastmode.save()
		self.currentServiceList.saveChannel(service)
		self.doCloseRecursive()

	def cancel(self):
		if self._awaitingDecision:
			self._keepResults(False)
			return
		self.exit(False)

	def doCloseRecursive(self):
		self.exit(True)

	def exit(self, returnValue):
		if self.currentInfobar.__class__.__name__ == "InfoBar":
			self.close(returnValue)
			return
		self.close()

	# ---- found-services list navigation ---------------------------------
	def listUp(self):
		self["servicelist"].up()

	def listDown(self):
		self["servicelist"].down()

	def listPageUp(self):
		self["servicelist"].pageUp()

	def listPageDown(self):
		self["servicelist"].pageDown()

	def __init__(self, session, scanList):
		Screen.__init__(self, session)

		self.scanList = scanList
		self._resultPromptShown = False
		self._awaitingDecision = False

		if hasattr(session, 'infobar'):
			self.currentInfobar = Screens.InfoBar.InfoBar.instance
			self.currentServiceList = self.currentInfobar.servicelist
			if self.session.pipshown and self.currentServiceList:
				if self.currentServiceList.dopipzap:
					self.currentServiceList.togglePipzap()
				if hasattr(self.session, 'pip'):
					del self.session.pip
				self.session.pipshown = False
		else:
			self.currentInfobar = None

		self.session.nav.stopService()

		self["scan_progress"] = ProgressBar()
		self["scan_state"] = Label(_("scan state"))
		self["network"] = Label()
		self["transponder"] = Label()
		self["pass"] = Label("")

		# live transponder signal panel (driven from the component's reader)
		self["snr_text"] = Label("SNR   ---")
		self["snr_slider"] = ProgressBar()
		self["agc_text"] = Label("AGC   ---")
		self["agc_slider"] = ProgressBar()
		self["lock_state"] = Label(_("Searching..."))

		# right pane: the list of found services
		self["svc_header"] = Label(_("Services found"))
		self["servicelist"] = ScrollingList()

		self["FrontendInfo"] = FrontendInfo()
		# Footer key legend. During the scan only RED (Cancel) is active; the
		# Keep / Keep & Watch / Discard choices are revealed in _scanComplete
		# once results exist, so the footer never advertises a key that does
		# nothing yet.
		self["key_red"] = Label(_("Cancel"))
		self["key_green"] = Label("")
		self["key_ok"] = Label("")

		self["actions"] = ActionMap(["SetupActions", "MenuActions", "DirectionActions", "NavigationActions", "ColorActions"],
		{
			"ok": self.ok,
			"save": self.ok,
			"cancel": self.cancel,
			"menu": self.doCloseRecursive,
			"up": self.listUp,
			"down": self.listDown,
			"left": self.listPageUp,
			"right": self.listPageDown,
			"pageUp": self.listPageUp,
			"pageDown": self.listPageDown,
			"green": self._doKeep,
			"red": self._doDiscard,
		}, -2)

		# own this layout regardless of the installed skin (field guide s.2)
		self.skin = SERVICESCAN_SKIN
		self.skinName = ["TNAP_ServiceScan"]
		self.setTitle(_("Service scan"))
		self.onFirstExecBegin.append(self.doServiceScan)

	def doServiceScan(self):
		# Snapshot three files to /tmp before eComponentScan starts. The C++ engine
		# (lib/components/scan.cpp line 33) calls db->flush() → saveServicelist()
		# as part of its own completion, BEFORE Python sees isDone(). By the time
		# the keep/discard prompt appears all three files on disk are already
		# overwritten. The snapshots let Discard put the box back exactly as it was.
		self._snap = {}
		for src, dst in (
			("/etc/enigma2/lamedb",                     "/tmp/lamedb"),
			("/etc/enigma2/lamedb5",                    "/tmp/lamedb5"),
			("/etc/enigma2/userbouquet.LastScanned.tv", "/tmp/userbouquet.LastScanned.tv"),
		):
			if os.path.exists(src):
				try:
					shutil.copy2(src, dst)
					self._snap[src] = dst
				except Exception:
					pass

		self["scan"] = CScan(self["scan_progress"], self["scan_state"], self["servicelist"], self["pass"], self.scanList, self["network"], self["transponder"], self["FrontendInfo"], self.session.summary,
			snrSlider=self["snr_slider"], snrText=self["snr_text"], agcSlider=self["agc_slider"], agcText=self["agc_text"], lockText=self["lock_state"])
		self["scan"].onScanComplete.append(self._scanComplete)

	# ---- keep / discard the scan results --------------------------------
	def _scanComplete(self):
		if self._resultPromptShown:
			return
		self._resultPromptShown = True
		found = 0
		try:
			found = int(self["scan"].foundServices)
		except:
			pass
		if found <= 0:
			return
		self._awaitingDecision = True
		self["scan_state"].setText(_("Done — %d channel(s) found.") % found)
		self["pass"].setText(_("Scroll the list to a channel, then choose below."))
		# Footer now reads: GREEN = Keep    OK = Keep & Watch    RED = Discard
		self["key_green"].setText(_("Keep"))
		self["key_ok"].setText(_("Keep & Watch"))
		self["key_red"].setText(_("Discard"))

	def _keepResults(self, keep, zap=False):
		self._awaitingDecision = False
		db = eDVBDB.getInstance()
		if db is None:
			self.exit(False)
			return
		snap = getattr(self, "_snap", {})
		if keep:
			# eComponentScan already saved lamedb (lib/components/scan.cpp line 33).
			# Discard the /tmp snapshots so they do not contaminate the next scan,
			# then refresh bouquets so the channel list reflects the new services.
			for dst in snap.values():
				try:
					if os.path.exists(dst):
						os.remove(dst)
				except Exception:
					pass
			db.reloadBouquets()
			if zap:
				self._zapToSelection()
			else:
				self.cancel()
		else:
			# Delete the three files that eComponentScan wrote, then restore the
			# pre-scan snapshots from /tmp. Writing both lamedb and lamedb5 (same
			# sequence as LamedbMerger) then calling reloadServicelist() puts the
			# in-memory database back to the pre-scan state without restarting enigma2.
			for src in snap:
				try:
					if os.path.exists(src):
						os.remove(src)
				except Exception:
					pass
			for src, dst in snap.items():
				try:
					if os.path.exists(dst):
						shutil.copy2(dst, src)
				except Exception:
					pass
			# Clean up /tmp snapshots.
			for dst in snap.values():
				try:
					if os.path.exists(dst):
						os.remove(dst)
				except Exception:
					pass
			db.reloadServicelist()
			db.reloadBouquets()
			self.cancel()

	def createSummary(self):
		print("ServiceScanCreateSummary")
		return ServiceScanSummary
