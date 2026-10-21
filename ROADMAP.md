# Roadmap: Blue Moon Deal Finder

The tool's job: **find the owner and confirm we can reach them.** It ranks properties so the 500 monthly Crexi credits go to the best ones, then runs the DRILL: it layers data source after source until every target property has a verified way to reach its owner. Conversations happen in the separate CRM, not here.

Status (v1.0): phases 1-4 are built and tested on a real Crexi export. Phase 5 is next. Google has not been called live yet (no key in the build environment), and the Claude photo check has only run on a test image: first real runs should use **Test on 5**.

## Contact levels

| Level | Name | Means | The drill's next move |
|---|---|---|---|
| 1 | No owner data | No owner name at all | Find a name: county auditor, recorder, Secretary of State |
| 2 | Possible owner | We have owner data from records, but haven't talked to them | Find a phone, email or social account (free sources first, then a Crexi credit) and reach out |
| 3 | Confirmed owner | We made contact and they confirmed they own it | **Hand off to the CRM.** The drill is done. |

Only talking to the owner confirms them; records alone never do. Contact info that is found but not yet tested shows as a yellow channel badge on a Level 2 property.

Side states: **Wrong info** (drops back a level and marks that source bad for this property), **Exhausted** (letters only), **Do not contact** (permanent).

Levels are tracked per channel: mail, phone, text, email, social, voicemail drop. The property's level is its best channel. **Penetration** = share of target properties at Level 3 (confirmed). Goal: 100%.

## Phases

### 1. Foundation (built)
- Import Crexi exports into one master list on the Mac Mini; merge duplicates.
- Brad's buy box: 10–80 units (sweet spot 20–50), within 60 minutes of Springfield, built 1970+, outdoor access preferred.
- Size from units + sq ft, then units, then sq ft, then unknown.
- Drive time and HUD rent fit.
- Ranked list exported to Google Sheets.

### 2. DRILL and contact levels (built)
- Contact levels above, per property, with channel badges.
- Free sources first: Ohio Secretary of State, county auditor and recorder, web search.
- Credit planner: credits don't roll over, so spend all 500 each month on the top-ranked possible owners (Level 2) after free sources.
- Penetration dashboard and drill timeline per property.
- Do Not Call flags on phone numbers.

### 3. Walk-up photo check (built; first live run pending)
- Street View + satellite photos, read by AI on the Claude subscription.
- Review queue for unsure results; corrections become examples.

### 4. Learning loops (built)
- Hit rate of each data source by owner type, to find the cheapest path to each kind of owner.
- Blue Moon score weights tuned from real outcomes and past deals (backtesting).

### 5. Owner profiles and archetypes (next)
Build a profile of the people behind each property from everything gathered, and group owners into archetypes to guide outreach. Each archetype gets:
- **Guesses about them:** what the records suggest about their situation.
- **Values:** what they likely care about in a sale.
- **Messaging:** how to tailor the first message, and which channel to lead with.

Starting hypotheses, to be tested against real conversations:

| Archetype | Signals | Likely values | Message angle |
|---|---|---|---|
| Long-time local owner | Individual or trust, owned 20+ years, no mortgage, local mailing | Legacy, simplicity, a fair and respectful buyer | Easy, certain exit on their timeline; seller financing as an option |
| Distant heirs | Trust or estate, out-of-state mailing, recent transfer | Less hassle, speed, splitting proceeds cleanly | We handle everything; quick, clean close |
| Tired self-manager | Small LLC owning 1–2 properties, signs of wear or vacancy | Relief from tenants and repairs | Take the work off their plate |
| Small portfolio operator | Owns 5–20 properties, active loans | Numbers, speed, certainty of close | Direct, numbers-first, proof we close |
| Institution | REIT, fund, 40+ properties | Process and pricing models | Low priority; rarely sells at a Blue Moon price |

Guardrail: profiles use only property, business and public-record facts relevant to a sale. No guessing about health, religion, ethnicity or other personal traits.

### Later
- FY2027 HUD rents once HUD's free map service posts them (or download HUD's files into data/hud and set `rent.source: hud_file`).
- Census rent by bedroom as a second rent check (free key).
- Do Not Call list import (area-code files from telemarketing.donotcall.gov).
- Blue Moon strike price from Brad's spreadsheet (separate buildout).
- More buy boxes for wholesale buyers, scored on the same list.
- Distress layers: tax delinquency, code violations.
