# Airport groups (metro & region → IATA expansion)

When a user says "Europe", "New York", "Bay Area", or "East Asia", they
usually mean "any of these major airports." The CLI accepts comma-lists of
IATA codes in origin/destination (e.g. `JFK,LGA,EWR`), so the agent's job is
to pick the right list.

Two flavors of grouping:

1. **IATA metro codes** that Matrix accepts directly as a single token
   (`NYC`, `LON`, `PAR`, `TYO`, etc.). These cover multi-airport cities
   and let the search engine pick.
2. **Manual region/country expansions** for things Matrix doesn't have a
   single code for (e.g. "Europe", "USA", "South America"). Use these as
   comma-list inputs to origin/destination.

## IATA metro codes (single-token, Matrix-native)

These resolve to "any of the city's airports" automatically. Prefer these
over manual comma-lists when one exists — they're shorter and Matrix
handles the airport-equivalence semantics:

| Metro | IATA metro | Constituent airports |
|---|---|---|
| New York City | `NYC` | JFK, LGA, EWR |
| London | `LON` | LHR, LGW, STN, LTN, LCY, SEN |
| Paris | `PAR` | CDG, ORY, BVA |
| Tokyo | `TYO` | NRT, HND |
| Moscow | `MOW` | SVO, DME, VKO |
| Stockholm | `STO` | ARN, BMA, NYO |
| Milan | `MIL` | MXP, LIN, BGY |
| Rome | `ROM` | FCO, CIA |
| Buenos Aires | `BUE` | EZE, AEP |
| São Paulo | `SAO` | GRU, CGH, VCP |
| Rio de Janeiro | `RIO` | GIG, SDU |
| Washington DC | `WAS` | IAD, DCA, BWI |
| Chicago | `CHI` | ORD, MDW |
| Houston | `HOU` | IAH, HOU |
| Bay Area | `QSF` | SFO, OAK, SJC |
| Los Angeles area | `LAX`† | LAX, BUR, LGB, ONT, SNA |
| Berlin | `BER` | BER (consolidated 2020 — TXL & SXF closed) |
| Osaka | `OSA` | KIX, ITM, UKB |
| Seoul | `SEL` | ICN, GMP |
| Beijing | `BJS` | PEK, PKX |
| Shanghai | `SHA`† | PVG, SHA (single airport code clashes with metro; use both explicitly) |
| Jakarta | `JKT` | CGK, HLP |
| Bangkok | `BKK`† | BKK, DMK |
| Bali (Denpasar) | `DPS` | DPS (single airport) |

† Where the metro code clashes or is ambiguous, fall back to comma-listing
the constituent airports. `LAX` is technically also the single-airport code
for Los Angeles International, so for "all LA-area airports" use the
explicit `LAX,BUR,LGB,ONT,SNA`.

When in doubt, pass the explicit comma-list — Matrix accepts it and the
behavior is unambiguous.

## Manual region expansions

For asks like "Europe" or "US west coast", expand to a curated list of major
hubs. These aren't exhaustive — they cover the highest-traffic airports that
most search results would want to consider.

### North America

| Region | Codes |
|---|---|
| US East Coast | `JFK,LGA,EWR,BOS,IAD,DCA,BWI,PHL,ATL,MIA,FLL,CLT,RDU,DTW,PIT` |
| US West Coast | `LAX,SFO,SEA,PDX,SAN,OAK,SJC,LAS,PHX,SLC` |
| US Midwest | `ORD,MDW,DTW,MSP,STL,MCI,IND,CMH,CVG,CLE` |
| US South | `ATL,MIA,FLL,MCO,TPA,IAH,DFW,DAL,AUS,SAT,HOU,NSH,MEM` |
| US major hubs (top 15) | `JFK,LGA,EWR,LAX,ORD,ATL,DFW,DEN,SFO,SEA,LAS,MIA,BOS,IAD,PHX` |
| Canada major | `YYZ,YVR,YUL,YYC,YEG,YOW,YHZ,YWG` |
| Mexico major | `MEX,CUN,GDL,MTY,PVR,SJD,TIJ` |

### Europe

