installdir = $(pkglibdir)/python/Plugins/SystemPlugins/Satfinder

SUBDIRS = meta

install_PYTHON = \
	__init__.py \
	plugin.py \
	signalbar.png

install_LTLIBRARIES = dvbreader.la

dvbreader_la_SOURCES = dvbreader.c
dvbreader_la_LDFLAGS = -avoid-version -module

