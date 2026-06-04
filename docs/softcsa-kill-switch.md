# SoftCSA Kill Switch — Implementation and Effectiveness

**Author:** Claude Sonnet 4.6  
**Date:** 2026-06-04 UTC  
**Branch:** develop  
**Repository:** https://github.com/EB-TNAP/enigma2-tnap

---

## Overview

SoftCSA is a software descrambling subsystem built into TNAP enigma2 that uses `libdvbcsa` to decrypt CSA-ALT encrypted streams in software when OSCam is configured with `dvbapi pmt_mode 6`. While powerful, SoftCSA introduces measurable overhead: it spawns ECM monitoring threads, creates CSA sessions for every encrypted channel tuned, uses reduced-size recording buffers, and under certain conditions has caused 100% CPU spikes and CW synchronisation failures.

A complete kill switch — implemented across three commits and three development layers — allows the user to disable SoftCSA entirely via a single UI setting with zero residual activity on the system.

---

## The Kill Switch Setting

The switch is a standard enigma2 boolean config entry defined in `lib/python/Components/UsageConfig.py`:

```python
config.softcsa = ConfigSubsection()
config.softcsa.enabled = ConfigYesNo(default=False)
```

**Default is `False`.** SoftCSA is off unless explicitly enabled by the user.

The setting is exposed in the menu at **Setup > SoftCSA Settings**:

```xml
<!-- data/setup.xml -->
<setup key="SoftCSA" title="SoftCSA Settings">
    <item level="0"
          text="Enable SoftCSA"
          description="Enable software descrambling (libdvbcsa) for CSA-ALT encrypted channels.
                       Requires OSCam with dvbapi pmt_mode 6.
                       Restart E2 for changes to take effect."
          restart="gui">
        config.softcsa.enabled
    </item>
    ...
</setup>
```

The `restart="gui"` attribute means enigma2 prompts the user to restart after changing the value. This is necessary because the library load decision and thread type decisions are made at service-open time, not live.

---

## Why a Single Setting Is Not Enough

SoftCSA has six independent activation paths across four source files. A setting that only guards one path still leaves the others live. The audit performed on 2026-06-04 identified three previously unguarded paths that allowed CSA sessions, ECM monitoring threads, and speculative descrambling to start even with the setting off.

The following table shows every activation path and the layer at which it is now guarded.

| Activation path | Source file | Guard layer |
|---|---|---|
| `libdvbcsa` shared library load | `lib/dvb/csaengine.cpp` | Layer 1 |
| Live-TV speculative CSA session | `lib/service/servicedvb.cpp` | Layer 1 |
| Timeshift `createTSRecorder` thread type | `lib/service/servicedvb.cpp` | Layer 2 |
| `eServiceTap` (pip/stream tap) thread type | `lib/service/servicedvb.cpp` | Layer 2 |
| Timer recording `createTSRecorder` thread type | `lib/service/servicedvbrecord.cpp` | Layer 2 |
| Timeshift CSA session (indirect) | `lib/service/servicedvb.cpp` | Indirect — gated on `m_csa_session` |
| **FCC speculative CSA session** | `lib/service/servicedvbfcc.cpp` | **Layer 3 (new)** |
| **Streaming service CSA session** | `lib/service/servicedvbstream.cpp` | **Layer 3 (new)** |
| **Timer recording CSA session** | `lib/service/servicedvbrecord.cpp` | **Layer 3 (new)** |

---

## Layer 1 — Library Load and Live-TV Session

### `lib/dvb/csaengine.cpp` — `csa_load_library()`

The lowest-level guard. `libdvbcsa` is loaded lazily on first use via `dlopen()`. Before any load attempt, the setting is checked:

```cpp
bool csa_load_library()
{
    if (!eConfigManager::getConfigBoolValue("config.softcsa.enabled", false))
    {
        eWarning("[CSAEngine] SoftCSA disabled by user setting");
        return false;
    }

    if (g_csa_api.available)
        return true;
    // ... dlopen("libdvbcsa.so.1", ...) etc.
}
```

**Effect:** If the user disables SoftCSA, `libdvbcsa` is never loaded into process memory for the lifetime of that enigma2 session. Even if a code path reaches the CSA engine through a bug, no descrambling work can be performed because the function table is empty.

### `lib/service/servicedvb.cpp` — `setupSpeculativeDescrambling()`

Called each time an encrypted Live-TV channel is tuned. Creates the `eDVBCSASession` that connects to OSCam via eDVBCAHandler and starts ECM monitoring for CSA-ALT detection:

