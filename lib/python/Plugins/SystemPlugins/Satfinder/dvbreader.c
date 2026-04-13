/*
 * DVB Reader - DVB transport stream section parser for the Satfinder plugin.
 *
 * Reads SDT, NIT, BAT and PAT sections from a live demux fd and returns
 * parsed data as Python dicts/lists.  Uses poll() so there is no FD_SETSIZE
 * limit on the file descriptor number.
 */

#include <Python.h>
#include <stdlib.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <linux/dvb/dmx.h>
#include <errno.h>
#include <string.h>
#include <time.h>
#include <poll.h>

#define UNUSED(x) (void)(x)

#define MAX_SECTION_SIZE        4096
#define SECTION_HEADER_LENGTH   3
#define DEFAULT_SECTION_TIMEOUT_MS   1000
#define DEFAULT_COMPLETE_TIMEOUT_MS  15000
#define TS_PACKET_SIZE          188

/* Global settings adjustable from Python via set_timeouts() / set_retry_count() */
static int g_section_timeout_ms  = DEFAULT_SECTION_TIMEOUT_MS;
static int g_complete_timeout_ms = DEFAULT_COMPLETE_TIMEOUT_MS;
static int g_retry_count         = 8;  /* kept for API compat; not used in loop logic */

/* --------------------------------------------------------------------------
 * Helpers
 * -------------------------------------------------------------------------- */

/* Set a long integer value in a dict without leaking the temporary object. */
static void dict_set_long(PyObject *dict, const char *key, long val)
{
	PyObject *v = PyLong_FromLong(val);
	PyDict_SetItemString(dict, key, v);
	Py_DECREF(v);
}

/* Set a unicode string value in a dict without leaking the temporary object. */
static void dict_set_str(PyObject *dict, const char *key, PyObject *val)
{
	PyDict_SetItemString(dict, key, val);
	Py_DECREF(val);
}

/*
 * Convert DVB-encoded text to a Python unicode string.
 * EN 300 468 Annex A: if the first byte is < 0x20 it is an encoding
 * specifier; otherwise the default encoding is ISO-6937 (approximated
 * here as Latin-1, which is correct for the lower 160 code points).
 */
static PyObject *convert_dvb_text(unsigned char *data, int len)
{
	if (len <= 0)
		return PyUnicode_FromString("");

	/* DVB service names are often null-padded; strip trailing nulls so they
	 * don't terminate enigma2's C-string renderer mid-output. */
	while (len > 0 && data[len - 1] == 0x00)
		len--;
	if (len <= 0)
		return PyUnicode_FromString("");

	if (data[0] < 0x20) {
		unsigned char enc = data[0];
		data++; len--;

		if (len < 0)
			return PyUnicode_FromString("");

		/* 0x15 = UTF-8 */
		if (enc == 0x15)
			return PyUnicode_DecodeUTF8((const char *)data, len, "replace");

		/* 0x11 = UCS-2 BMP, big-endian */
		if (enc == 0x11)
			return PyUnicode_Decode((const char *)data, len, "utf-16-be", "replace");

		/* 0x10 = ISO charset specified in following two bytes */
		if (enc == 0x10 && len >= 3) {
			unsigned short charset = (data[0] << 8) | data[1];
			data += 2; len -= 2;
			if (charset >= 1 && charset <= 15) {
				char codec[12];
				snprintf(codec, sizeof(codec), "iso8859_%d", charset);
				return PyUnicode_Decode((const char *)data, len, codec, "replace");
			}
			return PyUnicode_DecodeLatin1((const char *)data, len, "replace");
		}

		/*
		 * 0x01-0x0B map to ISO 8859-2 through ISO 8859-12 (approx).
		 * 0x0D = ISO 8859-14, 0x0E = ISO 8859-15.
		 * Python has codecs for -2 through -11 and -13 through -15.
		 */
		static const int enc_to_iso[] = {
			0,   /* 0x00 unused */
			2,   /* 0x01 */
			3,   /* 0x02 */
			4,   /* 0x03 */
			5,   /* 0x04 */
			6,   /* 0x05 */
			7,   /* 0x06 */
			8,   /* 0x07 */
			9,   /* 0x08 */
			10,  /* 0x09 */
			11,  /* 0x0A */
			13,  /* 0x0B */
			0,   /* 0x0C undefined */
			14,  /* 0x0D */
			15,  /* 0x0E */
		};
		if (enc >= 0x01 && enc <= 0x0E) {
			int iso = enc_to_iso[enc];
			if (iso > 0) {
				char codec[12];
				snprintf(codec, sizeof(codec), "iso8859_%d", iso);
				return PyUnicode_Decode((const char *)data, len, codec, "replace");
			}
		}

		/* Unknown single-byte prefix — treat remaining as Latin-1 */
		return PyUnicode_DecodeLatin1((const char *)data, len, "replace");
	}

	/* No prefix byte: default is ISO-6937; Latin-1 is a close approximation */
	return PyUnicode_DecodeLatin1((const char *)data, len, "replace");
}

