#include <fcntl.h>
#include <fstream>
#include <lib/dvb/idvb.h>
#include <dvbsi++/descriptor_tag.h>
#include <dvbsi++/service_descriptor.h>
#include <dvbsi++/satellite_delivery_system_descriptor.h>
#include <dvbsi++/s2_satellite_delivery_system_descriptor.h>
#include <dvbsi++/terrestrial_delivery_system_descriptor.h>
#include <dvbsi++/t2_delivery_system_descriptor.h>
#include <dvbsi++/cable_delivery_system_descriptor.h>
#include <dvbsi++/logical_channel_descriptor.h>
#include <dvbsi++/ca_identifier_descriptor.h>
#include <dvbsi++/registration_descriptor.h>
#include <dvbsi++/extension_descriptor.h>
#include <dvbsi++/frequency_list_descriptor.h>
#include <lib/base/nconfig.h> // access to python config
#include <lib/dvb/specs.h>
#include <lib/dvb/esection.h>
#include <lib/dvb/scan.h>
#include <lib/dvb/frontend.h>
#include <lib/dvb/db.h>
#include <lib/dvb/frontendparms.h>
#include <lib/base/eenv.h>
#include <lib/base/eerror.h>
#include <lib/base/estring.h>
#include <lib/dvb/dvb.h>
#include <lib/dvb/db.h>
#include <lib/python/python.h>
#include <errno.h>
#include "absdiff.h"

#define SCAN_eDebug(x...) do { if (m_scan_debug) eDebug(x); } while(0)
#define SCAN_eDebugNoNewLineStart(x...) do { if (m_scan_debug) eDebugNoNewLineStart(x); } while(0)
#define SCAN_eDebugNoNewLine(x...) do { if (m_scan_debug) eDebugNoNewLine(x); } while(0)

DEFINE_REF(eDVBScan);

std::set<int> eDVBScan::m_vct_known_positions;

eDVBScan::eDVBScan(iDVBChannel *channel, bool usePAT, bool debug)
	:m_channel(channel)
	,m_channel_state(iDVBChannel::state_idle)
	,m_ready(0)
	,m_ready_all(usePAT ? (readySDT|readyPAT) : readySDT)
	,m_pmt_running(false)
	,m_abort_current_pmt(false)
	,m_vct_succeeded(false)
	,m_vct_resolved(false)
	,m_vct_grace_pending(false)
	,m_flags(0)
	,m_usePAT(usePAT)
	,m_scan_debug(debug)
{
	if (m_channel->getDemux(m_demux))
		SCAN_eDebug("[scan.cpp-#47] failed to allocate demux!");
	m_channel->connectStateChange(sigc::mem_fun(*this, &eDVBScan::stateChange), m_stateChanged_connection);
}

eDVBScan::~eDVBScan()
{
}

int eDVBScan::isValidONIDTSID(int orbital_position, eOriginalNetworkID onid, eTransportStreamID tsid)
{
	if(onid.get() == 0 || (onid.get() == 1 && tsid < 2) || onid.get() >= 0xFF00)
	{
		return 0;
	}
	return 1;
}

eDVBNamespace eDVBScan::buildNamespace(eOriginalNetworkID onid, eTransportStreamID tsid, unsigned long hash)
{
	int orb_pos = (hash >> 16) & 0xFFFF;
	if (orb_pos == 0xFFFF) // cable
	{
		if (eConfigManager::getConfigBoolValue("config.usage.subnetwork_cable", true))
			hash &= ~0xFFFF;
	}
	else if (orb_pos == 0xEEEE) // terrestrial
	{
		if (eConfigManager::getConfigBoolValue("config.usage.subnetwork_terrestrial", true))
			hash &= ~0xFFFF;
	}
	else if (eConfigManager::getConfigBoolValue("config.usage.subnetwork", true)
		&& isValidONIDTSID(orb_pos, onid, tsid)) // on valid ONIDs, ignore frequency ("sub network") part
		hash &= ~0xFFFF;
	return eDVBNamespace(hash);
}

int eDVBScan::getSITimeout(int base_timeout) const
{
	/*
	 * Narrowband (low symbol rate) transponders carry SI at a much lower
	 * bitrate, and feed transponders frequently violate the nominal SI
	 * repetition intervals. Demod lock is also slower to stabilise at low
	 * symbol rates, eating into the table acquisition window. Scale the
	 * PAT/PMT timeouts up so SCPC carriers (SR < 1000 ksps) get enough
	 * time to deliver their tables instead of being declared empty.
	 */
	int type;
	if (m_ch_current && !m_ch_current->getSystem(type) && type == iDVBFrontend::feSatellite)
	{
		eDVBFrontendParametersSatellite parm;
		if (!m_ch_current->getDVBS(parm) && parm.symbol_rate > 0)
		{
			if (parm.symbol_rate < 1000000)      /* < 1 Msps */
				return base_timeout * 3;
			if (parm.symbol_rate < 4000000)      /* 1 - 4 Msps */
				return base_timeout * 2;
		}
	}
	return base_timeout;
}

void eDVBScan::stateChange(iDVBChannel *ch)
{
	int state;
	if (ch->getState(state))
		return;
	if (m_channel_state == state)
		return;

	if (state == iDVBChannel::state_ok)
	{
		if (m_ch_current && m_channel)
		{
			int type;
			m_ch_current->getSystem(type);
			if (type == iDVBFrontend::feTerrestrial)
			{
				eDVBFrontendParametersTerrestrial parm;
				m_ch_current->getDVBT(parm);
				if (parm.system == eDVBFrontendParametersTerrestrial::System_DVB_T_T2)
				{
					/* we have a lock, this is a valid DVB-T transponder, set our system parameter */
					parm.system = eDVBFrontendParametersTerrestrial::System_DVB_T;
					m_ch_current->setDVBT(parm);
				}
			}
		}
		if (!m_ch_blindscan.empty())
		{
			/* update current blindscan iteration channel with scanned parameters */
			if (m_ch_current && m_channel)
			{
				ePtr<iDVBFrontend> fe;
				m_channel->getFrontend(fe);
				if (fe)
				{
					ePtr<iDVBTransponderData> tp;
					fe->getTransponderData(tp, false);
					if (tp)
					{
						ePtr<eDVBFrontendParameters> feparm = new eDVBFrontendParameters;
						int type;
						m_ch_current->getSystem(type);
						switch (type)
						{
						case iDVBFrontend::feSatellite:
						{
							eDVBFrontendParametersSatellite parm;
							m_ch_current->getDVBS(parm);
							parm.system = tp->getSystem();
							parm.frequency = tp->getFrequency();
							parm.symbol_rate = tp->getSymbolRate();
							parm.modulation = tp->getModulation();
							feparm->setDVBS(parm);
							break;
						}
						case iDVBFrontend::feCable:
						{
							eDVBFrontendParametersCable parm;
							m_ch_current->getDVBC(parm);
							parm.system = tp->getSystem();
							parm.frequency = tp->getFrequency();
							parm.symbol_rate = tp->getSymbolRate();
							parm.modulation = tp->getModulation();
							feparm->setDVBC(parm);
							break;
						}
						case iDVBFrontend::feTerrestrial:
						{
							eDVBFrontendParametersTerrestrial parm;
							m_ch_current->getDVBT(parm);
							parm.system = tp->getSystem();
							parm.frequency = tp->getFrequency();
							parm.bandwidth = tp->getBandwidth();
							parm.modulation = tp->getConstellation();
							feparm->setDVBT(parm);
							break;
						}
						case iDVBFrontend::feATSC:
						{
							eDVBFrontendParametersATSC parm;
							m_ch_current->getATSC(parm);
							parm.system = tp->getSystem();
							parm.frequency = tp->getFrequency();
							feparm->setATSC(parm);
							break;
						}
						}
						m_ch_current = m_ch_blindscan_result = feparm;
					}
				}
			}
		}
		startFilter();
		m_channel_state = state;
	} else if (state == iDVBChannel::state_failed)
	{
		if (m_ch_current && m_channel)
		{
			int type;
			m_ch_current->getSystem(type);
			m_ch_unavailable.push_back(m_ch_current);
			if (type == iDVBFrontend::feTerrestrial)
			{
				eDVBFrontendParametersTerrestrial parm;
				m_ch_current->getDVBT(parm);
				if (parm.system == eDVBFrontendParametersTerrestrial::System_DVB_T_T2)
				{
					/* we have to scan T2 as well as T */
					ePtr<iDVBFrontend> fe;
					eDVBFrontendParameters eparm;
					parm.system = eDVBFrontendParametersTerrestrial::System_DVB_T2;
					eparm.setDVBT(parm);
					m_channel->getFrontend(fe);
					if (fe)
					{
						ePtr<iDVBFrontendParameters> feparm = new eDVBFrontendParameters(eparm);
						/* but only if the frontend supports T2 */
						if (fe->isCompatibleWith(feparm))
						{
							addChannelToScan(feparm);
						}
					}
				}
			}
		}
		if (!m_ch_blindscan.empty())
		{
			/* tune failure, this means the blindscan channel iteration run has completed */
			SCAN_eDebug("[scan.cpp-#211] blindscan channel completed");
			m_ch_blindscan.pop_front();
		}
		nextChannel();
	}
			/* unavailable will timeout, anyway. */
}

RESULT eDVBScan::nextChannel()
{
	ePtr<iDVBFrontend> fe;

	m_SDT = 0; m_PAT = 0; m_BAT = 0; m_NIT = 0; m_PMT = 0; m_VCT = 0;

	m_ready = 0;

	/* If the previous transponder lost lock mid-scan (common on marginal
	 * narrowband carriers), channelDone() never ran and PMT bookkeeping
	 * is stale. Reset it so leftover PMT PIDs from the previous transport
	 * stream are not read on the next one. */
	m_pmts_to_read.clear();
	m_pat_programs.clear();
	m_pmt_running = false;
	m_abort_current_pmt = false;
	m_vct_succeeded = false;
	m_vct_resolved = false;
	m_vct_grace_pending = false;
	if (m_vct_grace_timer)
		m_vct_grace_timer->stop();

	m_pat_tsid = eTransportStreamID();

		/* check what we need */
	m_ready_all = readySDT;

	if (m_flags & scanNetworkSearch)
		m_ready_all |= readyNIT;

	if (m_flags & scanSearchBAT)
		m_ready_all |= readyBAT;

	if (m_usePAT)
		m_ready_all |= readyPAT;

	if (!m_ch_blindscan.empty())
	{
		/* keep iterating with the same 'channel' till we get a tune failure */
		SCAN_eDebug("[scan.cpp-#244] blindscan channel iteration");
		m_ch_current = m_ch_blindscan.front();
	}
	else
	{
		m_ch_blindscan_result = NULL;
		if (m_ch_toScan.empty())
		{
			SCAN_eDebug("[scan.cpp-#252] No Transponders left: %zd Transponders Scanned, %zd Transponders Unavailable, %zd Transponders in /etc/lamedb.",
				m_ch_scanned.size(), m_ch_unavailable.size(), m_new_channels.size());
			m_event(evtFinish);
			return -ENOENT;
		}

		m_ch_current = m_ch_toScan.front();

		m_ch_toScan.pop_front();
	}

	if (m_channel->getFrontend(fe))
	{
		m_event(evtFail);
		return -ENOTSUP;
	}

	m_chid_current = eDVBChannelID();

	m_channel_state = iDVBChannel::state_idle;

	if (fe->tune(*m_ch_current, !m_ch_blindscan.empty()))
		return nextChannel();

	m_event(evtUpdate);
	return 0;
}

