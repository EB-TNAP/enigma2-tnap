# -*- coding: utf-8 -*-
from Screens.Screen import Screen

from Components.Label import Label
from Components.Pixmap import MultiPixmap, Pixmap


class PVRState(Screen):
	def __init__(self, session):
		Screen.__init__(self, session)
		self["state"] = Label(text="")
		self["speed"] = Label()
		self["statusicon"] = MultiPixmap()


class TimeshiftState(PVRState):
	def __init__(self, session):
		PVRState.__init__(self, session)
		# Name based components used by OpenATV style skins (for example Umbra).
		self["eventname"] = Label()
		self["PTSSeekBack"] = Pixmap()
		self["PTSSeekPointer"] = Pixmap()
