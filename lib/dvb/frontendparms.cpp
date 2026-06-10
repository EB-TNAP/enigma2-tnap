#include <linux/dvb/version.h>

#include <lib/dvb/dvb.h>
#include <lib/dvb/frontendparms.h>
#include <lib/base/eerror.h>
#include <lib/base/nconfig.h> // access to python config
#include <errno.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/ioctl.h>

// Define DTV_STAT_MODCOD if not defined in the DVB API
#ifndef DTV_STAT_MODCOD
#define DTV_STAT_MODCOD 92
#endif

// Define DVB_S2_MODCOD namespace for DVB-S2 MODCOD values
// This was removed from newer Linux DVB API headers but is still needed for Enigma2
namespace DVB_S2_MODCOD
{
	enum
	{
		DUMMY_PLF = 0,
		QPSK_1_4 = 1,
		QPSK_1_3 = 2,
		QPSK_2_5 = 3,
		QPSK_1_2 = 4,
		QPSK_3_5 = 5,
		QPSK_2_3 = 6,
		QPSK_3_4 = 7,
		QPSK_4_5 = 8,
		QPSK_5_6 = 9,
		QPSK_8_9 = 10,
		QPSK_9_10 = 11,
		PSK8_3_5 = 12,
		PSK8_2_3 = 13,
		PSK8_3_4 = 14,
		PSK8_5_6 = 15,
		PSK8_8_9 = 16,
		PSK8_9_10 = 17,
		APSK16_2_3 = 18,
		APSK16_3_4 = 19,
		APSK16_4_5 = 20,
		APSK16_5_6 = 21,
		APSK16_8_9 = 22,
		APSK16_9_10 = 23,
		APSK32_3_4 = 24,
		APSK32_4_5 = 25,
		APSK32_5_6 = 26,
		APSK32_8_9 = 27,
		APSK32_9_10 = 28
	};

	// Required SNR values in dB * 10 for each MODCOD (based on DVB-S2 spec)
	// Index corresponds to MODCOD value above
	static const int requiredSNR_x10[] = {
		0,   // 0: DUMMY_PLF
		-20, // 1: QPSK 1/4   (~-2.0 dB)
		-14, // 2: QPSK 1/3   (~-1.4 dB)
		-6,  // 3: QPSK 2/5   (~-0.6 dB)
		10,  // 4: QPSK 1/2   (~1.0 dB)
		23,  // 5: QPSK 3/5   (~2.3 dB)
		33,  // 6: QPSK 2/3   (~3.3 dB)
		41,  // 7: QPSK 3/4   (~4.1 dB)
		49,  // 8: QPSK 4/5   (~4.9 dB)
		54,  // 9: QPSK 5/6   (~5.4 dB)
		62,  // 10: QPSK 8/9  (~6.2 dB)
		64,  // 11: QPSK 9/10 (~6.4 dB)
		58,  // 12: 8PSK 3/5  (~5.8 dB)
		68,  // 13: 8PSK 2/3  (~6.8 dB)
		79,  // 14: 8PSK 3/4  (~7.9 dB)
		90,  // 15: 8PSK 5/6  (~9.0 dB)
		100, // 16: 8PSK 8/9  (~10.0 dB)
		102, // 17: 8PSK 9/10 (~10.2 dB)
		93,  // 18: 16APSK 2/3  (~9.3 dB)
		104, // 19: 16APSK 3/4  (~10.4 dB)
		109, // 20: 16APSK 4/5  (~10.9 dB)
		115, // 21: 16APSK 5/6  (~11.5 dB)
		124, // 22: 16APSK 8/9  (~12.4 dB)
		126, // 23: 16APSK 9/10 (~12.6 dB)
		127, // 24: 32APSK 3/4  (~12.7 dB)
		132, // 25: 32APSK 4/5  (~13.2 dB)
		138, // 26: 32APSK 5/6  (~13.8 dB)
		147, // 27: 32APSK 8/9  (~14.7 dB)
		149  // 28: 32APSK 9/10 (~14.9 dB)
	};
}


DEFINE_REF(eDVBFrontendStatus);

eDVBFrontendStatus::eDVBFrontendStatus(ePtr<eDVBFrontend> &fe)
: frontend(fe)
{
}

int eDVBFrontendStatus::getState() const
{
	int result;
	if (!frontend) return -1;
	if (frontend->getState(result) < 0) return -1;
	return result;
};

std::string eDVBFrontendStatus::getStateDescription() const
{
	switch (getState())
	{
	case iDVBFrontend_ENUMS::stateIdle:
		return "IDLE";
	case iDVBFrontend_ENUMS::stateTuning:
		return "TUNING";
	case iDVBFrontend_ENUMS::stateFailed:
		return "FAILED";
	case iDVBFrontend_ENUMS::stateLock:
		return "LOCKED";
	case iDVBFrontend_ENUMS::stateLostLock:
		return "LOSTLOCK";
	default:
		break;
	}
	return "UNKNOWN";
}

int eDVBFrontendStatus::getLocked() const
{
	if (!frontend) return 0;
	return frontend->readFrontendData(iFrontendInformation_ENUMS::lockState);
}