RESULT eDVBScan::startFilter()
{
	bool startSDT=true;
	int system;
	ASSERT(m_demux);

			/* only start required filters filter */

	if (m_ready_all & readyPAT)
		startSDT = m_ready & readyPAT;

	// m_ch_current is not set, when eDVBScan is just used for a SDT update
	if (!m_ch_current)
	{
		unsigned int channelFlags;
		m_channel->getCurrentFrontendParameters(m_ch_current);
		m_ch_current->getFlags(channelFlags);
		if (channelFlags & iDVBFrontendParameters::flagOnlyFree)
			m_flags |= scanOnlyFree;
	}

	m_ch_current->getSystem(system);
	if (system == iDVBFrontend::feATSC)
	{
		m_VCT = 0;
		m_VCT = new eTable<VirtualChannelTableSection>;
		if (m_VCT->start(m_demux, eDVBVCTSpec()))
			return -1;
		CONNECT(m_VCT->tableReady, eDVBScan::VCTready);
		startSDT = false;
	}
	else if (system == iDVBFrontend::feSatellite || system == iDVBFrontend::feCable)
	{
		/* Some DVB-S/S2 and DVB-C transponders carry ATSC PSIP on PID 0x1FFB
		 * (e.g. North American feeds on Eutelsat 117W). Start VCT alongside SDT
		 * on the first startFilter() call only.  startFilter() is called again
		 * after PAT arrives; at that point m_VCT is already active (or already
		 * succeeded and set validVCT), so we must not recreate it — doing so
		 * would destroy the completed sections before channelDone() processes them. */
		if (!m_VCT && !(m_ready & validVCT))
		{
			m_VCT = new eTable<VirtualChannelTableSection>;
			if (m_VCT->start(m_demux, eDVBVCTSpec()))
				m_VCT = 0;
			else
				CONNECT(m_VCT->tableReady, eDVBScan::VCTready);
		}
	}
	else
	{
		m_VCT = 0;
	}

	m_SDT = 0;
	if (startSDT && (m_ready_all & readySDT))
	{
		m_SDT = new eTable<ServiceDescriptionSection>;
		int tsid=-1;
		if (m_ready & readyPAT && m_ready & validPAT)
		{
			std::vector<ProgramAssociationSection*>::const_iterator i =
				m_PAT->getSections().begin();
			ASSERT(i != m_PAT->getSections().end());
			tsid = (*i)->getTableIdExtension(); // in PAT this is the transport stream id
			m_pat_tsid = eTransportStreamID(tsid);
			for (; i != m_PAT->getSections().end(); ++i)
			{
				const ProgramAssociationSection &pat = **i;
				ProgramAssociationConstIterator program = pat.getPrograms()->begin();
				for (; program != pat.getPrograms()->end(); ++program)
				{
					unsigned short pn = (*program)->getProgramNumber();
					m_pmts_to_read.insert(std::pair<unsigned short, service>(pn, service((*program)->getProgramMapPid())));
					m_pat_programs.insert(pn);
				}
			}
			m_PMT = new eTable<ProgramMapSection>;
			CONNECT(m_PMT->tableReady, eDVBScan::PMTready);
			PMTready(-2);
			// KabelBW HACK ... on 618Mhz and 626Mhz the transport stream id in PAT and SDT is different

			{
				int type;
				m_ch_current->getSystem(type);
				if (type == iDVBFrontend::feCable)
				{
					eDVBFrontendParametersCable parm;
					m_ch_current->getDVBC(parm);
					if ((tsid == 0x00d7 && absdiff(parm.frequency, 618000) < 2000) ||
						(tsid == 0x00d8 && absdiff(parm.frequency, 626000) < 2000))
						tsid = -1;
				}
			}
		}
		if (tsid == -1)
		{
			SCAN_eDebug("[scan.cpp] tsid == -1; using default SDT specification.");
			if (m_SDT->start(m_demux, eDVBSDTSpec().setTimeout(getSITimeout(2500))))
				return -1;
		}
		else 
		{
			SCAN_eDebug("[scan.cpp] tsid != -1; attempting to start SDT with eDVBSDTSpec(tsid, true).");
			if (m_SDT->start(m_demux, eDVBSDTSpec(tsid, true).setTimeout(getSITimeout(10500))))
			{
				SCAN_eDebug("[scan.cpp] First attempt with true failed; trying eDVBSDTSpec(tsid, false) as fallback.");
				if (m_SDT->start(m_demux, eDVBSDTSpec(tsid, false).setTimeout(getSITimeout(2500))))
				{
					SCAN_eDebug("[scan.cpp] Fallback attempt with false also failed; returning failure.");
					return -1;
				}
			}
		}
		SCAN_eDebug("[scan.cpp] SDT configuration completed.");
		CONNECT(m_SDT->tableReady, eDVBScan::SDTready);
	}

	if (!(m_ready & readyPAT))
	{
		m_PAT = 0;
		if (m_ready_all & readyPAT)
		{
			m_PAT = new eTable<ProgramAssociationSection>;
			if (m_PAT->start(m_demux, eDVBPATSpec(getSITimeout(8000))))
			{
				SCAN_eDebug("[scan.cpp] ERROR: failed to start PAT filter");
				return -1;
			}
			CONNECT(m_PAT->tableReady, eDVBScan::PATready);
		}

		m_NIT = 0;
		if (m_ready_all & readyNIT)
		{
			m_NIT = new eTable<NetworkInformationSection>;
			if (m_NIT->start(m_demux, eDVBNITSpec(m_networkid)))
				return -1;
			CONNECT(m_NIT->tableReady, eDVBScan::NITready);
		}

		m_BAT = 0;
		if (m_ready_all & readyBAT)
		{
			m_BAT = new eTable<BouquetAssociationSection>;
			if (m_BAT->start(m_demux, eDVBBATSpec()))
				return -1;
			CONNECT(m_BAT->tableReady, eDVBScan::BATready);
		}
	}
	return 0;
}

void eDVBScan::SDTready(int err)
{
	if (err)
	{
		// Only retry once with different SDT parameters
		if (!(m_ready & readySDT_retry))
		{
			m_ready |= readySDT_retry;
			SCAN_eDebug("[scan.cpp] SDT acquisition failed, retrying with alternate parameters");
			m_SDT = new eTable<ServiceDescriptionSection>;
			
			// Try with a different approach - no specific transport stream ID filter
			if (m_SDT->start(m_demux, eDVBSDTSpec().setTimeout(getSITimeout(2500))))
			{
				SCAN_eDebug("[scan.cpp] SDT retry also failed");
				m_ready |= readySDT;
				channelDone();
				return;
			}
			CONNECT(m_SDT->tableReady, eDVBScan::SDTready);
			return;
		}
	}
	
	SCAN_eDebug("[scan.cpp] Got SDT %d", err);
	m_ready |= readySDT;
	if (!err)
		m_ready |= validSDT;
	channelDone();
}

void eDVBScan::NITready(int err)
{
	SCAN_eDebug("[scan.cpp-#397]!!! GOT NIT!!!, err %d", err);
	m_ready |= readyNIT;
	if (!err)
		m_ready |= validNIT;
	channelDone();
}

void eDVBScan::BATready(int err)
{
	SCAN_eDebug("[scan.cpp-#406] got bat, err %d", err);
	m_ready |= readyBAT;
	if (!err)
		m_ready |= validBAT;
	channelDone();
}

void eDVBScan::PATready(int err)
{
	SCAN_eDebug("[scan.cpp-#415] got pat, err %d", err);
	m_ready |= readyPAT;
	if (!err)
		m_ready |= validPAT;
	startFilter(); // for starting the SDT filter
}

void eDVBScan::VCTready(int err)
{
	SCAN_eDebug("[scan.cpp-#424] got vct %d", err);
	m_vct_resolved = true;
	if (m_vct_grace_pending)
	{
		m_vct_grace_pending = false;
		if (m_vct_grace_timer)
			m_vct_grace_timer->stop();
	}
	/* In feATSC mode m_SDT is null, so VCT always satisfies readySDT.
	 * When running alongside SDT (DVB-S/C), only set readySDT on success
	 * so a successful VCT short-circuits the SDT timeout without blocking
	 * normal DVB scans on VCT timeout. */
	if (!m_SDT || !err)
		m_ready |= readySDT;
	if (!err)
	{
		m_ready |= validVCT;
		m_vct_succeeded = true;

		/* Remember this satellite as VCT-carrying so later transponders on
		 * it skip the short grace period and always wait for VCT properly. */
		int system;
		m_ch_current->getSystem(system);
		if (system == iDVBFrontend::feSatellite)
		{
			eDVBFrontendParametersSatellite sat;
			if (!m_ch_current->getDVBS(sat))
				m_vct_known_positions.insert(sat.orbital_position);
		}
	}
	channelDone();
}

void eDVBScan::vctGraceTimeout()
{
	SCAN_eDebug("[eDVBScan] VCT grace period elapsed with no data seen; proceeding without it");
	m_vct_grace_pending = false;
	m_vct_resolved = true;
	channelDone();
}

