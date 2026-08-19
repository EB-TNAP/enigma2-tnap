# -*- coding: utf-8 -*-
from Screens.Wizard import wizardManager
from Screens.Screen import Screen
from Screens.MessageBox import MessageBox
# from Screens.WizardLanguage import WizardLanguage
from Screens.Wizard import wizardManager, Wizard
from Screens.Time import TimeWizard
from Screens.HelpMenu import Rc
from Screens.Standby import TryQuitMainloop, QUIT_RESTART, QUIT_REBOOT
from Screens.NetworkSetup import NetworkAdapterSetup, NetworkWiFiAddFlow
from Components.SystemInfo import BoxInfo
try:
	from Plugins.SystemPlugins.OSDPositionSetup.overscanwizard import OverscanWizard
except:
	OverscanWizard = None
from Components.NetworkManager import networkManager
from Components.Console import Console
from Components.Pixmap import Pixmap
from Components.ProgressBar import ProgressBar
from Components.Label import Label
from Components.ScrollLabel import ScrollLabel
from Components.SystemInfo import BoxInfo
from Components.config import config, ConfigBoolean, configfile
from Tools.Directories import fileReadLines
# from Screens.LocaleSelection import LocaleSelection
from enigma import eConsoleAppContainer, eTimer, eActionMap
from re import search
import os

config.misc.firstrun = ConfigBoolean(default=True)
config.misc.wizardLanguageEnabled = ConfigBoolean(default=True)
config.misc.do_overscanwizard = ConfigBoolean(default=OverscanWizard and config.skin.primary_skin.value == "PLi-FullNightHD/skin.xml")


MODEL = BoxInfo.getItem("model")

MODULE_NAME = __name__.split(".")[-1]