/* --------------------------------------------------------------------------
 * Demux filter setup
 * -------------------------------------------------------------------------- */

static int setup_filter(int fd, uint16_t pid, uint8_t table_id, uint8_t mask, int feid)
{
	struct dmx_sct_filter_params filter;
	UNUSED(feid);

	memset(&filter, 0, sizeof(filter));
	filter.pid = pid;
	filter.filter.filter[0] = table_id;
	filter.filter.mask[0]   = mask;
	filter.flags = DMX_IMMEDIATE_START | DMX_CHECK_CRC;
	filter.timeout = 0;

	if (ioctl(fd, DMX_SET_FILTER, &filter) == -1)
		return -1;

	return 0;
}

/* --------------------------------------------------------------------------
 * Python-callable: open / close
 * -------------------------------------------------------------------------- */

static PyObject *dvbreader_open(PyObject *self, PyObject *args)
{
	UNUSED(self);
	char *demuxer;
	int pid, table_id, mask, feid, fd;

	if (!PyArg_ParseTuple(args, "siiii", &demuxer, &pid, &table_id, &mask, &feid))
		return NULL;

	fd = open(demuxer, O_RDWR);
	if (fd < 0)
		return PyLong_FromLong(-1);

	if (setup_filter(fd, pid, table_id, mask, feid) < 0) {
		close(fd);
		return PyLong_FromLong(-1);
	}

	return PyLong_FromLong(fd);
}

static PyObject *dvbreader_close(PyObject *self, PyObject *args)
{
	UNUSED(self);
	int fd;
	if (!PyArg_ParseTuple(args, "i", &fd))
		return NULL;
	if (fd >= 0)
		close(fd);
	Py_RETURN_NONE;
}

/* --------------------------------------------------------------------------
 * Section header parser
 * -------------------------------------------------------------------------- */

static int parse_header(unsigned char *data, int len, PyObject **header_dict)
{
	if (len < SECTION_HEADER_LENGTH)
		return -1;

	unsigned char  table_id             = data[0];
	unsigned short section_length       = ((data[1] & 0x0f) << 8) | data[2];
	unsigned short table_id_ext         = 0;
	unsigned char  version_number       = 0;
	unsigned char  section_number       = 0;
	unsigned char  last_section_number  = 0;
	unsigned short transport_stream_id  = 0;
	unsigned short original_network_id  = 0;

	if (section_length > MAX_SECTION_SIZE - SECTION_HEADER_LENGTH)
		return -1;
	if (len < SECTION_HEADER_LENGTH + section_length)
		return -1;

	if (len >= 8) {
		table_id_ext        = (data[3] << 8) | data[4];
		version_number      = (data[5] >> 1) & 0x1f;
		section_number      = data[6];
		last_section_number = data[7];

		if ((table_id == 0x42 || table_id == 0x46) && len >= 10) {
			transport_stream_id = table_id_ext;
			original_network_id = (data[8] << 8) | data[9];
		} else if ((table_id == 0x40 || table_id == 0x41) && len >= 10) {
			original_network_id = table_id_ext;
		}
	}

	*header_dict = PyDict_New();
	if (!*header_dict)
		return -1;

	dict_set_long(*header_dict, "table_id",            table_id);
	dict_set_long(*header_dict, "section_length",      section_length);
	dict_set_long(*header_dict, "table_id_ext",        table_id_ext);
	dict_set_long(*header_dict, "version_number",      version_number);
	dict_set_long(*header_dict, "section_number",      section_number);
	dict_set_long(*header_dict, "last_section_number", last_section_number);

	if (table_id == 0x42 || table_id == 0x46) {
		dict_set_long(*header_dict, "transport_stream_id", transport_stream_id);
		dict_set_long(*header_dict, "original_network_id", original_network_id);
	} else if (table_id == 0x40 || table_id == 0x41) {
		dict_set_long(*header_dict, "network_id", table_id_ext);
	}

	return section_length;
}

