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
					tpstatus = self._takeTpSignal()

					try:
						xml = "\n< Transponder SNR =['%s' raw=%s -%s], Strength = %.1f%% >\n< %s /> " %(self.signaltp, self.signaltp3, tpstatus, self.signaltp2, strftime("%a, %d %b %Y %H:%M:%S", localtime()))
						f = open(self.location, "a")
						f.writelines(xml)
					except:
						print("Non-Satellite Scan!")
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
						except KeyError:
							sat_name = ''
						if orb_pos > 1800:
							orb_pos = 3600 - orb_pos
							h = _('W')
						else:
							h = _('E')
						try:
							self.network1 = ("%d.%d%s") % ( orb_pos / 10, orb_pos % 10, h)  ##
						except:
							pass
						if '%d.%d' % (orb_pos / 10, orb_pos % 10) in sat_name:
							network = sat_name
						else:
							network = '%s %d.%d %s' % (sat_name, orb_pos / 10, orb_pos % 10, h)
						if "Ku-band" in sat_name:
							self.network1 += "_Ku-band"
						if "Ku-band" not in sat_name:
							self.network1 += "_C-band"
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
					xml = ['                         Scan Report \n\n']
					xml.append ('< File created on %s > \n' %(strftime("%A, %B %d, %Y at %H:%M:%S")))
					xml.append ("< Satellite =  %s >\n" % network) 
					xml.append ("< Receiver = %s %s >\n" %(BOX_MODEL, BOX_NAME))
					try:
					    xml.append ('< Enigma2 Image = %s > \n' % (about.getImageTypeString()))
					except:
					    from boxbranding import getImageVersion
					    xml.append ('< Enigma2 Image = %s > \n' % (getImageVersion()))
					xml.append ('< Kernel Version = %s > \n' % (about.getKernelVersionString()))
					xml.append ('< DVB Driver Date = %s > \n' % (about.getDriverInstalledDate()))
					xml.append ('< Blindscan Frequency Range = %s to %s MHz > \n' % (self.freq1, self.freq2))
					xml.append ('< Blindscan Symbol Rate Range = %s to %s Msps > \n' % (self.symbol1, self.symbol2))
					xml.append ('< Blindscan Only Free Channels or Services? = %s  > \n' % (self.free))
					if self.tuner != "":
					    xml.append ("< Tuner = %s >\n" % self.tuner)
					f = open(self.location, "w")
					f.writelines(xml)

				if tpnumb != 0:                                                                                                                                                                                                ##
					f = open(self.location, "a")
					xml = "\n\n< '%s' > Tp# %s" %(tp_text, tpnumb)
					f.writelines(xml)
				self.network.setText(network)
				self.transponder.setText(tp_text)
		if self.state == self.DonePartially:
			runtime = int(time()) - int(self.start_time)
			runtime = runtime + self.start_time1
			self.foundServices += self.scan.getNumServices()
			T = self.foundServices - self.r

			try:
				tpstatus = self._takeTpSignal()
				f = open(self.location, "a")
				if self.start_time1 > 10:
					self.transponder.setText(_("Blind Scan Time = %d Min.  %02d Sec.")  %( runtime / 60, (runtime % 60)))
					try:
						xml = "\n< Transponder SNR =['%s' raw=%s -%s], Strength = %.1f%% >\n< %s /> " %(self.signaltp, self.signaltp3, tpstatus, self.signaltp2, strftime("%a, %d %b %Y %H:%M:%S", localtime()))
					except:
						print("Non-Satellite Scan Line#300")
					xml += "< \n\nBlind Scan Time = %d Min. %02d Sec.\n"  %( runtime / 60, (runtime % 60))
				if self.start_time1 < 10:
					self.transponder.setText(_("Service Scan Time = %d Min.  %02d Sec.")  %( runtime / 60, (runtime % 60)))
					try:
						xml = "\n< Transponder SNR =['%s' raw=%s -%s], Strength = %.1f%% >\n< %s /> " %(self.signaltp, self.signaltp3, tpstatus, self.signaltp2, strftime("%a, %d %b %Y %H:%M:%S", localtime()))
						xml += "< \n\n Service Scan Completed in %d Minutes  %02d Seconds.\n\n"  %( runtime / 60, (runtime % 60))
					except:
						print("Non-Satellite Scan Line#309, FrontEnd id= ", self.feid)
						xml = "< \n\n Service Scan Completed in %d Minutes  %02d Seconds.\n\n"  %( runtime / 60, (runtime % 60))			
				self.text.setText(_("%d Channels ( TV = %d  Radio = %d)  %d of %d Transponders Scanned.")  %( self.foundServices, T, self.r, (self.t-1), self.tt)) 
				xml += ("%d Channels ( TV = %d  Radio = %d)  %d of %d Transponders Scanned.\n\n")  %( self.foundServices, T, self.r, (self.t-1), self.tt) 
				f.writelines(xml)
				f.close()
				self.rename = '%s/%s_%sch_%stp_%s.xml' %(self.xml_dir, self.network1, self.foundServices, self.tt, strftime("%m-%d-%Y_%H-%M-%S"))
				os.rename(self.location,self.rename)  # ServiceScan_
			except:
				self.transponder.setText(_("Scan Error!!Press exit to abort"))
		if self.state == self.Error:
			self.text.setText(_('ERROR - failed to scan (%s)!') % self.Errors[self.errorcode])
		if self.state == self.DonePartially or self.state == self.Error:
			self.delaytimer.start(100, True)

			
	def pollScanSignal(self):
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
		# Translate a raw frontend reading into on-screen SNR/AGC bars and
		# text. Mirrors the Octagon-vs-Edision logic used for the report so
		# the meter behaves correctly on both driver styles.
		locked = bool(status is not None and status & FE_HAS_LOCK)
		if not locked:
			self._setBar(self.snrSlider, 0)
			self._setBar(self.agcSlider, 0)
			if self.snrText is not None:
				self.snrText.setText("SNR ---")
			if self.agcText is not None:
				self.agcText.setText("AGC ---")
			if self.lockText is not None:
				self.lockText.setText(_("Searching..."))
			return

		# AGC / signal strength: register is a 0-65535 relative scale.
		agc_pct = None
		if strength:
			agc_pct = strength * 100.0 / 65535.0
		self._setBar(self.agcSlider, agc_pct if agc_pct is not None else 0)
		if self.agcText is not None:
			self.agcText.setText(("AGC %d%%" % int(agc_pct)) if agc_pct is not None else "AGC ---")

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
		self.scanStatusChanged()
		for x in self.scanList[self.run]["transponders"]:
			self.tt = self.tt+1
			self.scan.addInitial(x)

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
		NoName = "NoName"
		UnknownService ="(UnKnown Service)"
		f = open(self.location, "a")
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
		if newServiceRef[4] >= "2.1":                                
			if newServiceRef[4] !="B":
			    if newServiceRef[4] !="C":
			        if newServiceRef[4] !="A":
			            newServiceName += " --- UnKnown Service Type %s%s" %(newServiceRef[4],newServiceRef[5] )
		if newServiceRef[4] == "2" or newServiceRef[4] == "A":
			self.r = self.r + 1                                                                    
			newServiceName += ("   (Radio #%d)" % self.r)
		try:
			if BOX_MODEL == "edision":
			    xml = ('\n< [%s] %s  %s SNR = %.2f  /> '% (self.y, newServiceName, newServiceRef, self.signal))
			if BOX_MODEL != "edision":
			    xml = ('\n< [%s] %s  %s SNR = %s  /> '% (self.y, newServiceName, newServiceRef, self.signal))
		except:
			if BOX_MODEL == "edision":
			    xml = ('\n< [%s] %s  %s  /> '% (self.y, newServiceName, newServiceRef))
			if BOX_MODEL != "edision":
			    xml = ('\n< [%s] %s  %s  /> '% (self.y, newServiceName, newServiceRef))
		f.writelines(xml)
		self.y = self.y + 1
		self.servicelist.addItem((newServiceName, newServiceRef))
		self.lcd_summary and self.lcd_summary.updateService(newServiceName)



	def destroy(self):
		self.state = self.Idle
		if self.scan is not None:
			self.scan.statusChanged.get().remove(self.scanStatusChanged)
			self.scan.newService.get().remove(self.newService)
			self.scan = None
		return
