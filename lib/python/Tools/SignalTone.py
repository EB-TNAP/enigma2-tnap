# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# TNAP signal tone -- audible dish-aiming feedback
# ---------------------------------------------------------------------------
# What this is: a continuous sine tone whose pitch tracks the signal, the way
# every real satellite meter works. It exists because aiming a dish is a
# hill-climbing job -- you need to hear whether the last nudge helped, right
# now, with your hands on the mount and your eyes anywhere but the TV. A number
# on screen cannot do that and neither can speech: "twelve point five decibels"
# takes a second and a half to say, by which time you have moved.
#
# Why it is built this way (hardware-verified on an Octagon SF8008, HiSilicon,
# kernel 4.4.35):
#
#   * The box has a real ALSA card (HISI-AIAO, one playback substream) in
#     ADDITION to enigma2's /dev/dvb/adapter0/audioN decoder nodes, and it can
#     be opened by another process while enigma2 is running. That is what makes
#     any of this possible; on boxes where audio only exists behind
#     dvbaudiosink there is no path and toneAvailable() returns False.
#   * There is no aplay/alsa-utils in the image, but SoX is present and its
#     "play" writes to ALSA. ffmpeg with "-f alsa" also works and is the
#     fallback. gst-launch is present but this build has no audiotestsrc, so
#     gstreamer is not used.
#   * PCM is generated here and streamed to the player's stdin, rather than
#     playing prerecorded clips. Clips are the wrong primitive: you would need
#     dozens, each playback costs 100-300ms of process startup, and restarting
#     one four times a second sounds like a machine gun. One process, written
#     to continuously, gives a smooth glide and no restart clicks.
#
# The blocking write IS the clock. ALSA drains the pipe in real time, so the
# writer thread naturally paces itself and needs no timer of its own. The pipe
# is deliberately shrunk with F_SETPIPE_SZ, because the default 64KB would be
# ~740ms of queued audio -- the tone would lag the dish by most of a second.
#
# Threading contract, which matters given what leaked in this plugin before:
# the writer thread touches NOTHING but this object. It never sees the Screen,
# never a widget, never the frontend. The UI side only ever sets an integer
# target frequency under a lock. And there is a watchdog: if nobody calls
# update() for three seconds the tone shuts itself down, so a Screen that gets
# torn down without calling stop() cannot leave a process singing to an empty
# room or holding an fd.

import math
import os
import struct
import subprocess
import threading
import time

RATE = 44100
CHUNK = 1024                        # samples per write == 23ms of audio
PIPE_BYTES = 8192                   # ~93ms queued; keeps the tone responsive
WATCHDOG_SECS = 3.0

_TABLE_BITS = 11
_TABLE_SIZE = 1 << _TABLE_BITS      # 2048-entry single cycle
_TABLE_MASK = (_TABLE_SIZE << 16) - 1
_SINE = [int(32767.0 * math.sin(2.0 * math.pi * i / _TABLE_SIZE))
         for i in range(_TABLE_SIZE)]

# Three octaves, A3 to A6. Mapped exponentially so equal changes in signal
# sound like equal changes in pitch -- a linear Hz map wastes most of the
# audible range on the top half of the scale.
FREQ_MIN = 220.0
FREQ_MAX = 1760.0

# The "searching" state (no lock, tracking AGC) must be unmistakable by ear
# alone, because the whole point is that you are looking at the dish and not at
# the screen. Three things separate it from the locked tone, and they reinforce
# each other:
#
#   1. TIMBRE  -- a band-limited harmonic buzz instead of a pure sine.
#   2. RHYTHM  -- gated into discrete pulses instead of a continuous tone, with
#                 the PULSE RATE carrying the AGC reading. Geiger-counter
#                 mapping: faster means stronger. This is what makes AGC useful
#                 for finding an unknown bird without a known transponder.
#   3. REGISTER-- parked at the very bottom of the range, so losing lock always
#                 drops the pitch. Rising pitch must always mean better; the
#                 first version got this backwards, because AGC sits near 100%
#                 while SNR was mid-scale, so lock loss sounded like a WIN.
#
# The locked band (FREQ_MIN..FREQ_MAX above) is deliberately left alone -- it
# was verified by ear across 16dB..5dB and does not need changing.
SEARCH_FREQ_MIN = 140.0             # narrow: pitch is for character here,
SEARCH_FREQ_MAX = 260.0             # the AGC reading rides on the pulse rate
SEARCH_RATE_MIN = 1.5               # pulses/sec at 0% AGC
SEARCH_RATE_MAX = 8.0               # pulses/sec at 100% AGC