/* --------------------------------------------------------------------------
 * SDT parser
 * -------------------------------------------------------------------------- */

static int parse_service_descriptor(unsigned char *data, int len, PyObject *service)
{
	if (len < 5)
		return -1;

	unsigned char service_type          = data[2];
	unsigned char service_provider_len  = data[3];

	if (len < 4 + service_provider_len + 1)
		return -1;

	unsigned char service_name_len = data[4 + service_provider_len];

	if (len < 5 + service_provider_len + service_name_len)
		return -1;

	dict_set_long(service, "service_type", service_type);

	if (service_provider_len > 0)
		dict_set_str(service, "provider_name",
		             convert_dvb_text(&data[4], service_provider_len));
	else
		dict_set_str(service, "provider_name", PyUnicode_FromString(""));

	if (service_name_len > 0)
		dict_set_str(service, "service_name",
		             convert_dvb_text(&data[5 + service_provider_len], service_name_len));
	else
		dict_set_str(service, "service_name", PyUnicode_FromString(""));

	return 0;
}

static int parse_sdt_content(unsigned char *data, int len, PyObject *content_list)
{
	if (len < 11)
		return -1;

	int pos = 11; /* service entries start after 11-byte SDT header */
	int services_found = 0;

	while (pos + 4 < len) {
		unsigned short service_id           = (data[pos] << 8) | data[pos + 1];
		unsigned char  running_status       = (data[pos + 3] >> 5) & 0x07;
		unsigned char  free_ca              = (data[pos + 3] >> 4) & 0x01;
		unsigned short descriptors_loop_len = ((data[pos + 3] & 0x0f) << 8) | data[pos + 4];

		pos += 5;

		if (pos + descriptors_loop_len > len)
			break;

		PyObject *service = PyDict_New();
		dict_set_long(service, "service_id",     service_id);
		dict_set_long(service, "running_status", running_status);
		dict_set_long(service, "free_ca",        free_ca);
		/* defaults overwritten if service descriptor (0x48) is present */
		dict_set_str(service, "service_name",    PyUnicode_FromString(""));
		dict_set_str(service, "provider_name",   PyUnicode_FromString(""));
		dict_set_long(service, "service_type",   0);

		int desc_end = pos + descriptors_loop_len;
		while (pos < desc_end && pos + 2 <= len) {
			unsigned char tag  = data[pos];
			unsigned char dlen = data[pos + 1];
			if (pos + 2 + dlen > len)
				break;
			if (tag == 0x48)
				parse_service_descriptor(&data[pos], dlen + 2, service);
			pos += dlen + 2;
		}
		pos = desc_end; /* guarantee forward progress */

		PyList_Append(content_list, service);
		Py_DECREF(service);
		services_found++;
	}

	return (services_found > 0) ? 0 : -1;
}

/* --------------------------------------------------------------------------
 * NIT parser
 * -------------------------------------------------------------------------- */