int eDVBFrontendStatus::getSynced() const
{
	if (!frontend) return 0;
	return frontend->readFrontendData(iFrontendInformation_ENUMS::syncState);
}

int eDVBFrontendStatus::getBER() const
{
	if (!frontend || getState() == iDVBFrontend_ENUMS::stateTuning) return 0;
	return frontend->readFrontendData(iFrontendInformation_ENUMS::bitErrorRate);
}

int eDVBFrontendStatus::getSNR() const
{
	if (!frontend) return 0;
	return frontend->readFrontendData(iFrontendInformation_ENUMS::signalQuality);
}

int eDVBFrontendStatus::getSNRdB() const
{
	int value;
	if (!frontend) return 0;
	value = frontend->readFrontendData(iFrontendInformation_ENUMS::signalQualitydB);
	if (value == 0x12345678)
	{
		return -1; /* not supported */
	}
	return value;
}

int eDVBFrontendStatus::getSignalPower() const
{
	if (!frontend) return 0;
	return frontend->readFrontendData(iFrontendInformation_ENUMS::signalPower);
}

eDVBTransponderData::eDVBTransponderData(struct dtv_property *dtvproperties, unsigned int propertycount, bool original)
: originalValues(original)
{
	for (unsigned int i = 0; i < propertycount; i++)
	{
		dtvProperties.push_back(dtvproperties[i]);
	}
}

int eDVBTransponderData::getProperty(unsigned int cmd) const
{
	for (unsigned int i = 0; i < dtvProperties.size(); i++)
	{
		if (dtvProperties[i].cmd == cmd)
		{
			return dtvProperties[i].u.data;
		}
	}
	return -1;
}

int eDVBTransponderData::getInversion() const
{
	return -1;
}

unsigned int eDVBTransponderData::getFrequency() const
{
	return 0;
}

unsigned int eDVBTransponderData::getSymbolRate() const
{
	return 0;
}

int eDVBTransponderData::getOrbitalPosition() const
{
	return -1;
}

int eDVBTransponderData::getFecInner() const
{
	return -1;
}

int eDVBTransponderData::getModulation() const
{
	return -1;
}

int eDVBTransponderData::getPolarization() const
{
	return -1;
}

int eDVBTransponderData::getRolloff() const
{
	return -1;
}

int eDVBTransponderData::getPilot() const
{
	return -1;
}

int eDVBTransponderData::getSystem() const
{
	return -1;
}

int eDVBTransponderData::getIsId() const
{
	return -1;
}

int eDVBTransponderData::getPLSMode() const
{
	return -1;
}

int eDVBTransponderData::getPLSCode() const
{
	return -1;
}

int eDVBTransponderData::getT2MIPlpId() const
{
	return -1;
}

int eDVBTransponderData::getT2MIPid() const
{
	return -1;
}

int eDVBTransponderData::getBandwidth() const
{
	return -1;
}

int eDVBTransponderData::getCodeRateLp() const
{
	return -1;
}

int eDVBTransponderData::getCodeRateHp() const
{
	return -1;
}

int eDVBTransponderData::getConstellation() const
{
	return -1;
}

int eDVBTransponderData::getTransmissionMode() const
{
	return -1;
}

int eDVBTransponderData::getGuardInterval() const
{
	return -1;
}

int eDVBTransponderData::getHierarchyInformation() const
{
	return -1;
}

int eDVBTransponderData::getPlpId() const
{
	return -1;
}

DEFINE_REF(eDVBSatelliteTransponderData);

eDVBSatelliteTransponderData::eDVBSatelliteTransponderData(struct dtv_property *dtvproperties, unsigned int propertycount, eDVBFrontendParametersSatellite &transponderparms, int frequencyoffset, bool original, int modcod)
: eDVBTransponderData(dtvproperties, propertycount, original), transponderParameters(transponderparms), frequencyOffset(frequencyoffset), m_modcod(modcod)
{
    // If we already have MODCOD stored in transponder parameters, use it if not provided
    if (m_modcod == 0 && transponderParameters.modcod > 0)
    {
        m_modcod = transponderParameters.modcod;
    }
    
    // Try to extract MODCOD from properties if available
    if (m_modcod == 0 && !original)
    {
        for (unsigned int i = 0; i < propertycount; i++)
        {
            if (dtvproperties[i].cmd == DTV_STAT_MODCOD)
            {
                m_modcod = dtvproperties[i].u.data;
                break;
            }
        }
    }
}

std::string eDVBSatelliteTransponderData::getTunerType() const
{
	return "DVB-S";
}

int eDVBSatelliteTransponderData::getInversion() const
{
	if (originalValues) return transponderParameters.inversion;

	switch (getProperty(DTV_INVERSION))
	{
	case INVERSION_OFF: return eDVBFrontendParametersSatellite::Inversion_Off;
	case INVERSION_ON: return eDVBFrontendParametersSatellite::Inversion_On;
	default: eDebug("[eDVBSatelliteTransponderData] got unsupported inversion from frontend! report as INVERSION_AUTO!\n");
	[[fallthrough]];
	case INVERSION_AUTO: return eDVBFrontendParametersSatellite::Inversion_Unknown;
	}
}

