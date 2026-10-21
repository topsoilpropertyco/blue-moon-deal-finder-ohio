# How to work the DRILL

**DRILL = Data Resolution through Incremental List Liquidation.** Most investors buy a list, skip trace it once, reach about 3% of owners and throw the rest away. The DRILL keeps going: every owner a source can't crack moves to the next source, until every owner on the list is reached or we know exactly why not.

The goal is **penetration**: the share of owners we have confirmed by talking to them. It shows at the top of the DRILL screen. The target is 100%. Every buy-box owner counts except do-not-contact ones; marking an owner exhausted never raises it.

## The three contact levels

| Level | Name | Means |
|---|---|---|
| 1 | No owner data | We don't have an owner name yet. |
| 2 | Possible owner | Records name an owner, but we haven't talked to them. (A property manager or agent alone doesn't count.) |
| 3 | Confirmed owner | We reached them and they confirmed they own it. It goes to the CRM. |

Records never move a property to level 3. Only a conversation does.

## The stages, in order

Free sources come first. Each property only moves down the list if the stage before didn't solve it.

| # | Stage | What you do | Cost |
|---|---|---|---|
| 1 | Records baseline | Nothing. The app reads the owner and mailing address from the Crexi file. | Free |
| 2 | Portfolio match | Nothing. The app groups properties with the same owner or mailing address, and shares contacts between them. | Free |
| 3 | County auditor | Open the parcel page (the app builds the link). Confirm the current owner and tax mailing address. | Free |
| 4 | Rental registration | Franklin and Montgomery show the owner's or agent's phone online. Elsewhere the app tells you who to ask. Counties with no registry skip this stage automatically. | Free |
| 5 | LLC and trust lookup | Search the LLC at the Secretary of State. Record the statutory agent and any named members. | Free |
| 6 | Recorder documents | Open the deed or mortgage. The person who signed for the LLC is usually the decision-maker. | Free |
| 7 | On-site and manager contacts | Call the leasing office or park office. Ask for the owner's contact or ask them to pass a message. | Free |
| 7b | Occupant and operator lookup | Warehouses and outdoor storage: find the business using the building or yard (trucking company, regulated facility) and ask who the landlord is. | Free |
| 8 | Web and social search | Search the person with the city. Check LinkedIn, Facebook, business sites. People-search sites by hand only. | Free |
| 9 | Crexi credit | Spend one of the month's 500 credits on this owner in Crexi. | Credit |
| 10 | Court and probate records | Estates, evictions (they name the owner's attorney), liens, foreclosures, obituaries. | Free |
| 11 | Letters | Mail the owner, the statutory agent and "Owner" at the property. | Postage |
| 12 | Neighbors and local network | Ask adjacent owners, brokers and managers. | Free |
| 13 | Paid skip trace | Off until you decide to pay for a vendor. | Paid |
| 14 | Deep research | Every angle on one record. What's left after this is truly unreachable. | Free |

Some stages only show when they fit, for example rental registration for apartments in Ohio, or the LLC lookup only for entity owners. When the export has no owner name, the owner you record (a person or an LLC) decides which stages fit. Links only show for the asset classes they fit.

## A normal session

The DRILL screen counts the owners of buy-box properties, the ones worth chasing. Switch the board to **Everyone** to work the set-aside properties too; the DRILL tab's number is the hands-on work waiting for buy-box owners.

1. **Reach out first.** The "Reach out" card at the top of the DRILL screen lists owners with a phone or email you haven't tested. Confirmations are what move penetration, so start there. Check phone numbers against the Do Not Call list first (the card links it). Log every try: no answer, left a message, replied but not confirmed yet, wrong person or number, bounced, talked to the owner and confirmed, or do not contact. Reached a manager or relative who will pass your message on? Log "Left a message" so the number stays usable.
2. **Work a stage queue.** Pick a stage and press "Work this". You get one owner at a time, best Blue Moon score first, with the right links already filled in. Open the links, then press **Found** (and type what you found), **Nothing** or **Skip**. The next owner loads.
   - Keys: F found, N nothing, R matches records (County auditor), X not applicable (saved), J or S pass for now (nothing saved), K previous, O open the first link.
3. **Spend credits.** The Credits screen shows how many are left this month and the owners they should go to. Credits don't roll over, so the plan spends all of them at a steady daily pace, starting with owners the free stages couldn't crack, then the highest expected value (Blue Moon score x the credit's hit rate for the owner type). One plan covers every market, because the credits belong to the Crexi account. Owners with an untested phone or email wait in Reach out first. If the possible owners can't use every credit, the rest go to buy-box owners with no name yet, then the best owners just outside the buy box (`crexi: plan_fallbacks` in `config.yaml`).

## Rules the app enforces

- "Do not contact" is permanent. It spreads to that phone, email or mailing address everywhere, and to the owner's other properties (same owner name or portfolio). An outcome can never undo it; only "Allow contact again" on a contact takes it off the list, on every property at once. Undoing do-not-contact on a property works one property at a time.
- A wrong number or email is marked wrong and the drill keeps going.
- When every stage has been tried, the property is marked exhausted. Any new information reopens it. A new owner of record (a sale) starts a new pass for the new owner, and takes back a confirmation of the old owner.
- Crexi and people-search sites are never automated. The app only opens pages for you.
- Calls and texts to cell phones fall under telemarketing rules (TCPA and the Do Not Call list). Check numbers before calling, and call between 8 a.m. and 9 p.m. the owner's time.

## How it learns

Every Found and Nothing you record becomes a hit rate per stage and owner type. The DRILL screen shows your real hit rate next to the starting guess. Over time this shows which sources actually work for which owners (for example, the Secretary of State for LLCs, the auditor for trusts), and the app acts on it: once a free hands-on stage has 10 results for an owner type (`advice_min_tries`), it trades places with stages of the same goal by blended hit rate, so the most productive stage comes first for that owner type ("moved up by your results"). Automatic, credit, paid and manual stages never move. The credit plan uses the Crexi credit's hit rate per owner type the same way. Turn it off with `learn_order: false` in `config/drill.yaml`.