static int parse_nit_content(unsigned char *data, int len, PyObject *content_list)
{
	/*
	 * EN 300 468 Table 5:
	 *   bytes 0-7  : fixed header (table_id, section_length, network_id,
	 *                              version, section_num, last_section_num)
	 *   bytes 8-9  : reserved(4) + network_descriptors_length(12)
	 *   bytes 10.. : network descriptors
	 *   then       : reserved(4) + transport_stream_loop_length(12)
	 *   then       : TS entries
	 */
	int pos = 8;
	if (pos + 2 > len) return -1;

	unsigned short network_descriptors_length = ((data[pos] & 0x0f) << 8) | data[pos + 1];
	pos += 2 + network_descriptors_length;

	if (pos + 2 > len) return -1;
	unsigned short ts_loop_length = ((data[pos] & 0x0f) << 8) | data[pos + 1];
	pos += 2;

	int ts_loop_end = pos + ts_loop_length;
	while (pos < ts_loop_end && pos + 6 <= len) {
		unsigned short tsid  = (data[pos]     << 8) | data[pos + 1];
		unsigned short onid  = (data[pos + 2] << 8) | data[pos + 3];
		unsigned short dlen  = ((data[pos + 4] & 0x0f) << 8) | data[pos + 5];
		pos += 6;

		int desc_end = pos + dlen;
		while (pos < desc_end && pos + 2 <= len) {
			unsigned char  tag   = data[pos];
			unsigned char  dsize = data[pos + 1];

			if (pos + 2 + dsize > len)
				break;

			/* Satellite delivery system descriptor (EN 300 468 Table 41) */
			if (tag == 0x43 && dsize >= 11) {
				/*
				 * byte 8 of descriptor (data[pos+8]):
				 *   bit 7     : west_east_flag
				 *   bits 6-5  : polarisation
				 *   bits 4-3  : roll_off (DVB-S2 only)
				 *   bit 2     : modulation_system  (0=DVB-S, 1=DVB-S2)
				 *   bits 1-0  : modulation_type
				 */
				unsigned int frequency =
					((data[pos + 2] >> 4)  * 10000000U) +
					((data[pos + 2] & 0x0f) * 1000000U) +
					((data[pos + 3] >> 4)  *  100000U) +
					((data[pos + 3] & 0x0f) *  10000U) +
					((data[pos + 4] >> 4)  *   1000U) +
					((data[pos + 4] & 0x0f) *    100U) +
					((data[pos + 5] >> 4)  *     10U) +
					 (data[pos + 5] & 0x0f);

				unsigned short orbital_position  = (data[pos + 6] << 8) | data[pos + 7];
				unsigned char  west_east_flag    = (data[pos + 8] >> 7) & 0x01;
				unsigned char  polarization      = (data[pos + 8] >> 5) & 0x03;
				unsigned char  roll_off          = (data[pos + 8] >> 3) & 0x03;
				unsigned char  modulation_system = (data[pos + 8] >> 2) & 0x01;
				unsigned char  modulation_type   =  data[pos + 8]       & 0x03;

				unsigned int symbol_rate =
					((data[pos + 9]  >> 4)  * 1000000U) +
					((data[pos + 9]  & 0x0f) * 100000U) +
					((data[pos + 10] >> 4)  *  10000U) +
					((data[pos + 10] & 0x0f) *  1000U) +
					((data[pos + 11] >> 4)  *    100U) +
					((data[pos + 11] & 0x0f) *    10U) +
					((data[pos + 12] >> 4)  *      1U);

				unsigned char fec_inner = data[pos + 12] & 0x0f;

				PyObject *tp = PyDict_New();
				dict_set_long(tp, "transport_stream_id", tsid);
				dict_set_long(tp, "original_network_id", onid);
				dict_set_long(tp, "descriptor_tag",      tag);
				dict_set_long(tp, "frequency",           frequency * 10);
				dict_set_long(tp, "orbital_position",    orbital_position);
				dict_set_long(tp, "west_east_flag",      west_east_flag);
				dict_set_long(tp, "polarization",        polarization);
				dict_set_long(tp, "roll_off",            roll_off);
				dict_set_long(tp, "modulation_system",   modulation_system);
				dict_set_long(tp, "modulation_type",     modulation_type);
				dict_set_long(tp, "symbol_rate",         symbol_rate * 100);
				dict_set_long(tp, "fec_inner",           fec_inner);

				PyList_Append(content_list, tp);
				Py_DECREF(tp);
			}

			pos += dsize + 2;
		}
		pos = desc_end; /* guarantee forward progress */
	}

	return 0;
}

/* --------------------------------------------------------------------------
 * PMT parser
 * -------------------------------------------------------------------------- */

/*
 * Parse a PMT section.  Returns a list of dicts:
 *   [0] = { 'pcr_pid': N, 'encrypted': 0|1 }
 *   [1..] = { 'stream_type': N, 'pid': N, 'language': 'eng' }  (one per ES)
 *
 * Stream types of interest:
 *   0x01/0x02  MPEG-1/2 video
 *   0x03/0x04  MPEG-1/2 audio
 *   0x06       private data
 *   0x0f       AAC audio
 *   0x11       AAC-HE audio
 *   0x1b       H.264 video
 *   0x24       H.265/HEVC video
 *   0x81       AC-3 audio
 *   0x87       E-AC-3 audio
 */
