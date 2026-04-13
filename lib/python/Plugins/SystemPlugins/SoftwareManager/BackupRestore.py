# -*- coding: utf-8 -*-
from Screens.Screen import Screen
from Screens.MessageBox import MessageBox
from Screens.Console import Console
from Components.ActionMap import ActionMap, NumberActionMap
from Components.Pixmap import Pixmap
from Components.Label import Label
from Components.Sources.StaticText import StaticText
from Components.MenuList import MenuList
from Components.config import config, configfile, ConfigSubsection, ConfigText, ConfigLocations
from Components.ConfigList import ConfigList, ConfigListScreen
from Components.FileList import MultiFileSelectList
from enigma import eEnv, eEPGCache
from Tools.Directories import *
from os import path, makedirs, listdir, stat, rename, remove
from datetime import date

config.plugins.configurationbackup = ConfigSubsection()
config.plugins.configurationbackup.backuplocation = ConfigText(default='/media/hdd/', visible_width=50, fixed_size=False)
# TNAP: Added Wireguard, Samba, Tuxbox/oscam, SSH keys, custom scripts to backup
# NOTE: /lib/modules/ and /lib/firmware/ are intentionally excluded — they are
# kernel-version-specific and are reinstalled by the image. Backing them up and
# restoring after an image upgrade overwrites the new kernel's modules with
# incompatible ones, causing enigma2 or the system to crash at boot.
config.plugins.configurationbackup.backupdirs = ConfigLocations(default=[eEnv.resolve('${sysconfdir}/enigma2/'), '/etc/network/interfaces', '/etc/wpa_supplicant.conf', '/etc/wpa_supplicant.ath0.conf', '/etc/wpa_supplicant.wlan0.conf', "/etc/resolv.conf", '/etc/enigma2/nameserversdns.conf', '/etc/default_gw', '/etc/hostname', '/etc/wireguard/', '/etc/tuxbox/config/', '/etc/samba/', '/etc/epgimport/', '/etc/exports', '/etc/hosts', '/usr/script/', '/usr/keys/', '/home/root/.ssh/'])


def getBackupPath():
	backuppath = config.plugins.configurationbackup.backuplocation.value
	if backuppath.endswith('/'):
		return backuppath + 'backup'
	else:
		return backuppath + '/backup'


def getBackupFilename():
	# TNAP: Create descriptive backup filename with image, model, and date
	# Format: TNAP-7-sf8008-20260216-0829.tar.gz
	from Tools.HardwareInfo import HardwareInfo
	from time import strftime

	# Get image name from /etc/issue
	imageName = "TNAP-7"
	try:
		with open('/etc/issue', 'r') as f:
			line = f.readline().strip()
			# Parse "TNAP 7" to "TNAP-7"
			if line:
				imageName = line.replace('\\n', '').replace('\\l', '').strip().replace(' ', '-')
	except:
		pass

	# Get box model
	boxModel = "unknown"
	try:
		# Try /proc/stb/info/boxtype first (more reliable)
		with open('/proc/stb/info/boxtype', 'r') as f:
			boxModel = f.read().strip()
	except:
		try:
			# Fallback to HardwareInfo
			hwinfo = HardwareInfo()
			boxModel = hwinfo.get_device_name()
		except:
			pass

	# Get current date and time in format: YYYYMMDD-HHMM
	dateStr = strftime("%Y%m%d-%H%M")

	# Create filename: TNAP-7-sf8008-20260216-0829.tar.gz
	filename = "%s-%s-%s.tar.gz" % (imageName, boxModel, dateStr)

	return filename


