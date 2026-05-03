# -*- coding: utf-8 -*-
#
# TNAP Watch History
# Silently logs watched channels/shows with duration.
# Log file: /var/log/watched.log
#

from Plugins.Plugin import PluginDescriptor
from Screens.Screen import Screen
from Components.ActionMap import ActionMap
from Components.Sources.StaticText import StaticText
from Components.ScrollLabel import ScrollLabel
from ServiceReference import ServiceReference
from enigma import iPlayableService, eTimer
from datetime import datetime
import os

LOG_FILE = "/var/log/watched.log"
MAX_LOG_BYTES = 2 * 1024 * 1024   # rotate when file exceeds 2 MB
EPG_WAIT_MS   = 5000         # ms to wait for EPG before writing without it

_tracker = None


# ---------------------------------------------------------------------------
# Log rotation: keep the second half of the file when size limit is hit
# ---------------------------------------------------------------------------

def _rotateLog():
    try:
        if os.path.getsize(LOG_FILE) < MAX_LOG_BYTES:
            return
        with open(LOG_FILE, 'rb') as f:
            data = f.read()
        mid = len(data) // 2
        cut = data.find(b'\n', mid)
        if cut == -1:
            cut = mid
        with open(LOG_FILE, 'wb') as f:
            f.write(data[cut + 1:])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Background tracker — one instance lives for the whole session
# ---------------------------------------------------------------------------

class WatchHistoryTracker:
    def __init__(self, session):
        self.session = session
        self._start_time = None
        self._channel = ""
        self._title = ""
        self._desc = ""
        self._start_written = False

        # Timer fires if EPG doesn't arrive within EPG_WAIT_MS
        self._epg_timer = eTimer()
        self._epg_timer.callback.append(self._onEpgTimeout)

        session.nav.event.append(self._onEvent)
        # Capture service already playing before our hook was registered
        self._onServiceStart()

    def _onEvent(self, evt):
        if evt == iPlayableService.evStart:
            self._onServiceStart()
        elif evt == iPlayableService.evUpdatedEventInfo:
            self._onEpgUpdated()
        elif evt == iPlayableService.evEnd:
            self._epg_timer.stop()
            self._writeDuration()
            self._start_time = None
            self._channel = ""
            self._title = ""
            self._desc = ""
            self._start_written = False

    def _onServiceStart(self):
        nav = self.session.nav
        ref = nav.getCurrentlyPlayingServiceOrGroup()

        channel = ""
        if ref:
            try:
                channel = ServiceReference(ref).getServiceName() or ""
            except Exception:
                pass
        channel = channel.strip()

        # Skip if same channel (evStart can fire multiple times for same service)
        if channel and channel == self._channel:
            return

        # Flush previous channel
        self._epg_timer.stop()
        if self._start_time and self._channel:
            self._writeDuration()

        self._start_time = datetime.now()
        self._channel = channel
        self._title = ""
        self._desc = ""
        self._start_written = False

        if not self._channel:
            return

        # Try to get EPG right now (may already be available on init)
        self._fetchEpg()

        if self._title or self._desc:
            # EPG was available immediately — write now
            self._writeStart()
        else:
            # EPG not ready yet — wait up to EPG_WAIT_MS then write anyway
            self._epg_timer.start(EPG_WAIT_MS, True)

    def _fetchEpg(self):
        service = self.session.nav.getCurrentService()
        if not service:
            return
        try:
            info = service.info()
            event = info and info.getEvent(0)
            if event:
                self._title = (event.getEventName() or "").strip()
                short    = (event.getShortDescription()    or "").strip()
                extended = (event.getExtendedDescription() or "").strip()
                desc = extended or short
                self._desc = " ".join(desc.split()) if desc else ""
        except Exception:
            pass

    def _onEpgUpdated(self):
        if self._start_written:
            return
        self._fetchEpg()
        if self._title or self._desc:
            self._epg_timer.stop()
            self._writeStart()

    def _onEpgTimeout(self):
        if not self._start_written:
            self._fetchEpg()   # retry — EPG cache may be populated by now
            self._writeStart()

    def _writeStart(self):
        if not self._channel or not self._start_time or self._start_written:
            return
        self._start_written = True
        start = self._start_time.strftime("%Y-%m-%d %H:%M:%S")
        parts = [start, self._channel]
        if self._title:
            parts.append(self._title)
        if self._desc:
            parts.append(self._desc)
        line = " | ".join(parts) + "\n"
        try:
            _rotateLog()
            with open(LOG_FILE, 'a') as f:
                f.write(line)
        except Exception:
            pass

    def _writeDuration(self):
        if not self._start_time or not self._channel:
            return
        secs = int((datetime.now() - self._start_time).total_seconds())
        if secs < 10:
            return
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        dur = "%d:%02d:%02d" % (h, m, s)
        stop = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [stop, "watched %s" % dur, self._channel]
        if self._title:
            parts.append(self._title)
        line = " | ".join(parts) + "\n"
        try:
            with open(LOG_FILE, 'a') as f:
                f.write(line)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Plugin entry points