def _buildSearchTable():
	"""Buzzy but band-limited: harmonics 1..6 at 1/h. At the top of the search
	band (260Hz) the 6th harmonic is 1560Hz, far below Nyquist, so there is no
	aliasing -- which a naive square wave would have in abundance."""
	raw = []
	for i in range(_TABLE_SIZE):
		v = 0.0
		for h in (1, 2, 3, 4, 5, 6):
			v += math.sin(2.0 * math.pi * h * i / _TABLE_SIZE) / h
		raw.append(v)
	scale = 32767.0 / max(abs(v) for v in raw)
	return [int(v * scale) for v in raw]


def _buildPulseTable():
	"""One pulse period as a 0..65536 gain: smooth hump, silent either side.
	Raised cosine to the 2.5th power gives roughly a 40% duty cycle with edges
	soft enough not to click at any pulse rate."""
	out = []
	for i in range(_TABLE_SIZE):
		hump = 0.5 - 0.5 * math.cos(2.0 * math.pi * i / _TABLE_SIZE)
		out.append(int(65536.0 * (hump ** 2.5)))
	return out


_SEARCH = _buildSearchTable()
_PULSE = _buildPulseTable()
_SILENCE = b"\x00" * (CHUNK * 2)
_GLIDE = 0.32                       # per chunk; ~60ms portamento
_FADE = 0.25                        # per chunk; ~115ms in/out, no clicks

F_SETPIPE_SZ = 1031                 # Linux fcntl, kernel >= 2.6.35

# Rising pair on lock, falling pair on loss. (Hz, seconds).
# Rising pair on lock, falling pair on loss. The unlock pair ends just above
# the search band so it hands over to the buzz without a jump, and it plays
# even when the search sound is switched off -- you always get told.
CHIRP_LOCK = ((1174.7, 0.09), (1568.0, 0.13))
CHIRP_UNLOCK = ((440.0, 0.11), (277.2, 0.18))

# Ordered by preference. Each must accept raw S16_LE mono on stdin and put it
# on the default ALSA device.
_BACKENDS = (
	("sox", ["play", "-q", "--buffer", "2048",
	         "-t", "raw", "-e", "signed-integer", "-b", "16",
	         "-r", str(RATE), "-c", "1", "-"]),
	("ffmpeg", ["ffmpeg", "-hide_banner", "-loglevel", "quiet",
	            "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "-",
	            "-f", "alsa", "default"]),
)


def _tr(text):
	"""Use enigma2's gettext when running inside it, else pass through (so this
	module stays importable from a plain shell for testing)."""
	try:
		return _(text)
	except NameError:
		return text


def _which(binary):
	for d in os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(":"):
		path = os.path.join(d, binary)
		if os.path.isfile(path) and os.access(path, os.X_OK):
			return path
	return None


def pickBackend():
	"""Return (name, argv) for the first usable player, or (None, None).

	Requires an ALSA playback node as well as the binary: SoX is happy to
	install on a box whose kernel has no sound card at all.
	"""
	if not os.path.exists("/dev/snd/pcmC0D0p"):
		return None, None
	for name, argv in _BACKENDS:
		if _which(argv[0]):
			return name, argv
	return None, None


def toneAvailable():
	return pickBackend()[0] is not None


# Shared settings ------------------------------------------------------------
# Volume levels are amplitudes in percent; "0" is off. Defined here rather than
# in either plugin so the Signal finder and Positioner setup read and write the
# SAME setting -- set the level once and both screens honour it. The guard means
# it does not matter which screen imports this first, and re-importing never
# clobbers notifiers on an existing subsection.
TONE_LEVELS = ("0", "20", "35", "60")
NOLOCK_MODES = ("search", "off")