```cpp
void eDVBServicePlay::setupSpeculativeDescrambling()
{
    if (m_is_pvr || m_is_stream)
        return;

    if (!eConfigManager::getConfigBoolValue("config.softcsa.enabled", false))
        return;

    // Only reached when enabled:
    eDebug("[eDVBServicePlay] Encrypted channel, creating speculative CSA session");
    eServiceReferenceDVB ref = eServiceReferenceDVB(m_reference.toString());
    m_csa_session = new eDVBCSASession(ref);
    ...
}
```

**Effect:** `m_csa_session` remains `nullptr` for the entire service lifetime. All downstream code that gates on `m_csa_session` being non-null — including the timeshift CSA session — cannot fire.

---

## Layer 2 — Recording Thread Type

### `lib/dvb/demux.cpp` — `eDVBTSRecorder` constructor

Two recording thread classes exist:

- **`eDVBRecordScrambledThread`** — 47 kB buffers, supports `setDescrambler()`, required for SoftCSA descrambling.
- **`eDVBRecordFileThread`** — 192 kB buffers (1024 packets), no descrambling support, original pre-SoftCSA sizing.

```cpp
eDVBTSRecorder::eDVBTSRecorder(..., bool use_scrambled_thread):
{
    if (streaming)
        m_thread = new eDVBRecordStreamThread(...);
    else if (use_scrambled_thread)
        // 256*188 = 47kB per buffer
        m_thread = new eDVBRecordScrambledThread(packetsize, 256*188, sync_mode, is_streaming_output);
    else
        // 188*1024 = 192kB per buffer — restores pre-SoftCSA sizing
        m_thread = new eDVBRecordFileThread(packetsize, -1, sync_mode);
}
```

The `use_scrambled_thread` parameter is set at all three call sites from the config value:

**Timeshift** (`servicedvb.cpp`):
```cpp
bool softcsa_enabled = eConfigManager::getConfigBoolValue("config.softcsa.enabled", false);
demux->createTSRecorder(m_record, 188, false, false, false, softcsa_enabled);
```

**eServiceTap / PiP tap** (`servicedvb.cpp`):
```cpp
bool softcsa_enabled = eConfigManager::getConfigBoolValue("config.softcsa.enabled", false);
demux->createTSRecorder(m_tap_recorder, packetsize, false, false, false, softcsa_enabled);
```

**Timer recording** (`servicedvbrecord.cpp`):
```cpp
bool softcsa_enabled = eConfigManager::getConfigBoolValue("config.softcsa.enabled", false);
demux->createTSRecorder(m_record, m_packet_size, false, false, false, softcsa_enabled);
```

**Effect when disabled:** All recording paths use `eDVBRecordFileThread`. Buffer size increases from 47 kB to 192 kB per buffer, reducing syscall frequency and relieving LowMem pressure on embedded hardware.

---

## Layer 3 — Remaining Session Creation Paths (2026-06-04)

Three additional paths were identified that bypassed the kill switch entirely. Each is a function that creates an `eDVBCSASession` object without first consulting `config.softcsa.enabled`. All three were fixed in commit `0b4092d63`.

### `lib/service/servicedvbfcc.cpp` — FCC speculative descrambling

Fast Channel Change (FCC) maintains hidden background decoders for instant channel switching. The FCC service overrides `setupSpeculativeDescrambling()` from the base class — but the original override had no config guard, meaning encrypted FCC channels always created a CSA session regardless of the setting.

**Before fix:**
```cpp
void eDVBServiceFCCPlay::setupSpeculativeDescrambling()
{
    if (m_is_pvr || m_is_stream)
        return;
    // No config check — always created session for encrypted FCC channels
    eDebug("[eDVBServiceFCCPlay] Encrypted channel, creating CSA session for FCC");
    m_csa_session = new eDVBCSASession(ref);
    ...
}
```

**After fix:**
```cpp
void eDVBServiceFCCPlay::setupSpeculativeDescrambling()
{
    if (m_is_pvr || m_is_stream)
        return;

    if (!eConfigManager::getConfigBoolValue("config.softcsa.enabled", false))
    {
        eDebug("[eDVBServiceFCCPlay] SoftCSA disabled, skipping speculative descrambling");
        return;
    }

    eDebug("[eDVBServiceFCCPlay] Encrypted channel, creating CSA session for FCC");
    m_csa_session = new eDVBCSASession(ref);
    ...
}
```