# ---------------------------------------------------------------------------

def autostart(reason, **kwargs):
    if reason == 0:
        session = kwargs.get("session")
        if session:
            global _tracker
            _tracker = WatchHistoryTracker(session)


def openViewer(session, **kwargs):
    session.open(WatchHistoryViewer)


def Plugins(**kwargs):
    return [
        PluginDescriptor(
            name=_("TNAP Watch History"),
            description=_("Silently logs watched channels and shows"),
            where=PluginDescriptor.WHERE_SESSIONSTART,
            needsRestart=False,
            fnc=autostart,
        ),
        PluginDescriptor(
            name=_("TNAP Watch History"),
            description=_("View recently watched channels and shows"),
            where=PluginDescriptor.WHERE_PLUGINMENU,
            needsRestart=False,
            fnc=openViewer,
        ),
    ]


# ---------------------------------------------------------------------------
# Viewer screen
# ---------------------------------------------------------------------------

class WatchHistoryViewer(Screen):
    skin = """
        <screen name="WatchHistoryViewer" position="center,center" size="960,580"
                title="Watch History" resolution="1280,720">
            <ePixmap pixmap="buttons/red.png"    position="0,0"   size="140,40" alphatest="on" />
            <ePixmap pixmap="buttons/yellow.png" position="160,0" size="140,40" alphatest="on" />
            <widget source="key_red"    render="Label" position="0,0"   zPosition="1"
                    size="140,40" font="Regular;20" halign="center" valign="center"
                    backgroundColor="#9f1313" transparent="1" />
            <widget source="key_yellow" render="Label" position="160,0" zPosition="1"
                    size="140,40" font="Regular;20" halign="center" valign="center"
                    backgroundColor="#a08000" transparent="1" />
            <widget source="key_info"   render="Label" position="320,0" size="640,40"
                    font="Regular;18" valign="center" foregroundColor="#00fff000" />
            <widget name="text" position="0,50" size="960,520" font="Regular;20" />
        </screen>"""

    def __init__(self, session):
        Screen.__init__(self, session)
        self.setTitle(_("Watch History"))

        self["key_red"]    = StaticText(_("Close"))
        self["key_yellow"] = StaticText(_("Clear Log"))
        self["key_info"]   = StaticText(_("Up/Down: scroll    Yellow: clear log"))
        self["text"]       = ScrollLabel()

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions", "DirectionActions"],
            {
                "ok":     self.close,
                "cancel": self.close,
                "red":    self.close,
                "yellow": self.clearLog,
                "up":     self["text"].pageUp,
                "down":   self["text"].pageDown,
            }, -1)

        self["text"].setText(self._loadLog())

    def _loadLog(self):
        if not os.path.isfile(LOG_FILE):
            return _("No watch history yet.\n\nChannels will be logged here as you watch them.")
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if not lines:
                return _("Log is empty.")
            lines.reverse()
            header = _("Timestamp             Channel | Show Title | Description\n")
            header += "-" * 70 + "\n"
            return header + "".join(lines)
        except Exception as e:
            return _("Could not read log: %s") % str(e)

    def clearLog(self):
        try:
            if os.path.isfile(LOG_FILE):
                os.remove(LOG_FILE)
            self["text"].setText(_("Log cleared."))
        except Exception as e:
            self["text"].setText(_("Could not clear log: %s") % str(e))