class WizardStart(Wizard, Rc):
	def __init__(self, session, silent=True, showSteps=False, neededTag=None):
		self.xmlfile = ["startwizard.xml"]
		Wizard.__init__(self, session, showSteps=False)
		Rc.__init__(self)
		self.skinName = ["StartWizard"]
		if isinstance(self.skinName, str):
			self.skinName.insert(0, "StartWizard")
		self["wizard"] = Pixmap()
		self.nwSelectedIface = None
		self.nwIpFound = ""
		self.nwPollTimer = None
		self.nwPollCount = 0
		self.nwSubFlowActive = False
		self.nwPollIntervalMs = 1500
		self.nwPollMaxAttempts = 12  # 18 s total

	def markDone(self):
		# All boxes use the same remote control setting except the dm8000, which needs its own.
		if MODEL in ("dm8000"):
			config.misc.rcused.value = 0
		else:
			config.misc.rcused.value = 1
		config.misc.rcused.save()

		config.misc.firstrun.value = 0
		config.misc.firstrun.save()
		configfile.save()

	def hasPartitions(self):
		partitions = fileReadLines("/proc/partitions", source=MODULE_NAME)
		count = 0
		black = BoxInfo.getItem("mtdblack")
		for line in partitions:
			parts = line.strip().split()
			if parts:
				device = parts[3]
				if not device.startswith(black) and (search(r"^sd[a-z][1-9][\d]*$", device) or search(r"^mmcblk[\d]p[\d]*$", device)):
					count += 1
		return count > 0

		# ------------------------------------------------------------------
	# Network setup steps.
	#
	# nwifaceselect (adapter list) → nwconfig (NetworkAdapterSetup) → either:
	#   - LAN: poll for an IP, then skip past nwstatus straight to nwdns
	#   - WLAN, activated on save: Wi-Fi scan + connection setup, then land on
	#     nwstatus to show the result (Continue → nwdns, or Configure another
	#     interface → back to nwifaceselect)
	#   - WLAN, not activated on save: straight back to nwifaceselect
	# ------------------------------------------------------------------

	def nwListInterfaces(self):
		result = []
		for interface, adapter in networkManager.adapters.items():
			result.append((f"{_("Wi-Fi") if adapter.isWiFi else _("LAN")}  ({interface})  –  {networkManager.getFriendlyAdapterDescription(interface)}", interface))
		result.append((_("Skip network setup"), "skip"))
		return result

	def nwIfaceSelected(self, value):
		self.nwSelectedIface = None if value == "skip" else value

	def nwIfaceMoved(self):  # Called on every cursor move in startwizard.xml's interface list; no-op here, subclasses may override.
		pass

	def nwAdvanceFromSelect(self):
		self.currStep = self.getStepWithID("network" if self.nwSelectedIface is None else "nwconfig")
		self.afterAsyncCode()

	def nwOpenSetup(self):
		def nwPollIp():
			def ip4Str(addr):
				joined = ".".join(str(x) for x in addr)
				return "" if joined == "0.0.0.0" else joined

			adapter = networkManager.adapters.get(self.nwSelectedIface)
			if adapter is not None:
				# Check the IP via netInfo's link state, not a raw address lookup - a stale
				# address can survive on a down interface and would look "connected" too
				# early. Same check as NetworkWiFiActivator.checkIp().
				networkManager.applyNetinfo()
				netInfo = adapter.netInfo
				ip = ip4Str(netInfo.ip)
				if netInfo.link and ip:
					self.nwIpFound = ip
					self.nwDone()
					return
			self.nwPollCount += 1
			if self.nwPollCount >= self.nwPollMaxAttempts:
				self.nwDone()
				return
			self.nwPollTimer.start(self.nwPollIntervalMs, True)

		def nwStartIpPoll():
			self.nwPollCount = 0
			self.nwIpFound = ""
			if self.nwPollTimer:
				self.nwPollTimer.stop()
			self.nwPollTimer = eTimer()
			self.nwPollTimer.callback.append(nwPollIp)
			# Check immediately instead of waiting a full interval first – activateInterface()
			# already waited for ifup/DHCP, so the IP is often already there.
			nwPollIp()

		def nwWifiFlowDone(ip=""):
			# NetworkWiFi already ran NetworkWiFiActivator (ifup + wpa_supplicant
			# + IP poll) and reports the result here, so there is nothing left to activate
			# or poll for. Show the result on the status step, same as the LAN path.
			print(f"[WizardStart] nwWifiFlowDone called, ip={ip}")
			self.nwSubFlowActive = False
			self.nwIpFound = ip
			self.nwShowStatusStep()

		def nwAdapterSetupDone(saved=False):
			# NetworkAdapterSetup.keySave() closes with (False, True); keyCancel()
			# closes with no args, so "saved" is only truthy after an actual save.
			print(f"[WizardStart] nwAdapterSetupDone: saved={saved!r}")
			# NetworkAdapterSetup.keySave() already called networkManager.save(),
			# which now applies whatever the adapter needs (ifup/ifdown, or a
			# full restart) itself based on what actually changed.
			if adapter.isWiFi:
				if saved and adapter.adapterEnabled:
					# The adapter was just activated – jump straight into the Wi-Fi
					# scan/connect flow instead of leaving the user stuck with an
					# enabled adapter and no SSID configured. Each screen in this
					# chain (scan → connection setup → activator) opens itself from
					# within the previous one's close callback, and Session.close()
					# briefly restores this Wizard as the current dialog in between
					# (see the comment in StartEnigma.Session.close()) – long enough
					# to re-fire onShown/updateValues() on the "nwconfig" step and
					# reopen NetworkAdapterSetup, which looks like an infinite loop.
					# nwSubFlowActive blocks that spurious re-entry until the whole
					# Wi-Fi flow really is done.
					self.nwSubFlowActive = True
					NetworkWiFiAddFlow.start(self.session, adapter=adapter, callback=nwWifiFlowDone)
				else:
					self.nwBackToList()
			else:
				nwStartIpPoll()

		if self.nwSubFlowActive:
			print("[WizardStart] nwOpenSetup: Spurious re-entry while Wi-Fi sub-flow is active -> ignored!")
			return

		try:
			adapter = networkManager.adapters.get(self.nwSelectedIface) if self.nwSelectedIface else None
			if adapter is None:
				self.nwDone()
				return
			self.session.openWithCallback(nwAdapterSetupDone, NetworkAdapterSetup, adapter)
			print("[WizardStart] nwOpenSetup: openWithCallback returned, updateValues_in_onShown=%s" % (self.updateValues in self.onShown))
		except Exception as err:
			print(f"[WizardStart] nwOpenSetup: EXCEPTION {err} -> nwDone")
			self.nwDone()

	def nwBackToList(self):
		self.nwSubFlowActive = False
		if self.nwPollTimer:
			self.nwPollTimer.stop()
			self.nwPollTimer = None
		# getStepWithID()/findStepByName() returns the enumerate() index (0-based),
		# one less than the step's real 1-based key in self.wizard – every other
		# caller in this framework (nwDone() below, afterAsyncCode()) adds this
		# same +1 to compensate. Omitting it here landed on the *previous* step
		# (nwconfig) instead of nwifaceselect, which re-ran nwOpenSetup() and
		# reopened NetworkAdapterSetup – looked like an infinite loop.
		self.currStep = self.getStepWithID("nwifaceselect") + 1
		self.updateValues()

	def nwShowStatusStep(self):
		self.nwSubFlowActive = False
		if self.nwPollTimer:
			self.nwPollTimer.stop()
			self.nwPollTimer = None
		# See the +1 note in nwBackToList() above – same off-by-one compensation.
		self.currStep = self.getStepWithID("nwstatus") + 1
		self.updateValues()

	def nwDone(self):
		self.nwSubFlowActive = False
		if self.nwPollTimer:
			self.nwPollTimer.stop()
			self.nwPollTimer = None
		self.currStep = self.getStepWithID("nwstatus") + 1
		self.updateValues()

	def nwShowStatus(self):
		if self.nwPollTimer:
			self.nwPollTimer.stop()
			self.nwPollTimer = None
		if self.nwIpFound:
			self["text"].setText(_("Network connected successfully.\n\nInterface: %s\nIP address: %s") % (self.nwSelectedIface or "", self.nwIpFound))
		else:
			self["text"].setText(_("No IP address was received.\n\nThe network connection could not be established."))


