#ifndef __lib_dvb_specs_h
#define __lib_dvb_specs_h

#include <lib/dvb/idvb.h>
#include <lib/dvb/idemux.h>
#include <dvbsi++/program_map_section.h>
#include <dvbsi++/service_description_section.h>
#include <dvbsi++/network_information_section.h>
#include <dvbsi++/bouquet_association_section.h>
#include <dvbsi++/program_association_section.h>
#include <dvbsi++/event_information_section.h>
#include <dvbsi++/application_information_section.h>

struct eDVBPMTSpec
{
	eDVBTableSpec m_spec;
public:
	eDVBPMTSpec(int pid, int sid, int timeout = 20000)
	{
		m_spec.pid     = pid;
		m_spec.tid     = ProgramMapSection::TID;
		m_spec.tidext  = sid;
		m_spec.timeout = timeout; // ProgramMapSection::TIMEOUT;
		m_spec.flags   = eDVBTableSpec::tfAnyVersion |
			eDVBTableSpec::tfHaveTID | eDVBTableSpec::tfHaveTIDExt |
			eDVBTableSpec::tfCheckCRC | eDVBTableSpec::tfHaveTimeout;
	}
	operator eDVBTableSpec &()
	{
		return m_spec;
	}
};

struct eDVBSDTSpec
{
	eDVBTableSpec m_spec;
public:
	eDVBSDTSpec()
	{
		m_spec.pid     = ServiceDescriptionSection::PID;
		m_spec.tid     = ServiceDescriptionSection::TID;
		/*
		 * from Digital Video Broadcasting (DVB) Guidelines on implementation and usage of Service Information (SI)
		 * DVB Document A005 June 2017:
		 *
		 * "all sections of the SDT for the actual multiplex shall be transmitted at least every 2 s"
		 * "all sections of the SDT for other TSs shall be transmitted at least every 10 s if present"
		 */
		m_spec.timeout = 2500; // ServiceDescriptionSection::TIMEOUT;
		m_spec.flags   = eDVBTableSpec::tfAnyVersion |
			eDVBTableSpec::tfHaveTID | eDVBTableSpec::tfCheckCRC |
			eDVBTableSpec::tfHaveTimeout;
	}
	eDVBSDTSpec(int tsid, bool other=false)
	{
		m_spec.pid     = ServiceDescriptionSection::PID;
		m_spec.tid     = ServiceDescriptionSection::TID;
		m_spec.tidext  = tsid;
		/*
		 * from Digital Video Broadcasting (DVB) Guidelines on implementation and usage of Service Information (SI)
		 * DVB Document A005 June 2017:
		 *
		 * "all sections of the SDT for the actual multiplex shall be transmitted at least every 2 s"
		 * "all sections of the SDT for other TSs shall be transmitted at least every 10 s if present"
		 */
		m_spec.timeout = other ? 10500 : 2500; // ServiceDescriptionSection::TIMEOUT;
		m_spec.flags   = eDVBTableSpec::tfAnyVersion |
			eDVBTableSpec::tfHaveTID | eDVBTableSpec::tfCheckCRC |
			eDVBTableSpec::tfHaveTIDExt | eDVBTableSpec::tfHaveTimeout;
		if (other)
		{
			// SDT other transport stream have TID 0x46 (current is 0x42)
			// so we mask out the third bit in table id mask..
			m_spec.flags |= eDVBTableSpec::tfHaveTIDMask;
			m_spec.tid_mask = 0xFB;
		}
	}
	/*
	 * Override the default timeout, chainable:
	 *   eDVBSDTSpec(tsid, true).setTimeout(x)
	 * Used by the channel scan to give narrowband (low symbol rate)
	 * transponders more time to deliver their SI tables; the DVB
	 * repetition guidelines above are frequently violated by SCPC
	 * feed transponders.
	 */
	eDVBSDTSpec &setTimeout(int timeout)
	{
		m_spec.timeout = timeout;
		return *this;
	}
	operator eDVBTableSpec &()
	{
		return m_spec;
	}
};

struct eDVBNITSpec
{
	eDVBTableSpec m_spec;
public:
	eDVBNITSpec(int networkid = 0)
	{
		m_spec.pid     = NetworkInformationSection::PID;
		m_spec.tid     = NetworkInformationSection::TID;
		//m_spec.timeout = NetworkInformationSection::TIMEOUT;
		m_spec.timeout = 30000;  // Some Australian broadcasters don't send complete NIT every 10 seconds as required by standards
		m_spec.flags   = eDVBTableSpec::tfAnyVersion |
			eDVBTableSpec::tfHaveTID | eDVBTableSpec::tfCheckCRC |
			eDVBTableSpec::tfHaveTimeout;
		/*
		 * Also receive NIT-other (TID 0x41, e.g. transponders advertised
		 * for sibling networks on the same orbital position). This finds
		 * additional transponders during network scans; entries pointing
		 * to other satellites are validated and dropped by eDVBScan via
		 * the orbital position check.
		 */
		m_spec.flags |= eDVBTableSpec::tfHaveTIDMask;
		m_spec.tid_mask = 0xFE; /* match 0x40 (actual) and 0x41 (other) */
		if (networkid)
		{
			m_spec.flags |= eDVBTableSpec::tfHaveTIDExt | eDVBTableSpec::tfHaveTIDExtMask;
			m_spec.tidext = networkid;
			m_spec.tidext_mask = 0xFFFF;
		}
	}
	operator eDVBTableSpec &()
	{
		return m_spec;
	}
};