static int parse_pmt_content(unsigned char *data, int len, PyObject *content_list)
{
	if (len < 12) return -1;
	if (data[0] != 0x02) return -1;

	int section_length = ((data[1] & 0x0f) << 8) | data[2];
	if (section_length < 9) return -1;

	unsigned short pcr_pid      = ((data[8] & 0x1f) << 8) | data[9];
	unsigned short prog_info_len = ((data[10] & 0x0f) << 8) | data[11];
	int end = 3 + section_length - 4; /* exclude 4-byte CRC */

	if (end > len) end = len;

	/* Scan program descriptors for CA */
	int encrypted = 0;
	int pi_pos = 12;
	while (pi_pos + 2 <= 12 + (int)prog_info_len && pi_pos + 2 <= len) {
		unsigned char dtag = data[pi_pos];
		unsigned char dlen = data[pi_pos + 1];
		if (dtag == 0x09) encrypted = 1;
		pi_pos += dlen + 2;
	}

	/* First entry: PCR PID + encryption flag */
	PyObject *info = PyDict_New();
	dict_set_long(info, "pcr_pid",   pcr_pid);
	dict_set_long(info, "encrypted", encrypted);
	PyList_Append(content_list, info);
	Py_DECREF(info);

	/* Elementary stream entries */
	int pos = 12 + prog_info_len;
	while (pos + 5 <= end) {
		unsigned char  stream_type  = data[pos];
		unsigned short el_pid       = ((data[pos + 1] & 0x1f) << 8) | data[pos + 2];
		unsigned short es_info_len  = ((data[pos + 3] & 0x0f) << 8) | data[pos + 4];

		PyObject *stream = PyDict_New();
		dict_set_long(stream, "stream_type", stream_type);
		dict_set_long(stream, "pid", el_pid);

		/* Scan ES descriptors for language (0x0a) */
		int dpos = pos + 5;
		int es_end = pos + 5 + es_info_len;
		while (dpos + 2 <= es_end && dpos + 2 <= len) {
			unsigned char dtag = data[dpos];
			unsigned char dlen = data[dpos + 1];
			if (dtag == 0x0a && dlen >= 3)
				dict_set_str(stream, "language",
				    PyUnicode_DecodeASCII((const char *)&data[dpos + 2], 3, "replace"));
			dpos += dlen + 2;
		}

		PyList_Append(content_list, stream);
		Py_DECREF(stream);
		pos += 5 + es_info_len;
	}

	return 0;
}

/* --------------------------------------------------------------------------
 * poll()-based timed read
 * -------------------------------------------------------------------------- */

static ssize_t read_with_timeout(int fd, void *buf, size_t count, int timeout_ms)
{
	struct pollfd pfd;
	pfd.fd      = fd;
	pfd.events  = POLLIN;
	pfd.revents = 0;

	int ret;
	Py_BEGIN_ALLOW_THREADS
	ret = poll(&pfd, 1, timeout_ms);
	Py_END_ALLOW_THREADS

	if (ret < 0)
		return (errno == EINTR) ? 0 : -1;
	if (ret == 0)
		return 0;
	if (!(pfd.revents & POLLIN))
		return 0;

	ssize_t n;
	Py_BEGIN_ALLOW_THREADS
	n = read(fd, buf, count);
	Py_END_ALLOW_THREADS
	return n;
}

/* --------------------------------------------------------------------------
 * Core section reader (wall-clock deadline loop)
 * -------------------------------------------------------------------------- */