unsigned int eDVBSatelliteTransponderData::getFrequency() const
{
	if (originalValues) return transponderParameters.frequency;

	return getProperty(DTV_FREQUENCY) + frequencyOffset;
}

unsigned int eDVBSatelliteTransponderData::getSymbolRate() const
{
	if (originalValues) return transponderParameters.symbol_rate;

	return getProperty(DTV_SYMBOL_RATE);
}

int eDVBSatelliteTransponderData::getOrbitalPosition() const
{
	return transponderParameters.orbital_position;
}

int eDVBSatelliteTransponderData::getFecInner() const
{
	if (originalValues) return transponderParameters.fec;

	switch (getProperty(DTV_INNER_FEC))
	{
	case FEC_1_2: return eDVBFrontendParametersSatellite::FEC_1_2;
	case FEC_2_3: return eDVBFrontendParametersSatellite::FEC_2_3;
	case FEC_3_4: return eDVBFrontendParametersSatellite::FEC_3_4;
	case FEC_3_5: return eDVBFrontendParametersSatellite::FEC_3_5;
	case FEC_4_5: return eDVBFrontendParametersSatellite::FEC_4_5;
	case FEC_5_6: return eDVBFrontendParametersSatellite::FEC_5_6;
	case FEC_6_7: return eDVBFrontendParametersSatellite::FEC_6_7;
	case FEC_7_8: return eDVBFrontendParametersSatellite::FEC_7_8;
	case FEC_8_9: return eDVBFrontendParametersSatellite::FEC_8_9;
	case FEC_9_10: return eDVBFrontendParametersSatellite::FEC_9_10;
	case FEC_NONE: return eDVBFrontendParametersSatellite::FEC_None;
	default: eDebug("[eDVBSatelliteTransponderData] got unsupported FEC from frontend! report as FEC_AUTO!\n %d", (getProperty(DTV_INNER_FEC)));
	[[fallthrough]];
	case FEC_AUTO: return eDVBFrontendParametersSatellite::FEC_Auto;
	}
}

int eDVBSatelliteTransponderData::getModulation() const
{
	if (originalValues) return transponderParameters.modulation;

	switch (getProperty(DTV_MODULATION))
	{
	default: eDebug("[eDVBSatelliteTransponderData] got unsupported modulation from frontend! report as QPSK!");
	[[fallthrough]];
	case QPSK: return eDVBFrontendParametersSatellite::Modulation_QPSK;
	case PSK_8: return eDVBFrontendParametersSatellite::Modulation_8PSK;
	case APSK_16: return eDVBFrontendParametersSatellite::Modulation_16APSK;
	case APSK_32: return eDVBFrontendParametersSatellite::Modulation_32APSK;
	}
}

int eDVBSatelliteTransponderData::getPolarization() const
{
	return transponderParameters.polarisation;
}

int eDVBSatelliteTransponderData::getRolloff() const
{
	if (originalValues) return transponderParameters.rolloff;

	switch (getProperty(DTV_ROLLOFF))
	{
	case ROLLOFF_20: return eDVBFrontendParametersSatellite::RollOff_alpha_0_20;
	case ROLLOFF_25: return eDVBFrontendParametersSatellite::RollOff_alpha_0_25;
	case ROLLOFF_35: return eDVBFrontendParametersSatellite::RollOff_alpha_0_35;
	default:
	case ROLLOFF_AUTO: return eDVBFrontendParametersSatellite::RollOff_auto;
	}
}

int eDVBSatelliteTransponderData::getPilot() const
{
	if (originalValues) return transponderParameters.pilot;

	switch (getProperty(DTV_PILOT))
	{
	case PILOT_OFF: return eDVBFrontendParametersSatellite::Pilot_Off;
	case PILOT_ON: return eDVBFrontendParametersSatellite::Pilot_On;
	default:
	case PILOT_AUTO: return eDVBFrontendParametersSatellite::Pilot_Unknown;
	}
}

int eDVBSatelliteTransponderData::getSystem() const
{
	if (originalValues) return transponderParameters.system;

	switch (getProperty(DTV_DELIVERY_SYSTEM))
	{
	default: eDebug("[eDVBSatelliteTransponderData] got unsupported system from frontend! report as DVBS!");
	[[fallthrough]];
	case SYS_DVBS: return eDVBFrontendParametersSatellite::System_DVB_S;
	case SYS_DVBS2: return eDVBFrontendParametersSatellite::System_DVB_S2;
	}
}

int eDVBSatelliteTransponderData::getIsId() const
{
	if (originalValues) return transponderParameters.is_id;

	unsigned int stream_id = getProperty(DTV_STREAM_ID);
	if (stream_id == NO_STREAM_ID_FILTER) return transponderParameters.is_id;
	return stream_id & 0xFF;
}

int eDVBSatelliteTransponderData::getPLSMode() const
{
	if (originalValues) return transponderParameters.pls_mode;

	if (getProperty(DTV_API_VERSION) >= DVB_VERSION(5, 11))
		return eDVBFrontendParametersSatellite::PLS_Gold;

	unsigned int stream_id = getProperty(DTV_STREAM_ID);
	if (stream_id == NO_STREAM_ID_FILTER) return transponderParameters.pls_mode;
	return (stream_id >> 26) & 0x3;
}