void eDVBScan::PMTready(int err)
{
//	SCAN_eDebug("[scan.cpp-#433] got pmt %d", err);
	if (!err)
	{
		bool scrambled = false;
		bool have_audio = false;
		bool have_video = false;
		unsigned short pcrpid = 0xFFFF;
		std::vector<ProgramMapSection*>::const_iterator i;

		for (i = m_PMT->getSections().begin(); i != m_PMT->getSections().end(); ++i)
		{
			const ProgramMapSection &pmt = **i;
			if (pcrpid == 0xFFFF)
				pcrpid = pmt.getPcrPid();
			else
				SCAN_eDebug("[scan.cpp-#448]   already have a pcrpid %04x %04x", pcrpid, pmt.getPcrPid());
			ElementaryStreamInfoConstIterator es;
			for (es = pmt.getEsInfo()->begin(); es != pmt.getEsInfo()->end(); ++es)
			{
				int isaudio = 0, isvideo = 0, is_scrambled = 0, forced_audio = 0, forced_video = 0;
				switch ((*es)->getType())
				{
				case 0x1b: // AVC Video Stream (MPEG4 H264)
				case 0x24: // H265 HEVC
				case 0x10: // MPEG 4 Part 2
				case 0x01: // MPEG 1 video
				case 0x02: // MPEG 2 video
					isvideo = 1;
					forced_video = 1;
					[[fallthrough]];
				case 0x03: // MPEG 1 audio
				case 0x04: // MPEG 2 audio
				case 0x0f: // MPEG 2 AAC
				case 0x11: // MPEG 4 AAC
					if (!isvideo)
					{
						forced_audio = 1;
						isaudio = 1;
					}
					[[fallthrough]];
				case 0x06: // PES Private
				case 0x81: // user private
				case 0xEA: // TS_PSI_ST_SMPTE_VC1
					for (DescriptorConstIterator desc = (*es)->getDescriptors()->begin();
							desc != (*es)->getDescriptors()->end(); ++desc)
					{
						uint8_t tag = (*desc)->getTag();
						/* PES private can contain AC-3, DTS or lots of other stuff.
						   check descriptors to get the exakt type. */
						if (!forced_video && !forced_audio)
						{
							switch (tag)
							{
							case 0x1C: // TS_PSI_DT_MPEG4_Audio
							case 0x2B: // TS_PSI_DT_MPEG2_AAC
							case AAC_DESCRIPTOR:
							case AC3_DESCRIPTOR:
							case DTS_DESCRIPTOR:
							case AUDIO_STREAM_DESCRIPTOR:
								isaudio = 1;
								break;
							case 0x28: // TS_PSI_DT_AVC
							case 0x1B: // TS_PSI_DT_MPEG4_Video
							case VIDEO_STREAM_DESCRIPTOR:
								isvideo = 1;
								break;
							case REGISTRATION_DESCRIPTOR: /* some services don't have a separate AC3 descriptor */
							{
								RegistrationDescriptor *d = (RegistrationDescriptor*)(*desc);
								switch (d->getFormatIdentifier())
								{
								case 0x44545331 ... 0x44545333: // DTS1/DTS2/DTS3
								case 0x41432d33: // == 'AC-3'
								case 0x42535344: // == 'BSSD' (LPCM)
									isaudio = 1;
									break;
								case 0x56432d31: // == 'VC-1'
									isvideo = 1;
									break;
								default:
									break;
								}
							}
							default:
								break;
							}
						}
						if (tag == CA_DESCRIPTOR)
							is_scrambled = 1;
					}
				default:
					break;
				}
				if (isvideo)
					have_video = true;
				else if (isaudio)
					have_audio = true;
				else
					continue;
				if (is_scrambled)
					scrambled = true;
			}
			for (DescriptorConstIterator desc = pmt.getDescriptors()->begin();
				desc != pmt.getDescriptors()->end(); ++desc)
			{
				if ((*desc)->getTag() == CA_DESCRIPTOR)
					scrambled = true;
			}
		}
		m_pmt_in_progress->second.scrambled = scrambled;
		if ( have_video )
			m_pmt_in_progress->second.serviceType = 1;
		else if ( have_audio )
			m_pmt_in_progress->second.serviceType = 2;
		else
			m_pmt_in_progress->second.serviceType = 100;
	}
	if (err == -1) // timeout or removed by sdt
		m_pmts_to_read.erase(m_pmt_in_progress++);
	else if (m_pmt_running)
		++m_pmt_in_progress;
	else
	{
		m_pmt_in_progress = m_pmts_to_read.begin();
		m_pmt_running = true;
	}

	if (m_pmt_in_progress != m_pmts_to_read.end())
		m_PMT->start(m_demux, eDVBPMTSpec(m_pmt_in_progress->second.pmtPid, m_pmt_in_progress->first, getSITimeout(4000)));
	else
	{
		m_PMT = 0;
		m_pmt_running = false;
		channelDone();
	}
}


void eDVBScan::addKnownGoodChannel(const eDVBChannelID &chid, iDVBFrontendParameters *feparm)
{
		/* add it to the list of known channels. */
	if (chid)
		m_new_channels.insert(std::pair<eDVBChannelID,ePtr<iDVBFrontendParameters> >(chid, feparm));
}

void eDVBScan::addChannelToScan(iDVBFrontendParameters *feparm)
{
		/* check if we don't already have that channel ... */

	int type;
	feparm->getSystem(type);

	switch(type)
	{
	case iDVBFrontend::feSatellite:
	{
		eDVBFrontendParametersSatellite parm;
		feparm->getDVBS(parm);
		SCAN_eDebug("[scan.cpp-#591] try to add sat %d %d %d %d %d %d",
			parm.orbital_position, parm.frequency, parm.symbol_rate, parm.polarisation, parm.fec, parm.modulation);
		break;
	}
	case iDVBFrontend::feCable:
	{
		eDVBFrontendParametersCable parm;
		feparm->getDVBC(parm);
		SCAN_eDebug("[scan.cpp-#599] try to add cable %d %d %d %d",
			parm.frequency, parm.symbol_rate, parm.modulation, parm.fec_inner);
		break;
	}
	case iDVBFrontend::feTerrestrial:
	{
		eDVBFrontendParametersTerrestrial parm;
		feparm->getDVBT(parm);
		SCAN_eDebug("[scan.cpp-#607] try to add terres %d %d %d %d %d %d %d %d",
			parm.frequency, parm.modulation, parm.transmission_mode, parm.hierarchy,
			parm.guard_interval, parm.code_rate_LP, parm.code_rate_HP, parm.bandwidth);
		break;
	}
	case iDVBFrontend::feATSC:
	{
		eDVBFrontendParametersATSC parm;
		feparm->getATSC(parm);
		SCAN_eDebug("[scan.cpp-#616] try to add atsc %d %d %d %d",
			parm.frequency, parm.modulation, parm.inversion, parm.system);
		break;
	}
	}

	int found_count=0;
		/* ... in the list of channels to scan */
	for (std::list<ePtr<iDVBFrontendParameters> >::iterator i(m_ch_toScan.begin()); i != m_ch_toScan.end();)
	{
		if (sameChannel(*i, feparm))
		{
			if (!found_count)
			{
				*i = feparm;  // update
				SCAN_eDebug("[eDVBScan]   update");
			}
			else
			{
				SCAN_eDebug("[eDVBScan]   remove dupe");
				m_ch_toScan.erase(i++);
				continue;
			}
			++found_count;
		}
		++i;
	}

	if (found_count > 0)
	{
		SCAN_eDebug("[scan.cpp-#636]   already in todo list");
		return;
	}

		/* ... in the list of successfully scanned channels */
	for (std::list<ePtr<iDVBFrontendParameters> >::const_iterator i(m_ch_scanned.begin()); i != m_ch_scanned.end(); ++i)
		if (sameChannel(*i, feparm))
		{
			SCAN_eDebug("[eDVBScan]   successfully scanned");
			return;
		}

		/* ... in the list of unavailable channels */
	for (std::list<ePtr<iDVBFrontendParameters> >::const_iterator i(m_ch_unavailable.begin()); i != m_ch_unavailable.end(); ++i)
		if (sameChannel(*i, feparm, true))
		{
			SCAN_eDebug("[eDVBScan]   scanned but not available");
			return;
		}

		/* ... on the current channel */
	if (sameChannel(m_ch_current, feparm))
	{
		SCAN_eDebug("[scan.cpp-#642]   is current");
		return;
	}

	SCAN_eDebug("[scan.cpp-#646]   really add");
		/* otherwise, add it to the todo list. */
	m_ch_toScan.push_front(feparm); // better.. then the rotor not turning wild from east to west :)
}

int eDVBScan::sameChannel(iDVBFrontendParameters *ch1, iDVBFrontendParameters *ch2, bool exact) const
{
	int diff;

	if (!ch1 || !ch2)
		return 0;

	if (ch1->calculateDifference(ch2, diff, exact))
		return 0;

	/*
	 * Default merge window: 4 MHz (kHz units for DVB-S/C; Hz for DVB-T,
	 * which effectively means "exact" for terrestrial - same as upstream).
	 *
	 * For satellite, scale the window down with the narrower of the two
	 * symbol rates. A fixed 4 MHz window incorrectly merges adjacent
	 * narrowband (low symbol rate / SCPC feed) transponders, so closely
	 * spaced carriers below ~6 Msps would never all be scanned. Using
	 * roughly 2/3 of the narrower symbol rate (~half the occupied
	 * bandwidth incl. roll-off) keeps wideband behaviour identical while
	 * letting feed transponders spaced ~1 MHz apart coexist in the list.
	 *
	 * Floor of 500 kHz: below that, LNB LOF drift would create duplicate
	 * entries for the same physical carrier (harmless, but wastes time).
	 */
	int tolerance = 4000;
	int type1, type2;
	if (!ch1->getSystem(type1) && !ch2->getSystem(type2)
		&& type1 == iDVBFrontend::feSatellite && type2 == iDVBFrontend::feSatellite)
	{
		eDVBFrontendParametersSatellite p1, p2;
		if (!ch1->getDVBS(p1) && !ch2->getDVBS(p2))
		{
			int min_sr = p1.symbol_rate < p2.symbol_rate ? p1.symbol_rate : p2.symbol_rate;
			if (min_sr > 0 && min_sr < 6000000)
			{
				tolerance = (min_sr / 1000) * 2 / 3; /* kHz */
				if (tolerance < 500)
					tolerance = 500;
			}
		}
	}

	if (diff < tolerance)
		return 1;
	return 0;
}