static PyObject *read_section(int fd, uint8_t table_id,
                              uint8_t table_id_mask, uint8_t next_table_id)
{
	unsigned char buffer[MAX_SECTION_SIZE];

	/* Compute wall-clock deadline using CLOCK_MONOTONIC */
	struct timespec deadline;
	clock_gettime(CLOCK_MONOTONIC, &deadline);
	deadline.tv_sec  += g_complete_timeout_ms / 1000;
	deadline.tv_nsec += (g_complete_timeout_ms % 1000) * 1000000L;
	if (deadline.tv_nsec >= 1000000000L) {
		deadline.tv_sec++;
		deadline.tv_nsec -= 1000000000L;
	}

	while (1) {
		/* Check deadline */
		struct timespec now;
		clock_gettime(CLOCK_MONOTONIC, &now);
		if (now.tv_sec > deadline.tv_sec ||
		    (now.tv_sec == deadline.tv_sec && now.tv_nsec >= deadline.tv_nsec))
			break;

		ssize_t bytes_read = read_with_timeout(fd, buffer, sizeof(buffer),
		                                       g_section_timeout_ms);
		if (bytes_read <= 0)
			continue;

		/* Verify table ID */
		if ((buffer[0] & table_id_mask) != (table_id & table_id_mask) &&
		    (next_table_id == 0 || buffer[0] != next_table_id))
			continue;

		PyObject *header = NULL;
		int section_length = parse_header(buffer, (int)bytes_read, &header);
		if (section_length < 0)
			continue;

		PyObject *content = PyList_New(0);
		if (!content) {
			Py_DECREF(header);
			return NULL;
		}

		int parse_ok = 1;

		if (buffer[0] == 0x42 || buffer[0] == 0x46) {
			if (parse_sdt_content(buffer, section_length + 3, content) < 0)
				parse_ok = 0;
		} else if (buffer[0] == 0x40 || buffer[0] == 0x41) {
			if (parse_nit_content(buffer, section_length + 3, content) < 0)
				parse_ok = 0;
		} else if (buffer[0] == 0x00) {
			/* PAT fallback */
			if (bytes_read >= 8) {
				int pos = 8;
				while (pos + 4 <= bytes_read && pos + 4 <= section_length + 3) {
					unsigned short pgnum = (buffer[pos] << 8) | buffer[pos + 1];
					unsigned short pid   = ((buffer[pos + 2] & 0x1f) << 8) | buffer[pos + 3];
					if (pgnum != 0) {
						PyObject *svc = PyDict_New();
						dict_set_long(svc, "service_id",   pgnum);
						dict_set_long(svc, "pmt_pid",      pid);
						dict_set_long(svc, "from_pat",     1);
						dict_set_long(svc, "service_type", 1);
						dict_set_str(svc,  "service_name",
						             PyUnicode_FromFormat("Service %d", pgnum));
						dict_set_str(svc,  "provider_name",
						             PyUnicode_FromString(""));
						PyList_Append(content, svc);
						Py_DECREF(svc);
					}
					pos += 4;
				}
			}
		}

		if (!parse_ok) {
			Py_DECREF(header);
			Py_DECREF(content);
			continue;
		}

		PyObject *result = PyDict_New();
		PyDict_SetItemString(result, "header",  header);
		PyDict_SetItemString(result, "content", content);
		Py_DECREF(header);
		Py_DECREF(content);
		return result;
	}

	Py_RETURN_NONE;
}

/* --------------------------------------------------------------------------
 * Python-callable read functions
 * -------------------------------------------------------------------------- */

static PyObject *dvbreader_read_sdt(PyObject *self, PyObject *args)
{
	UNUSED(self);
	int fd, table_id, table_id_mask;
	if (!PyArg_ParseTuple(args, "iii", &fd, &table_id, &table_id_mask))
		return NULL;

	PyObject *result = read_section(fd, table_id, table_id_mask, 0);

	/* If SDT returned nothing or empty content, try PAT */
	int try_pat = (result == Py_None || result == NULL);
	if (!try_pat && result != Py_None) {
		PyObject *content = PyDict_GetItemString(result, "content");
		if (content && PyList_Size(content) == 0) {
			Py_DECREF(result);
			try_pat = 1;
		}
	}
	if (try_pat) {
		if (setup_filter(fd, 0x00, 0x00, 0xff, 0) == 0)
			result = read_section(fd, 0x00, 0xff, 0);
		else {
			Py_INCREF(Py_None);
			result = Py_None;
		}
	}

	return result;
}

static PyObject *dvbreader_read_nit(PyObject *self, PyObject *args)
{
	UNUSED(self);
	int fd, table_id, next_table_id;
	if (!PyArg_ParseTuple(args, "iii", &fd, &table_id, &next_table_id))
		return NULL;
	return read_section(fd, table_id, 0xff, next_table_id);
}

static PyObject *dvbreader_read_bat(PyObject *self, PyObject *args)
{
	int fd, table_id, bouquet_id;
	if (!PyArg_ParseTuple(args, "iii", &fd, &table_id, &bouquet_id))
		return NULL;
	UNUSED(bouquet_id);
	return read_section(fd, table_id, 0xff, 0);
}

static PyObject *dvbreader_read_fastscan(PyObject *self, PyObject *args)
{
	int fd, table_id, bouquet_id;
	if (!PyArg_ParseTuple(args, "iii", &fd, &table_id, &bouquet_id))
		return NULL;
	UNUSED(bouquet_id);
	return read_section(fd, table_id, 0xff, 0);
}