| Region | Codes |
|---|---|
| Europe major hubs | `LHR,CDG,FRA,AMS,IST,MAD,BCN,FCO,MUC,ZRH,VIE,CPH,DUB,LGW,STN,ORY,MXP,GVA` |
| UK & Ireland | `LHR,LGW,STN,LTN,LCY,MAN,EDI,GLA,BHX,DUB,ORK,SNN` |
| France & Iberia | `CDG,ORY,NCE,LYS,MRS,TLS,MAD,BCN,LIS,OPO,VLC,SVQ,AGP` |
| Germany, Switzerland, Austria | `FRA,MUC,BER,DUS,HAM,STR,CGN,ZRH,GVA,BSL,VIE,SZG,INN` |
| Italy & Greece | `FCO,MXP,LIN,VCE,NAP,BLQ,FLR,ATH,SKG,HER,JTR` |
| Nordics & Baltics | `CPH,ARN,OSL,HEL,RIX,TLL,VNO,GOT,BGO` |
| Eastern Europe | `WAW,KRK,PRG,BUD,OTP,SOF,BEG,VIE,KBP` |
| Iberia (deeper) | `MAD,BCN,LIS,OPO,VLC,SVQ,AGP,PMI,IBZ,LPA,TFS,FNC` |
| Mediterranean & Turkey | `IST,SAW,LCA,LCK,TLV,BEY,AMM,ATH,SKG,HER` |

### Asia & Oceania

| Region | Codes |
|---|---|
| East Asia hubs | `HND,NRT,KIX,ICN,GMP,PEK,PKX,PVG,SHA,CAN,SZX,HKG,TPE,KHH` |
| Southeast Asia hubs | `SIN,BKK,DMK,KUL,CGK,DPS,MNL,HAN,SGN,RGN,PNH,VTE` |
| South Asia | `DEL,BOM,BLR,MAA,HYD,CCU,COK,KTM,CMB,DAC,LHE,ISB,KHI` |
| Middle East hubs | `DXB,DWC,AUH,DOH,RUH,JED,KWI,BAH,MCT,TLV,AMM,BEY` |
| Australia & NZ | `SYD,MEL,BNE,PER,ADL,AKL,WLG,CHC,ZQN` |

### South America & Africa

| Region | Codes |
|---|---|
| South America hubs | `GRU,GIG,SCL,EZE,AEP,LIM,BOG,UIO,CCS,PTY,MVD,ASU,LPB` |
| Africa hubs | `JNB,CPT,DUR,NBO,ADD,LOS,ABV,ACC,LFW,CMN,RAK,CAI,HRG,SSH` |

### Special groupings

| Group | Codes | Note |
|---|---|---|
| Star Alliance Europe hubs | `FRA,MUC,ZRH,VIE,CPH,IST,LIS,WAW,ATH,BRU` | Use with `--routing 'STAR+'` or `--extension 'ALLIANCE star-alliance'` for clean alliance-only routings |
| Oneworld Europe hubs | `LHR,MAD,HEL,DUB,WAW` | LHR is BA, MAD is IB, HEL is AY |
| SkyTeam Europe hubs | `CDG,AMS,FCO,PRG,BUD` | CDG is AF, AMS is KL, FCO is AZ |
| US ski destinations | `DEN,SLC,JAC,ASE,EGE,BZN,RNO,MMH` | For winter season searches |
| Hawaii | `HNL,OGG,KOA,LIH` | All inhabited islands' major airports |
| Caribbean major | `SJU,STT,MBJ,PUJ,POP,NAS,GCM,AUA,CUR,BGI` | High-traffic Caribbean leisure |

## How to use these from the CLI

```bash
# Use a metro code (Matrix-native):
flight fare NYC LON --dep 2026-08-15

# Use an explicit comma-list:
flight fare JFK,LGA,EWR LHR,LGW,STN --dep 2026-08-15

# "Anywhere in Europe from US east coast":
flight fare JFK,LGA,EWR,BOS,IAD LHR,CDG,FRA,AMS,IST,MAD,BCN,FCO,MUC,ZRH,VIE,CPH,DUB --dep 2026-08-15
```

## Pitfalls

1. **`LAX`, `BKK`, `SHA` are simultaneously airport and metro codes.** When
   ambiguous, expand to the explicit list. `LAX` alone gives you LAX
   airport only — for "any LA-area airport" use `LAX,BUR,LGB,ONT,SNA`.

2. **Wide lists explode the result space.** "Anywhere in Europe to anywhere
   in Asia" can return thousands of itineraries and risks calendar
   brownouts. Narrow the destination side first, or use `--stops 1` /
   `--routing` to constrain.

3. **Metro codes may not match alliance-loyalty needs.** `NYC` includes
   JFK/LGA/EWR — but EWR is the United (Star Alliance) hub while JFK is
   AA-heavy (Oneworld) and DL-heavy (SkyTeam). If the user wants Star
   Alliance, prefer `EWR` solo or `NYC` + `--extension 'ALLIANCE star-alliance'`.

4. **List is curated, not exhaustive.** Smaller cities and seasonal
   destinations aren't in here. When in doubt, ask the user for the
   specific city and translate from there.