void eDVBScan::channelDone()
{
	/* SDT's spec-mandated <=2s repetition means it usually completes before
	 * VCT's slower PSIP cycle. If SDT were processed immediately here on a
	 * transponder that also carries VCT, its services would already be
	 * committed below (and validSDT cleared) before the discard check a
	 * few lines down ever gets a chance to run against a VCT that resolves
	 * a moment later - VCT would then add its own copies on top, producing
	 * the exact duplicate-service bug this file was already patched for.
	 * So: hold everything below (discard check, SDT/VCT/NIT processing,
	 * transponder-complete) until VCT is resolved one way or the other -
	 * unless it has shown no activity at all, in which case a short grace
	 * period is enough. See vctGraceTimeout() and m_vct_known_positions. */
	if (m_VCT && !m_vct_resolved)
	{
		bool must_wait = !m_VCT->getSections().empty();

		if (!must_wait)
		{
			int system;
			m_ch_current->getSystem(system);
			if (system == iDVBFrontend::feSatellite)
			{
				eDVBFrontendParametersSatellite sat;
				if (!m_ch_current->getDVBS(sat) &&
					m_vct_known_positions.find(sat.orbital_position) != m_vct_known_positions.end())
					must_wait = true;
			}
		}

		if (must_wait)
		{
			SCAN_eDebug("[eDVBScan] VCT pending (data seen, or known PSIP satellite); deferring SDT processing");
			return;
		}

		if (!m_vct_grace_pending)
		{
			SCAN_eDebug("[eDVBScan] No VCT activity yet; granting a short grace period before processing SDT");
			m_vct_grace_pending = true;
			if (!m_vct_grace_timer)
			{
				m_vct_grace_timer = eTimer::create(eApp);
				CONNECT(m_vct_grace_timer->timeout, eDVBScan::vctGraceTimeout);
			}
			m_vct_grace_timer->start(750, true);
		}
		return;
	}

	/* On DVB-S/C transponders that carry ATSC PSIP, VCT and SDT can both
	 * succeed. VCT takes priority. Keep the success state for the lifetime
	 * of the transponder because validVCT is cleared after VCT processing;
	 * SDT may complete in a later callback and must still be discarded. */
	if (m_vct_succeeded && (m_ready & validSDT))
	{
		int ch_system_check;
		m_ch_current->getSystem(ch_system_check);
		if (ch_system_check != iDVBFrontend::feATSC)
		{
			SCAN_eDebug("[eDVBScan] VCT succeeded on DVB-S/C; discarding SDT results");
			m_ready &= ~validSDT;
		}
	}

	if ((m_ready & validSDT) && m_SDT && !m_SDT->getSections().empty() && (!(m_flags & scanOnlyFree) || !m_pmt_running))
	{
		unsigned long hash = 0;

		m_ch_current->getHash(hash);

		eOriginalNetworkID onid = (**m_SDT->getSections().begin()).getOriginalNetworkId();
		eTransportStreamID tsid = (**m_SDT->getSections().begin()).getTransportStreamId();
		eDVBNamespace dvbnamespace = buildNamespace(onid, tsid, hash);

		/* Detect namespace collision: if a channel with the same stripped namespace+TSID+ONID
		 * already exists in the database but points to a different physical transponder,
		 * preserve the frequency in the namespace to keep services unique.
		 * This prevents services with the same SID on different transponders (e.g. EBU feeds)
		 * from overwriting each other during manual scan.
		 * Check both the persistent DB and m_new_channels so fresh blindscans (where the
		 * conflicting transponder has not yet been committed to lamedb) are also handled. */
		eDVBChannelID chid_check(dvbnamespace, tsid, onid);
		{
			ePtr<iDVBFrontendParameters> existing_ch;
			bool found = !eDVBDB::getInstance()->getChannelFrontendData(chid_check, existing_ch);
			if (!found)
			{
				auto it = m_new_channels.find(chid_check);
				if (it != m_new_channels.end())
				{
					existing_ch = it->second;
					found = true;
				}
			}
			if (found)
			{
				int diff = 0;
				if (!m_ch_current->calculateDifference(&*existing_ch, diff, false) && diff >= 2000)
				{
					dvbnamespace = eDVBNamespace(hash);
					SCAN_eDebug("[eDVBScan] namespace collision detected: different transponder uses same TSID/ONID, preserving frequency in namespace");
				}
			}
		}

//		SCAN_eDebug("[scan.cpp-#669] SDT: ");
		std::vector<ServiceDescriptionSection*>::const_iterator i;
		for (i = m_SDT->getSections().begin(); i != m_SDT->getSections().end(); ++i)
			processSDT(dvbnamespace, **i);
		m_ready &= ~validSDT;
	}

	if ((m_ready & validVCT) && m_VCT && !m_VCT->getSections().empty())
	{
		unsigned long hash = 0;

		m_ch_current->getHash(hash);

		int onid = 0; /* TODO: ATSC ONID? */
		int vct_system;
		m_ch_current->getSystem(vct_system);
		/* For DVB-S/C frontends carrying ATSC PSIP, the VCT section's transport_stream_id
		 * may differ from the DVB PAT TSID.  Use the PAT TSID so the channel database
		 * entry and all service references share the same key that enigma2's satellite
		 * transponder-info lookup expects.  For native feATSC frontends keep the VCT TSID. */
		eTransportStreamID tsid = (vct_system == iDVBFrontend::feATSC || m_pat_tsid == eTransportStreamID())
			? (**m_VCT->getSections().begin()).getTransportStreamId()
			: m_pat_tsid;
		eDVBNamespace dvbnamespace = buildNamespace(eOriginalNetworkID(onid), tsid, hash);

		/* Detect namespace collision (same as SDT block above, including m_new_channels) */
		eDVBChannelID chid_check(dvbnamespace, tsid, eOriginalNetworkID(onid));
		{
			ePtr<iDVBFrontendParameters> existing_ch;
			bool found = !eDVBDB::getInstance()->getChannelFrontendData(chid_check, existing_ch);
			if (!found)
			{
				auto it = m_new_channels.find(chid_check);
				if (it != m_new_channels.end())
				{
					existing_ch = it->second;
					found = true;
				}
			}
			if (found)
			{
				int diff = 0;
				if (!m_ch_current->calculateDifference(&*existing_ch, diff, false) && diff >= 2000)
				{
					dvbnamespace = eDVBNamespace(hash);
					SCAN_eDebug("[eDVBScan] namespace collision detected: different transponder uses same TSID/ONID, preserving frequency in namespace");
				}
			}
		}

		SCAN_eDebug("[scan.cpp-#688] VCT: ");
		std::vector<VirtualChannelTableSection*>::const_iterator i;
		for (i = m_VCT->getSections().begin(); i != m_VCT->getSections().end(); ++i)
			processVCT(dvbnamespace, **i, onid);
		m_ready &= ~validVCT;
	}

	if (m_ready & validNIT)
	{
		int system;
		std::list<ePtr<iDVBFrontendParameters> > m_ch_toScan_backup;
		m_ch_current->getSystem(system);
		SCAN_eDebug("[scan.cpp-#701] dumping NIT");
		if (m_flags & clearToScanOnFirstNIT)
		{
			m_ch_toScan_backup = m_ch_toScan;
			m_ch_toScan.clear();
		}
		std::vector<NetworkInformationSection*>::const_iterator i;
		for (i = m_NIT->getSections().begin(); i != m_NIT->getSections().end(); ++i)
		{
			if (m_networkid && m_networkid != (*i)->getTableIdExtension()) // in NIT this is the network id
			{
				SCAN_eDebug("[scan.cpp-#711] ignoring NetworkId %d!", (*i)->getTableIdExtension());
				continue;
			}

			const TransportStreamInfoList &tsinfovec = *(*i)->getTsInfo();

			for (TransportStreamInfoConstIterator tsinfo(tsinfovec.begin());
				tsinfo != tsinfovec.end(); ++tsinfo)
			{
				SCAN_eDebug("[scan.cpp-#720] TSID: %04x ONID: %04x", (*tsinfo)->getTransportStreamId(),
					(*tsinfo)->getOriginalNetworkId());
				bool T2 = false;
				eDVBFrontendParametersTerrestrial t2transponder;
				eOriginalNetworkID onid = (*tsinfo)->getOriginalNetworkId();
				eTransportStreamID tsid = (*tsinfo)->getTransportStreamId();
				eDVBNamespace ns(0);

				for (DescriptorConstIterator desc = (*tsinfo)->getDescriptors()->begin();
						desc != (*tsinfo)->getDescriptors()->end(); ++desc)
				{
					switch ((*desc)->getTag())
					{
					case CABLE_DELIVERY_SYSTEM_DESCRIPTOR:
					{
						if (system != iDVBFrontend::feCable)
							break; // when current locked transponder is no cable transponder ignore this descriptor
						CableDeliverySystemDescriptor &d = (CableDeliverySystemDescriptor&)**desc;
						ePtr<eDVBFrontendParameters> feparm = new eDVBFrontendParameters;
						eDVBFrontendParametersCable cable;
						cable.set(d);
						feparm->setDVBC(cable);

						unsigned long hash=0;
						feparm->getHash(hash);
						ns = buildNamespace(onid, tsid, hash);

						addChannelToScan(feparm);
						break;
					}
					case TERRESTRIAL_DELIVERY_SYSTEM_DESCRIPTOR:
					{
						if (system != iDVBFrontend::feTerrestrial)
							break; // when current locked transponder is no terrestrial transponder ignore this descriptor
						TerrestrialDeliverySystemDescriptor &d = (TerrestrialDeliverySystemDescriptor&)**desc;
						ePtr<eDVBFrontendParameters> feparm = new eDVBFrontendParameters;
						eDVBFrontendParametersTerrestrial terr;
						terr.set(d);
						feparm->setDVBT(terr);
						
						unsigned long hash=0;
						feparm->getHash(hash);
						ns = buildNamespace(onid, tsid, hash);

						addChannelToScan(feparm);
						break;
					}
					case LOGICAL_CHANNEL_DESCRIPTOR:
					{
						// we handle it later
						break;
					}
					case S2_SATELLITE_DELIVERY_SYSTEM_DESCRIPTOR:
					{
						SCAN_eDebug("[scan.cpp] S2_SATELLITE_DELIVERY_SYSTEM_DESCRIPTOR found");
						if (system != iDVBFrontend::feSatellite)
							break; // when current locked transponder is no satellite transponder ignore this descriptor
						S2SatelliteDeliverySystemDescriptor &d = (S2SatelliteDeliverySystemDescriptor&)**desc;
						eDVBFrontendParametersSatellite sat;
						sat.set(d);

						eDVBFrontendParametersSatellite p;
						m_ch_current->getDVBS(p);

						if (p.is_id != sat.is_id || p.pls_mode != sat.pls_mode || p.pls_code != sat.pls_code)
						{
							/* multistream sibling on the current transponder:
							 * keep tuned RF parameters, apply stream/PLS data */
							ePtr<eDVBFrontendParameters> feparm = new eDVBFrontendParameters;
							p.set(d); //set multistream descriptor to current tuned data
							feparm->setDVBS(p);
							addChannelToScan(feparm);
						}
						/* The S2 descriptor carries no frequency; do NOT fall
						 * through into the DVB-S descriptor handler (that would
						 * reinterpret this descriptor as a
						 * SatelliteDeliverySystemDescriptor and read garbage). */
						break;
					}
					case SATELLITE_DELIVERY_SYSTEM_DESCRIPTOR:
					{
						if (system != iDVBFrontend::feSatellite)
							break; // when current locked transponder is no satellite transponder ignore this descriptor

						SatelliteDeliverySystemDescriptor &d = (SatelliteDeliverySystemDescriptor&)**desc;
						if (d.getFrequency() < 10000)
							break;

						ePtr<eDVBFrontendParameters> feparm = new eDVBFrontendParameters;
						eDVBFrontendParametersSatellite sat;
						sat.set(d);

						eDVBFrontendParametersSatellite p;
						m_ch_current->getDVBS(p);

						/* some NITs report a slightly different orbital position
						 * than the one we tuned; snap to the tuned position */
						if (absdiff(p.orbital_position, sat.orbital_position) < 5)
							sat.orbital_position = p.orbital_position;
						/* some NITs have the west/east flag inverted */
						if (absdiff(absdiff(3600, p.orbital_position), sat.orbital_position) < 5)
						{
							SCAN_eDebug("[eDVBScan] NIT entry with incorrect west/east flag, correcting %d -> %d",
								sat.orbital_position, p.orbital_position);
							sat.orbital_position = p.orbital_position;
						}

						feparm->setDVBS(sat);

						if (sat.orbital_position != p.orbital_position)
						{
							SCAN_eDebug("[eDVBScan] dropping NIT transponder on different satellite (%d.%d vs %d.%d)",
								sat.orbital_position/10, sat.orbital_position%10,
								p.orbital_position/10, p.orbital_position%10);
							break;
						}

						unsigned long hash = 0;
						feparm->getHash(hash);
						ns = buildNamespace(onid, tsid, hash);

						addChannelToScan(feparm);
						break;
					}
					case EXTENSION_DESCRIPTOR:
					{
						if (system != iDVBFrontend::feTerrestrial)
							break; // when current locked transponder is no terrestrial transponder ignore this descriptor

						ExtensionDescriptor &d = (ExtensionDescriptor&)**desc;
						switch (d.getExtensionTag())
						{
						case T2_DELIVERY_SYSTEM_DESCRIPTOR:
							T2 = true;
							T2DeliverySystemDescriptor &d = (T2DeliverySystemDescriptor&)**desc;
							t2transponder.set(d);

							// fetch T2 namespace for LCN output, where frequency data may not be in SI table
							ePtr<iDVBFrontend> fe;
							ePtr<iDVBTransponderData> trdata;
							if (!m_channel->getFrontend(fe))
							{
								fe->getTransponderData(trdata, true);
								int freq = trdata->getFrequency();
								long hash = 0xEEEE0000;
								hash |= (freq/1000000)&0xFFFF;
								ns = buildNamespace(onid, tsid, hash);  // used in case LOGICAL_CHANNEL_DESCRIPTOR
							}  // end fetch T2 namespace

							for (T2CellConstIterator cell = d.getCells()->begin();
								cell != d.getCells()->end(); ++cell)
							{
								for (T2FrequencyConstIterator freq = (*cell)->getCentreFrequencies()->begin();
									freq != (*cell)->getCentreFrequencies()->end(); ++freq)
								{
									t2transponder.frequency = (*freq) * 10;
									ePtr<eDVBFrontendParameters> feparm = new eDVBFrontendParameters;
									feparm->setDVBT(t2transponder);
									addChannelToScan(feparm);
								}
							}
						}
						break;
					}
					case FREQUENCY_LIST_DESCRIPTOR:
					{
						if (system != iDVBFrontend::feTerrestrial)
							break; // when current locked transponder is no terrestrial transponder ignore this descriptor
						if (!T2)
							break;

						FrequencyListDescriptor &d = (FrequencyListDescriptor&)**desc;
						if (d.getCodingType() != 0x03)
							break;

						for (CentreFrequencyConstIterator it = d.getCentreFrequencies()->begin();
								it != d.getCentreFrequencies()->end(); ++it)
						{
							t2transponder.frequency = (*it) * 10;
							ePtr<eDVBFrontendParameters> feparm = new eDVBFrontendParameters;
							feparm->setDVBT(t2transponder);
							addChannelToScan(feparm);
						}
						break;
					}
					default:
						SCAN_eDebug("[scan.cpp-#850] descr<%x>", (*desc)->getTag());
						break;
					}
				}
				// we do this after the main loop because we absolutely need the namespace
				for (DescriptorConstIterator desc = (*tsinfo)->getDescriptors()->begin();
					desc != (*tsinfo)->getDescriptors()->end(); ++desc)
				{
					switch ((*desc)->getTag())
					{
						case LOGICAL_CHANNEL_DESCRIPTOR:
						{
							if (!(system == iDVBFrontend::feTerrestrial || system == iDVBFrontend::feCable))
								break; // when current locked transponder is not terrestrial or cable ignore this descriptor

							if (ns.get() == 0)
								break; // invalid namespace

							int signal = 0;
							ePtr<iDVBFrontend> fe;

							if (!m_channel->getFrontend(fe))
								signal = fe->readFrontendData(iFrontendInformation_ENUMS::signalQuality);

							LogicalChannelDescriptor &d = (LogicalChannelDescriptor&)**desc;
							for (LogicalChannelListConstIterator it = d.getChannelList()->begin(); it != d.getChannelList()->end(); it++)
							{
								LogicalChannel *ch = *it;
								if (ch->getVisibleServiceFlag())
								{
									eDVBDB::getInstance()->addLcnToDB(ns.get(), onid.get(), tsid.get(), eServiceID(ch->getServiceId()).get(), ch->getLogicalChannelNumber(), signal);
									SCAN_eDebug("NAMESPACE: %08x ONID: %04x TSID: %04x SID: %04x LCN: %05d SIGNAL: %08d", ns.get(), onid.get(), tsid.get(), ch->getServiceId(), ch->getLogicalChannelNumber(), signal);
								}
							}
							break;
						}
						default:
							break;
					}
				}
			}

		}

			/* a pitfall is to have the clearToScanOnFirstNIT-flag set, and having channels which have
			   no or invalid NIT. this code will not erase the toScan list unless at least one valid entry
			   has been found.

			   This is not a perfect solution, as the channel could contain a partial NIT. Life's bad.
			*/
		if (m_flags & clearToScanOnFirstNIT)
		{
			if (m_ch_toScan.empty())
			{
				eWarning("[scan.cpp-#868] clearToScanOnFirstNIT was set, but NIT is invalid. Refusing to stop scan.");
				m_ch_toScan = m_ch_toScan_backup;
			} else
	 			m_flags &= ~clearToScanOnFirstNIT;
 		}
		m_ready &= ~validNIT;
	}

	if (m_pmt_running || (m_ready & m_ready_all) != m_ready_all)
	{
		if (m_abort_current_pmt)
		{
			m_abort_current_pmt = false;
			PMTready(-1);
		}
		return;
	}

	SCAN_eDebug("[scan.cpp-#886] Transponder Search Complete!");

		/* if we had services on this channel, we declare
		   this channels as "known good". add it.

		   (TODO: not yet implemented)
		   a NIT entry could have possible overridden
		   our frontend data with more exact data.

		   (TODO: not yet implemented)
		   the tuning process could have lead to more
		   exact data than the user entered.

		   The channel id was probably corrected
		   by the data written in the SDT. this is
		   important, as "initial transponder lists"
		   usually don't have valid CHIDs (and that's
		   good).

		   These are the reasons for adding the transponder
		   here, and not before.
		*/

	int type;
	if (m_ch_current->getSystem(type))
		type = -1;

	for (m_pmt_in_progress = m_pmts_to_read.begin(); m_pmt_in_progress != m_pmts_to_read.end();)
	{
		eServiceReferenceDVB ref;
		ePtr<eDVBService> service = new eDVBService;

		if (!m_chid_current)
		{
			unsigned long hash = 0;

			m_ch_current->getHash(hash);

			m_chid_current = eDVBChannelID(
				buildNamespace(eOriginalNetworkID(0), m_pat_tsid, hash),
				m_pat_tsid, eOriginalNetworkID(0));
		}

		if (m_pmt_in_progress->second.serviceType == 1)
			SCAN_eDebug("[scan.cpp-#930] SID %04x is TV", m_pmt_in_progress->first);
		else if (m_pmt_in_progress->second.serviceType == 2)
			SCAN_eDebug("[scan.cpp-#932] SID %04x is Radio", m_pmt_in_progress->first);
		else
			SCAN_eDebug("[scan.cpp-#934] SID %04x is DATA, (ServiceType = %04x)", m_pmt_in_progress->first, m_pmt_in_progress->second.serviceType);

		ref.set(m_chid_current);
		ref.setServiceID(m_pmt_in_progress->first);
		ref.setServiceType(m_pmt_in_progress->second.serviceType);

		if (type != -1)
		{
			char sname[255];
			char pname[255];
			memset(pname, 0, sizeof(pname));
			memset(sname, 0, sizeof(sname));
			switch(type)
			{
				case iDVBFrontend::feSatellite:
				{
					eDVBFrontendParametersSatellite parm;
					m_ch_current->getDVBS(parm);
					snprintf(sname, 255, "%d%c SID 0x%02x",
							parm.frequency/1000,
							parm.polarisation ? 'V' : 'H',
							m_pmt_in_progress->first);
					snprintf(pname, 255, "%s %s %d%c %d.%d°%c",
						parm.system ? "DVB-S2" : "DVB-S",
						parm.modulation == eDVBFrontendParametersSatellite::Modulation_Auto ? "AUTO" :
						parm.modulation == eDVBFrontendParametersSatellite::Modulation_QPSK ? "QPSK" :
						parm.modulation == eDVBFrontendParametersSatellite::Modulation_8PSK ? "8PSK" :
						parm.modulation == eDVBFrontendParametersSatellite::Modulation_QAM16 ? "QAM16" :
						parm.modulation == eDVBFrontendParametersSatellite::Modulation_16APSK ? "16APSK" : "32APSK",
						parm.frequency/1000,
						parm.polarisation ? 'V' : 'H',
						parm.orbital_position/10,
						parm.orbital_position%10,
						parm.orbital_position > 0 ? 'E' : 'W');
					break;
				}
				case iDVBFrontend::feTerrestrial:
				{
					eDVBFrontendParametersTerrestrial parm;
					m_ch_current->getDVBT(parm);
					snprintf(sname, 255, "%d SID 0x%02x",
						parm.frequency/1000,
						m_pmt_in_progress->first);
					break;
				}
				case iDVBFrontend::feCable:
				{
					eDVBFrontendParametersCable parm;
					m_ch_current->getDVBC(parm);
					snprintf(sname, 255, "%d SID 0x%02x",
						parm.frequency/1000,
						m_pmt_in_progress->first);
					break;
				}
				case iDVBFrontend::feATSC:
				{
					eDVBFrontendParametersATSC parm;
					m_ch_current->getATSC(parm);
					snprintf(sname, 255, "%d SID 0x%02x",
						parm.frequency/1000,
						m_pmt_in_progress->first);
					break;
				}
			}
			SCAN_eDebug("[scan.cpp-#998] name = '%s'", sname);
			int tsonid = 0;
			if( m_chid_current )
				tsonid = ( m_chid_current.transport_stream_id.get() << 16 )
					| m_chid_current.original_network_id.get();
			service->m_service_name = strip_non_graph(convertDVBUTF8(sname,-1,tsonid,0));
			service->genSortName();
			service->m_provider_name = strip_non_graph(convertDVBUTF8(pname,-1,tsonid,0));
		}

		if (!(m_flags & scanOnlyFree) || !m_pmt_in_progress->second.scrambled) {
//			SCAN_eDebug("[scan.cpp-#1009] add not scrambled!");
			m_new_servicerefs.push_back(ref);
			std::pair<std::map<eServiceReferenceDVB, ePtr<eDVBService> >::iterator, bool> i =
				m_new_services.insert(std::pair<eServiceReferenceDVB, ePtr<eDVBService> >(ref, service));
			if (i.second)
			{
				m_last_service = i.first;
				m_event(evtNewService);
			}
		}
		else
			SCAN_eDebug("[scan.cpp-#1020] dont add... is scrambled!");
		m_pmts_to_read.erase(m_pmt_in_progress++);
	}

	if (!m_chid_current)
		eWarning("[scan.cpp-#1025] the current channel's ID was not corrected - not adding channel");
	else
	{
		addKnownGoodChannel(m_chid_current, m_ch_current);
		if (m_chid_current)
		{
			switch(type)
			{
				case iDVBFrontend::feSatellite:
				case iDVBFrontend::feTerrestrial:
				case iDVBFrontend::feCable:
				case iDVBFrontend::feATSC:
				{
					ePtr<iDVBFrontend> fe;
					if (!m_channel->getFrontend(fe))
					{
						int frequency = fe->readFrontendData(iFrontendInformation_ENUMS::frequency);
//						eDebug("[scan.cpp-#1042] add tuner data for tsid %04x, onid %04x, ns %08x",
//							m_chid_current.transport_stream_id.get(), m_chid_current.original_network_id.get(),
//							m_chid_current.dvbnamespace.get());
						m_tuner_data.insert(std::pair<eDVBChannelID, int>(m_chid_current, frequency));
					}
				}
				default:
					break;
			}
		}
	}

	m_ch_scanned.push_back(m_ch_current);

	for (std::list<ePtr<iDVBFrontendParameters> >::iterator i(m_ch_toScan.begin()); i != m_ch_toScan.end();)
	{
		if (sameChannel(*i, m_ch_current))
		{
			SCAN_eDebug("[scan.cpp-#1061] remove dupe 2");
			m_ch_toScan.erase(i++);
			continue;
		}
		++i;
	}

	nextChannel();
}