def setLanguageFromBackup(backupfile):
	try:
		import tarfile
		tar = tarfile.open(backupfile)
		for member in tar.getmembers():
			if member.name == 'etc/enigma2/settings':
				for line in tar.extractfile(member):
					line = line.decode()
					if line.startswith('config.osd.language'):
						languageToSelect = line.strip().split('=')[1]
						if languageToSelect:
							from Components.Language import language
							language.activateLanguage(languageToSelect)
							break
		tar.close()
	except:
		pass


def checkForAvailableAutoBackup():
	from os import listdir
	backupfiles = []
	try:
		for media in os.listdir("/media/"):
			mediapath = os.path.join("/media/", media)
			if os.path.isdir(mediapath):
				backupdir = os.path.join(mediapath, "backup")
				if os.path.isdir(backupdir):
					for filename in listdir(backupdir):
						if filename.endswith(".tar.gz"):
							fullpath = os.path.join(backupdir, filename)
							try:
								# Get stat info now to avoid race condition
								mtime = os.stat(fullpath).st_mtime
								backupfiles.append((fullpath, mtime))
							except OSError:
								# File disappeared between listdir and stat - skip it
								pass
	except:
		pass

	if backupfiles:
		# Sort by modification time (newest first)
		backupfiles.sort(key=lambda x: x[1], reverse=True)
		# Use the most recent backup file
		setLanguageFromBackup(backupfiles[0][0])
		return True
	return False