int eDVBSatelliteTransponderData::getPLSCode() const
{
	if (originalValues) return transponderParameters.pls_code;

	if (getProperty(DTV_API_VERSION) >= DVB_VERSION(5, 11))
		return getProperty(DTV_SCRAMBLING_SEQUENCE_INDEX);

	unsigned int stream_id = getProperty(DTV_STREAM_ID);
	if (stream_id == NO_STREAM_ID_FILTER) return transponderParameters.pls_code;
	return (stream_id >> 8) & 0x3FFFF;
}

int eDVBSatelliteTransponderData::getT2MIPlpId() const
{
	if (originalValues) return transponderParameters.t2mi_plp_id;

	/* FIXME HACK ALERT use unused by enigma2 ISDBT SEGMENT IDX to pass T2MI PLP ID */
	unsigned int t2mi_plp_id = getProperty(DTV_ISDBT_SB_SEGMENT_IDX);
	if (t2mi_plp_id == eDVBFrontendParametersSatellite::No_T2MI_PLP_Id) return transponderParameters.t2mi_plp_id;
	if (!(t2mi_plp_id & 0x80000000)) return transponderParameters.t2mi_plp_id;
	return t2mi_plp_id & 0xFF;
}

int eDVBSatelliteTransponderData::getT2MIPid() const
{
	if (originalValues) return transponderParameters.t2mi_pid;

	/* FIXME HACK ALERT use unused by enigma2 ISDBT SEGMENT IDX to pass T2MI PID */
	unsigned int t2mi_pid = getProperty(DTV_ISDBT_SB_SEGMENT_IDX);
	if (t2mi_pid == eDVBFrontendParametersSatellite::No_T2MI_PLP_Id) return transponderParameters.t2mi_pid;
	if (!(t2mi_pid & 0x80000000)) return transponderParameters.t2mi_pid;
	return (t2mi_pid >> 16) & 0x1FFF;
}

std::string eDVBSatelliteTransponderData::getMODCODDescription() const
{
    switch (m_modcod)
    {
        case DVB_S2_MODCOD::QPSK_1_4: return "QPSK 1/4";
        case DVB_S2_MODCOD::QPSK_1_3: return "QPSK 1/3";
        case DVB_S2_MODCOD::QPSK_2_5: return "QPSK 2/5";
        case DVB_S2_MODCOD::QPSK_1_2: return "QPSK 1/2";
        case DVB_S2_MODCOD::QPSK_3_5: return "QPSK 3/5";
        case DVB_S2_MODCOD::QPSK_2_3: return "QPSK 2/3";
        case DVB_S2_MODCOD::QPSK_3_4: return "QPSK 3/4";
        case DVB_S2_MODCOD::QPSK_4_5: return "QPSK 4/5";
        case DVB_S2_MODCOD::QPSK_5_6: return "QPSK 5/6";
        case DVB_S2_MODCOD::QPSK_8_9: return "QPSK 8/9";
        case DVB_S2_MODCOD::QPSK_9_10: return "QPSK 9/10";
        case DVB_S2_MODCOD::PSK8_3_5: return "8PSK 3/5";
        case DVB_S2_MODCOD::PSK8_2_3: return "8PSK 2/3";
        case DVB_S2_MODCOD::PSK8_3_4: return "8PSK 3/4";
        case DVB_S2_MODCOD::PSK8_5_6: return "8PSK 5/6";
        case DVB_S2_MODCOD::PSK8_8_9: return "8PSK 8/9";
        case DVB_S2_MODCOD::PSK8_9_10: return "8PSK 9/10";
        case DVB_S2_MODCOD::APSK16_2_3: return "16APSK 2/3";
        case DVB_S2_MODCOD::APSK16_3_4: return "16APSK 3/4";
        case DVB_S2_MODCOD::APSK16_4_5: return "16APSK 4/5";
        case DVB_S2_MODCOD::APSK16_5_6: return "16APSK 5/6";
        case DVB_S2_MODCOD::APSK16_8_9: return "16APSK 8/9";
        case DVB_S2_MODCOD::APSK16_9_10: return "16APSK 9/10";
        case DVB_S2_MODCOD::APSK32_3_4: return "32APSK 3/4";
        case DVB_S2_MODCOD::APSK32_4_5: return "32APSK 4/5";
        case DVB_S2_MODCOD::APSK32_5_6: return "32APSK 5/6";
        case DVB_S2_MODCOD::APSK32_8_9: return "32APSK 8/9";
        case DVB_S2_MODCOD::APSK32_9_10: return "32APSK 9/10";
        default:
            // Fall back to modulation + FEC if we don't have a specific MODCOD value
            std::string mod;
            switch(getModulation())
            {
                case eDVBFrontendParametersSatellite::Modulation_QPSK: mod = "QPSK"; break;
                case eDVBFrontendParametersSatellite::Modulation_8PSK: mod = "8PSK"; break;
                case eDVBFrontendParametersSatellite::Modulation_16APSK: mod = "16APSK"; break;
                case eDVBFrontendParametersSatellite::Modulation_32APSK: mod = "32APSK"; break;
                default: mod = "Unknown"; break;
            }
            
            std::string fec;
            switch(getFecInner())
            {
                case eDVBFrontendParametersSatellite::FEC_1_2: fec = "1/2"; break;
                case eDVBFrontendParametersSatellite::FEC_2_3: fec = "2/3"; break;
                case eDVBFrontendParametersSatellite::FEC_3_4: fec = "3/4"; break;
                case eDVBFrontendParametersSatellite::FEC_3_5: fec = "3/5"; break;
                case eDVBFrontendParametersSatellite::FEC_4_5: fec = "4/5"; break;
                case eDVBFrontendParametersSatellite::FEC_5_6: fec = "5/6"; break;
                case eDVBFrontendParametersSatellite::FEC_6_7: fec = "6/7"; break;
                case eDVBFrontendParametersSatellite::FEC_7_8: fec = "7/8"; break;
                case eDVBFrontendParametersSatellite::FEC_8_9: fec = "8/9"; break;
                case eDVBFrontendParametersSatellite::FEC_9_10: fec = "9/10"; break;
                default: fec = "Auto"; break;
            }
            
            if (mod != "Unknown" && fec != "Auto")
                return mod + " " + fec;
            return "Unknown";
    }
}

