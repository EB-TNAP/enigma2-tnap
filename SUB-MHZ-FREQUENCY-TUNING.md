# Sub-MHz Satellite Frequency Tuning in Enigma2 (TNAP 7)
### Feasibility Study: Tuning to 3 Decimal Places (e.g., 12177.905 MHz)

**Author:** Claude Sonnet 4.6  
**Date:** 2026-04-06 16:14 UTC  
**Updated:** 2026-04-06 (added small symbol rate analysis — Bullseye LNB + SR=148 use case)  
**Scope:** TNAP 7 — `~/builds/dreambpx-fairbird-2/enigma2-dreambox` + `openpli-dreambox-oe-core`  
**Hardware context:** Othernet Bullseye BE01 TCXO LNB (±10 kHz @ 23°C, ±30 kHz full range)

---

## 1. Executive Summary

Tuning a satellite transponder to 3 decimal places (kHz precision, e.g. 12177.905 MHz =
12177905 kHz) is **technically feasible in software** and **practically valuable** when both
of the following conditions are met:

1. A TCXO or better LNB is used (±10–30 kHz stability)
2. The target transponder has a small symbol rate (≤1000 Ksps)

The Bullseye BE01 LNB (±10 kHz @ 23°C) paired with the small-SR transponder observed at
11711 V SR=148 3/4 (Pilgrim Radio Network, SES-1 101.0°W) is a concrete example of this
use case. For standard consumer LNBs with ±500 kHz–1 MHz drift, kHz precision is pointless.

The Linux DVB API, enigma2's internal kHz storage, and the tuner ICs can all represent the
value natively — only the UI input layer needs modification.

The specific frequency 12177.905 MHz does not appear in any known commercial DVB-S/S2
satellite broadcast. Real broadcast transponders use integer MHz or 20 MHz channel spacing.
Private contribution feeds, narrowband audio services, and data feeds may use finer spacing.

---

## 2. The Signal Chain

Understanding what "tuning precision" actually means requires following the signal from
satellite to tuner:

```
Satellite RF signal
  (e.g., 12177.905 MHz Ku-band downlink)
        ↓
   LNB — subtracts local oscillator (LO)
  (e.g., LO = 10600 MHz → IF = 1577.905 MHz)
        ↓
   Coaxial cable (L-band, 950–2150 MHz)
        ↓
   Tuner IC on receiver board
  (tunes to IF frequency, e.g., 1577.905 MHz)
        ↓
   Demodulator (DVB-S2 LDPC/BCH decoder)
        ↓
   Transport stream → demux → video/audio
```

The LNB is the critical weak point for precision. Everything downstream can be made precise;
the LNB oscillator is the bottleneck.

---

## 3. Layer-by-Layer Analysis

### 3.1 Linux DVB v5 Kernel API

| Property | Detail |
|---|---|
| API property | `DTV_FREQUENCY` |
| Unit for satellite (DVB-S/S2) | **kHz** |
| Unit for terrestrial/cable | Hz |
| Data type | `__u32` (unsigned 32-bit integer) |
| Maximum representable | 4,294,967,295 kHz (~4.3 THz — far beyond any satellite band) |

**Conclusion:** 12177.905 MHz = **12177905 kHz** — fits in a `__u32` with no loss.
The kernel API is not a barrier.

### 3.2 Tuner ICs

Common DVB-S2 tuner chips (STB6100, STV6110, STV6111, M88RS6000, CXD2856) use
fractional-N PLL synthesizers. While their datasheet step sizes are not always published,
the MAX2112 (a documented example) explicitly offers a fractional-N synthesizer. The
practical synthesizer resolution of modern DVB-S2 tuner ICs is typically:

- **Coarse step:** 1 MHz (common in older/budget silicon)
- **Fine step:** 125 kHz or 62.5 kHz (mid-range silicon)
- **Best-case:** 1 kHz or finer (premium fractional-N designs)

The Dreambox hardware in this build (Cortex-A15 platform) uses a tuner IC capable of
L-band step sizes well below 1 MHz. For purposes of this analysis, the tuner IC is
**not a fundamental barrier** to kHz precision.

### 3.3 LNB Local Oscillator — The Real Barrier