class BackupScreen(ConfigListScreen, Screen):
	skin = """
		<screen position="135,144" size="350,310" title="Backup is running" >
		<widget name="config" position="10,10" size="330,250" transparent="1" scrollbarMode="showOnDemand" />
		</screen>"""

	def __init__(self, session, runBackup=False):
		Screen.__init__(self, session)
		self.setTitle(_("Backup is running..."))
		self.runBackup = runBackup
		self["actions"] = ActionMap(["WizardActions", "DirectionActions"],
		{
			"ok": self.close,
			"back": self.close,
			"cancel": self.close,
		}, -1)
		self.finished_cb = None
		self.backuppath = getBackupPath()
		self.backupfile = getBackupFilename()
		self.fullbackupfilename = self.backuppath + "/" + self.backupfile
		self.list = []
		ConfigListScreen.__init__(self, self.list)
		if self.runBackup:
			self.onShown.append(self.doBackup)

	def doBackup(self):
		# Clear the RestartUI flag before saving — if this transient flag is True
		# (set during a previous restart) and gets captured in the backup, restoring
		# it causes enigma2 to enter "UI restart mode" expecting prior session state
		# that doesn't exist, resulting in an immediate crash on the next boot.
		try:
			config.misc.RestartUI.value = False
		except Exception:
			pass
		configfile.save()
		if config.plugins.softwaremanager.epgcache.value:
			eEPGCache.getInstance().save()
		try:
			if (path.exists(self.backuppath) == False):
				makedirs(self.backuppath)
			self.backupdirs = ' '.join(config.plugins.configurationbackup.backupdirs.value)
			# TNAP: Filename already includes date, no need to rename old backup
			if self.finished_cb:
				self.session.openWithCallback(self.finished_cb, Console, title=_("Backup is running..."), cmdlist=["tar -czvf " + self.fullbackupfilename + " " + self.backupdirs], finishedCallback=self.backupFinishedCB, closeOnSuccess=True)
			else:
				self.session.open(Console, title=_("Backup is running..."), cmdlist=["tar -czvf " + self.fullbackupfilename + " " + self.backupdirs], finishedCallback=self.backupFinishedCB, closeOnSuccess=True)
		except OSError:
			if self.finished_cb:
				self.session.openWithCallback(self.finished_cb, MessageBox, _("Sorry, your backup destination is not writeable.\nPlease select a different one."), MessageBox.TYPE_INFO, timeout=10)
			else:
				self.session.openWithCallback(self.backupErrorCB, MessageBox, _("Sorry, your backup destination is not writeable.\nPlease select a different one."), MessageBox.TYPE_INFO, timeout=10)

	def backupFinishedCB(self, retval=None):
		# TNAP: Create symlinks for compatibility with PLi and old TNAP restore scripts
		try:
			import os
			import time

			if not path.exists(self.fullbackupfilename):
				print("[BackupRestore] Backup file not found:", self.fullbackupfilename)
				self.close(False)
				return

			# Get MAC address for PLi-AutoBackup naming
			macaddr = ""
			try:
				with open("/sys/class/net/eth0/address", "r") as f:
					macaddr = f.read().strip().replace(":", "")
			except:
				pass

			# Create enigma2settingsbackup.tar.gz symlink (for old restore scripts)
			enigma2_link = path.join(self.backuppath, "enigma2settingsbackup.tar.gz")
			if path.exists(enigma2_link):
				if path.islink(enigma2_link) or path.isfile(enigma2_link):
					os.remove(enigma2_link)
			os.symlink(self.backupfile, enigma2_link)
			print("[BackupRestore] Created symlink: enigma2settingsbackup.tar.gz ->", self.backupfile)

			# Create PLi-AutoBackup symlink (for OpenPli restore compatibility)
			if macaddr:
				autobackup_link = path.join(self.backuppath, "PLi-AutoBackup%s.tar.gz" % macaddr)
			else:
				autobackup_link = path.join(self.backuppath, "PLi-AutoBackup.tar.gz")

			if path.exists(autobackup_link):
				if path.islink(autobackup_link) or path.isfile(autobackup_link):
					os.remove(autobackup_link)
			os.symlink(self.backupfile, autobackup_link)
			print("[BackupRestore] Created symlink: PLi-AutoBackup ->", self.backupfile)

			# Create timestamp file
			timestamp_file = path.join(self.backuppath, ".timestamp")
			with open(timestamp_file, "w") as f:
				f.write(str(int(time.time())))

			# TNAP: Create autoinstall file with package list
			print("[BackupRestore] Creating autoinstall file...")
			if macaddr:
				autoinstall_file = path.join(self.backuppath, "autoinstall%s" % macaddr)
			else:
				autoinstall_file = path.join(self.backuppath, "autoinstall")

			# Get list of installed packages that weren't in the base image
			installed_file = "/etc/installed"
			if path.exists(installed_file):
				import subprocess
				# Get currently installed packages
				result = subprocess.run(["opkg", "list_installed"], capture_output=True, text=True)
				current_packages = set(line.split()[0] for line in result.stdout.strip().split('\n') if line)

				# Get base image packages
				with open(installed_file, 'r') as f:
					base_packages = set(line.strip() for line in f if line.strip())

				# Find packages installed after base image (current - base)
				extra_packages = current_packages - base_packages

				# Write autoinstall file
				with open(autoinstall_file, 'w') as f:
					for package in sorted(extra_packages):
						f.write(package + '\n')

				# Create autoinstall symlink (without MAC address for compatibility)
				autoinstall_link = path.join(self.backuppath, "autoinstall")
				if macaddr and autoinstall_file != autoinstall_link:
					if path.exists(autoinstall_link):
						if path.islink(autoinstall_link) or path.isfile(autoinstall_link):
							os.remove(autoinstall_link)
					os.symlink(path.basename(autoinstall_file), autoinstall_link)

				print("[BackupRestore] Created autoinstall file with %d packages" % len(extra_packages))
			else:
				print("[BackupRestore] /etc/installed not found, skipping autoinstall creation")
		except Exception as e:
			print("[BackupRestore] Error creating symlinks/autoinstall:", str(e))
			pass  # Don't fail backup if symlink/autoinstall creation fails

		self.close(True)

	def backupErrorCB(self, retval=None):
		self.close(False)

	def runAsync(self, finished_cb):
		self.finished_cb = finished_cb
		self.doBackup()