void eDVBScan::start(const eSmartPtrList<iDVBFrontendParameters> &known_transponders, int flags, int networkid)
{
	std::list<ePtr<iDVBFrontendParameters> > *transponderlist = &m_ch_toScan;
	m_flags = flags;
	m_networkid = networkid;
	m_ch_toScan.clear();
	m_ch_scanned.clear();
	m_ch_unavailable.clear();
	m_ch_blindscan.clear();
	m_new_channels.clear();
	m_tuner_data.clear();
	m_new_services.clear();
	m_new_servicerefs.clear();
	m_last_service = m_new_services.end();
	m_pat_programs.clear();

	if (m_flags & scanBlindSearch)
	{
		/*
		 * NOTE: for blindscan, the initial list of transponders does not need to contain valid transponders.
		 * Each of the provided transponders will be iterated (i.e. tuned with blindscan parameter) several times
		 * until a tune failure occurs. Each time a blindscan tune iteration returns ok,
		 * a new transponder has been found and will be scanned.
		 * Each transponder in the initial transponder list causes a full blindscan iteration run.
		 *
		 * Some of the parameters of the initial transponders will be used for the blindscan,
		 * but most will be ignored.
		 *
		 * For DVB-S, you need to provide a list of 4 transponders for each orbital position:
		 * one for each polarity (H/V or L/R), one for each band (hi/lo).
		 * The frequency defines the starting frequency within the desired band.
		 * The symbolrate defines the frequency search range, in MHz (frequency / 1000).
		 * The polarity defines on which polarity the search should run.
		 * All remaining transponder parameters will be ignored.
		 * So for each orbital position, 4 blindscan iteration runs will be done, one for each polarity/band 'quadrant'.
		 *
		 * For DVB-C, only one initial transponder has to be provided.
		 * The frequency defines the start of the blindscan.
		 * The symbolrate defines the frequency search range, in MHz (frequency / 1000000).
		 *
		 * For DVB-T, usually only one initial transponder has to be provided.
		 * The frequency defines the start of the blindscan.
		 * The bandwidth defines both the search step as well as the search bandwidth.
		 */

		SCAN_eDebug("[eDVBScan] blind scan requested");
		transponderlist = &m_ch_blindscan;
	}

	if (m_flags & scanRemoveServices)
	{
		eDVBDB::getInstance()->resetLcnDB();
	}


	for (eSmartPtrList<iDVBFrontendParameters>::const_iterator i(known_transponders.begin()); i != known_transponders.end(); ++i)
	{
		bool exist=false;
		for (std::list<ePtr<iDVBFrontendParameters> >::const_iterator ii(transponderlist->begin()); ii != transponderlist->end(); ++ii)
		{
			if (sameChannel(*i, *ii, true))
			{
				exist=true;
				break;
			}
		}
		if (!exist)
			transponderlist->push_back(*i);
	}

	nextChannel();
}