static PyObject *dvbreader_read_pmt(PyObject *self, PyObject *args)
{
	UNUSED(self);
	int fd;
	if (!PyArg_ParseTuple(args, "i", &fd))
		return NULL;

	unsigned char buffer[MAX_SECTION_SIZE];

	struct timespec deadline;
	clock_gettime(CLOCK_MONOTONIC, &deadline);
	deadline.tv_sec  += g_complete_timeout_ms / 1000;
	deadline.tv_nsec += (g_complete_timeout_ms % 1000) * 1000000L;
	if (deadline.tv_nsec >= 1000000000L) {
		deadline.tv_sec++;
		deadline.tv_nsec -= 1000000000L;
	}

	while (1) {
		struct timespec now;
		clock_gettime(CLOCK_MONOTONIC, &now);
		if (now.tv_sec > deadline.tv_sec ||
		    (now.tv_sec == deadline.tv_sec && now.tv_nsec >= deadline.tv_nsec))
			break;

		ssize_t bytes_read = read_with_timeout(fd, buffer, sizeof(buffer),
		                                       g_section_timeout_ms);
		if (bytes_read <= 0)
			continue;
		if (buffer[0] != 0x02)
			continue;

		PyObject *content = PyList_New(0);
		if (!content) return NULL;

		if (parse_pmt_content(buffer, (int)bytes_read, content) < 0) {
			Py_DECREF(content);
			continue;
		}
		return content;
	}

	Py_RETURN_NONE;
}

static PyObject *dvbreader_read_ts(PyObject *self, PyObject *args)
{
	UNUSED(self);
	int fd;
	if (!PyArg_ParseTuple(args, "i", &fd))
		return NULL;

	unsigned char buffer[TS_PACKET_SIZE];
	ssize_t bytes_read = read_with_timeout(fd, buffer, sizeof(buffer),
	                                       g_section_timeout_ms);
	if (bytes_read <= 0)
		Py_RETURN_NONE;
	return PyBytes_FromStringAndSize((char *)buffer, bytes_read);
}

/* --------------------------------------------------------------------------
 * Python-callable configuration
 * -------------------------------------------------------------------------- */

static PyObject *dvbreader_set_timeouts(PyObject *self, PyObject *args)
{
	UNUSED(self);
	int section_ms, complete_ms;
	if (!PyArg_ParseTuple(args, "ii", &section_ms, &complete_ms))
		return NULL;
	g_section_timeout_ms  = section_ms;
	g_complete_timeout_ms = complete_ms;
	Py_RETURN_NONE;
}

static PyObject *dvbreader_set_retry_count(PyObject *self, PyObject *args)
{
	UNUSED(self);
	int count;
	if (!PyArg_ParseTuple(args, "i", &count))
		return NULL;
	g_retry_count = count;
	Py_RETURN_NONE;
}

/* --------------------------------------------------------------------------
 * Python-callable parse functions (compatibility API)
 * -------------------------------------------------------------------------- */

static PyObject *dvbreader_parse_header(PyObject *self, PyObject *args)
{
	UNUSED(self);
	PyObject *byteArray;
	if (!PyArg_ParseTuple(args, "O", &byteArray))
		return NULL;

	Py_buffer buf;
	if (PyObject_GetBuffer(byteArray, &buf, PyBUF_SIMPLE) < 0)
		return NULL;

	PyObject *header = NULL;
	int rc = parse_header(buf.buf, buf.len, &header);
	PyBuffer_Release(&buf);

	if (rc < 0)
		Py_RETURN_NONE;
	return header;
}

static PyObject *dvbreader_parse_header_nit(PyObject *self, PyObject *args)
{
	return dvbreader_parse_header(self, args);
}

static PyObject *dvbreader_parse_header_bat(PyObject *self, PyObject *args)
{
	return dvbreader_parse_header(self, args);
}

static PyObject *dvbreader_parse_sdt(PyObject *self, PyObject *args)
{
	UNUSED(self);
	PyObject *byteArray;
	if (!PyArg_ParseTuple(args, "O", &byteArray))
		return NULL;

	Py_buffer buf;
	if (PyObject_GetBuffer(byteArray, &buf, PyBUF_SIMPLE) < 0)
		return NULL;

	PyObject *content = PyList_New(0);
	if (!content) { PyBuffer_Release(&buf); return NULL; }

	if (parse_sdt_content(buf.buf, buf.len, content) < 0) {
		PyBuffer_Release(&buf);
		Py_DECREF(content);
		Py_RETURN_NONE;
	}
	PyBuffer_Release(&buf);
	return content;
}

static PyObject *dvbreader_parse_nit(PyObject *self, PyObject *args)
{
	UNUSED(self);
	PyObject *byteArray;
	if (!PyArg_ParseTuple(args, "O", &byteArray))
		return NULL;

	Py_buffer buf;
	if (PyObject_GetBuffer(byteArray, &buf, PyBUF_SIMPLE) < 0)
		return NULL;

	PyObject *content = PyList_New(0);
	if (!content) { PyBuffer_Release(&buf); return NULL; }

	if (parse_nit_content(buf.buf, buf.len, content) < 0) {
		PyBuffer_Release(&buf);
		Py_DECREF(content);
		Py_RETURN_NONE;
	}
	PyBuffer_Release(&buf);
	return content;
}

