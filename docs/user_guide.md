# User guide: Donegal house price lookup

Release `rel-229d61affb29`, published 7 October 2026. Register data runs to 24 September
2026.

## What it does

You type a Donegal address. The app shows every sale of that home recorded in the public
Property Price Register. It then brings a past sale price forward to a later year, using
how Donegal prices moved in between:

> **estimate = past sale price × (Donegal index in the later year ÷ index in the sale year)**

Every estimate comes with a 90% range. When the evidence is too thin to give a fair
number, the app says why instead of showing one.

**It is not a valuation.** It cannot see extensions, renovation, condition, views or
anything else that makes one home different. It answers one question: *"If this home had
moved exactly with the Donegal market, what would its last price be now?"*

## Who uses it, and for what

| You are | Your question | How to use it | What to keep in mind |
|---|---|---|---|
| **A homeowner** | How has my home moved since I bought it? | Search your address; anchor on your purchase; choose the latest year | Changes you made to the home are not in the number |
| **A buyer** | Is the asking price in line with the market since it last sold? | Anchor on the last sale and compare the estimate with the asking price | A big gap may be renovation or a hot street, not overpricing; ask why |
| **A seller** | What would the market alone suggest? | The same, as a starting point for a conversation with an agent | Agents see condition and demand; the index does not |
| **A local reader** (journalist, councillor, student) | How have Donegal and Letterkenny prices moved? | Use the dashboard, not the lookup | Look at the shaded band and the suppressed years |

### Example (real register data, release `rel-229d61affb29`)

*26 Maginn Avenue, Buncrana*:
- **Last comparable sale:** €80,000 in February 2016.
- **Brought to 2025:** Donegal prices rose 2.20× between 2016 and 2025, giving an
  estimate of **€176,300** (90% range €74,700 to €320,100).
- **What happened:** the home actually resold for €270,000 in December 2024, inside the
  range but well above the estimate. That gap is what renovation or local demand looks
  like: the index cannot know about it, and the range is wide for exactly that reason.

## How to get access

1. Ask the workspace admin to invite you to the Databricks workspace. You sign in with your
   own account; nobody uses a shared login.
2. The admin grants you **Can use** on the app `ireland-rsi-lookup-donegal`, and view
   access to the dashboard *Ireland repeat-sales index (donegal)*.
3. Open the app link and sign in.

On Databricks Free Edition the app stops after 24 hours, so the admin may need to start it
(Apps → `ireland-rsi-lookup-donegal` → Start). There is no anonymous public access.
Readers without an account can use the release report PDF.

## Using the lookup, step by step

1. **Address:** type part of it, e.g. `19 Ard Colmcille Letterkenny`. Abbreviations such
   as *Apt*, *Rd* and *Co* are understood.
2. **Property:** if several homes match, pick yours. The app never guesses between them.
3. **Sale history:** every recorded sale, with the price as filed. New homes also show a
   VAT-adjusted price. The *Anchor* column says whether each sale can be used to start an
   estimate, and why not if it can't (e.g. *not a full market price*).
4. **Anchor sale:** defaults to the most recent usable sale; you can pick another.
5. **Target year:** defaults to the latest published year.
6. **Read the result:**
   - **Estimate and 90% range:** nine times in ten, a home that tracked the market sold
     inside this range in testing.
   - **Index change:** how much Donegal prices moved between the two years.
   - **Notes:** which index was used, whether a year is *provisional*, and which sale was
     the anchor.
7. **Chart:** the Donegal index with its own shaded band. That band is the uncertainty of
   the *index*, which is much narrower than the range for a single home.

## When there is no estimate

| Message | What it means | What you can do |
|---|---|---|
| No property matches | The address is not in the register (no recorded sale since 2010) | Try fewer words or a different spelling |
| No eligible anchor | Every sale is non-market, a bulk filing or has an unresolved VAT basis | Nothing; the sale facts are still shown |
| Unresolved VAT | A new-build price whose VAT basis cannot be established | Choose a different sale as the anchor |
| Unsupported period | The sale or target year is suppressed (e.g. 2011-2012 had too few resales to measure reliably) | Choose a different anchor or target year |
| Uncalibrated interval | The estimate for that holding period failed its accuracy test | None currently; all three holding bands pass |

## Questions

**Why Donegal and not Letterkenny for my Letterkenny home?** Letterkenny has its own
published index, but in testing it predicted Letterkenny resales *less* accurately than the
Donegal index (median error 28.8% vs 24.2%). Estimates therefore use Donegal.

**What does *provisional* mean?** The newest year (2026) is incomplete, and late filings
will revise it.

**How often is it updated?** Monthly, on the 10th. Each update is a new release, and old
releases are kept so results can be compared.

**How accurate is it?** On 446 later sales it had never seen, the typical error was 25.6%,
against 37.8% for the old price unadjusted. Most of the remaining error is about the
individual home, not the market.

**Who can see my searches?** The app does not save them. The SQL warehouse's query
history, which workspace admins can see, records the queries the app runs. The app shows
only public register facts (address as filed, date, price) and never any person's name.