class AutoRestoreWizard(Screen):
	skin = """
		<screen name="AutoRestoreWizard" position="center,center" size="560,400" title="Restore settings">
			<ePixmap pixmap="buttons/red.png" position="0,0" size="140,40" alphaTest="on" />
			<ePixmap pixmap="buttons/green.png" position="140,0" size="140,40" alphaTest="on" />
			<widget source="key_red" render="Label" position="0,0" zPosition="1" size="140,40" font="Regular;20" horizontalAlignment="center" verticalAlignment="center" backgroundColor="#9f1313" transparent="1" />
			<widget source="key_green" render="Label" position="140,0" zPosition="1" size="140,40" font="Regular;20" horizontalAlignment="center" verticalAlignment="center" backgroundColor="#1f771f" transparent="1" />
			<widget name="info" position="10,50" size="540,50" font="Regular;20" halign="center" valign="center"/>
			<widget name="filelist" position="10,110" size="540,230" scrollbarMode="showOnDemand" />
		</screen>"""

	def __init__(self, session):
		Screen.__init__(self, session)
		self.setTitle(_("Restore settings from backup"))

		from Components.ActionMap import ActionMap
		from Components.Sources.StaticText import StaticText
		from Components.MenuList import MenuList
		from Components.Label import Label

		self["key_red"] = StaticText(_("Skip"))
		self["key_green"] = StaticText(_("Restore"))
		self["info"] = Label(_("Select a backup to restore:"))

		self.backupfiles = []
		self.buildFileList()

		self["filelist"] = MenuList(self.backupfiles)

		self["actions"] = ActionMap(["OkCancelActions", "ColorActions"],
		{
			"cancel": self.skip,
			"red": self.skip,
			"green": self.restore,
			"ok": self.restore,
		}, -1)

		# TNAP: Check for restore mode flags (but don't auto-restore, let user pick backup)
		self.restoreAllPlugins = False
		self.restoreNoPlugins = False
		self.checkAutoRestore()

	def checkAutoRestore(self):
		"""Check for restore mode flags created by RestoreOptionsWizard"""
		# Check for settings restore flag in /media/hdd/images/config/
		autoRestorePaths = ["/media/hdd/images/config", "/media/usb/images/config"]

		for basePath in autoRestorePaths:
			settingsFlag = os.path.join(basePath, "settings")
			pluginsFlag = os.path.join(basePath, "plugins")
			noPluginsFlag = os.path.join(basePath, "noplugins")

			if os.path.isfile(settingsFlag):
				print("[AutoRestoreWizard] Found settings restore flag at:", settingsFlag)

				if os.path.isfile(pluginsFlag):
					print("[AutoRestoreWizard] Found plugins restore flag - will restore all plugins")
					self.restoreAllPlugins = True
				elif os.path.isfile(noPluginsFlag):
					print("[AutoRestoreWizard] Found noplugins restore flag - will NOT restore plugins")
					self.restoreNoPlugins = True

				# Update info label to show restore mode
				if self.restoreAllPlugins:
					self["info"].setText(_("Select backup to restore (with all plugins):"))
				elif self.restoreNoPlugins:
					self["info"].setText(_("Select backup to restore (settings only):"))
				else:
					self["info"].setText(_("Select backup to restore:"))

				# Clean up flag files after reading them
				try:
					os.unlink(settingsFlag)
					if os.path.isfile(pluginsFlag):
						os.unlink(pluginsFlag)
					if os.path.isfile(noPluginsFlag):
						os.unlink(noPluginsFlag)
					# Clean up restore mode flags too
					for mode in ["slow", "fast", "turbo"]:
						modeFlag = os.path.join(basePath, mode)
						if os.path.isfile(modeFlag):
							os.unlink(modeFlag)
				except:
					pass

				break

	def buildFileList(self):
		from os import listdir
		backuplist = []
		try:
			for media in os.listdir("/media/"):
				mediapath = os.path.join("/media/", media)
				if os.path.isdir(mediapath):
					backupdir = os.path.join(mediapath, "backup")
					if os.path.isdir(backupdir):
						for filename in listdir(backupdir):
							if filename.endswith(".tar.gz"):
								fullpath = os.path.join(backupdir, filename)
								# Skip symlinks to avoid showing duplicates
								if not os.path.islink(fullpath):
									backuplist.append((fullpath, filename, os.stat(fullpath).st_mtime))
		except:
			pass

		# Sort by modification time, newest first
		backuplist.sort(key=lambda x: x[2], reverse=True)
		self.backupfiles = [(item[1], item[0]) for item in backuplist]  # (display name, full path)

	def skip(self):
		self.close()

	def restore(self):
		if self.backupfiles:
			selected = self["filelist"].getCurrent()
			if selected:
				filename = selected[1]  # full path
				self.session.openWithCallback(self.doRestore, MessageBox,
					_("Are you sure you want to restore this backup:\n%s\n\nYour receiver will restart after restore!") % selected[0],
					MessageBox.TYPE_YESNO)

	def doRestore(self, answer):
		if answer:
			selected = self["filelist"].getCurrent()
			if selected:
				self.filename = selected[1]
				from Screens.Console import Console

				# Set autoinstall flags based on restore mode
				if self.restoreAllPlugins:
					# User wants all plugins restored
					print("[AutoRestore] Restore mode: Settings + All plugins")
					if os.path.isfile("/etc/.doNotAutoinstall"):
						os.unlink("/etc/.doNotAutoinstall")
					open('/etc/.doAutoinstall', 'w').close()
				elif self.restoreNoPlugins:
					# User wants settings only, no plugins
					print("[AutoRestore] Restore mode: Settings only (no plugins)")
					if os.path.isfile("/etc/.doAutoinstall"):
						os.unlink("/etc/.doAutoinstall")
					open('/etc/.doNotAutoinstall', 'w').close()
				else:
					# Default: restore with autoinstall
					print("[AutoRestore] Restore mode: Default (with autoinstall)")
					if os.path.isfile("/etc/.doNotAutoinstall"):
						os.unlink("/etc/.doNotAutoinstall")
					open('/etc/.doAutoinstall', 'w').close()

				# TNAP: Extract backup and immediately kill Enigma2 to prevent race condition
				# Same method as RestoreMenu - no stages, no config reloading
				# This prevents Enigma2's autosave from overwriting restored settings (losing LNB configs)
				# After extraction: reset RestartUI=False and sync before kill.
				# Backups made before the doBackup() fix may contain RestartUI=True, causing an
				# immediate crash on the next start (enigma2 enters "UI restart mode" expecting
				# prior session shared state that no longer exists after SIGKILL).
				print("[AutoRestore] Restoring backup:", self.filename)
				self.session.open(Console, title=_("Restoring..."),
					cmdlist=[
						"tar -xzvf " + self.filename + " -C /",
						"sed -i 's/config\\.misc\\.RestartUI=.*/config.misc.RestartUI=False/' /etc/enigma2/settings 2>/dev/null || true",
						"sync",
						"killall -9 enigma2",
					])
		else:
			self.skip()


