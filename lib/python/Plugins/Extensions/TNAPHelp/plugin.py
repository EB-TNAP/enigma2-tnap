# -*- coding: utf-8 -*-
#
# TNAP Help Viewer
# Displays plain-text guide documents from /usr/share/enigma2/help/
#

from Plugins.Plugin import PluginDescriptor
from Screens.Screen import Screen
from Components.ActionMap import ActionMap
from Components.Sources.StaticText import StaticText
from Components.ScrollLabel import ScrollLabel
from Components.MenuList import MenuList
import os

HELP_DIR = "/usr/share/enigma2/help/"


def main(session, **kwargs):
    session.open(TNAPHelpIndex)


def Plugins(**kwargs):
    return [
        PluginDescriptor(
            name=_("TNAP Help"),
            description=_("View help guides and documentation"),
            where=PluginDescriptor.WHERE_PLUGINMENU,
            needsRestart=False,
            fnc=main
        )
    ]


class TNAPHelpIndex(Screen):
    skin = """
        <screen name="TNAPHelpIndex" position="center,center" size="620,440"
                title="TNAP Help Guides" resolution="1280,720">
            <ePixmap pixmap="buttons/red.png"   position="0,0"   size="140,40" alphatest="on" />
            <ePixmap pixmap="buttons/green.png" position="160,0" size="140,40" alphatest="on" />
            <widget source="key_red"   render="Label" position="0,0"   zPosition="1"
                    size="140,40" font="Regular;20" halign="center" valign="center"
                    backgroundColor="#9f1313" transparent="1" />
            <widget source="key_green" render="Label" position="160,0" zPosition="1"
                    size="140,40" font="Regular;20" halign="center" valign="center"
                    backgroundColor="#1f771f" transparent="1" />
            <widget name="list" position="0,50" size="620,380"
                    scrollbarMode="showOnDemand" />
        </screen>"""

    def __init__(self, session):
        Screen.__init__(self, session)
        self.setTitle(_("TNAP Help Guides"))

        self["key_red"]   = StaticText(_("Close"))
        self["key_green"] = StaticText(_("Open"))

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions"],
            {
                "ok":     self.openDoc,
                "green":  self.openDoc,
                "red":    self.close,
                "cancel": self.close,
            }, -1)

        self["list"] = MenuList(self._buildList())

    def _buildList(self):
        entries = []
        if os.path.isdir(HELP_DIR):
            for fname in sorted(os.listdir(HELP_DIR)):
                if fname.endswith(".txt"):
                    title = fname[:-4].replace("-", " ").replace("_", " ").title()
                    entries.append((title, os.path.join(HELP_DIR, fname)))
        if not entries:
            entries = [(_("No documents found"), None)]
        return entries

    def openDoc(self):
        sel = self["list"].getCurrent()
        if sel and sel[1]:
            self.session.open(TNAPHelpViewer, sel[0], sel[1])


class TNAPHelpViewer(Screen):
    skin = """
        <screen name="TNAPHelpViewer" position="center,center" size="960,580"
                title="TNAP Help" resolution="1280,720">
            <ePixmap pixmap="buttons/red.png" position="0,0" size="140,40" alphatest="on" />
            <widget source="key_red"  render="Label" position="0,0" zPosition="1"
                    size="140,40" font="Regular;20" halign="center" valign="center"
                    backgroundColor="#9f1313" transparent="1" />
            <widget source="key_info" render="Label" position="160,0" size="800,40"
                    font="Regular;18" valign="center" foregroundColor="#00fff000" />
            <widget name="text" position="0,50" size="960,520" font="Regular;22" />
        </screen>"""

    def __init__(self, session, title, path):
        Screen.__init__(self, session)
        self.setTitle(title)

        self["key_red"]  = StaticText(_("Close"))
        self["key_info"] = StaticText(_("Up/Down: scroll page    OK or Exit: close"))
        self["text"]     = ScrollLabel()

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions", "DirectionActions"],
            {
                "ok":     self.close,
                "cancel": self.close,
                "red":    self.close,
                "up":     self["text"].goPageUp,
                "down":   self["text"].goPageDown,
            }, -1)

        try:
            with open(path, "r") as f:
                content = f.read()
        except Exception as e:
            content = _("Could not load document: %s") % str(e)

        self["text"].setText(content)
