# -*- coding: utf-8 -*-
# TNAP receiver identifier.
#
# Produces a stable pseudonymous identifier for the physical receiver,
# derived from the eth0 hardware address. The address itself is never
# transmitted or stored. The value is recomputed on demand and is
# deliberately not cached to disk: a stored copy could be transplanted
# to another receiver by a settings restore, a recomputed one cannot.
#
# See TNAP_Receiver_ID_Implementation.md

import os
import re
from hashlib import sha256

from Components.SystemInfo import BoxInfo

# Global TNAP constant. NEVER change this value without also bumping
# RID_PREFIX below -- changing it silently re-identifies every receiver.
RID_SALT = "tnap-feed-rid-v1"
RID_PREFIX = "1-"
UA_FORMAT_VERSION = "TNAP-Feed/1.0"

MAC_PATH = "/sys/class/net/eth0/address"
OPT_OUT_PATH = "/etc/enigma2/no-feed-id"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _readMac():
	try:
		with open(MAC_PATH, "r") as fd:
			mac = fd.read().strip().lower()
	except OSError:
		return None
	if len(mac) != 17 or mac.count(":") != 5:
		return None
	if mac == "00:00:00:00:00:00":
		return None
	try:
		first = int(mac[:2], 16)
	except ValueError:
		return None
	if first & 0x03:  # multicast or locally administered
		return None
	return mac


def receiverId():
	"""Return the receiver identifier, or None if it cannot be derived."""
	if os.path.exists(OPT_OUT_PATH):
		return None
	mac = _readMac()
	if not mac:
		return None
	digest = sha256(("%s:%s" % (RID_SALT, mac)).encode("utf-8")).hexdigest()
	return "%s%s" % (RID_PREFIX, digest[:12])


def _field(key):
	return _SAFE.sub("", str(BoxInfo.getItem(key) or ""))


def feedUserAgent():
	"""Return the TNAP feed User-Agent, or None if no identifier is available."""
	rid = receiverId()
	if not rid:
		return None
	return "%s (rid=%s; model=%s; image=%s; build=%s)" % (
		UA_FORMAT_VERSION, rid, _field("model"), _field("imageversion"), _field("imagebuild"))