int eDVBSatelliteTransponderData::getRequiredSNR() const
{
    if (m_modcod > 0 && m_modcod < (int)(sizeof(DVB_S2_MODCOD::requiredSNR_x10) / sizeof(int)))
        return DVB_S2_MODCOD::requiredSNR_x10[m_modcod];
    
    // If we don't have a specific MODCOD, we can approximate based on modulation and FEC
    if (getSystem() == eDVBFrontendParametersSatellite::System_DVB_S2)
    {
        int mod = getModulation();
        int fec = getFecInner();
        
        // These are approximations based on typical values
        if (mod == eDVBFrontendParametersSatellite::Modulation_QPSK)
        {
            switch (fec)
            {
                case eDVBFrontendParametersSatellite::FEC_1_2: return 41;  // ~4.1 dB
                case eDVBFrontendParametersSatellite::FEC_2_3: return 52;  // ~5.2 dB
                case eDVBFrontendParametersSatellite::FEC_3_4: return 60;  // ~6.0 dB
                case eDVBFrontendParametersSatellite::FEC_3_5: return 48;  // ~4.8 dB
                case eDVBFrontendParametersSatellite::FEC_4_5: return 64;  // ~6.4 dB
                case eDVBFrontendParametersSatellite::FEC_5_6: return 67;  // ~6.7 dB
                case eDVBFrontendParametersSatellite::FEC_8_9: return 74;  // ~7.4 dB
                case eDVBFrontendParametersSatellite::FEC_9_10: return 75; // ~7.5 dB
                default: return 60; // average value
            }
        }
        else if (mod == eDVBFrontendParametersSatellite::Modulation_8PSK)
        {
            switch (fec)
            {
                case eDVBFrontendParametersSatellite::FEC_2_3: return 83;  // ~8.3 dB
                case eDVBFrontendParametersSatellite::FEC_3_4: return 94;  // ~9.4 dB
                case eDVBFrontendParametersSatellite::FEC_3_5: return 78;  // ~7.8 dB
                case eDVBFrontendParametersSatellite::FEC_5_6: return 107; // ~10.7 dB
                case eDVBFrontendParametersSatellite::FEC_8_9: return 118; // ~11.8 dB
                case eDVBFrontendParametersSatellite::FEC_9_10: return 120; // ~12.0 dB
                default: return 100; // average value
            }
        }
        else if (mod == eDVBFrontendParametersSatellite::Modulation_16APSK)
        {
            return 130; // ~13 dB average
        }
        else if (mod == eDVBFrontendParametersSatellite::Modulation_32APSK)
        {
            return 170; // ~17 dB average
        }
    }
    else // DVB-S
    {
        // DVB-S is always QPSK
        int fec = getFecInner();
        switch (fec)
        {
            case eDVBFrontendParametersSatellite::FEC_1_2: return 43;  // ~4.3 dB
            case eDVBFrontendParametersSatellite::FEC_2_3: return 56;  // ~5.6 dB
            case eDVBFrontendParametersSatellite::FEC_3_4: return 67;  // ~6.7 dB
            case eDVBFrontendParametersSatellite::FEC_5_6: return 77;  // ~7.7 dB
            case eDVBFrontendParametersSatellite::FEC_7_8: return 85;  // ~8.5 dB
            default: return 60; // average value
        }
    }
    
    return 0; // Unknown
}

DEFINE_REF(eDVBCableTransponderData);

eDVBCableTransponderData::eDVBCableTransponderData(struct dtv_property *dtvproperties, unsigned int propertycount, eDVBFrontendParametersCable &transponderparms, bool original)
 : eDVBTransponderData(dtvproperties, propertycount, original), transponderParameters(transponderparms)
{
}

std::string eDVBCableTransponderData::getTunerType() const
{
	return "DVB-C";
}

int eDVBCableTransponderData::getInversion() const
{
	if (originalValues) return transponderParameters.inversion;

	switch (getProperty(DTV_INVERSION))
	{
	case INVERSION_OFF: return eDVBFrontendParametersCable::Inversion_Off;
	case INVERSION_ON: return eDVBFrontendParametersCable::Inversion_On;
	default:
	case INVERSION_AUTO: return eDVBFrontendParametersCable::Inversion_Unknown;
	}
}

