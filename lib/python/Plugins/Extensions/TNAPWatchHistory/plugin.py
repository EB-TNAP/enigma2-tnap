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
from enigma import iPlayableService
from datetime import datetime
import os

LOG_FILE = "/var/log/watched.log"
MAX_LOG_BYTES = 512 * 1024   # rotate when file exceeds 512 KB

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
        # Keep everything after the midpoint newline
        mid = len(data) // 2
        cut = data.find(b'\n', mid)
        if cut == -1:
            cut = mid
        trimmed = data[cut + 1:]
        with open(LOG_FILE, 'wb') as f:
            f.write(trimmed)
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
        session.nav.event.append(self._onEvent)
        # Capture service already playing before our hook was registered
        self._captureService()

    def _onEvent(self, evt):
        if evt == iPlayableService.evStart:
            self._captureService()

    def _captureService(self):
        nav = self.session.nav
        ref = nav.getCurrentlyPlayingServiceOrGroup()
        service = nav.getCurrentService()

        channel = ""
        title = ""

        if ref:
            try:
                channel = ServiceReference(ref).getServiceName() or ""
            except Exception:
                pass

        if service:
            try:
                info = service.info()
                event = info and info.getEvent(0)
                if event:
                    title = event.getEventName() or ""
            except Exception:
                pass

        channel = channel.strip()
        title = title.strip()

        # Skip if same channel (evStart can fire multiple times for same service)
        if channel and channel == self._channel:
            return

        self._start_time = datetime.now()
        self._channel = channel
        self._title = title

        if self._channel:
            self._writeLog()

    def _writeLog(self):
        if not self._channel:
            return
        start = self._start_time.strftime("%Y-%m-%d %H:%M:%S")
        if self._title:
            line = "%s | %s | %s\n" % (start, self._channel, self._title)
        else:
            line = "%s | %s\n" % (start, self._channel)
        try:
            _rotateLog()
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
                "up":     self["text"].goPageUp,
                "down":   self["text"].goPageDown,
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
            # Show most-recent entries first
            lines.reverse()
            header = _("Start time            Channel / Show\n")
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