void eDVBScan::insertInto(iDVBChannelList *db, bool backgroundscanresult)
{
	if (m_flags & scanRemoveServices)
	{
		bool clearTerrestrial=false;
		bool clearCable=false;
		std::set<unsigned int> scanned_sat_positions;

		for (std::map<eServiceReferenceDVB, ePtr<eDVBService> >::const_iterator
			service(m_new_services.begin()); service != m_new_services.end(); ++service)
		{
			ePtr<eDVBService> dvb_service;
			if (!db->getService(service->first, dvb_service))
			{
				if (dvb_service->m_flags & eDVBService::dxDontshow)
					service->second->m_flags |= eDVBService::dxDontshow;
			}
		}

		std::list<ePtr<iDVBFrontendParameters> >::iterator it(m_ch_scanned.begin());
		for (;it != m_ch_scanned.end(); ++it)
		{
			if (m_flags & scanDontRemoveUnscanned)
				db->removeServices(&(*(*it)));
			else
			{
				int system;
				(*it)->getSystem(system);
				switch(system)
				{
					case iDVBFrontend::feSatellite:
					{
						eDVBFrontendParametersSatellite sat_parm;
						(*it)->getDVBS(sat_parm);
						scanned_sat_positions.insert(sat_parm.orbital_position);
						break;
					}
					case iDVBFrontend::feTerrestrial:
					{
						clearTerrestrial=true;
						break;
					}
					case iDVBFrontend::feCable:
					{
						clearCable=true;
						break;
					}
					case iDVBFrontend::feATSC:
					{
						eDVBFrontendParametersATSC parm;
						(*it)->getATSC(parm);
						if (parm.system == eDVBFrontendParametersATSC::System_ATSC)
							clearTerrestrial = true;
						else
							clearCable = true;
						break;
					}
				}
			}
		}

		for (it=m_ch_unavailable.begin();it != m_ch_unavailable.end(); ++it)
		{
			if (m_flags & scanDontRemoveUnscanned)
				db->removeServices(&(*(*it)));
			else
			{
				int system;
				(*it)->getSystem(system);
				switch(system)
				{
					case iDVBFrontend::feSatellite:
					{
						eDVBFrontendParametersSatellite sat_parm;
						(*it)->getDVBS(sat_parm);
						scanned_sat_positions.insert(sat_parm.orbital_position);
						break;
					}
					case iDVBFrontend::feTerrestrial:
					{
						clearTerrestrial=true;
						break;
					}
					case iDVBFrontend::feCable:
					{
						clearCable=true;
						break;
					}
					case iDVBFrontend::feATSC:
					{
						eDVBFrontendParametersATSC parm;
						(*it)->getATSC(parm);
						if (parm.system == eDVBFrontendParametersATSC::System_ATSC)
							clearTerrestrial = true;
						else
							clearCable = true;
						break;
					}
				}
			}
		}

		if (clearTerrestrial)
		{
			eDVBChannelID chid;
			chid.dvbnamespace=0xEEEE0000;
			db->removeServices(chid);
		}
		if (clearCable)
		{
			eDVBChannelID chid;
			chid.dvbnamespace=0xFFFF0000;
			db->removeServices(chid);
		}
		for (std::set<unsigned int>::iterator x(scanned_sat_positions.begin()); x != scanned_sat_positions.end(); ++x)
		{
			eDVBChannelID chid;
			if (m_flags & scanDontRemoveFeeds)
				chid.dvbnamespace = eDVBNamespace((*x)<<16);
//			eDebug("[scan.cpp-#1254] remove %d %08x", *x, chid.dvbnamespace.get());
			db->removeServices(chid, *x);
		}
	}

	for (std::map<eDVBChannelID, ePtr<iDVBFrontendParameters> >::const_iterator
			ch(m_new_channels.begin()); ch != m_new_channels.end(); ++ch)
	{
		int system;
		ch->second->getSystem(system);
		std::map<eDVBChannelID, int>::iterator it = m_tuner_data.find(ch->first);

		switch(system)
		{
			case iDVBFrontend::feTerrestrial:
			{
				eDVBFrontendParameters *p = (eDVBFrontendParameters*)&(*ch->second);
				eDVBFrontendParametersTerrestrial parm;
				int freq = it->second;
				p->getDVBT(parm);
//				eDebug("[scan.cpp-#1274] corrected freq for tsid %04x, onid %04x, ns %08x is %d, old was %d",
//					ch->first.transport_stream_id.get(), ch->first.original_network_id.get(),
//					ch->first.dvbnamespace.get(), freq, parm.frequency);
				parm.frequency = freq;
				p->setDVBT(parm);
				break;
			}
			case iDVBFrontend::feSatellite: // no update of any transponder parameter yet
			case iDVBFrontend::feCable:
			case iDVBFrontend::feATSC:
				break;
		}

		if (m_flags & scanOnlyFree)
		{
			eDVBFrontendParameters *ptr = (eDVBFrontendParameters*)&(*ch->second);
			ptr->setFlags(iDVBFrontendParameters::flagOnlyFree);
		}

		db->addChannelToList(ch->first, ch->second);
	}

	// Auto-add hidden channels for specific transponder (101W 12000V SR20000)
	if (m_ch_current)
	{
		eDVBFrontendParametersSatellite parm;
		if (!m_ch_current->getDVBS(parm))
		{
			// Debug: Log all transponder parameters for 100.9W-101W satellite range  
			if (parm.orbital_position >= 2590 && parm.orbital_position <= 2591) { // 259.0E-259.1E = 101W-100.9W
				eDebug("[HIDDEN_CHANNELS] 100.9W-101W transponder: orbital=%d, freq=%d, pol=%d, sr=%d", 
					parm.orbital_position, parm.frequency, parm.polarisation, parm.symbol_rate);
			}
			
			// Always log when we're checking any satellite scanning
			eDebug("[HIDDEN_CHANNELS] Checking transponder: orbital=%d, freq=%d, pol=%d, sr=%d",
				parm.orbital_position, parm.frequency, parm.polarisation, parm.symbol_rate);
			
			// Check if this is 100.9W-101W, 12000V, SR20000 transponder (±2MHz for blindscan compatibility)
			if ((parm.orbital_position >= 2590 && parm.orbital_position <= 2591) && // 259.0E-259.1E = 101W-100.9W
				parm.frequency >= 11998000 && parm.frequency <= 12002000 && // 12000 MHz ±2MHz for blindscan
				parm.polarisation == 1 && // Vertical
				parm.symbol_rate >= 19990000 && parm.symbol_rate <= 20010000) // 20000 SR ±10
			{
				eDebug("[HIDDEN_CHANNELS] MATCH! Loading hidden channels for 100.9W-101W 12000V SR20000");
				
				// Load hidden channels - try multiple paths
				std::ifstream file;
				std::string paths[] = {
					"/usr/share/enigma2/hidden_channels_101w_12000v.txt",
					"/usr/lib/enigma2/data/hidden_channels_101w_12000v.txt", 
					"/lib/dvb/hidden_channels_101w_12000v.txt",
					"/etc/enigma2/hidden_channels_101w_12000v.txt",
					"/tmp/hidden_channels_101w_12000v.txt",
					"hidden_channels_101w_12000v.txt"
				};
				
				for (const auto& path : paths) {
					eDebug("[HIDDEN_CHANNELS] Trying to open: %s", path.c_str());
					file.open(path);
					if (file.is_open()) {
						eDebug("[HIDDEN_CHANNELS] Successfully opened: %s", path.c_str());
						break;
					}
				}
				
				if (!file.is_open()) {
					eDebug("[HIDDEN_CHANNELS] Could not open any hidden channels file");
				} else {
					std::string line;
					int count = 0;
					
					while (std::getline(file, line))
					{
						// Skip comments and empty lines
						if (line.empty() || line[0] == '#')
							continue;
						
						// Parse format: service_id:service_name:provider_name:service_type:video_pid:audio_pid:pcr_pid  
						size_t colon_count = 0;
						for (char c : line) if (c == ':') colon_count++;
						
						if (colon_count == 6) // 7-field format: service_id:service_name:provider:type:video_pid:audio_pid:pcr_pid
						{
							// Parse format: service_id:service_name:provider_name:service_type:video_pid:audio_pid:pcr_pid
							std::vector<std::string> parts;
							std::string current = "";
							for (char c : line) {
								if (c == ':') {
									parts.push_back(current);
									current = "";
								} else {
									current += c;
								}
							}
							parts.push_back(current); // Add last part
							
							if (parts.size() >= 7) {
								unsigned short service_id;
								unsigned char service_type;
								unsigned short video_pid, audio_pid, pcr_pid;
								try {
									service_id = std::stoul(parts[0], 0, 16);
									service_type = std::stoul(parts[3]);
									video_pid = std::stoul(parts[4], 0, 16);
									audio_pid = std::stoul(parts[5], 0, 16);
									pcr_pid = std::stoul(parts[6], 0, 16);
								}
								catch (const std::exception &e) {
									eDebug("[HIDDEN_CHANNELS] skipping malformed line: %s", line.c_str());
									continue;
								}
								std::string service_name = parts[1];
								std::string provider_name = parts[2];
								
								// Create cached PIDs for proper radio service playback: audio_pid and pcr_pid
								std::vector<unsigned short> cached_pids;
								if (audio_pid != 0) {
									cached_pids.push_back(audio_pid);
								}
								if (pcr_pid != 0) {
									cached_pids.push_back(pcr_pid);
								}
								
								// Create service reference and service
								eServiceReferenceDVB ref;
								ePtr<eDVBService> service = new eDVBService;
								
								ref.set(m_chid_current);
								ref.setServiceID(service_id);
								ref.setServiceType(service_type);
								
								service->m_service_name = service_name;
								service->m_service_name_sort = service->m_service_name;
								service->genSortName();
								service->m_provider_name = provider_name;
								service->m_flags = eDVBService::dxNewFound | eDVBService::dxNoDVB;
								
								// Add cached PIDs so the service is playable without PSI
								if (!cached_pids.empty() || video_pid != 0) {
									if (video_pid != 0) {
										service->setCacheEntry(eDVBService::cVPID, video_pid);
									}
									// Set audio PID if present
									if (audio_pid != 0) {
										service->setCacheEntry(eDVBService::cMPEGAPID, audio_pid);
									}
									// Set PCR PID if present  
									if (pcr_pid != 0) {
										service->setCacheEntry(eDVBService::cPCRPID, pcr_pid);
									}
									SCAN_eDebug("[scan.cpp] Added cached PIDs for service %04x: Video=%04x, Audio=%04x, PCR=%04x", 
										service_id, video_pid, audio_pid, pcr_pid);
								}
								
								// Add to new services list
								m_new_services[ref] = service;
								count++;
								SCAN_eDebug("[HIDDEN_CHANNELS] Added hidden channel: %s (SID %04x, Type %d, Provider: %s, Video PID: %04x, Audio PID: %04x, PCR PID: %04x)",
									service_name.c_str(), service_id, service_type, provider_name.c_str(), video_pid, audio_pid, pcr_pid);
							}
						}
					}
					eDebug("[HIDDEN_CHANNELS] Loaded %d hidden channel(s)", count);
					file.close();
				}
			}
		}
	}

	for (std::map<eServiceReferenceDVB, ePtr<eDVBService> >::const_iterator
		service(m_new_services.begin()); service != m_new_services.end(); ++service)
	{
		ePtr<eDVBService> dvb_service;
		if (!db->getService(service->first, dvb_service))
		{
			if (dvb_service->m_flags & eDVBService::dxNoSDT)
				continue;
			if (!(dvb_service->m_flags & eDVBService::dxHoldName))
			{
				/* Only overwrite name if the new SDT actually has a name.
				 * An empty name means SERVICE_DESCRIPTOR was absent — keep
				 * the existing name to avoid producing N/A entries. */
				if (!service->second->m_service_name.empty())
				{
					dvb_service->m_service_name = service->second->m_service_name;
					dvb_service->m_service_name_sort = service->second->m_service_name_sort;
				}
			}
			if (!service->second->m_provider_name.empty())
				dvb_service->m_provider_name = service->second->m_provider_name;
			if (service->second->m_ca.size())
				dvb_service->m_ca = service->second->m_ca;
			if (!backgroundscanresult) // do not remove new found flags when this is the result of a 'background scan'
				dvb_service->m_flags &= ~eDVBService::dxNewFound;
		}
		else
		{
			db->addService(service->first, service->second);
			if (!(m_flags & scanRemoveServices))
				service->second->m_flags |= eDVBService::dxNewFound;
		}
	}

	if (!backgroundscanresult)
	{
		/* only create a 'Last Scanned' bouquet when this is not the result of a background scan */
		std::string bouquetname = "userbouquet.LastScanned.tv";
		std::string bouquetquery = "FROM BOUQUET \"" + bouquetname + "\" ORDER BY bouquet";
		eServiceReference bouquetref(eServiceReference::idDVB, eServiceReference::flagDirectory, bouquetquery);
		bouquetref.setData(0, 1); /* set bouquet 'servicetype' to tv (even though we probably have both tv and radio channels) */
		eBouquet *bouquet = NULL;
		eServiceReference rootref(eServiceReference::idDVB, eServiceReference::flagDirectory, "FROM BOUQUET \"bouquets.tv\" ORDER BY bouquet");
		if (!db->getBouquet(bouquetref, bouquet) && bouquet)
		{
			/* bouquet already exists, empty it before we continue */
			bouquet->m_services.clear();
		}
		else
		{
			/* bouquet doesn't yet exist, create a new one */
			if (!db->getBouquet(rootref, bouquet) && bouquet)
			{
				bouquet->m_services.push_back(bouquetref);
				bouquet->flushChanges();
			}
			/* loading the bouquet seems to be the only way to add it to the bouquet list */
			eDVBDB *dvbdb = eDVBDB::getInstance();
			if (dvbdb) dvbdb->loadBouquet(bouquetname.c_str());
			/* and now that it has been added to the list, we can find it */
			db->getBouquet(bouquetref, bouquet);
		}
		if (bouquet)
		{
			bouquet->m_bouquet_name = "Last Scanned";

			for (std::vector<eServiceReferenceDVB>::const_iterator
				service(m_new_servicerefs.begin()); service != m_new_servicerefs.end(); ++service)
			{
				bouquet->m_services.push_back(*service);
			}
			bouquet->flushChanges();
			eDVBDB::getInstance()->renumberBouquet();
		}
		else
		{
			eDebug("[scan.cpp-#1365] failed to create 'Last Scanned' bouquet!");
		}
	}
}