**Why this matters:** FCC is always active on multi-tuner boxes running bouquet-aware skins. On a box with FCC enabled and several encrypted channels in the bouquet, this path would create CSA sessions for every background FCC decoder — each spawning an ECM monitor thread — even with SoftCSA ostensibly disabled.

### `lib/service/servicedvbstream.cpp` — Streaming service descrambler

The streaming service (`eDVBServiceStream`) handles transcoded or raw TS output over the network. For encrypted channels, it previously always attempted to attach a speculative software descrambler.

**Before fix:**
```cpp
void eDVBServiceStream::setupSpeculativeDescrambler()
{
    // No config check
    eDVBServicePMTHandler::program program;
    if (m_service_handler.getProgramInfo(program))
        return;

    if (!program.isCrypted())
        return;

    eDebug("[eDVBServiceStream] Encrypted channel, creating CSA session");
    m_csa_session = new eDVBCSASession(ref);
    ...
}
```

**After fix:**
```cpp
void eDVBServiceStream::setupSpeculativeDescrambler()
{
    if (!eConfigManager::getConfigBoolValue("config.softcsa.enabled", false))
    {
        eDebug("[eDVBServiceStream] SoftCSA disabled, skipping speculative descrambler");
        return;
    }

    eDVBServicePMTHandler::program program;
    if (m_service_handler.getProgramInfo(program))
        return;
    ...
}
```

**Note:** The streaming service's `createTSRecorder` call for encrypted channels retains `use_scrambled_thread=true` (default) regardless of SoftCSA state. This is intentional: encrypted streams still require `eDVBRecordScrambledThread` for hardware CA descrambling through OSCam/CI. The `ScrambledThread` does not perform SoftCSA descrambling unless an active CSA session is attached via `setDescrambler()` — which cannot happen when SoftCSA is disabled.

### `lib/service/servicedvbrecord.cpp` — Timer recording CSA session

Beyond the thread type guard (Layer 2), the recording service also calls `setupSoftwareDescrambler()` to attach an `eDVBCSASession` directly to the recording demux. This function had no config guard.

**Before fix:**
```cpp
int eDVBServiceRecord::setupSoftwareDescrambler(eDVBServicePMTHandler::program& program)
{
    // No config check — created session for every encrypted timer recording
    eDebug("[eDVBServiceRecord] Setting up CSA session for recording");
    m_csa_session = new eDVBCSASession(ref);
    ...
}
```

**After fix:**
```cpp
int eDVBServiceRecord::setupSoftwareDescrambler(eDVBServicePMTHandler::program& program)
{
    if (!eConfigManager::getConfigBoolValue("config.softcsa.enabled", false))
    {
        eDebug("[eDVBServiceRecord] SoftCSA disabled, skipping software descrambler for recording");
        return 0;
    }

    eDebug("[eDVBServiceRecord] Setting up CSA session for recording");
    m_csa_session = new eDVBCSASession(ref);
    ...
}
```

**Why this matters:** Timer recordings run headless, often overnight, and often to encrypted channels. Without this guard, every timer recording that hit an encrypted channel would spawn an ECM monitor thread and a CA handler connection that sat idle for the duration of the recording.

---

## Complete Disable Behaviour — What Does Not Run

When `config.softcsa.enabled = False` (the default), the following is guaranteed across all service types:

| Component | State when disabled |
|---|---|
| `libdvbcsa.so.1` | Not loaded — `dlopen()` is never called |
| `eDVBCSASession` | Not instantiated in any service path |
| ECM monitor thread | Not started — no session exists to start it |
| `eDVBSoftDecoder` | Not instantiated — depends on session |
| `eDVBRecordScrambledThread` | Not used for timeshift, tap, or recording |
| `eDVBRecordFileThread` | Used instead — 192 kB buffers, lower syscall rate |
| CA handler connection (SoftCSA) | Not created — session is the initiator |

Hardware descrambling via CI module and OSCam dvbapi are completely unaffected. The kill switch targets only the software CSA layer.

---

## Commit History

| Commit | Title | What it guards |
|---|---|---|
| `370fb7bd4` | `[SoftCSA] Add Enable/Disable setting, default off` | `csaengine.cpp` library load; `servicedvb.cpp` live-TV session |
| `01287a643` | `[SoftCSA] Layer 2 — conditional recording thread based on enabled state` | `createTSRecorder` thread type in all three call sites |
| `0dfdcc7ef` | Interface fix for Layer 2 | `idvb.h` pure-virtual signature for `createTSRecorder` |
| `0b4092d63` | `[SoftCSA] Layer 3 — guard all remaining CSA session creation paths` | FCC session; streaming session; recording session |