class AutoInstallWizard(Screen):
	skin = """<screen name="AutoInstall" position="fill" flags="wfNoBorder">
		<panel position="left" size="5%,*"/>
		<panel position="right" size="5%,*"/>
		<panel position="top" size="*,5%"/>
		<panel position="bottom" size="*,5%"/>
		<widget name="header" position="top" size="*,48" font="Regular;38" noWrap="1"/>
		<widget name="progress" position="top" size="*,24" backgroundColor="#00242424"/>
		<eLabel position="top" size="*,2"/>
		<widget name="AboutScrollLabel" font="Fixed;20" position="fill"/>
	</screen>"""

	def __init__(self, session):
		Screen.__init__(self, session)
		self["progress"] = ProgressBar()
		self["progress"].setRange((0, 100))
		self["progress"].setValue(0)
		self["AboutScrollLabel"] = ScrollLabel("")
		self["header"] = Label(_("Autoinstalling please wait for packages being updated"))

		self.logfile = open('/home/root/autoinstall.log', 'w')
		self.container = eConsoleAppContainer()
		self.container.appClosed.append(self.appClosed)
		self.container.dataAvail.append(self.dataAvail)
		self.package = None
		self.update_retries = 0
		self.start_timer = None
		self.retry_timer = None

		import glob
		mac_address = open('/sys/class/net/eth0/address', 'r').readline().strip().replace(":", "")
		autoinstallfiles = glob.glob('/media/*/backup/autoinstall%s' % mac_address) + glob.glob('/media/net/*/backup/autoinstall%s' % mac_address)
		if not autoinstallfiles:
			autoinstallfiles = glob.glob('/media/*/backup/autoinstall') + glob.glob('/media/net/*/backup/autoinstall')
		autoinstallfiles.sort(key=os.path.getmtime, reverse=True)
		for autoinstallfile in autoinstallfiles:
			if os.path.isfile(autoinstallfile):
				autoinstalldir = os.path.dirname(autoinstallfile)
				self.packages = [package.strip() for package in open(autoinstallfile).readlines()] + [os.path.join(autoinstalldir, file) for file in os.listdir(autoinstalldir) if file.endswith(".ipk")]
				if self.packages:
					self.number_of_packages = len(self.packages)
					# Delay the initial opkg update to avoid lock contention with the
					# module-level "opkg list_installed" that runs asynchronously at
					# enigma2 startup to create /etc/installed.
					self.start_timer = eTimer()
					self.start_timer.callback.append(self._doUpdate)
					self.start_timer.start(5000, True)
					return

		self.abort()

	def _doUpdate(self):
		self.container.execute("opkg update")

	def run_console(self):
		self["progress"].setValue(100 * (self.number_of_packages - len(self.packages)) / self.number_of_packages)
		try:
			open("/proc/progress", "w").write(str(self["progress"].value))
		except IOError:
			pass
		self.package = self.packages.pop(0)
		self["header"].setText(_("Autoinstalling %s") % self.package + " - %s%%" % self["progress"].value)
		try:
			if self.container.execute('opkg install "%s"' % self.package):
				raise Exception("failed to execute command!")
				self.appClosed(True)
		except Exception as e:
			self.appClosed(True)

	def dataAvail(self, data):
		if isinstance(data, bytes):
			data = data.decode()
		self["AboutScrollLabel"].appendText(data)
		self.logfile.write(data)

	def appClosed(self, retval=False):
		if retval:
			if self.package:
				self.dataAvail("An error occurred during installing %s - Please try again later\n" % self.package)
			else:
				self.dataAvail("An error occurred during opkg update - Please try again later\n")
				# Retry opkg update: the most common cause is lock contention with
				# the asynchronous "opkg list_installed" that runs at enigma2 startup.
				if self.update_retries < 3:
					self.update_retries += 1
					self.dataAvail("[AutoInstall] Retrying opkg update in 5s (attempt %d/3)...\n" % self.update_retries)
					self.retry_timer = eTimer()
					self.retry_timer.callback.append(self._doUpdate)
					self.retry_timer.start(5000, True)
					return
				self.dataAvail("[AutoInstall] opkg update failed after 3 retries, proceeding with cached package list\n")
		installed = [line.strip().split(":", 1)[1].strip() for line in open('/var/lib/opkg/status').readlines() if line.startswith('Package:')]
		self.packages = [package for package in self.packages if package not in installed]
		if self.packages:
			self.run_console()
		else:
			self["progress"].setValue(100)
			self["header"].setText(_("Autoinstalling Completed"))
			self.delay = eTimer()
			self.delay.callback.append(self.abort)
			eActionMap.getInstance().bindAction('', 0, self.abort)
			self.delay.startLongTimer(5)

	def abort(self, key=None, flag=None):
		if hasattr(self, 'delay'):
			self.delay.stop()
			eActionMap.getInstance().unbindAction('', self.abort)
			self.container.appClosed.remove(self.appClosed)
			self.container.dataAvail.remove(self.dataAvail)
		if self.start_timer is not None:
			self.start_timer.stop()
			self.start_timer = None
		if self.retry_timer is not None:
			self.retry_timer.stop()
			self.retry_timer = None
		self.container = None
		self.logfile.close()
		try:
			os.unlink("/etc/.doAutoinstall")
		except OSError:
			pass
		# After installing packages, perform full system reboot
		from Screens.Standby import TryQuitMainloop, QUIT_REBOOT
		self.session.open(TryQuitMainloop, QUIT_REBOOT)