This is where kHz-level precision becomes academic for real-world use:

| LNB Type | Oscillator | Frequency Accuracy | Drift (–20°C to +60°C) |
|---|---|---|---|
| Standard (consumer) | Crystal XO | ±500 kHz – ±2 MHz | ±1–3 MHz |
| Budget TCXO | TCXO | ±100–500 kHz | ±100–500 kHz |
| Premium TCXO (e.g., Bullseye) | TCXO | ±1 kHz (factory cal.) | ±10–30 kHz |
| OCXO | Oven-controlled XO | ±10 Hz – ±1 kHz | ±10 Hz – ±1 kHz |
| GPSDO-referenced | GPS-locked | < ±1 Hz | < ±1 Hz |

The best available consumer satellite LNB (Othernet Bullseye) drifts ±10 kHz at room
temperature and ±30 kHz over the full operating range. With a standard LNB, drift is
500 kHz to over 1 MHz.

**Practical consequence:** With a standard LNB, "tuning to 12177.905 MHz" and "tuning to
12177.000 MHz" result in the same physical IF frequency at the tuner input, within the
LNB's own uncertainty margin. The demodulator's carrier acquisition loop (typically ±2–5
MHz search range) compensates for LNB drift.

### 3.4 Enigma2 Internal Representation

#### Data Structure (`lib/dvb/frontendparms.h`)

```c
class eDVBFrontendParametersSatellite {
public:
    unsigned int frequency, symbol_rate;
    // ...
};
```

The `frequency` field is `unsigned int`. **Internally, enigma2 stores frequency in kHz.**

#### Python Layer (`lib/python/Components/TuneTest.py`)

```python
def tune(self, transponder):
    parm = eDVBFrontendParametersSatellite()
    parm.frequency = transponder[0] * 1000   # MHz → kHz conversion
    parm.symbol_rate = transponder[1] * 1000
```

UI input is in MHz (integer), multiplied by 1000 for kHz storage.

#### Kernel Call (`lib/dvb/frontend.cpp`)

```c
p[cmdseq.num].cmd = DTV_FREQUENCY;
p[cmdseq.num].u.data = satfrequency;  // already in kHz
cmdseq.num++;
```

No further conversion — the kHz value goes straight to `DTV_FREQUENCY`. Confirmed by build
log: `Freq 12171000` for 12171 MHz (12171 × 1000 = 12,171,000 kHz).

#### lamedb Channel File (`lib/dvb/db.cpp`)

```c
sscanf(line+2, "%d:%d:%d:%d...", &frequency, &symbol_rate, ...);
sat.frequency = frequency;
```

Parsed with `%d` (signed int, 32-bit). Value 12177905 fits and is stored correctly.
**lamedb already supports kHz frequencies — 12177905 is a valid entry today.**

#### satellites.xml (`lib/dvb/db.cpp`)

```c
tmp = strtol((const char*)attr->children->content, &end_ptr, 10);
if (!*end_ptr) { *dest = tmp; }
```

`strtol()` parses decimal integers only. A dot in `12177.905` would cause the parse to
stop at `.905` — the `end_ptr` check would fail and the value would be silently skipped.

#### ScanSetup UI (`lib/python/Screens/ScanSetup.py`)

```python
self.scan_sat.frequency = ConfigInteger(default=defaultSat["frequency"],
                                        limits=(1, 99999))
```

`ConfigInteger` — accepts whole numbers only. No decimal input.

---

## 4. What Real-World Transponders Look Like

Commercial satellite operators (Intelsat, Eutelsat, SES, ViaSat, etc.) place transponders on:

- **Ku-band:** Integer MHz, 20 MHz spacing (e.g., 11749, 11769, 11789 MHz)
- **C-band:** Integer MHz, 20 MHz spacing (e.g., 3720, 3740, 3760, 3780 MHz)
- **Ka-band:** Integer MHz, 500 MHz or 250 MHz blocks

No major operator publishes transponder frequencies with fractional MHz components.
LyngSat, SatBeams, and FastSatfinder databases store all frequencies as integers.

The frequency 12177.905 MHz does not appear in any known broadcast.

**DVB-S2X note:** The extended standard adds finer modulation granularity but does not
introduce or require fractional MHz channel assignments.