struct eDVBBATSpec
{
	eDVBTableSpec m_spec;
public:
	eDVBBATSpec()
	{
		m_spec.pid     = BouquetAssociationSection::PID;
		m_spec.tid     = BouquetAssociationSection::TID;
		m_spec.timeout = BouquetAssociationSection::TIMEOUT;
		m_spec.flags   = eDVBTableSpec::tfAnyVersion |
			eDVBTableSpec::tfHaveTID | eDVBTableSpec::tfCheckCRC |
			eDVBTableSpec::tfHaveTimeout;
	}
	operator eDVBTableSpec &()
	{
		return m_spec;
	}
};

struct eDVBPATSpec
{
	eDVBTableSpec m_spec;
public:
	eDVBPATSpec(int timeout=20000)
	{
		m_spec.pid     = ProgramAssociationSection::PID;
		m_spec.tid     = ProgramAssociationSection::TID;
		m_spec.timeout = timeout; // ProgramAssociationSection::TIMEOUT;
		m_spec.flags   = eDVBTableSpec::tfAnyVersion |
			eDVBTableSpec::tfHaveTID | eDVBTableSpec::tfCheckCRC |
			eDVBTableSpec::tfHaveTimeout;
	}
	operator eDVBTableSpec &()
	{
		return m_spec;
	}
};

class eDVBEITSpec
{
	eDVBTableSpec m_spec;
public:
		/* this is for now&next on actual transponder. */
	eDVBEITSpec(int sid)
	{
		m_spec.pid     = EventInformationSection::PID;
		m_spec.tid     = EventInformationSection::TID;
		m_spec.tidext  = sid;
		m_spec.timeout = EventInformationSection::TIMEOUT;
		m_spec.flags   = eDVBTableSpec::tfAnyVersion |
			eDVBTableSpec::tfHaveTID | eDVBTableSpec::tfHaveTIDExt |
			eDVBTableSpec::tfCheckCRC | eDVBTableSpec::tfHaveTimeout;
	}
	operator eDVBTableSpec &()
	{
		return m_spec;
	}
};

class eDVBEITSpecOther
{
	eDVBTableSpec m_spec;
public:
		/* this is for now&next on actual transponder. */
	eDVBEITSpecOther(int sid)
	{
		m_spec.pid     = EventInformationSection::PID;
		m_spec.tid     = TID_EIT_OTHER;
		m_spec.tidext  = sid;
		m_spec.timeout = EventInformationSection::TIMEOUT;
		m_spec.flags   = eDVBTableSpec::tfAnyVersion |
			eDVBTableSpec::tfHaveTID | eDVBTableSpec::tfHaveTIDExt |
			eDVBTableSpec::tfCheckCRC | eDVBTableSpec::tfHaveTimeout;
	}
	operator eDVBTableSpec &()
	{
		return m_spec;
	}
};

struct eDVBAITSpec
{
	eDVBTableSpec m_spec;
public:
	eDVBAITSpec(int pid)
	{
		m_spec.pid     = pid;
		m_spec.tid     = ApplicationInformationSection::TID;
		m_spec.timeout = ApplicationInformationSection::TIMEOUT;
		m_spec.flags   = eDVBTableSpec::tfAnyVersion |
			eDVBTableSpec::tfHaveTID | eDVBTableSpec::tfCheckCRC |
			eDVBTableSpec::tfHaveTimeout;
	}
	operator eDVBTableSpec &()
	{
		return m_spec;
	}
};

struct eDVBDSMCCDLDataSpec
{
	eDVBTableSpec m_spec;
public:
	eDVBDSMCCDLDataSpec(int pid)
	{
		m_spec.pid     = pid;
		m_spec.tid     = TID_DSMCC_DL_DATA;
		m_spec.timeout = 20000;
		m_spec.flags   = eDVBTableSpec::tfAnyVersion |
			eDVBTableSpec::tfHaveTID | eDVBTableSpec::tfCheckCRC |
			eDVBTableSpec::tfHaveTimeout;
	}
	operator eDVBTableSpec &()
	{
		return m_spec;
	}
};

#endif