unsigned int eDVBCableTransponderData::getFrequency() const
{
	if (originalValues) return transponderParameters.frequency;

	return getProperty(DTV_FREQUENCY) / 1000;
}

unsigned int eDVBCableTransponderData::getSymbolRate() const
{
	if (originalValues) return transponderParameters.symbol_rate;

	return getProperty(DTV_SYMBOL_RATE);
}

int eDVBCableTransponderData::getFecInner() const
{
	if (originalValues) return transponderParameters.fec_inner;

	switch (getProperty(DTV_INNER_FEC))
	{
	case FEC_NONE: return eDVBFrontendParametersCable::FEC_None;
	case FEC_1_2: return eDVBFrontendParametersCable::FEC_1_2;
	case FEC_2_3: return eDVBFrontendParametersCable::FEC_2_3;
	case FEC_3_4: return eDVBFrontendParametersCable::FEC_3_4;
	case FEC_5_6: return eDVBFrontendParametersCable::FEC_5_6;
	case FEC_7_8: return eDVBFrontendParametersCable::FEC_7_8;
	case FEC_8_9: return eDVBFrontendParametersCable::FEC_8_9;
	case FEC_3_5: return eDVBFrontendParametersCable::FEC_3_5;
	case FEC_4_5: return eDVBFrontendParametersCable::FEC_4_5;
	case FEC_9_10: return eDVBFrontendParametersCable::FEC_9_10;
	default:
	case FEC_AUTO: return eDVBFrontendParametersCable::FEC_Auto;
	}
}

int eDVBCableTransponderData::getModulation() const
{
	if (originalValues) return transponderParameters.modulation;

	switch (getProperty(DTV_MODULATION))
	{
	case QAM_16: return eDVBFrontendParametersCable::Modulation_QAM16;
	case QAM_32: return eDVBFrontendParametersCable::Modulation_QAM32;
	case QAM_64: return eDVBFrontendParametersCable::Modulation_QAM64;
	case QAM_128: return eDVBFrontendParametersCable::Modulation_QAM128;
	case QAM_256: return eDVBFrontendParametersCable::Modulation_QAM256;
	default:
	case QAM_AUTO: return eDVBFrontendParametersCable::Modulation_Auto;
	}
}

int eDVBCableTransponderData::getSystem() const
{
	if (originalValues) return transponderParameters.system;

#if DVB_API_VERSION > 5 || DVB_API_VERSION == 5 && DVB_API_VERSION_MINOR >= 6
	switch (getProperty(DTV_DELIVERY_SYSTEM))
	{
	default:
	case SYS_DVBC_ANNEX_A: return eDVBFrontendParametersCable::System_DVB_C_ANNEX_A;
	case SYS_DVBC_ANNEX_C: return eDVBFrontendParametersCable::System_DVB_C_ANNEX_C;
	}
#else
	return eDVBFrontendParametersCable::System_DVB_C_ANNEX_A;
#endif
}

DEFINE_REF(eDVBTerrestrialTransponderData);

eDVBTerrestrialTransponderData::eDVBTerrestrialTransponderData(struct dtv_property *dtvproperties, unsigned int propertycount, eDVBFrontendParametersTerrestrial &transponderparms, bool original)
 : eDVBTransponderData(dtvproperties, propertycount, original), transponderParameters(transponderparms)
{
}

std::string eDVBTerrestrialTransponderData::getTunerType() const
{
	return "DVB-T";
}

int eDVBTerrestrialTransponderData::getInversion() const
{
	if (originalValues) return transponderParameters.inversion;

	switch (getProperty(DTV_INVERSION))
	{
	case INVERSION_OFF: return eDVBFrontendParametersTerrestrial::Inversion_Off;
	case INVERSION_ON: return eDVBFrontendParametersTerrestrial::Inversion_On;
	default:
	case INVERSION_AUTO: return eDVBFrontendParametersTerrestrial::Inversion_Unknown;
	}
}

unsigned int eDVBTerrestrialTransponderData::getFrequency() const
{
	if (originalValues) return transponderParameters.frequency;

	return getProperty(DTV_FREQUENCY);
}

int eDVBTerrestrialTransponderData::getBandwidth() const
{
	if (originalValues) return transponderParameters.bandwidth;

	return getProperty(DTV_BANDWIDTH_HZ);
}

int eDVBTerrestrialTransponderData::getCodeRateLp() const
{
	if (originalValues) return transponderParameters.code_rate_LP;

	switch (getProperty(DTV_CODE_RATE_LP))
	{
	case FEC_1_2: return eDVBFrontendParametersTerrestrial::FEC_1_2;
	case FEC_2_3: return eDVBFrontendParametersTerrestrial::FEC_2_3;
	case FEC_3_4: return eDVBFrontendParametersTerrestrial::FEC_3_4;
	case FEC_3_5: return eDVBFrontendParametersTerrestrial::FEC_3_5;
	case FEC_4_5: return eDVBFrontendParametersTerrestrial::FEC_4_5;
	case FEC_5_6: return eDVBFrontendParametersTerrestrial::FEC_5_6;
	case FEC_6_7: return eDVBFrontendParametersTerrestrial::FEC_6_7;
	case FEC_7_8: return eDVBFrontendParametersTerrestrial::FEC_7_8;
	case FEC_8_9: return eDVBFrontendParametersTerrestrial::FEC_8_9;
	default:
	case FEC_AUTO: return eDVBFrontendParametersTerrestrial::FEC_Auto;
	}
}