class BackupSelection(Screen):
	skin = """
		<screen name="BackupSelection" position="center,center" size="560,400" title="Select files/folders to backup">
			<ePixmap pixmap="buttons/red.png" position="0,0" size="140,40" alphaTest="on" />
			<ePixmap pixmap="buttons/green.png" position="140,0" size="140,40" alphaTest="on" />
			<ePixmap pixmap="buttons/yellow.png" position="280,0" size="140,40" alphaTest="on" />
			<widget source="key_red" render="Label" position="0,0" zPosition="1" size="140,40" font="Regular;20" horizontalAlignment="center" verticalAlignment="center" backgroundColor="#9f1313" transparent="1" />
			<widget source="key_green" render="Label" position="140,0" zPosition="1" size="140,40" font="Regular;20" horizontalAlignment="center" verticalAlignment="center" backgroundColor="#1f771f" transparent="1" />
			<widget source="key_yellow" render="Label" position="280,0" zPosition="1" size="140,40" font="Regular;20" horizontalAlignment="center" verticalAlignment="center" backgroundColor="#a08500" transparent="1" />
			<widget name="checkList" position="5,50" size="550,250" transparent="1" scrollbarMode="showOnDemand" />
		</screen>"""

	def __init__(self, session):
		Screen.__init__(self, session)
		self.setTitle(_("Select files/folders to backup"))
		self["key_red"] = StaticText(_("Cancel"))
		self["key_green"] = StaticText(_("Save"))
		self["key_yellow"] = StaticText()

		self.selectedFiles = config.plugins.configurationbackup.backupdirs.value
		defaultDir = '/'
		inhibitDirs = ["/bin", "/boot", "/dev", "/autofs", "/lib", "/proc", "/sbin", "/sys", "/hdd", "/tmp", "/mnt", "/media"]
		self.filelist = MultiFileSelectList(self.selectedFiles, defaultDir, inhibitDirs=inhibitDirs)
		self["checkList"] = self.filelist

		self["actions"] = ActionMap(["DirectionActions", "OkCancelActions", "ShortcutActions"],
		{
			"cancel": self.exit,
			"red": self.exit,
			"yellow": self.changeSelectionState,
			"green": self.saveSelection,
			"ok": self.okClicked,
			"left": self.left,
			"right": self.right,
			"down": self.down,
			"up": self.up
		}, -1)
		if not self.selectionChanged in self["checkList"].onSelectionChanged:
			self["checkList"].onSelectionChanged.append(self.selectionChanged)
		self.onLayoutFinish.append(self.layoutFinished)

	def layoutFinished(self):
		idx = 0
		self["checkList"].moveToIndex(idx)
		self.selectionChanged()

	def selectionChanged(self):
		current = self["checkList"].getCurrent()[0]
		if len(current) > 2:
			text = _("Deselect") if current[2] else _("Select")
			self["key_yellow"].setText(text)

	def up(self):
		self["checkList"].up()

	def down(self):
		self["checkList"].down()

	def left(self):
		self["checkList"].pageUp()

	def right(self):
		self["checkList"].pageDown()

	def changeSelectionState(self):
		self["checkList"].changeSelectionState()
		self.selectedFiles = self["checkList"].getSelectedList()

	def saveSelection(self):
		self.selectedFiles = self["checkList"].getSelectedList()
		config.plugins.configurationbackup.backupdirs.value = self.selectedFiles
		config.plugins.configurationbackup.backupdirs.save()
		config.plugins.configurationbackup.save()
		config.save()
		self.close(None)

	def exit(self):
		self.close(None)

	def okClicked(self):
		if self.filelist.canDescent():
			self.filelist.descent()