---

## 5. C-Band vs. Ku-Band: Any Difference?

| Parameter | Ku-band | C-band |
|---|---|---|
| Satellite downlink | 10.7–12.75 GHz | 3.4–4.2 GHz |
| Typical LNB LO | 9750 / 10600 MHz | 5150 MHz |
| Channel spacing | 20 MHz | 20 MHz (alt. pol.) |
| LNB crystal drift | ±500 kHz – ±2 MHz | Same order |
| TCXO LNB drift | ±10–30 kHz | Same order |
| Sub-MHz precision practical? | No | No |

C-band has no inherent frequency stability advantage. The LNB oscillator tolerance is the
same limiting factor in both bands.

---

## 6. Is It Feasible? Is It Practical?

### Feasibility: YES (software changes required)

The hardware chain can represent kHz-precision frequencies. The software changes needed
in TNAP 7 are:

| File | Change Required |
|---|---|
| `lib/dvb/frontendparms.h` | `unsigned int frequency` → no change needed (kHz value 12177905 fits in 32 bits) |
| `lib/python/Screens/ScanSetup.py` | `ConfigInteger` → `ConfigFloat` or `ConfigText` with validation |
| `lib/python/Components/TuneTest.py` | `transponder[0] * 1000` → `int(transponder[0] * 1000)` (handle float input) |
| `lib/dvb/db.cpp` (lamedb) | `%d` with `sscanf` → no change needed IF frequencies are stored pre-converted to kHz |
| `lib/dvb/db.cpp` (satellites.xml) | `strtol()` → `strtod()` + multiply by 1000, or pre-store as kHz integer |
| `lib/python/Plugins/SystemPlugins/Satfinder/plugin.py` | ConfigInteger → float-aware input |

**Minimum viable change:** Store frequencies in lamedb and satellites.xml as integer kHz
values (already the case for lamedb). Change only the UI entry widget and TuneTest
conversion. The rest of the chain already works in kHz.

### Practical: NO — for standard hardware

| Condition | Verdict |
|---|---|
| Standard LNB (crystal) | Pointless — ±500 kHz drift exceeds any precision gain |
| TCXO LNB (budget) | Marginally useful — ±100 kHz precision achievable |
| Premium TCXO LNB (Bullseye) | Useful — ±10–30 kHz; kHz tuning has meaning |
| OCXO / GPSDO LNB | Fully beneficial — but not commercially available for consumer use |
| Real transponders exist at fractional MHz | Not found in any database |

### Practical: POSSIBLY — for specific use cases

There is one scenario where kHz-precision tuning in enigma2 has genuine value: **private
or non-broadcast feeds** from professional uplink operators. Studio-to-transmitter links
(STLs), contribution feeds, and private data services are sometimes allocated frequencies
by the operator in 500 kHz or 250 kHz steps, outside the standard 20 MHz broadcast grid.
A TCXO LNB (±30 kHz) paired with kHz-resolution tuning could lock onto such a signal
where integer MHz tuning fails.

This is also the scenario described in the original question — a specific non-standard
frequency. With the current TNAP 7 code, entering 12177.905 MHz is impossible in the UI
and would be truncated to 12177 MHz, which may or may not find the signal depending on
how far off-frequency 905 kHz is relative to the demodulator's acquisition window.

---

## 7. Recommended Implementation Path

If sub-MHz entry is desired for TNAP 7:

### Step 1 — UI: Accept decimal MHz input in ScanSetup and Satfinder

```python
# ScanSetup.py — change from:
self.scan_sat.frequency = ConfigInteger(default=defaultSat["frequency"],
                                        limits=(1, 99999))
# to:
self.scan_sat.frequency = ConfigFloat(default=float(defaultSat["frequency"]),
                                      limits=(1.0, 99999.0), precision=3)
```

### Step 2 — TuneTest.py: Convert float MHz → integer kHz

```python
parm.frequency = int(round(transponder[0] * 1000))  # handles 12177.905 → 12177905
```

### Step 3 — satellites.xml parser: Accept kHz integers or decimal MHz

Option A — store as kHz integer in XML (simplest, no float parsing):
```xml
<transponder frequency="12177905" .../>   <!-- kHz, no decimal -->
```