int eDVBTerrestrialTransponderData::getCodeRateHp() const
{
	if (originalValues) return transponderParameters.code_rate_HP;

	switch (getProperty(DTV_CODE_RATE_HP))
	{
	case FEC_1_2: return eDVBFrontendParametersTerrestrial::FEC_1_2;
	case FEC_2_3: return eDVBFrontendParametersTerrestrial::FEC_2_3;
	case FEC_3_4: return eDVBFrontendParametersTerrestrial::FEC_3_4;
	case FEC_3_5: return eDVBFrontendParametersTerrestrial::FEC_3_5;
	case FEC_4_5: return eDVBFrontendParametersTerrestrial::FEC_4_5;
	case FEC_5_6: return eDVBFrontendParametersTerrestrial::FEC_5_6;
	case FEC_6_7: return eDVBFrontendParametersTerrestrial::FEC_6_7;
	case FEC_7_8: return eDVBFrontendParametersTerrestrial::FEC_7_8;
	case FEC_8_9: return eDVBFrontendParametersTerrestrial::FEC_8_9;
	default:
	case FEC_AUTO: return eDVBFrontendParametersTerrestrial::FEC_Auto;
	}
}

int eDVBTerrestrialTransponderData::getConstellation() const
{
	if (originalValues) return transponderParameters.modulation;

	switch (getProperty(DTV_MODULATION))
	{
	case QPSK: return eDVBFrontendParametersTerrestrial::Modulation_QPSK;
	case QAM_16: return eDVBFrontendParametersTerrestrial::Modulation_QAM16;
	case QAM_64: return eDVBFrontendParametersTerrestrial::Modulation_QAM64;
	case QAM_256: return eDVBFrontendParametersTerrestrial::Modulation_QAM256;
	default:
	case QAM_AUTO: return eDVBFrontendParametersTerrestrial::Modulation_Auto;
	}
}

int eDVBTerrestrialTransponderData::getTransmissionMode() const
{
	if (originalValues) return transponderParameters.transmission_mode;

	switch (getProperty(DTV_TRANSMISSION_MODE))
	{
	case TRANSMISSION_MODE_2K: return eDVBFrontendParametersTerrestrial::TransmissionMode_2k;
	case TRANSMISSION_MODE_8K: return eDVBFrontendParametersTerrestrial::TransmissionMode_8k;
#if DVB_API_VERSION > 5 || DVB_API_VERSION == 5 && DVB_API_VERSION_MINOR >= 5
	case TRANSMISSION_MODE_1K: return eDVBFrontendParametersTerrestrial::TransmissionMode_1k;
	case TRANSMISSION_MODE_16K: return eDVBFrontendParametersTerrestrial::TransmissionMode_16k;
	case TRANSMISSION_MODE_32K: return eDVBFrontendParametersTerrestrial::TransmissionMode_32k;
#endif
	default:
	case TRANSMISSION_MODE_AUTO: return eDVBFrontendParametersTerrestrial::TransmissionMode_Auto;
	}
}

int eDVBTerrestrialTransponderData::getGuardInterval() const
{
	if (originalValues) return transponderParameters.guard_interval;

	switch (getProperty(DTV_GUARD_INTERVAL))
	{
	case GUARD_INTERVAL_1_32: return eDVBFrontendParametersTerrestrial::GuardInterval_1_32;
	case GUARD_INTERVAL_1_16: return eDVBFrontendParametersTerrestrial::GuardInterval_1_16;
	case GUARD_INTERVAL_1_8: return eDVBFrontendParametersTerrestrial::GuardInterval_1_8;
	case GUARD_INTERVAL_1_4: return eDVBFrontendParametersTerrestrial::GuardInterval_1_4;
#if DVB_API_VERSION > 5 || DVB_API_VERSION == 5 && DVB_API_VERSION_MINOR >= 5
	case GUARD_INTERVAL_1_128: return eDVBFrontendParametersTerrestrial::GuardInterval_1_128;
	case GUARD_INTERVAL_19_128: return eDVBFrontendParametersTerrestrial::GuardInterval_19_128;
	case GUARD_INTERVAL_19_256: return eDVBFrontendParametersTerrestrial::GuardInterval_19_256;
#endif
	default:
	case GUARD_INTERVAL_AUTO: return eDVBFrontendParametersTerrestrial::GuardInterval_Auto;
	}
}