def levelName(value):
	return {"0": _tr("Off"), "20": _tr("Low"),
	        "35": _tr("Medium"), "60": _tr("High")}.get(value, value)


def noLockName(value):
	return {"search": _tr("Search pulse"), "off": _tr("Silent")}.get(value, value)


def toneConfig():
	"""Return the shared config subsection, creating it once.

	Imported lazily so this module stays usable outside enigma2 (handy for
	testing the generator from a plain shell).
	"""
	from Components.config import config, ConfigSubsection, ConfigSelection
	if not hasattr(config.plugins, "tnap_signaltone"):
		config.plugins.tnap_signaltone = ConfigSubsection()
		config.plugins.tnap_signaltone.level = ConfigSelection(
			default="0", choices=[(v, levelName(v)) for v in TONE_LEVELS])
		config.plugins.tnap_signaltone.nolock = ConfigSelection(
			default="search", choices=[(v, noLockName(v)) for v in NOLOCK_MODES])
	return config.plugins.tnap_signaltone


def cycleLevel(value):
	try:
		return TONE_LEVELS[(TONE_LEVELS.index(value) + 1) % len(TONE_LEVELS)]
	except ValueError:
		return TONE_LEVELS[0]


def cycleNoLock(value):
	try:
		return NOLOCK_MODES[(NOLOCK_MODES.index(value) + 1) % len(NOLOCK_MODES)]
	except ValueError:
		return NOLOCK_MODES[0]


def freqForPercent(pct):
	"""Map 0-100 onto the tone range, exponentially."""
	if pct < 0:
		pct = 0
	elif pct > 100:
		pct = 100
	return FREQ_MIN * (FREQ_MAX / FREQ_MIN) ** (pct / 100.0)


def searchFreqForPercent(pct):
	if pct < 0:
		pct = 0
	elif pct > 100:
		pct = 100
	return SEARCH_FREQ_MIN + (SEARCH_FREQ_MAX - SEARCH_FREQ_MIN) * (pct / 100.0)


def searchRateForPercent(pct):
	"""Pulses per second for an AGC reading. Exponential, same reasoning as
	pitch: the ear judges rate ratios, not differences."""
	if pct < 0:
		pct = 0
	elif pct > 100:
		pct = 100
	return SEARCH_RATE_MIN * (SEARCH_RATE_MAX / SEARCH_RATE_MIN) ** (pct / 100.0)