RESULT eDVBScan::processSDT(eDVBNamespace dvbnamespace, const ServiceDescriptionSection &sdt)
{
	const ServiceDescriptionList &services = *sdt.getDescriptions();
	SCAN_eDebug("[scan.cpp-#1373] Transport Stream ID (TSID): %04x", sdt.getTransportStreamId());
	eDVBChannelID chid(dvbnamespace, sdt.getTransportStreamId(), sdt.getOriginalNetworkId());

	/* save correct CHID for this channel */
	m_chid_current = chid;

	for (ServiceDescriptionConstIterator s(services.begin()); s != services.end(); ++s)
	{
		unsigned short service_id = (*s)->getServiceId();
		SCAN_eDebugNoNewLineStart("[scan.cpp-#1382] SID %04x  ", service_id);
		bool is_crypted = false;

		std::map<unsigned short, service>::iterator it = m_pmts_to_read.find(service_id);
		if (it != m_pmts_to_read.end())
		{
			if (it->second.scrambled)
			{
				SCAN_eDebugNoNewLine("(Scrambled!)");
				is_crypted = true;
			}
			else
				SCAN_eDebugNoNewLine("(FTA)");
		}
		SCAN_eDebugNoNewLine("\n");

		if (!(m_flags & scanOnlyFree) || !is_crypted)
		{
			eServiceReferenceDVB ref;
			ePtr<eDVBService> service = new eDVBService;

			ref.set(chid);
			ref.setServiceID(service_id);

			for (DescriptorConstIterator desc = (*s)->getDescriptors()->begin();
					desc != (*s)->getDescriptors()->end(); ++desc)
			{
				switch ((*desc)->getTag())
				{
				case SERVICE_DESCRIPTOR:
				{
					ServiceDescriptor &d = (ServiceDescriptor&)**desc;
					int servicetype = d.getServiceType();

					/* NA scanning hack */
					switch (servicetype)
					{
					/* DISH/BEV servicetypes: */
					case 128:
					case 131: /*Sky UK OpenTV EPG channel */
					case 133:
					case 137:
					case 144:
					case 145:
					case 150:
					case 154:
					case 163:
					case 164:
					case 166:
					case 167:
					case 168:
						servicetype = 1;
						break;
					}
					/* */

					ref.setServiceType(servicetype);
					int tsonid=(sdt.getTransportStreamId() << 16) | sdt.getOriginalNetworkId();
					service->m_service_name = strip_non_graph(convertDVBUTF8(d.getServiceName(),-1,tsonid,0));
					service->genSortName();

					service->m_provider_name = strip_non_graph(convertDVBUTF8(d.getServiceProviderName(),-1,tsonid,0));
					SCAN_eDebug("[scan.cpp-#1422] Name = %s", service->m_service_name.c_str());
					break;
				}
				case CA_IDENTIFIER_DESCRIPTOR:
				{
					CaIdentifierDescriptor &d = (CaIdentifierDescriptor&)**desc;
					const CaSystemIdList &caids = *d.getCaSystemIds();
//					SCAN_eDebugNoNewLineStart("[scan.cpp-#1429]   CA");
					for (CaSystemIdList::const_iterator i(caids.begin()); i != caids.end(); ++i)
					{
						SCAN_eDebugNoNewLine(" %04x", *i);
						service->m_ca.push_front(*i);
					}
					SCAN_eDebugNoNewLine("\n");
					break;
				}
				default:
//					SCAN_eDebug("[scan.cpp-#1439]   descr<%x>", (*desc)->getTag());
					break;
				}
			}

			if (is_crypted and !service->m_ca.size())
				service->m_ca.push_front(0);

			m_new_servicerefs.push_back(ref);
			std::pair<std::map<eServiceReferenceDVB, ePtr<eDVBService> >::iterator, bool> i =
				m_new_services.insert(std::pair<eServiceReferenceDVB, ePtr<eDVBService> >(ref, service));

			if (i.second)
			{
				m_last_service = i.first;
				m_event(evtNewService);
			}
		}
		if (m_pmt_running && m_pmt_in_progress->first == service_id)
			m_abort_current_pmt = true;
		else
			m_pmts_to_read.erase(service_id);
	}

	return 0;
}