class RestoreMenu(Screen):
	skin = """
		<screen name="RestoreMenu" position="center,center" size="560,400" title="Restore backups" >
			<ePixmap pixmap="buttons/red.png" position="0,0" size="140,40" alphaTest="on" />
			<ePixmap pixmap="buttons/green.png" position="140,0" size="140,40" alphaTest="on" />
			<ePixmap pixmap="buttons/yellow.png" position="280,0" size="140,40" alphaTest="on" />
			<widget source="key_red" render="Label" position="0,0" zPosition="1" size="140,40" font="Regular;20" horizontalAlignment="center" verticalAlignment="center" backgroundColor="#9f1313" transparent="1" />
			<widget source="key_green" render="Label" position="140,0" zPosition="1" size="140,40" font="Regular;20" horizontalAlignment="center" verticalAlignment="center" backgroundColor="#1f771f" transparent="1" />
			<widget source="key_yellow" render="Label" position="280,0" zPosition="1" size="140,40" font="Regular;20" horizontalAlignment="center" verticalAlignment="center" backgroundColor="#a08500" transparent="1" />
			<widget name="filelist" position="5,50" size="550,230" scrollbarMode="showOnDemand" />
		</screen>"""

	def __init__(self, session, plugin_path):
		Screen.__init__(self, session)
		self.setTitle(_("Restore backups"))
		self.skin_path = plugin_path

		self["key_red"] = StaticText(_("Cancel"))
		self["key_green"] = StaticText(_("Restore"))
		self["key_yellow"] = StaticText(_("Delete"))
		self["summary_description"] = StaticText("")

		self.sel = []
		self.val = []
		self.entry = False
		self.exe = False

		self.path = ""

		self["actions"] = NumberActionMap(["SetupActions"],
		{
			"ok": self.KeyOk,
			"cancel": self.keyCancel
		}, -1)

		self["shortcuts"] = ActionMap(["ShortcutActions"],
		{
			"red": self.keyCancel,
			"green": self.KeyOk,
			"yellow": self.deleteFile,
		})
		self.flist = []
		self["filelist"] = MenuList(self.flist)
		self.fill_list()
		self.onLayoutFinish.append(self.layoutFinished)

	def layoutFinished(self):
		self.checkSummary()

	def fill_list(self):
		self.flist = []
		self.path = getBackupPath()
		if (path.exists(self.path) == False):
			makedirs(self.path)
		for file in listdir(self.path):
			if (file.endswith(".tar.gz")):
				fullpath = path.join(self.path, file)
				# Skip symlinks to avoid showing duplicates
				if not path.islink(fullpath):
					self.flist.append((file))
					self.entry = True
		self.flist.sort(reverse=True)
		self["filelist"].l.setList(self.flist)

	def KeyOk(self):
		if not self.exe and self.entry:
			self.sel = self["filelist"].getCurrent()
			if self.sel:
				self.val = self.path + "/" + self.sel
				self.session.openWithCallback(self.startRestore, MessageBox, _("Are you sure you want to restore\nthe following backup:\n%s\nYour receiver will restart after the backup has been restored!") % (self.sel))

	def keyCancel(self):
		self.close()

	def keyUp(self):
		self["filelist"].up()
		self.checkSummary()

	def keyDown(self):
		self["filelist"].down()
		self.checkSummary()

	def startRestore(self, ret=False):
		if ret:
			self.exe = True
			# After extracting the backup, reset RestartUI to False in the restored settings.
			# Backups made before the doBackup() fix may contain RestartUI=True, which causes
			# enigma2 to enter "UI restart mode" expecting prior session shared state that no
			# longer exists (enigma2 was SIGKILL'd), resulting in an immediate crash on restart.
			# The sed is silent (2>/dev/null) and a no-op if the key is absent (default=False).
			# sync flushes the extracted files to disk before killing enigma2.
			self.session.open(Console, title=_("Restoring..."), cmdlist=[
				"tar -xzvf " + self.path + "/" + self.sel + " -C /",
				"sed -i 's/config\\.misc\\.RestartUI=.*/config.misc.RestartUI=False/' /etc/enigma2/settings 2>/dev/null || true",
				"sync",
				"killall -9 enigma2",
			])

	def deleteFile(self):
		if not self.exe and self.entry:
			self.sel = self["filelist"].getCurrent()
			if self.sel:
				self.val = self.path + "/" + self.sel
				self.session.openWithCallback(self.startDelete, MessageBox, _("Are you sure you want to delete\nthe following backup:\n") + self.sel)

	def startDelete(self, ret=False):
		if ret:
			self.exe = True
			print("removing:", self.val)
			if path.exists(self.val):
				remove(self.val)
			self.exe = False
			self.fill_list()

	def checkSummary(self):
		cur = self["filelist"].getCurrent()
		self["summary_description"].text = cur