class SignalTone:
	"""Streaming pitch-mapped tone. One process, one writer thread.

	Lifecycle: start() -> update(...) repeatedly -> stop(). pause()/resume()
	fade the output without tearing the process down, for when a dialog opens
	over the top of the screen.
	"""

	def __init__(self, volume=0.35):
		self.backend = None
		self._argv = None
		self._proc = None
		self._thread = None
		self._fd = -1
		self._lock = threading.Lock()
		self._shutdown_lock = threading.Lock()
		self._running = False
		self._volume = max(0.05, min(0.85, float(volume)))
		self._target_freq = FREQ_MIN
		self._paused = False
		self._searching = True
		self._search_enabled = True
		self._pulse_rate = SEARCH_RATE_MIN
		self._locked = None
		self._chirps = []
		self._chirp_freq = 0.0
		self._chirp_left = 0
		self._chirp_snap = False
		self._watchdog = 0.0
		self._last_freq = 0.0
		self.error = None

	# -- state as the UI sees it -------------------------------------------

	def isRunning(self):
		return self._running

	def describe(self):
		"""Short status for the on-screen indicator."""
		if not self._running:
			return ""
		if self._paused:
			return _tr("muted")
		if self._searching:
			return _tr("search") if self._search_enabled else _tr("silent")
		return "%d Hz" % int(self._last_freq)

	def setVolume(self, volume):
		with self._lock:
			self._volume = max(0.05, min(0.85, float(volume)))

	def setSearchEnabled(self, enabled):
		"""Whether the no-lock AGC pulse sounds at all. Off means silence
		between the unlock chirp and the next lock -- for anyone who only
		wants to hear a signal they can actually use."""
		with self._lock:
			self._search_enabled = bool(enabled)

	# -- lifecycle ---------------------------------------------------------

	def start(self):
		"""Spawn the player and begin writing. Returns True on success."""
		if self._running:
			return True
		self.error = None
		self.backend, self._argv = pickBackend()
		if not self._argv:
			self.error = "no ALSA player available"
			return False
		try:
			self._proc = subprocess.Popen(
				self._argv,
				stdin=subprocess.PIPE,
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
				bufsize=0,
				close_fds=True)
		except (OSError, ValueError) as e:
			self.error = "%s: %s" % (self._argv[0], e)
			self._proc = None
			return False

		self._fd = self._proc.stdin.fileno()
		# Small pipe == low latency. Best effort: an old kernel or a hardened
		# one may refuse, in which case the tone simply lags a little more.
		try:
			import fcntl
			fcntl.fcntl(self._fd, F_SETPIPE_SZ, PIPE_BYTES)
		except Exception as e:
			print("[SignalTone] could not shrink pipe (%s) -- extra latency" % e)

		self._running = True
		self._paused = False
		self._locked = None
		self._watchdog = time.monotonic() + WATCHDOG_SECS
		self._thread = threading.Thread(target=self._writer, name="signaltone")
		self._thread.daemon = True
		self._thread.start()
		print("[SignalTone] started via %s" % self.backend)
		return True

	def stop(self):
		"""Stop the tone and reap the player. Idempotent, safe from any thread."""
		self._running = False
		thread = self._thread
		if thread is not None and thread is not threading.current_thread():
			thread.join(0.5)
		self._teardown()
		self._thread = None

	def _teardown(self):
		"""Close the pipe and reap the child exactly once.

		Both stop() and the writer thread can arrive here, so it is guarded.
		Everything is closed explicitly: this plugin has already been bitten
		by fd exhaustion once and a leaked pipe per Satfinder open would put
		it straight back there.
		"""
		with self._shutdown_lock:
			proc, self._proc = self._proc, None
			self._fd = -1
			if proc is None:
				return
			try:
				if proc.stdin is not None:
					proc.stdin.close()
			except (OSError, ValueError):
				pass
			# Closing stdin is EOF; the player drains its buffer and exits.
			try:
				proc.wait(timeout=1.0)
			except Exception:
				try:
					proc.terminate()
					proc.wait(timeout=1.0)
				except Exception:
					try:
						proc.kill()
						proc.wait(timeout=1.0)
					except Exception:
						pass

	def pause(self):
		with self._lock:
			self._paused = True

	def resume(self):
		with self._lock:
			self._paused = False
			self._watchdog = time.monotonic() + WATCHDOG_SECS

	# -- fed from the UI ---------------------------------------------------

	def update(self, agc_pct, snr_pct, locked):
		"""Point the tone at the current signal.

		Below lock the pitch follows AGC and the tone wobbles; above lock it
		follows SNR and goes steady. AGC is the right source pre-lock for the
		same reason the trend graph draws it -- it is the only thing moving
		while you are still finding the bird. The change in character, plus a
		chirp on the transition, is what tells you lock happened, so the pitch
		jumping between the two sources is not confusing.
		"""
		if not self._running:
			return
		agc_pct = agc_pct or 0
		with self._lock:
			self._watchdog = time.monotonic() + WATCHDOG_SECS
			self._searching = not locked
			if locked:
				self._target_freq = freqForPercent(snr_pct)
			else:
				self._target_freq = searchFreqForPercent(agc_pct)
				self._pulse_rate = searchRateForPercent(agc_pct)
			if self._locked is None:
				self._locked = locked          # first reading: no chirp
			elif locked != self._locked:
				self._locked = locked
				self._chirps = list(CHIRP_LOCK if locked else CHIRP_UNLOCK)

	# -- audio thread ------------------------------------------------------

	def _writer(self):
		"""Generate and write PCM until told to stop.

		Touches only this object -- no Screen, no widgets, no frontend. The
		write blocks while ALSA drains, which is what paces the loop.
		"""
		phase = 0
		gate_phase = 0
		gain = 0.0                       # ramped, so start/stop never click
		amp = 0                          # fixed-point level, carried between
		                                 # chunks so the ramp is continuous
		freq = FREQ_MIN
		try:
			while self._running:
				with self._lock:
					if time.monotonic() > self._watchdog:
						print("[SignalTone] watchdog: no update for %.0fs, stopping"
						      % WATCHDOG_SECS)
						break
					target = self._target_freq
					searching = self._searching
					rate = self._pulse_rate
					silent = searching and not self._search_enabled
					want = 0.0 if self._paused else self._volume
					if self._chirp_left <= 0 and self._chirps:
						self._chirp_freq, dur = self._chirps.pop(0)
						self._chirp_left = int(RATE * dur)
						self._chirp_snap = True
					chirping = self._chirp_left > 0
					if chirping:
						target = self._chirp_freq
						searching = False
						silent = False       # always announce lock changes
						self._chirp_left -= CHUNK
						if self._chirp_snap:
							self._chirp_snap = False
							freq = target      # chirps start on pitch
					self._last_freq = freq

				if silent:
					want = 0.0
				gain += (want - gain) * _FADE
				freq += (target - freq) * _GLIDE

				# An exponential ramp never actually reaches zero, so snap it
				# once it is inaudible and push real digital silence instead of
				# a buzz at -70dBFS. Keeps the pipe fed -- the write is still
				# what paces this loop -- at almost no CPU, which matters for
				# both "silent" no-lock mode and a dialog sitting open on top.
				if want <= 0.0 and gain < 0.002:
					gain = 0.0
					amp = 0
					if not self._writeAll(_SILENCE):
						break
					continue

				# Integer-only inner loop: fixed-point phase, 16 fractional
				# bits, into a power-of-two table so the wrap is a mask, and a
				# linearly ramped amplitude so chunks join seamlessly. Stepping
				# amplitude at chunk boundaries instead was worth ~1800 counts
				# at a sine peak -- a soft but real tick on every fade -- which
				# is the one thing that would make this sound cheap.
				step = int(freq * _TABLE_SIZE * 65536.0 / RATE)
				amp_end = int(gain * 65536.0)
				amp_step = (amp_end - amp) // CHUNK
				out = [0] * CHUNK

				if searching and not silent:
					# Buzz through the pulse envelope. Two lookups per sample;
					# only the search path pays for it, so the locked tone --
					# the one you use for fine aiming -- stays on the cheap loop.
					table = _SEARCH
					pulse = _PULSE
					gate_step = int(rate * _TABLE_SIZE * 65536.0 / RATE)
					for i in range(CHUNK):
						env = (amp * pulse[gate_phase >> 16]) >> 16
						out[i] = (table[phase >> 16] * env) >> 16
						phase = (phase + step) & _TABLE_MASK
						gate_phase = (gate_phase + gate_step) & _TABLE_MASK
						amp += amp_step
				else:
					table = _SINE
					for i in range(CHUNK):
						out[i] = (table[phase >> 16] * amp) >> 16
						phase = (phase + step) & _TABLE_MASK
						amp += amp_step
				amp = amp_end

				if not self._writeAll(struct.pack("<%dh" % CHUNK, *out)):
					break
		except Exception as e:
			print("[SignalTone] writer stopped: %s: %s" % (e.__class__.__name__, e))
		finally:
			self._running = False
			self._teardown()

	def _writeAll(self, buf):
		"""os.write until the buffer is gone. False if the player went away."""
		fd = self._fd
		if fd < 0:
			return False
		view = memoryview(buf)
		while view:
			try:
				written = os.write(fd, view)
			except (BrokenPipeError, OSError, ValueError):
				return False
			if written <= 0:
				return False
			view = view[written:]
		return True