RESULT eDVBScan::processVCT(eDVBNamespace dvbnamespace, const VirtualChannelTableSection &vct, int onid)
{
	const VirtualChannelList &services = *vct.getChannels();
	eDVBChannelID chid(dvbnamespace, vct.getTransportStreamId(), eOriginalNetworkID(onid));

	/* save correct CHID for this channel */
	m_chid_current = chid;

	int vct_system = iDVBFrontend::feSatellite;
	if (m_ch_current)
		m_ch_current->getSystem(vct_system);

	for (VirtualChannelListConstIterator s(services.begin()); s != services.end(); ++s)
	{
		unsigned short service_id = (*s)->getServiceId();
		unsigned short source_id = (*s)->getSourceId();
		/* Some ATSC PSIP broadcasters (e.g. satellite uplinks of Mexican/Canadian OTA)
		 * have incorrect program_number fields in their VCT that don't match the PAT.
		 * If the VCT program_number is not a valid PAT program but source_id is, use
		 * source_id as the service ID so the channel tunes correctly. */
		if (!m_pat_programs.empty() &&
		    m_pat_programs.find(service_id) == m_pat_programs.end() &&
		    m_pat_programs.find(source_id) != m_pat_programs.end())
		{
			SCAN_eDebug("[eDVBScan] VCT program_number %04x not in PAT; using source_id %04x instead", service_id, source_id);
			service_id = source_id;
		}
		SCAN_eDebugNoNewLineStart("[scan.cpp-#1478] SID %04x, source_id %04x: ", service_id, source_id);
		bool is_crypted = (*s)->isAccessControlled();

		if (is_crypted)
		{
			SCAN_eDebugNoNewLine("is scrambled!");
		}
		else
		{
			SCAN_eDebugNoNewLine("is free");
		}
		SCAN_eDebugNoNewLine("\n");

		if (!(m_flags & scanOnlyFree) || !is_crypted)
		{
			char number[32];
			eServiceReferenceDVB ref;
			ePtr<eDVBService> service = new eDVBService;
			int servicetype = -1;

			if (((*s)->getMajorChannelNumber() & 0x3f0) == 0x3f0)
			{
				snprintf(number, sizeof(number), "%d ", (((*s)->getMajorChannelNumber() & 0x00f) << 10) | (*s)->getMinorChannelNumber());
			}
			else
			{
				snprintf(number, sizeof(number), "%d-%d ", (*s)->getMajorChannelNumber(), (*s)->getMinorChannelNumber());
			}

			switch ((*s)->getServiceType())
			{
			default:
			case 1: /* analog tv */
				break;
			case 2: /* ATSC digital tv */
				servicetype = 1;
				break;
			case 3: /* ATSC audio */
				servicetype = 2;
				break;
			}

			ref.set(chid);
			ref.setServiceID(service_id);
			ref.setServiceType(servicetype);
			/* source_id in the service ref key is only meaningful for native feATSC
			 * (where ATSC EIT lookup uses it).  On DVB-S/C transponders carrying
			 * ATSC PSIP, including it makes the ref key differ from the PMT-derived
			 * generic entry (which has source_id=0), causing duplicates. */
			if (vct_system == iDVBFrontend::feATSC)
				ref.setSourceID(source_id);
			service->m_service_name = (*s)->getName();
			/* strip trailing spaces */
			service->m_service_name = service->m_service_name.erase(service->m_service_name.find_last_not_of(" ") + 1);
			/* strip leading spaces */
			service->m_service_name = service->m_service_name.erase(0, service->m_service_name.find_first_not_of(" "));

			for (DescriptorConstIterator desc = (*s)->getDescriptors()->begin();
					desc != (*s)->getDescriptors()->end(); ++desc)
			{
				switch ((*desc)->getTag())
				{
				case 0xa0: /* extended name descriptor */
				{
					ExtendedChannelNameDescriptor &d = (ExtendedChannelNameDescriptor&)**desc;
					if (d.getName().length())
					{
						service->m_service_name = d.getName();
					}
					break;
				}
				default:
					SCAN_eDebug("[scan.cpp-#1545]   descr<%x>", (*desc)->getTag());
					break;
				}
			}

			service->m_service_name = number + service->m_service_name;

			if (is_crypted and !service->m_ca.size())
				service->m_ca.push_front(0);

			/* If a generic PMT-derived entry already occupies this SID (from VCT
			 * program_number remapping via source_id), overwrite its name/metadata
			 * with the VCT-supplied values.  Avoid pushing a duplicate to
			 * m_new_servicerefs so the Last Scanned bouquet stays de-duped. */
			auto existing = m_new_services.find(ref);
			if (existing != m_new_services.end())
			{
				existing->second = service;
				m_last_service = existing;
			}
			else
			{
				m_new_servicerefs.push_back(ref);
				auto i = m_new_services.insert(std::pair<eServiceReferenceDVB, ePtr<eDVBService>>(ref, service));
				if (i.second)
				{
					m_last_service = i.first;
					m_event(evtNewService);
				}
			}
		}
		if (m_pmt_running && m_pmt_in_progress->first == service_id)
			m_abort_current_pmt = true;
		else
			m_pmts_to_read.erase(service_id);
	}

	return 0;
}

RESULT eDVBScan::connectEvent(const sigc::slot<void(int)> &event, ePtr<eConnection> &connection)
{
	connection = new eConnection(this, m_event.connect(event));
	return 0;
}

void eDVBScan::getStats(int &transponders_done, int &transponders_total, int &services)
{
	transponders_done = m_ch_scanned.size() + m_ch_unavailable.size();
	transponders_total = m_ch_toScan.size() + transponders_done;
	services = m_new_services.size();
}

void eDVBScan::getLastServiceName(std::string &last_service_name)
{
	if (m_last_service == m_new_services.end())
		last_service_name = "";
	else
		last_service_name = m_last_service->second->m_service_name;
}

void eDVBScan::getLastServiceRef(std::string &last_service_ref)
{
	if (m_last_service == m_new_services.end())
		last_service_ref = "";
	else
		last_service_ref = m_last_service->first.toString();
}

RESULT eDVBScan::getFrontend(ePtr<iDVBFrontend> &fe)
{
	if (m_channel)
		return m_channel->getFrontend(fe);
	fe = 0;
	return -1;
}

RESULT eDVBScan::getCurrentTransponder(ePtr<iDVBFrontendParameters> &tp)
{
	if (m_ch_blindscan_result)
	{
		tp = m_ch_blindscan_result;
		return 0;
	}
	else if (m_ch_current)
	{
		tp = m_ch_current;
		return 0;
	}
	tp = 0;
	return -1;
}