class RestoreScreen(ConfigListScreen, Screen):
	skin = """
		<screen position="135,144" size="350,310" title="Restore is running..." >
		<widget name="config" position="10,10" size="330,250" transparent="1" scrollbarMode="showOnDemand" />
		</screen>"""

	def __init__(self, session, runRestore=False):
		Screen.__init__(self, session)
		self.setTitle(_("Restoring..."))
		self.runRestore = runRestore
		self["actions"] = ActionMap(["WizardActions", "DirectionActions"],
		{
			"ok": self.close,
			"back": self.close,
			"cancel": self.close,
		}, -1)
		self.finished_cb = None
		self.backuppath = getBackupPath()
		self.backupfile = getBackupFilename()
		self.fullbackupfilename = self.backuppath + "/" + self.backupfile
		self.list = []
		ConfigListScreen.__init__(self, self.list)
		if self.runRestore:
			self.onShown.append(self.doRestore)

	def doRestore(self):
		if path.exists("/proc/stb/vmpeg/0/dst_width"):
			restorecmdlist = ["tar -xzvf " + self.fullbackupfilename + " -C /", "echo 0 > /proc/stb/vmpeg/0/dst_height", "echo 0 > /proc/stb/vmpeg/0/dst_left", "echo 0 > /proc/stb/vmpeg/0/dst_top", "echo 0 > /proc/stb/vmpeg/0/dst_width", "killall -9 enigma2"]
		else:
			restorecmdlist = ["tar -xzvf " + self.fullbackupfilename + " -C /", "killall -9 enigma2"]
		if self.finished_cb:
			self.session.openWithCallback(self.finished_cb, Console, title=_("Restoring..."), cmdlist=restorecmdlist)
		else:
			self.session.open(Console, title=_("Restoring..."), cmdlist=restorecmdlist)

	def backupFinishedCB(self, retval=None):
		self.close(True)

	def backupErrorCB(self, retval=None):
		self.close(False)

	def runAsync(self, finished_cb):
		self.finished_cb = finished_cb
		self.doRestore()
