# How to work the DRILL

**DRILL = Data Resolution through Incremental List Liquidation.** Most investors buy a list, skip trace it once, reach about 3% of owners and throw the rest away. The DRILL keeps going: every owner a source can't crack moves to the next source, until every owner on the list is reached or we know exactly why not.

The goal is **penetration**: the share of owners we have confirmed by talking to them. It shows at the top of the DRILL screen. The target is 100%.

## The three contact levels

| Level | Name | Means |
|---|---|---|
| 1 | No owner data | We don't have an owner name yet. |
| 2 | Possible owner | Records name an owner, but we haven't talked to them. |
| 3 | Confirmed owner | We reached them and they confirmed they own it. It goes to the CRM. |

Records never move a property to level 3. Only a conversation does.

## The stages, in order

Free sources come first. Each property only moves down the list if the stage before didn't solve it.

| # | Stage | What you do | Cost |
|---|---|---|---|
| 1 | Records baseline | Nothing. The app reads the owner and mailing address from the Crexi file. | Free |
| 2 | Portfolio match | Nothing. The app groups properties with the same owner or mailing address, and shares contacts between them. | Free |
| 3 | County auditor | Open the parcel page (the app builds the link). Confirm the current owner and tax mailing address. | Free |
| 4 | Rental registration | Franklin and Montgomery show the owner's or agent's phone online. Elsewhere the app tells you who to ask. | Free |
| 5 | LLC and trust lookup | Search the LLC at the Secretary of State. Record the statutory agent and any named members. | Free |
| 6 | Recorder documents | Open the deed or mortgage. The person who signed for the LLC is usually the decision-maker. | Free |
| 7 | On-site and manager contacts | Call the leasing office or park office. Ask for the owner's contact or ask them to pass a message. | Free |
| 8 | Web and social search | Search the person with the city. Check LinkedIn, Facebook, business sites. People-search sites by hand only. | Free |
| 9 | Crexi credit | Spend one of the month's 500 credits on this owner in Crexi. | Credit |
| 10 | Court and probate records | Estates, evictions (they name the owner's attorney), liens, foreclosures, obituaries. | Free |
| 11 | Letters | Mail the owner, the statutory agent and "Owner" at the property. | Postage |
| 12 | Neighbors and local network | Ask adjacent owners, brokers and managers. | Free |
| 13 | Paid skip trace | Off until you decide to pay for a vendor. | Paid |
| 14 | Deep research | Every angle on one record. What's left after this is truly unreachable. | Free |

Some stages only show when they fit, for example rental registration for apartments in Ohio, or the LLC lookup only for entity owners.

## A normal session

1. **Reach out first.** The "Reach out" card at the top of the DRILL screen lists owners with a phone or email you haven't tested. Confirmations are what move penetration, so start there. Log every try: no answer, left a message, wrong person, bounced, talked to the owner and confirmed, or do not contact.
2. **Work a stage queue.** Pick a stage and press "Work this". You get one owner at a time, best Blue Moon score first, with the right links already filled in. Open the links, then press **Found** (and type what you found), **Nothing** or **Skip**. The next owner loads.
   - Keys: F found, N nothing, S skip, J/K next/previous, O open the first link.
3. **Spend credits.** The Credits screen shows how many are left this month and the owners they should go to. Credits don't roll over, so the plan spends all of them at a steady daily pace, starting with owners the free stages couldn't crack.

## Rules the app enforces

- "Do not contact" is permanent and spreads to that number everywhere.
- A wrong number or email is marked wrong and the drill keeps going.
- When every stage has been tried, the property is marked exhausted. Any new information reopens it.
- Crexi and people-search sites are never automated. The app only opens pages for you.
- Calls and texts to cell phones fall under telemarketing rules (TCPA and the Do Not Call list). Check numbers before calling, and call between 8 a.m. and 9 p.m. the owner's time.

## How it learns

Every Found and Nothing you record becomes a hit rate per stage and owner type. The DRILL screen shows your real hit rate next to the starting guess. Over time this shows which sources actually work for which owners (for example, the Secretary of State for LLCs, the auditor for trusts), so you work the most productive stages first.