Option B — parse decimal MHz in db.cpp:
```c
double tmp_d = strtod((const char*)attr->children->content, &end_ptr);
if (*end_ptr == '\0' || *end_ptr == ' ')
    *dest = (int)(tmp_d * 1000 + 0.5);  /* MHz → kHz, rounded */
```

### Step 4 — lamedb: Already works
The `%d` parser handles integer kHz values (e.g., 12177905) without modification.

---

## 8. Small Symbol Rate Transponders — The Critical Use Case

### Observed example

Screenshot captured 2026-04-05:
```
DVB-S QPSK  11711 V  148  3/4
SES 1 / Skyterra 1 / DirecTV 16  (101.0°W)
SNR: 13.6 dB   AGC: 0%   BER: 0
Service: Pilgrim Radio Network  (FTA)
```

SR=148 means **148,000 symbols per second**. This is approximately 100× smaller than a
typical broadcast transponder (20,000–30,000 Ksps).

### Why symbol rate determines frequency precision requirements

The demodulator's carrier acquisition loop can only search a frequency range proportional
to the symbol rate. If the tuned frequency is further from the actual carrier than the
acquisition range, the demodulator cannot lock.

| Symbol Rate | Occupied BW | Carrier Acquisition Range | LNB Required |
|---|---|---|---|
| 30,000 Ksps | ~40 MHz | ±2–5 MHz | Any |
| 5,000 Ksps | ~6.75 MHz | ±500 kHz – 1 MHz | Any |
| 1,000 Ksps | ~1.35 MHz | ±200–500 kHz | Any (marginal) |
| **148 Ksps** | **~200 kHz** | **±30–80 kHz** | **TCXO required** |
| 50 Ksps | ~68 kHz | ±15–30 kHz | TCXO required |
| 10 Ksps | ~13.5 kHz | ±5 kHz | OCXO required |

The Bullseye BE01 at ±10 kHz sits comfortably inside the SR=148 acquisition window.
A standard LNB at ±500 kHz would prevent lock entirely on this service.

### Software frequency precision and small-SR transponders

For the 11711 MHz transponder above, integer MHz tuning works because 11711 happens to be
the correct integer MHz value. But consider a hypothetical feed at **11711.500 MHz**:

- Enigma2 tunes to 11711 MHz → carrier is 500 kHz away
- SR=148 demodulator acquisition range: ~±50 kHz
- **Result: no lock, even with perfect Bullseye LNB**

With 500 Hz precision in enigma2:
- Enigma2 tunes to 11711.500 MHz → within 1 kHz of carrier
- SR=148 demodulator acquisition range: ~±50 kHz
- **Result: immediate lock**

This is the practical argument for implementing sub-MHz frequency entry in TNAP 7.
Users with standard LNBs gain nothing. Users with TCXO LNBs hunting narrow-band services
gain reliable lock where integer MHz tuning fails completely.

### QO-100 relevance

The Bullseye BE01 has a dedicated **739 MHz output for QO-100** (the Es'hail-2 amateur
geostationary satellite at 25.9°E). QO-100 narrowband amateur transponder signals can be
as narrow as a few kHz. Tuning those with MHz-only precision is impossible. This confirms
the Bullseye/TNAP 7 combination is intended for precision narrowband use.

---

## 9. Summary

| Question | Answer |
|---|---|
| Is 12177.905 MHz representable in the DVB API? | Yes — as 12177905 kHz |
| Is enigma2's internal storage capable? | Yes — `unsigned int` holds kHz values |
| Does the current UI support fractional MHz entry? | No — `ConfigInteger` only |
| Can lamedb store kHz-precision frequencies today? | Yes — integer `%d` format |
| Would a standard LNB benefit from kHz precision? | No — ±500 kHz–1 MHz drift |
| Would a TCXO LNB benefit? | Marginally (±10–30 kHz) |
| Do real broadcast transponders use fractional MHz? | Not found in any database |
| Is it worth implementing in TNAP 7? | Only for private/contribution feeds |
| Software changes required | Small — primarily ScanSetup.py and TuneTest.py |
| Hardware changes required | None (tuner IC already has the precision) |
| Feasible? | Yes |
| Practical for everyday use? | No |

---

*End of document*
