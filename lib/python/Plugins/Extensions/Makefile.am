installdir = $(pkglibdir)/python/Plugins/Extensions

SUBDIRS = CutListEditor FileCommander PicturePlayer MediaScanner MediaPlayer GraphMultiEPG SocketMMI DVDBurn TNAPHelp StreamlinkWrapper YTDLWrapper YTDLPWrapper

if HAVE_LIBDDVD
SUBDIRS += DVDPlayer
endif

install_PYTHON =	\
	__init__.py
