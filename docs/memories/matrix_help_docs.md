# Matrix in-app help docs (extracted from SPA bundle)

Auto-extracted from gstatic.com/alkali/*.js by evaluating the
bundle's `L`/`Y`/`Q` render calls as Python (they happen to be
valid Python expressions). Regenerate:

```
uv run --script research/extract_help_docs.py
```

## Itineraries

| Syntax | Example | Meaning |
|---|---|---|
| -CODESHARE | -CODESHARE | Disallow codeshares |
| MAXSTOPS n | MAXSTOPS 2 | Set a limit on the number of stops on this portion of the trip. |
| MAXDUR hh:mm | MAXDUR 6:45 | Set a limit on the duration of this portion of the trip. |
| MAXMILES n | MAXMILES 2900 | Set a cap on the number of miles flown on this portion of the trip. |
| MINMILES n | MINMILES 2600 | Set a floor on the number of miles flown on this portion of the trip. |
| MINCONNECT hh:mm | MINCONNECT 1:00 | Set a minimum connection time. |
| MAXCONNECT hh:mm | MAXCONNECT 2:00 | Set a maximum length of connection time. |
| ALLIANCE code1 code2 ... | ALLIANCE star-alliance | Permit only flights on these carriers in this alliance (or alliances). Separate multiple alliances with spaces. Supported alliances are oneworld, skyteam, and star-alliance. |
| -AIRLINES code1 code2 ... | -AIRLINES AA BA | Prohibit flights on the specified carriers. |
| AIRLINES code1 code2 ... | AIRLINES BA AF | Allow only flights on the specified carriers. |
| OPAIRLINES code1 code2 ... | OPAIRLINES AA | Allow only flights operated by these carriers (no matter the marketing carrier). |
| -OPAIRLINES code1 code2 ... | -OPAIRLINES AA | Prohibit flights operated by these carriers (no matter the marketing carrier). |
| -CITIES code1 code2 ... | -CITIES DFW ORD | Prohibit connections at these cities. |
| -REDEYES | -REDEYES | Prohibit overnight flights. |
| -OVERNIGHTS | -OVERNIGHTS | Prohibit solutions requiring overnight stops. |
| AIRCRAFT aircraft1 aircraft2 | AIRCRAFT T:737 C:PROP | Allow flights on the listed equipment types (prefixed with T:) or categories (prefixed with C:). Categories include C:JET, C:TURBOPROP, C:PISTON, C:TRAIN, C:HELICOPTER, C:AMPHIBIAN, and C:SURFACE. For the list of equipment types see the Aircraft Types tab. This code may be negated to prohibit the listed aircraft types and categories. |
| -PROPS | -PROPS | Prohibit flights on propeller planes. |
| -NOFIRSTCLASS | -NOFIRSTCLASS | All flights must have a first class cabin (though flights may still be booked in another cabin) |

## Faring

| Syntax | Example | Meaning |
|---|---|---|
| +CABIN code1 code2 ... | +CABIN 1 | Require booking in the specified cabin classes. For first class, use 1; for second (or business), use 2; for premium economy, use premium-coach or pe; and for economy, use 3. |
| -CABIN code1 code2 ... | -CABIN 3 | Prohibit booking in the specified cabin classes. See +CABIN for what codes to use for each cabin class. |
| F BC=code | F bc=y | Use fares with the specified prime booking code. Note: the actual booking class used may be different due to being overridden by the carrier's booking code exception table. |
| F BC=code\|BC=code\|... | F bc=y\|bc=b | Specify that fares use one of several prime booking codes (e.g. book in either Y or B class). See the note on the above item. |
| F  carrier.city1+city2.farebasis | Specify which fares to use. Multiple alternate fare specifications can be separated by a vertical bar. See below for specific examples. |  |
| F CC.AAA+BBB.FFFFFF | F aa.lon+chi.yup | Specify carrier, market (city pair), and fare basis code of the fares to use (e.g. only AA LON-CHI YUP fares). |
| F ..FFFFFF | F ..yup\|..f | Specify the fare code (but not carrier or market) of the fare to use (e.g. either YUP or F fares on any airline and between any city pairs). |
| F .AAA+BBB. | F .lon+chi. | Specify the market (city pair) for the fares (e.g. use only LON-CHI through fares). |
| F CC..FFFFFF | F aa..yup\|aa..f | Specify the carrier and fare basis code, but not the market (e.g. use either YUP or F fares on AA for any city pair). |
| F ..F- | F ..y-\|..b- | Specify the fare basis using "wildcards" (e.g. only use fare bases that start with either Y or B). |

## Aircraft Types

| Code | Parent | Aircraft |
|---|---|---|
| AT4 | ATR | ATR 42 |
| ATZ | ATZ | ATR 42 Freighter |
| ATD | ATR | ATR 42-400 |
| AT5 | ATR | ATR 42-500 |
| AT7 | ATR | ATR 72 |
| ATF | ATF | ATR 72 (Freighter) |
| ATR | ATR | ATR42/ATR72 |
| ND2 | ND2 | Aerospatiale N 262/Frakes Mohawk 298 |
| CRV | CRV | Aerospatiale SE.210 Caravelle |
| NDC | NDC | Aerospatiale SN601 Corvette |
| AT3 | ATR | Aerospatiale/Alenia ATR42 300/400 |
| SSC | SSC | Aerospatiale/British Aerospace Concorde |
| AGH | AGH | AgustaWestland A109 |
| AWH | AWH | AgustaWestland AW139 |
| AW6 | AW6 | AgustaWestland AW169 |
| AW8 | AW8 | AgustaWestland AW189 |
| 220 | 220 | Airbus A220 Passenger |
| 221 | 220 | Airbus A220-100 Passenger |
| 223 | 220 | Airbus A220-300 Passenger |
| ABF | ABF | Airbus A300 (Freighter) |
| AB3 | AB3 | Airbus A300 Passenger |
| ABY | ABF | Airbus A300-600 Freighter |
| AB6 | AB3 | Airbus A300-600 Passenger |
| ABB | ABF | Airbus A300-600ST Beluga (Freighter) |
| AB4 | AB3 | Airbus A300B2/B4 Passenger |
| ABX | ABF | Airbus A300B4/A300C4/A300F4 Freighter |
| 31F | 31F | Airbus A310 Freighter |
| 310 | 310 | Airbus A310 Passenger |
| 31X | 31F | Airbus A310-200 Freighter |
| 312 | 310 | Airbus A310-200 Passenger |
| 31Y | 31F | Airbus A310-300 Freighter |
| 313 | 310 | Airbus A310-300 Passenger |
| 318 | 32S | Airbus A318 |
| 31A | 32S | Airbus A318 (sharklets) |
| 32S | 32S | Airbus A318/319/320/321 |
| 319 | 32S | Airbus A319 |
| 31B | 32S | Airbus A319 (sharklets) |
| 31N | 32S | Airbus A319neo |
| 320 | 32S | Airbus A320 |
| 32F | 32F | Airbus A320 (Freighter) |
| 32A | 32S | Airbus A320 (Sharklets) |
| 32N | 32S | Airbus A320neo |
| 321 | 32S | Airbus A321 |
| 32X | 32X | Airbus A321 (Freighter) |
| 32B | 32S | Airbus A321 (Sharklets) |
| 32Q | 32S | Airbus A321neo |
| 330 | 330 | Airbus A330 |
| 33F | 33F | Airbus A330 Freighter |
| 332 | 330 | Airbus A330-200 |
| 33X | 33F | Airbus A330-200 Freighter |
| 333 | 330 | Airbus A330-300 |
| 33Y | 33F | Airbus A330-300 Freighter |
| 338 | 330 | Airbus A330-800neo Passenger |
| 339 | 330 | Airbus A330-900neo |
| 340 | 340 | Airbus A340 |
| 342 | 340 | Airbus A340-200 |
| 343 | 340 | Airbus A340-300 |
| 345 | 340 | Airbus A340-500 |
| 346 | 340 | Airbus A340-600 |
| 350 | 350 | Airbus A350 |
| 351 | 350 | Airbus A350-1000 |
| 358 | 350 | Airbus A350-800 |
| 359 | 350 | Airbus A350-900 |
| 380 | 380 | Airbus A380 Passenger |
| 388 | 380 | Airbus A380-800 Passenger |
| 38F | 38F | Airbus A380-800F(Freighter) |
| AI4 | AI4 | Airbus Atlas |
| 33B | 33F | Airbus Beluga XL |
| H16 | H16 | Airbus Helicopter H-160 |
| NDH | NDH | Airbus Helicopters A365C/A365N Dauphin2 |
| MBH | MBH | Airbus Helicopters BO 105 |
| EC4 | EC4 | Airbus Helicopters H 145 / H 145T2 |
| EC5 | EC5 | Airbus Helicopters H 155 |
| EC7 | EC7 | Airbus Helicopters H 175 |
| NDE | NDE | Airbus Helicopters H125 |
| EC3 | EC3 | Airbus Helicopters H130/H130T2 |
| APH | APH | Airbus Helicopters H215 |
| L4F | L4F | Aircraft Industries (LET)410 freight |
| A38 | A38 | Antonov AN-38 |
| A81 | A81 | Antonov AN148-100 |
| ANF | ANF | Antonov An-12 |
| A4F | A4F | Antonov An-124 Ruslan |
| A40 | A40 | Antonov An-140 |
| A58 | A58 | Antonov An-158 |
| A78 | A78 | Antonov An-178 |
| A22 | A22 | Antonov An-22 |
| A5F | A5F | Antonov An-225 |
| AN4 | AN4 | Antonov An-24 |
| A26 | AN6 | Antonov An-26 |
| AN6 | AN6 | Antonov An-26/30/32 |
| A30 | AN6 | Antonov An-30 |
| A32 | AN6 | Antonov An-32 |
| AN7 | AN7 | Antonov An-72/74 |
| A28 | A28 | Antonov An28/PZL Mielec M-28 Skytruck |
| TAT | TRN | Auto Train |
| AR1 | ARJ | Avro RJ100 |
| AR7 | ARJ | Avro RJ70 |
| ARJ | ARJ | Avro RJ70/RJ85/RJ100 |
| AR8 | ARJ | Avro RJ85 |
| ARX | ARX | Avro Regional Jet RJX Avroliner |
| AX1 | ARX | Avro Regional Jet RJX100 Avroliner |
| AX8 | ARX | Avro Regional Jet RJX85 Avroliner |
| 14X | 14F | BAE Systems 146-100 Freighter |
| 14Y | 14F | BAE Systems 146-200 (Freighter) |
| 14Z | 14F | BAE Systems 146-300 (Freighter) |
| APF | APF | BAE Systems ATP Freighter |
| BET | BEC | Beechcraft (Lght Acft - Twin Turboprop) |
| BEC | BEC | Beechcraft (Light Aircraft) |
| BE1 | BE1 | Beechcraft 1900 Airliner |
| BES | BE1 | Beechcraft 1900C Airliner |
| BEH | BE1 | Beechcraft 1900D Airliner |
| BE9 | BE9 | Beechcraft C99 Airliner |
| BE2 | BEC | Beechcraft-Lght Acft-Twin Piston Engine |
| BH2 | BH2 | Bell (Helicopters) |
| D1X | D1F | Boeing (Douglas) DC-10-10 (Freighter) |
| DC3 | DC3 | Boeing (Douglas) DC-3 Passenger |
| DC4 | DC4 | Boeing (Douglas) DC-4 |
| DC6 | DC6 | Boeing (Douglas) DC-6B Passenger |
| DC8 | DC8 | Boeing (Douglas) DC-8 Passenger |
| D8T | D8F | Boeing (Douglas) DC-8-50 Freighter |
| D8X | D8F | Boeing (Douglas) DC-8-61/62/63 Frtr |
| D8L | DC8 | Boeing (Douglas) DC-8-62 Passenger |
| D8Y | D8F | Boeing (Douglas) DC-8-71/72/73 Frtr |
| D8Q | DC8 | Boeing (Douglas) DC-8-72 Passenger |
| DC9 | DC9 | Boeing (Douglas) DC-9 Passenger |
| D9X | D9F | Boeing (Douglas) DC-9-10 Freighter |
| D91 | DC9 | Boeing (Douglas) DC-9-10 Passenger |
| D92 | DC9 | Boeing (Douglas) DC-9-20 |
| D9C | D9F | Boeing (Douglas) DC-9-30 Freighter |
| D9D | D9F | Boeing (Douglas) DC-9-40 Freighter |
| D1F | D1F | Boeing (Douglas) DC10 (Freighter) |
| D1Y | D1F | Boeing (Douglas) DC10 - 30/40 Freighter |
| D10 | D10 | Boeing (Douglas) DC10 Passenger |
| D11 | D10 | Boeing (Douglas) DC10-10/15 Passenger |
| D1M | D1M | Boeing (Douglas) DC10-30(Mixed Config) |
| D1C | D10 | Boeing (Douglas) DC10-30/40 (Pax) |
| D3F | D3F | Boeing (Douglas) DC3 Freighter |
| D6F | D6F | Boeing (Douglas) DC6A/B/C Freighter |
| D8F | D8F | Boeing (Douglas) DC8 Freighter |
| D8M | D8M | Boeing (Douglas) DC8-62 Mixed Config |
| D9F | D9F | Boeing (Douglas) DC9 Freighter |
| D93 | DC9 | Boeing (Douglas) DC9-30 Passenger |
| D94 | DC9 | Boeing (Douglas) DC9-40 Passenger |
| D95 | DC9 | Boeing (Douglas) DC9-50 Passenger |
| M1F | M1F | Boeing (Douglas) MD-11 (Freighter) |
| M1M | M1M | Boeing (Douglas) MD-11 Mixed Config |
| M11 | M11 | Boeing (Douglas) MD-11 Passenger |
| M80 | M80 | Boeing (Douglas) MD-80 |
| M81 | M80 | Boeing (Douglas) MD-81 |
| M82 | M80 | Boeing (Douglas) MD-82 |
| M83 | M80 | Boeing (Douglas) MD-83 |
| M87 | M80 | Boeing (Douglas) MD-87 |
| M88 | M80 | Boeing (Douglas) MD-88 |
| M90 | M90 | Boeing (Douglas) MD-90 |
| M2F | M2F | Boeing (Douglas) MD82 Freighter |
| M3F | M3F | Boeing (Douglas) MD83 Freighter |
| M8F | M8F | Boeing (Douglas) MD88 Freighter |
| 377 | 377 | Boeing 377 Stratocruiser |
| 37F | 377 | Boeing 377 Stratocruiser |
| 70F | 70F | Boeing 707-320B/320C (Freighter) |
| 70M | 70M | Boeing 707-320B/320C (Mixed Config) |
| 703 | 707 | Boeing 707-320B/320C (Passenger) |
| 707 | 707 | Boeing 707/720 Passenger |
| 717 | 717 | Boeing 717-200 |
| B72 | 707 | Boeing 720/020B |
| 72F | 72F | Boeing 727 (Freighter) |
| 72M | 72M | Boeing 727 (Mixed Configuration) |
| 727 | 727 | Boeing 727 (Passenger) |
| 72X | 72F | Boeing 727-100 (Freighter) |
| 72B | 72M | Boeing 727-100 (Mixed Configuration) |
| 721 | 727 | Boeing 727-100 (Passenger) |
| 72S | 722 | Boeing 727-200 |
| 72Y | 72F | Boeing 727-200 (Freighter) |
| 72C | 72M | Boeing 727-200 (Mixed Config) |
| 72W | 727 | Boeing 727-200 (Passenger) winglets |
| 72A | 722 | Boeing 727-200 Advanced |
| 722 | 727 | Boeing 727-200 Passenger |
| 73F | 73F | Boeing 737 (Freighter) |
| 73M | 73M | Boeing 737 (Mixed Configuration) |
| 7M1 | 737 | Boeing 737 MAX 10 Passenger |
| 737 | 737 | Boeing 737 Passenger |
| 731 | 737 | Boeing 737-100 Passenger |
| 73X | 73F | Boeing 737-200 (Freighter) |
| 73L | 73M | Boeing 737-200 (Mixed Configuration) |
| 73A | 732 | Boeing 737-200 Advanced Passenger |
| 732 | 737 | Boeing 737-200 Passenger |
| 73Y | 73F | Boeing 737-300 (Freighter) |
| 73N | 73M | Boeing 737-300 (Mixed Configuration) |
| 73C | 737 | Boeing 737-300 (Winglets) Passenger |
| 733 | 737 | Boeing 737-300 Passenger |
| 73P | 73F | Boeing 737-400 (Freighter) |
| 73Q | 73M | Boeing 737-400 (Mixed Configuration) |
| 734 | 737 | Boeing 737-400 Passenger |
| 73E | 737 | Boeing 737-500 (Winglets) Passenger |
| 735 | 737 | Boeing 737-500 Passenger |
| 736 | 737 | Boeing 737-600 Passenger |
| 73R | 73M | Boeing 737-700 (Mixed Configuration) |
| 7S7 | 737 | Boeing 737-700 (Scimitar Winglets) Pax |
| 73T | 73F | Boeing 737-700 (Winglets) Freighter |
| 73W | 737 | Boeing 737-700 (Winglets) Passenger |
| 73S | 732 | Boeing 737-700 Freighter |
| 73G | 737 | Boeing 737-700 Passenger |
| 7S8 | 737 | Boeing 737-800 (Scimitar Winglets) Pax |
| 73H | 737 | Boeing 737-800 (Winglets) Passenger |
| 7F8 | 73F | Boeing 737-800 F (Scimitar Winglets) |
| 73U | 73F | Boeing 737-800 Freighter |
| 73K | 73F | Boeing 737-800 Freighter (winglets) |
| 738 | 737 | Boeing 737-800 Passenger |
| 7S9 | 737 | Boeing 737-900 (Scimitar Winglets) Pax |
| 73J | 737 | Boeing 737-900 (Winglets) Passenger |
| 739 | 737 | Boeing 737-900 Passenger |
| 7M7 | 737 | Boeing 737MAX 7 Passenger |
| 7M8 | 737 | Boeing 737MAX 8 Passenger |
| 7M9 | 737 | Boeing 737MAX 9 Passenger |
| 74F | 74F | Boeing 747 (Freighter) |
| 74M | 74M | Boeing 747 (Mixed Configuration) |
| 747 | 747 | Boeing 747 (Passenger) |
| 741 | 747 | Boeing 747-100 (Passenger) |
| 74T | 74F | Boeing 747-100 Freighter |
| 74X | 74F | Boeing 747-200 (Freighter) |
| 74C | 74M | Boeing 747-200 (Mixed Configuration) |
| 742 | 747 | Boeing 747-200 (Passenger) |
| 743 | 747 | Boeing 747-300/747-100/200 SUD (Pax) |
| 74U | 74F | Boeing 747-300/747-200 SUD (Freighter) |
| 74D | 74M | Boeing 747-300/747-200 SUD (Mxd Config) |
| 74J | 747 | Boeing 747-400 (Domestic) Passenger |
| 74E | 74M | Boeing 747-400 (Mixed Configuration) |
| 744 | 747 | Boeing 747-400 (Passenger) |
| 74B | 74F | Boeing 747-400 Swingtail Freighter |
| 74Y | 74F | Boeing 747-400F (Freighter) |
| 74H | 747 | Boeing 747-8 Passenger |
| 74N | 74F | Boeing 747-8F (Freighter) |
| 74L | 747 | Boeing 747SP Passenger |
| 74V | 74F | Boeing 747SR (Freighter) |
| 74R | 747 | Boeing 747SR Passenger |
| 757 | 757 | Boeing 757 (Passenger) |
| 75E | 757 | Boeing 757 (winglets) |
| 75W | 757 | Boeing 757-200 (Winglets) Passenger |
| 75C | 75F | Boeing 757-200 Freighter |
| 75V | 75F | Boeing 757-200 Freighter (winglets) |
| 75M | 75M | Boeing 757-200 Mixed Configuration |
| 752 | 757 | Boeing 757-200 Passenger |
| 75F | 75F | Boeing 757-200PF (Freighter) |
| 75T | 757 | Boeing 757-300 (winglets) Passenger |
| 753 | 757 | Boeing 757-300 Passenger |
| 76F | 76F | Boeing 767 Freighter |
| 767 | 767 | Boeing 767 Passenger |
| 76X | 76F | Boeing 767-200 Freighter |
| 762 | 767 | Boeing 767-200 Passenger |
| 76V | 76F | Boeing 767-300 (winglets) Freighter |
| 76W | 767 | Boeing 767-300 (winglets) Passenger |
| 76Y | 76F | Boeing 767-300 Freighter |
| 763 | 767 | Boeing 767-300 Passenger |
| 764 | 767 | Boeing 767-400 Passenger |
| 77F | 77F | Boeing 777 Freighter |
| 777 | 777 | Boeing 777 Passenger |
| 772 | 777 | Boeing 777-200/200ER Passenger |
| 77X | 77F | Boeing 777-200F Freighter |
| 77L | 777 | Boeing 777-200LR |
| 773 | 777 | Boeing 777-300 Passenger |
| 77W | 777 | Boeing 777-300ER Passenger |
| 77V | 77F | Boeing 777-300ERSF Freighter |
| 779 | 777 | Boeing 777-9 Passenger |
| 787 | 787 | Boeing 787 |
| 781 | 787 | Boeing 787-10 |
| 783 | 787 | Boeing 787-300 |
| 788 | 787 | Boeing 787-8 |
| 789 | 787 | Boeing 787-9 |
| C17 | C17 | Boeing C17 Globemaster |
| CCX | CCX | Bombardier BD-700 Global Exp/G5000/5500 |
| CSB | CSB | Bombardier C Series |
| CS1 | CSB | Bombardier CS100 |
| CS3 | CSB | Bombardier CS300 |
| CL3 | CL3 | Bombardier Challenger 300 |
| CL5 | CL5 | Bombardier Challenger 350 |
| CC6 | CC6 | Bombardier Global 6000 / 6500 |
| CC7 | CC7 | Bombardier Global 7000 |
| C75 | C75 | Bombardier Global 7500 |
| CR5 | CRJ | Bombardier Regional Jet 550 |
| B14 | B11 | British Aerospace (BAC) 1-11 400/475 |
| B15 | B11 | British Aerospace (BAC) 1-11 500/Rombac |
| B11 | B11 | British Aerospace (BAC) One-Eleven |
| B12 | B11 | British Aerospace (BAC) One-Eleven 200 |
| B13 | B11 | British Aerospace (BAC) One-Eleven 300 |
| H25 | H25 | British Aerospace (Hawker Siddeley) 125 |
| HS7 | HS7 | British Aerospace (Hawker Siddeley) 748 |
| 14F | 14F | British Aerospace 146 (Freighter) |
| 146 | 146 | British Aerospace 146 Passenger |
| 141 | 146 | British Aerospace 146-100 Passenger |
| 142 | 146 | British Aerospace 146-200 Passenger |
| 143 | 146 | British Aerospace 146-300 Passenger |
| ATP | ATP | British Aerospace ATP |
| HSF | HSF | British Aerospace Argosy |
| HPH | HPH | British Aerospace Handley Page Herald |
| JST | JST | British Aerospace Jetstream |
| J31 | JST | British Aerospace Jetstream 31 |
| J32 | JST | British Aerospace Jetstream 32 |
| J41 | JST | British Aerospace Jetstream 41 |
| VGF | VGF | British Aerospace Merchantman |
| TRD | TRD | British Aerospace Trident |
| VCV | VCV | British Aerospace Viscount |
| DHD | DHD | British Aerospace(De Havilland)104 Dove |
| DHH | DHH | British Aerospace(DeHavilland)114 Heron |
| BNT | BNT | Britten-Norman BN-2A Mk.III Trislander |
| BNI | BNI | Britten-Norman BN-2A/BN-2B Islander |
| BUS | BUS | Bus |
| BTA | BTA | Business Turbo-Prop Aircraft |
| CJ7 | CNJ | CESSNA 700 Citation Longitude |
| CJS | CNJ | CESSNA Citation Sovereign |
| C19 | 919 | COMAC C-919 |
| CRK | CRJ | Canadair (Bombardier) Regional Jet 1000 |
| CL4 | CL4 | Canadair CL-44 |
| CCJ | CCJ | Canadair CL-600/601/604/605/650 |
| CRA | CRJ | Canadair CRJ Series 705 |
| CRJ | CRJ | Canadair Regional Jet |
| CR1 | CRJ | Canadair Regional Jet 100 |
| CR2 | CRJ | Canadair Regional Jet 200 |
| CR7 | CRJ | Canadair Regional Jet 700 |
| CR9 | CRJ | Canadair Regional Jet 900 |
| CRF | CRF | Canadair Regional Jet Freighter |
| CS9 | CS9 | Casa / IAe C-295 |
| CS2 | CS2 | Casa C212/Nusantara NC-212 Aviocar |
| CS5 | CS5 | Casa/Nusantara CN-235 |
| CNA | CNA | Cessna (Light Aircraft) |
| CNF | CNF | Cessna 208B Caravan |
| CS4 | CS4 | Cessna 408 SkyCourier |
| CJ1 | CNJ | Cessna 500/501/525/M2 Citation |
| CJM | CNJ | Cessna 510 Mustang Citation |
| CJ2 | CNJ | Cessna 550/551/552 Citation |
| CJ5 | CNJ | Cessna 560 Citation |
| CJL | CNJ | Cessna 560 XL/XLS Citation |
| CJ6 | CNJ | Cessna 650 Citation |
| CJ8 | CNJ | Cessna 680 Citation |
| CN7 | CNJ | Cessna 750 Citation X |
| CJX | CNJ | Cessna 750 Citation X |
| CNJ | CNJ | Cessna Citation |
| CJT | CNJ | Cessna Citation Latitude |
| CN2 | CNA | Cessna Light Acft (Twin piston engines) |
| CN1 | CNA | Cessna Light Acft(Single piston engine) |
| CNT | CNA | Cessna Light Aircraft (Twin Turboprop) |
| CNC | CNA | Cessna Light Aircraft(Single Turboprop) |
| SF5 | SF5 | Cirrus SJ-X Vision |
| C21 | C21 | Comac ARJ21 |
| C27 | C21 | Comac ARJ21-700 |
| 919 | 919 | Comac C919 |
| TCM | TRN | Commuter Train |
| CVV | CVF | Convair 240 (Freighter) |
| CV2 | CVR | Convair 240 (Passenger) |
| CVR | CVR | Convair 240/440/580 (Passenger) |
| CVX | CVF | Convair 340/440 (Freighter) |
| CV4 | CVR | Convair 440 Metropolitan (Passenger) |
| CVF | CVF | Convair 440/580/600/640 (Freighter) |
| CV5 | CVR | Convair 580 Passenger |
| CVY | CVF | Convair 580/5800/600/640 (freighter) |
| CWC | CWC | Curtiss C-46 Commando |
| D62 | D62 | DIAMOND AIRCRAFT DA-62 |
| DFL | DFL | Dassault Falcon |
| DF1 | DFL | Dassault Falcon 10/100 |
| DF2 | DFL | Dassault Falcon 10/20/100/200/2000 |
| D20 | DFL | Dassault Falcon 2000/2000DX |
| D2L | DFL | Dassault Falcon 2000EX/EASY/LX |
| DF5 | DFL | Dassault Falcon 50/50EX |
| DF6 | DFL | Dassault Falcon 6X |
| DF7 | DFL | Dassault Falcon 7X |
| DF8 | DFL | Dassault Falcon 8X |
| D9L | DFL | Dassault Falcon 900LX |
| DF9 | DFL | Dassault Falcon 900s EASY |
| DAM | DAM | Dassault-Breguet Mercure |
| DF3 | DFL | Dassault-Breguet Myst-Falcon (50/900) |
| DHF | DHF | De Havilland (Bombardier) DHC-8 Frt |
| DHR | DHB | De Havilland (Bombardier)DHC-2 Turbo |
| D3X | DHF | De Havilland DHC-8-300 Dash 8/8Q Frt |
| D4X | DHF | De Havilland DHC-8-400 Dash 8Q-Freight |
| DHP | DHB | De Havilland-Bombardier DHC2 Beaver |
| DHB | DHB | De Havilland-Bombardier DHC2 Beaver/Turbo Beaver |
| DHS | DHO | De Havilland-Bombardier DHC3 Otter |
| DHO | DHO | De Havilland-Bombardier DHC3 Otter/Turbo Otter |
| DHL | DHO | De Havilland-Bombardier DHC3 Turbo Otter |
| DHC | DHC | De Havilland-Bombardier DHC4 Caribou |
| DHT | DHT | De Havilland-Bombardier DHC6 Twin Otter |
| DH7 | DH7 | De Havilland-Bombardier DHC7 Dash 7 |
| DH8 | DH8 | De Havilland-Bombardier DHC8 Dash 8 |
| DH4 | DH8 | De Havilland-Bombardier DHC8 Dash 8-400/8Q |
| DH1 | DH8 | De Havilland-Bombardier DHC8-100 Dash 8/8Q |
| DH2 | DH8 | De Havilland-Bombardier DHC8-200 Dash 8/8Q |
| DH3 | DH8 | De Havilland-Bombardier DHC8-300 Dash 8/8Q |
| D42 | D42 | Diamond Aircraft DA42 Twin Star |
| DR3 | DRF | Dronamics Black Swan |
| DRF | DRF | Dronamics Drone Freighter |
| EV5 | EV5 | EVEKTOR EV-55 Outback |
| EAC | EAC | Eclipse |
| EA5 | EAC | Eclipse 500 |
| EMB | EMB | Embraer 110 Bandeirante |
| EM2 | EM2 | Embraer 120 Brasilia |
| E70 | EMJ | Embraer 170 |
| EMJ | EMJ | Embraer 170/195 |
| E75 | EMJ | Embraer 175 |
| E7W | EMJ | Embraer 175 (Enhanced Winglets) |
| E90 | EMJ | Embraer 190 |
| 290 | EMJ | Embraer 190 E2 |
| E95 | EMJ | Embraer 195 |
| 295 | EMJ | Embraer 195 E2 |
| EM3 | EM3 | Embraer EMB-135 |
| EP1 | EPH | Embraer EMB-500 Phenom 100 |
| EP3 | EPH | Embraer EMB-505 Phenom 300 |
| EML | EML | Embraer Legacy |
| EM4 | EML | Embraer Legacy 450 |
| EM5 | EML | Embraer Legacy 500 |
| EPH | EPH | Embraer Phenom |
| ET5 | ETR | Embraer Praetor 500 |
| ET6 | ETR | Embraer Praetor 600 |
| ETR | ETR | Embraer Praetor ET5/ET6 |
| ERJ | ERJ | Embraer RJ 135/140/145 |
| EM7 | EMJ | Embraer RJ-170 Regional Jet |
| EM9 | EMJ | Embraer RJ-190 Regional Jet |
| ER3 | ERJ | Embraer RJ135 |
| ERD | ERJ | Embraer RJ140 |
| ER4 | ERJ | Embraer RJ145 |
| 275 | EMJ | Embraer175 E2 |
| EQV | EQV | Equipment Varies |
| SWF | SWF | Fairchild (Swearingen)SA226 freight |
| D28 | D28 | Fairchild Dornier 228 |
| D38 | D38 | Fairchild Dornier 328-100 |
| FRJ | FRJ | Fairchild Dornier 328JET |
| FA7 | FA7 | Fairchild Dornier 728JET |
| FK7 | FK7 | Fairchild Industries FH227 |
| SWM | SWM | Fairchild SA26/SA226/SA227 Merlin/Metro |
| 100 | 100 | Fokker 100 |
| F50 | F50 | Fokker 50 |
| F5F | F5F | Fokker 50 Freighter |
| F70 | F70 | Fokker 70 |
| F27 | F27 | Fokker F27 Friendship/Fairchild F27 |
| F28 | F28 | Fokker F28 Fellowship |
| F21 | F28 | Fokker F28-1000 Fellowship |
| F22 | F28 | Fokker F28-2000 Fellowship |
| F23 | F28 | Fokker F28-3000 Fellowship |
| F24 | F28 | Fokker F28-4000 Fellowship |
| GA5 | GRJ | GULFSTREAM AEROSPACE G500 |
| GA6 | GRJ | GULFSTREAM AEROSPACE G600 |
| GA1 | GA1 | Gippsland Aeronautics GA10 |
| GA8 | GA8 | Gippsland Aeronautics GA8 Airvan |
| CD2 | CD2 | Gippsland Aeronautics N22B/N24A Nomad |
| GRG | GRG | Grumman G-21 Goose (Amphibian) |
| GRM | GRM | Grumman G-73 Mallard (Amphibian) |
| GRS | GRS | Gulfstream Aerospace (Grumman) GULF. I/I-C |
| GR1 | GRJ | Gulfstream Aerospace G-100/G-150 |
| G2B | GRJ | Gulfstream Aerospace G-1159 IIB |
| G2S | GRJ | Gulfstream Aerospace G-1159 IISP |
| GJ3 | GRJ | Gulfstream Aerospace G-1159A III |
| GR2 | GRJ | Gulfstream Aerospace G-200 (Galaxy) |
| GRJ | GRJ | Gulfstream Aerospace G/stream 2/3/4/5/6 |
| GJ2 | GRJ | Gulfstream Aerospace G1159 II |
| GR3 | GRJ | Gulfstream Aerospace G280 |
| GJ6 | GRJ | Gulfstream Aerospace G650 |
| GJ7 | GRJ | Gulfstream Aerospace G700 |
| GJ8 | GRJ | Gulfstream Aerospace G800 |
| GJ4 | GRJ | Gulfstream Aerospace IV (G300-450) |
| GJ5 | GRJ | Gulfstream Aerospace V (G500/G550) |
| YN2 | YN2 | Harbin Yunshuji Y12 |
| H21 | HBA | Hawker 1000 |
| H20 | HBA | Hawker 200 |
| PR1 | HBA | Hawker 390 Premier 1/1A |
| BE4 | HBA | Hawker 400 Beechjet/400A/400XP/400T |
| H24 | HBA | Hawker 4000 |
| H28 | HBA | Hawker 850XP/900 |
| H29 | HBA | Hawker 900XP |
| HBA | HBA | Hawker Beechcraft |
| BEP | BEC | Hawker Beechcraft (Light/single piston) |
| BEF | BEF | Hawker Beechcraft 1900 (Freighter) |
| HEC | HEC | Helio H250 Courier/H295/395SuperCourier |
| THS | TRN | High Speed Train |
| TRS | TRN | High Speed Train |
| HHJ | HHJ | Honda HA-420 JondaJet |
| THT | TRN | Hotel Train |
| HOV | HOV | Hovercraft |
| I4F | I4F | Ilyushin II-114T Freighter |
| I9F | I9F | Ilyushin II-96 Freighter |
| IL4 | IL4 | Ilyushin IL-14 |
| IL8 | IL8 | Ilyushin Il-18 |
| IL6 | IL6 | Ilyushin Il-62 |
| IL7 | IL7 | Ilyushin Il-76 |
| ILW | ILW | Ilyushin Il-86 |
| IL9 | IL9 | Ilyushin Il-96 Passenger |
| I14 | I14 | Ilyushin Il114 |
| 219 | 219 | Indonesian Aerospace (IAe) N219 |
| ICE | TRN | Inter-City Express |
| TIC | TRN | Intercity Train |
| WWP | WWP | Israel Aircraft Ind.1124 Westwind |
| JET | JET | Jet |
| JU5 | JU5 | Junkers Ju 52/3m |
| LCH | LCH | Launch/Boat |
| LRJ | LRJ | Learjet |
| LJ2 | LRJ | Learjet 23/24/25 |
| LJ3 | LRJ | Learjet 28/29/31/35/36 |
| LJ4 | LRJ | Learjet 40/45 |
| LJ6 | LRJ | Learjet 55/60 |
| LJ7 | LRJ | Learjet 70/75 |
| LJ8 | LRJ | Learjet 85 |
| L4T | L4T | Let 410 |
| LMO | LMO | Limousine |
| LHP | LHP | Lockheed L-100 Hercules Passenger |
| L1A | L10 | Lockheed L-1011-1 Tristar |
| L12 | L10 | Lockheed L-1011-200/250 Tristar |
| L49 | L49 | Lockheed L-1049 Super Constellation |
| LOM | LOM | Lockheed L-188 Electra Mixed |
| L11 | L10 | Lockheed L1011 Tristar 1/50/100/150/200 |
| L15 | L10 | Lockheed L1011 Tristar 500 Passenger |
| L1F | L1F | Lockheed L1011 Tristar Freighter |
| L10 | L10 | Lockheed L1011 Tristar Passenger |
| LOH | LOH | Lockheed L182/L282/L382 (L100) Hercules |
| LOF | LOF | Lockheed L188 Electra (Freighter) |
| LOE | LOE | Lockheed L188 Electra (Passenger) |
| MD9 | MD9 | MD Helicopters MD 900 Explorer |
| D14 | D10 | McDonnell Douglas DC-10-40 |
| D9S | DC9 | McDonnell Douglas DC-9 (30/40/50) |
| M95 | M90 | McDonnell Douglas MD-95 |
| MTL | TRN | Metroliner Train |
| MIH | MIH | Mil Mi-8/Mi-17/Mi-171/Mi-172 |
| MU2 | MU2 | Mitsubishi MU-2 |
| YS1 | YS1 | NAMC YS-11 |
| PN6 | PN6 | Partenavia P68 |
| P18 | P18 | Piaggio P180 Avanti II |
| PL2 | PL2 | Pilatus PC-12 |
| PL4 | PL4 | Pilatus PC-24 |
| PL6 | PL6 | Pilatus PC6 Turbo-Porter |
| PA1 | PAG | Piper (Light Aircraft - Single Piston) |
| PA2 | PAG | Piper (Light Aircraft - Twin Piston) |
| PAT | PAG | Piper (Light Aircraft - Twin Turboprop) |
| PAG | PAG | Piper (Light Aircraft) |
| RFS | RFS | Road Feeder Service (Truck) |
| TBM | TBM | SOCATA TBM-700 |
| S20 | S20 | Saab 2000 |
| SF3 | SF3 | Saab 340 |
| SFF | SFF | Saab 340 Freighter |
| SFA | SF3 | Saab 340 Passenger |
| SFB | SF3 | Saab 340B |
| SY8 | SY8 | Shaanxi Y-8 |
| SH3 | SH3 | Shorts 330 (SD3-30) |
| SH6 | SH6 | Shorts 360 (SD3-60) |
| SHB | SHB | Shorts SC.5 Belfast |
| SHS | SHS | Shorts Skyvan (SC-7) |
| S58 | S58 | Sikorsky S-58T |
| S76 | S76 | Sikorsky S-76 |
| S61 | S61 | Sikorsky S61 |
| TSL | TRN | Sleeper Train |
| SU1 | SU1 | Sukhoi Superjet |
| SU7 | SU1 | Sukhoi Superjet 100-75 |
| SU9 | SU1 | Sukhoi Superjet 100-95 |
| S9S | SU1 | Sukhoi Superjet 100-95 (Saberlets) |
| TPT | TPT | Tecnam P2012 |
| T12 | TPT | Tecnam P2012 Traveller |
| PIG | CNA | Tooth's Duct Tape Wonder Pig |
| TRN | TRN | Train |
| TGV | TRN | Train A Grand Vitesse |
| TEE | TRN | Trans-Europe Express |
| T2F | T2F | Tupolev Tu-204 Freighter |
| T20 | T20 | Tupolev Tu-204/Tu-214 |
| T34 | T34 | Tupolev Tu-334 |
| TU3 | TU3 | Tupolev Tu134 |
| TU5 | TU5 | Tupolev Tu154 |
| ACD | ACD | Twin (Aero) Commander/Turbo/Jetprop |
| ACT | ACD | Twin (Aero) Turbo/Jetprop Commander |
| ACP | ACD | Twin Commander Aircraft |
| MA6 | MA6 | Xian Yunshuji MA-60 |
| M6F | M6F | Xian Yunshuji MA600 Freighter |
| YN7 | YN7 | Xian Yunshuji Y7/MA60 |
| YMC | YMC | Yakovelev MC-21 |
| YM3 | YMC | Yakovelev MC-21-300 Passenger |
| YK4 | YK4 | Yakovlev Yak-40 |
| YK2 | YK2 | Yakovlev Yak-42/142 |
| YN5 | YN5 | Yunshuji-5 |

