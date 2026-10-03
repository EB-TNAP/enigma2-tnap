# -*- coding: utf-8 -*-
from Components.Element import Element
from Components.GUIComponent import GUIComponent
from Components.VariableText import VariableText
from Tools.CList import CList

from enigma import eButton


class Button(VariableText, GUIComponent):
	def __init__(self, text="", onClick=[]):
		GUIComponent.__init__(self)
		VariableText.__init__(self)
		self.downstream_elements = CList()  # Allows the button to also be used as a skin source, as OpenATV style skins do.
		self.master = None
		self.setText(text)
		self.onClick = onClick

	def push(self):
		for x in self.onClick:
			x()
		return 0

	def disable(self):
		pass

	def enable(self):
		pass

	def setText(self, text):
		VariableText.setText(self, text)
		self.downstream_elements.changed((Element.CHANGED_ALL,))

	text = property(VariableText.getText, setText)

	def getBoolean(self):
		return bool(self.message)

	boolean = property(getBoolean)

# Source methods:
	def connectDownstream(self, downstream):
		self.downstream_elements.append(downstream)
		if self.master is None:
			self.master = downstream

	def checkSuspend(self):
		pass

	def disconnectDownstream(self, downstream):
		self.downstream_elements.remove(downstream)
		if self.master == downstream:
			self.master = None

	GUI_WIDGET = eButton

	def postWidgetCreate(self, instance):
		instance.setText(self.text)
		instance.selected.get().append(self.push)

	def preWidgetRemove(self, instance):
		instance.selected.get().remove(self.push)