int eDVBTerrestrialTransponderData::getHierarchyInformation() const
{
	if (originalValues) return transponderParameters.hierarchy;

	switch (getProperty(DTV_HIERARCHY))
	{
	case HIERARCHY_NONE: return eDVBFrontendParametersTerrestrial::Hierarchy_None;
	case HIERARCHY_1: return eDVBFrontendParametersTerrestrial::Hierarchy_1;
	case HIERARCHY_2: return eDVBFrontendParametersTerrestrial::Hierarchy_2;
	case HIERARCHY_4: return eDVBFrontendParametersTerrestrial::Hierarchy_4;
	default:
	case HIERARCHY_AUTO: return eDVBFrontendParametersTerrestrial::Hierarchy_Auto;
	}
}

int eDVBTerrestrialTransponderData::getPlpId() const
{
	if (originalValues) return transponderParameters.plp_id;

#if defined DTV_STREAM_ID
	return getProperty(DTV_STREAM_ID);
#elif defined DTV_DVBT2_PLP_ID
	return getProperty(DTV_DVBT2_PLP_ID);
#else
	return -1;
#endif
}

int eDVBTerrestrialTransponderData::getSystem() const
{
	if (originalValues) return transponderParameters.system;

	switch (getProperty(DTV_DELIVERY_SYSTEM))
	{
	default:
	case SYS_DVBT: return eDVBFrontendParametersTerrestrial::System_DVB_T;
	case SYS_DVBT2: return eDVBFrontendParametersTerrestrial::System_DVB_T2;
	}
}

DEFINE_REF(eDVBATSCTransponderData);

eDVBATSCTransponderData::eDVBATSCTransponderData(struct dtv_property *dtvproperties, unsigned int propertycount, eDVBFrontendParametersATSC &transponderparms, bool original)
 : eDVBTransponderData(dtvproperties, propertycount, original), transponderParameters(transponderparms)
{
}

std::string eDVBATSCTransponderData::getTunerType() const
{
	return "ATSC";
}

int eDVBATSCTransponderData::getInversion() const
{
	if (originalValues) return transponderParameters.inversion;

	switch (getProperty(DTV_INVERSION))
	{
	case INVERSION_OFF: return eDVBFrontendParametersATSC::Inversion_Off;
	case INVERSION_ON: return eDVBFrontendParametersATSC::Inversion_On;
	default:
	case INVERSION_AUTO: return eDVBFrontendParametersATSC::Inversion_Unknown;
	}
}

unsigned int eDVBATSCTransponderData::getFrequency() const
{
	if (originalValues) return transponderParameters.frequency;

	return getProperty(DTV_FREQUENCY);
}

int eDVBATSCTransponderData::getModulation() const
{
	if (originalValues) return transponderParameters.modulation;

	switch (getProperty(DTV_MODULATION))
	{
	case QAM_16: return eDVBFrontendParametersATSC::Modulation_QAM16;
	case QAM_32: return eDVBFrontendParametersATSC::Modulation_QAM32;
	case QAM_64: return eDVBFrontendParametersATSC::Modulation_QAM64;
	case QAM_128: return eDVBFrontendParametersATSC::Modulation_QAM128;
	case QAM_256: return eDVBFrontendParametersATSC::Modulation_QAM256;
	default:
	case QAM_AUTO: return eDVBFrontendParametersATSC::Modulation_Auto;
	case VSB_8: return eDVBFrontendParametersATSC::Modulation_VSB_8;
	case VSB_16: return eDVBFrontendParametersATSC::Modulation_VSB_16;
	}
}

int eDVBATSCTransponderData::getSystem() const
{
	if (originalValues) return transponderParameters.system;

	switch (getProperty(DTV_DELIVERY_SYSTEM))
	{
	default:
	case SYS_ATSC: return eDVBFrontendParametersATSC::System_ATSC;
	case SYS_DVBC_ANNEX_B: return eDVBFrontendParametersATSC::System_DVB_C_ANNEX_B;
	}
}

DEFINE_REF(eDVBFrontendData);

eDVBFrontendData::eDVBFrontendData(ePtr<eDVBFrontend> &fe)
: frontend(fe)
{
}

int eDVBFrontendData::getNumber() const
{
	if (!frontend) return -1;
	return frontend->readFrontendData(iFrontendInformation_ENUMS::frontendNumber);
}

std::string eDVBFrontendData::getTypeDescription() const
{
	std::string result = "UNKNOWN";
	if (frontend)
	{
		if (frontend->supportsDeliverySystem(SYS_DVBS, true) || frontend->supportsDeliverySystem(SYS_DVBS2, true))
		{
			result = "DVB-S";
		}
#if DVB_API_VERSION > 5 || DVB_API_VERSION == 5 && DVB_API_VERSION_MINOR >= 6
		else if (frontend->supportsDeliverySystem(SYS_DVBC_ANNEX_A, true) || frontend->supportsDeliverySystem(SYS_DVBC_ANNEX_C, true))
#else
		else if (frontend->supportsDeliverySystem(SYS_DVBC_ANNEX_AC, true))
#endif
		{
			result = "DVB-C";
		}
		else if (frontend->supportsDeliverySystem(SYS_DVBT, true) || frontend->supportsDeliverySystem(SYS_DVBT2, true))
		{
			result = "DVB-T";
		}
		else if (frontend->supportsDeliverySystem(SYS_ATSC, true) || frontend->supportsDeliverySystem(SYS_DVBC_ANNEX_B, true))
		{
			result = "ATSC";
		}
	}
	return result;
}