static PyObject *dvbreader_parse_bat(PyObject *self, PyObject *args)
{
	UNUSED(self);
	UNUSED(args);
	return PyList_New(0);
}

static PyObject *dvbreader_parse_fastscan(PyObject *self, PyObject *args)
{
	UNUSED(self);
	UNUSED(args);
	return PyList_New(0);
}

static PyObject *dvbreader_parse_table(PyObject *self, PyObject *args)
{
	UNUSED(self);
	PyObject *byteArray;
	int table_id;
	if (!PyArg_ParseTuple(args, "Oi", &byteArray, &table_id))
		return NULL;

	Py_buffer buf;
	if (PyObject_GetBuffer(byteArray, &buf, PyBUF_SIMPLE) < 0)
		return NULL;

	PyObject *header = NULL;
	int section_length = parse_header(buf.buf, buf.len, &header);
	if (section_length < 0) { PyBuffer_Release(&buf); Py_RETURN_NONE; }

	PyObject *content = PyList_New(0);
	if (!content) { Py_DECREF(header); PyBuffer_Release(&buf); return NULL; }

	if (table_id == 0x42 || table_id == 0x46)
		parse_sdt_content(buf.buf, buf.len, content);
	else if (table_id == 0x40 || table_id == 0x41)
		parse_nit_content(buf.buf, buf.len, content);

	PyObject *result = PyDict_New();
	PyDict_SetItemString(result, "header",  header);
	PyDict_SetItemString(result, "content", content);
	Py_DECREF(header);
	Py_DECREF(content);
	PyBuffer_Release(&buf);
	return result;
}

/* --------------------------------------------------------------------------
 * Module definition
 * -------------------------------------------------------------------------- */

static PyMethodDef DvbreaderMethods[] = {
	{"open",             dvbreader_open,             METH_VARARGS, "Open a demux device for filtering."},
	{"close",            dvbreader_close,            METH_VARARGS, "Close a demux device."},
	{"read_sdt",         dvbreader_read_sdt,         METH_VARARGS, "Read SDT section."},
	{"read_nit",         dvbreader_read_nit,         METH_VARARGS, "Read NIT section."},
	{"read_bat",         dvbreader_read_bat,         METH_VARARGS, "Read BAT section."},
	{"read_fastscan",    dvbreader_read_fastscan,     METH_VARARGS, "Read FastScan section."},
	{"read_pmt",         dvbreader_read_pmt,          METH_VARARGS, "Read PMT section; returns list [{pcr_pid,encrypted}, {stream_type,pid[,language]}, ...]."},
	{"read_ts",          dvbreader_read_ts,           METH_VARARGS, "Read generic TS packet."},
	{"set_timeouts",     dvbreader_set_timeouts,      METH_VARARGS, "Set section and complete timeouts."},
	{"set_retry_count",  dvbreader_set_retry_count,   METH_VARARGS, "Set retry count (API compat)."},
	{"parse_header",     dvbreader_parse_header,      METH_VARARGS, "Parse section header."},
	{"parse_header_nit", dvbreader_parse_header_nit,  METH_VARARGS, "Parse NIT header."},
	{"parse_header_bat", dvbreader_parse_header_bat,  METH_VARARGS, "Parse BAT header."},
	{"parse_sdt",        dvbreader_parse_sdt,         METH_VARARGS, "Parse SDT content."},
	{"parse_nit",        dvbreader_parse_nit,         METH_VARARGS, "Parse NIT content."},
	{"parse_bat",        dvbreader_parse_bat,         METH_VARARGS, "Parse BAT content."},
	{"parse_fastscan",   dvbreader_parse_fastscan,    METH_VARARGS, "Parse FastScan content."},
	{"parse_table",      dvbreader_parse_table,       METH_VARARGS, "Parse any table type."},
	{NULL, NULL, 0, NULL}
};

static struct PyModuleDef dvbreadermodule = {
	PyModuleDef_HEAD_INIT,
	"dvbreader",
	"DVB Stream Reader",
	-1,
	DvbreaderMethods,
	NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC PyInit_dvbreader(void) {
	return PyModule_Create(&dvbreadermodule);
}