class IncorrectBoxInfoWizard(MessageBox):
	def __init__(self, session):
		MessageBox.__init__(self, session, _("The enigma.info file for the boxinformation is not available or the content is invalid.\nPress any key to continue?"), type=MessageBox.TYPE_WARNING, timeout=20, simple=True)

	def close(self, value):
		MessageBox.close(self)


class WizardLanguage(Wizard, Rc):
	def __init__(self, session, silent=True, showSteps=False, neededTag=None):
		self.xmlfile = ["wizardlanguage.xml"]
		Wizard.__init__(self, session, showSteps=False)
		Rc.__init__(self)
		self.skinName = ["WizardLanguage", "WizardStart", "StartWizard"]
		self.oldLanguage = config.osd.language.value
		self["wizard"] = Pixmap()
		self["HelpWindow"] = Pixmap()
		self["HelpWindow"].hide()
		self.setTitle(_("Start Wizard"))

	def saveWizardChanges(self):
		config.misc.wizardLanguageEnabled.value = 0
		config.misc.wizardLanguageEnabled.save()
		configfile.save()
		if config.osd.language.value != self.oldLanguage:
			self.session.open(TryQuitMainloop, QUIT_RESTART)
		self.close()


if not os.path.isfile("/etc/installed"):
	from Components.Console import Console
	Console().ePopen("opkg list_installed | cut -d ' ' -f 1 > /etc/installed;chmod 444 /etc/installed")

# See RestoreSettings in StartEnigma.py (around line 528) for how these wizards get run.
if config.misc.firstrun.value:
	wizardManager.registerWizard(WizardLanguage, config.misc.wizardLanguageEnabled.value, priority=0)
wizardManager.registerWizard(IncorrectBoxInfoWizard, not BoxInfo.getItem("checksum"), priority=0)
wizardManager.registerWizard(AutoInstallWizard, os.path.isfile("/etc/.doAutoinstall"), priority=0)
wizardManager.registerWizard(AutoRestoreWizard, config.misc.wizardLanguageEnabled.value and config.misc.firstrun.value and checkForAvailableAutoBackup(), priority=0)
#wizardManager.registerWizard(LocaleSelection, config.misc.wizardLanguageEnabled.value, priority=10)
if OverscanWizard:
	wizardManager.registerWizard(OverscanWizard, config.misc.do_overscanwizard.value, priority=30)
wizardManager.registerWizard(WizardStart, config.misc.firstrun.value, priority=30)
#wizardManager.registerWizard(TimeWizard, config.misc.firstrun.value, priority=40)
