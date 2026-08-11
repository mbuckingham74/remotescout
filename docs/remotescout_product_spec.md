# RemoteScout

## Product Specification

### 1. Purpose

RemoteScout is a personal job recommendation and application-tracking tool for finding high-quality remote positions.

The problem RemoteScout solves is not a lack of job listings. Aggregate job sites can expose a large number of opportunities, but manually reviewing them creates too much noise and makes it difficult to focus on actually applying.

RemoteScout searches broadly but presents narrowly.

Each day, it identifies remote job postings from aggregate sources, removes clearly irrelevant positions, evaluates the remaining positions against the candidate's résumé, and presents only the **three strongest recommendations**.

Aggregate job sites are used for **discovery only**. RemoteScout should locate and link to the corresponding job posting on the actual employer's website before recommending a position. Applications are always completed manually on the employer's site.

After applying, the candidate marks the recommendation as **Applied**. It moves into the application tracker and must not be recommended again.

---

## 2. Core Principles

### Three recommendations, not a job feed

RemoteScout presents a maximum of **three recommended positions per day**.

The system may discover and evaluate many more positions internally, but those candidates are not exposed as a backlog for the candidate to review.

The purpose of RemoteScout is to make the selection decision manageable, not to create another large list of jobs.

The expected daily workflow is:

1. Open RemoteScout.
2. Review the three recommendations.
3. Apply manually to the appropriate positions.
4. Mark completed applications as Applied.
5. Leave RemoteScout.

### Quality over quantity

Three mediocre jobs are not better than one strong job.

Jobs must meet a minimum recommendation threshold before they can appear. If only one or two positions meet that threshold, RemoteScout shows only those positions. If none qualify, an empty recommendation list is valid.

### Aggregate sites are discovery sources

Aggregate job sites provide broad coverage and are useful for discovering opportunities.

They are not the desired application destination.

RemoteScout should attempt to locate the same position on the hiring company's own careers site and use that employer-owned posting as the canonical application link.

A recommendation should not direct the candidate to apply through an aggregate job site when an employer-owned posting can be identified.

### The résumé defines fit

Recommendations are based primarily on how well the requirements and responsibilities of a position match the candidate's actual résumé and experience.

RemoteScout should prefer genuinely strong matches over positions that merely contain matching keywords.

### Applied means do not recommend again

Once the candidate marks a position Applied, RemoteScout must exclude that position from future recommendations.

The same position may appear on multiple aggregate sites. Those listings should be treated as representations of the same employment opportunity whenever they can reasonably be identified as such.

---

## 3. Scope

RemoteScout has two primary user-facing functions:

1. **Daily Recommendations**
2. **Application Tracker**

Everything else exists to support those two functions.

---

# 4. Job Discovery

RemoteScout discovers currently available remote positions from one or more aggregate job sources.

The discovery layer should favor broad coverage rather than attempting to make fine-grained recommendation decisions itself.

For each discovered position, RemoteScout should obtain as much of the following information as the source provides:

- Job title
- Employer
- Location / remote designation
- Job description
- Compensation, when available
- Date posted, when available
- Aggregate source
- Source listing URL

Discovery sources may overlap. The same position appearing through multiple sources should not become multiple recommendations.

RemoteScout is specifically for **remote employment**. Positions that clearly require regular on-site or hybrid attendance are not eligible.

---

# 5. Filtering

Filtering removes positions that are clearly inappropriate before recommendation scoring.

Its purpose is straightforward: RemoteScout should not spend recommendation effort evaluating jobs that plainly have nothing to do with the candidate's background or requirements.

Examples include:

- Unrelated occupations
- Clearly inappropriate job families
- Clearly incompatible seniority
- On-site or hybrid positions
- Geographic restrictions that make the candidate ineligible
- Other explicit requirements that clearly make the position impossible or inappropriate

Filtering should be conservative where the answer is ambiguous.

A position that is obviously irrelevant should be rejected. A position whose suitability requires judgment should proceed to scoring.

Filtering and scoring serve different purposes:

**Filtering:** Could this reasonably be a position for this candidate?

**Scoring:** How good a match is this position compared with the other plausible positions?

---

# 6. Recommendation Scoring

Positions that survive filtering are evaluated against the candidate's résumé.

Each position receives a numerical recommendation score using a consistent scale.

The scoring process should consider the substance of the position, including factors such as:

- Match between required experience and demonstrated experience
- Match between responsibilities and previous work
- Relevant technical knowledge
- Product, program, or operational experience
- Seniority
- Leadership expectations
- Industry or domain relevance where meaningful
- Remote/location compatibility
- Important requirements that are absent from the résumé

The scorer should distinguish between:

- Experience clearly demonstrated by the résumé
- Experience that is reasonably transferable
- Requirements for which there is little or no supporting evidence

A high score should mean that the position is genuinely worth spending time applying to.

The score is a ranking mechanism, not a prediction that the candidate will receive an interview or offer.

Each scored recommendation should also contain a short explanation of why the position is or is not a particularly strong fit.

A configurable minimum score determines whether a position is eligible for recommendation.

Positions below that threshold are not shown.

---

# 7. Employer Posting Resolution

Aggregate listings are discovery leads.

Before a high-ranking candidate becomes a recommendation, RemoteScout should attempt to locate the corresponding position on the employer's own careers site or employer-controlled applicant tracking system.

The resolution process should verify, as reasonably possible, that:

- The employer matches.
- The position matches.
- The posting is still available.
- The resulting URL is an employer-controlled application destination rather than another aggregate listing.

The employer posting becomes the canonical URL for the position.

If a promising aggregate listing cannot be confidently matched to an employer posting, RemoteScout should not invent or guess an application URL.

Employer-site resolution should be concentrated on positions likely to become recommendations rather than performed unnecessarily for every discovered listing.

---

# 8. Daily Recommendations

The primary RemoteScout screen is a simple table containing the day's recommendations.

RemoteScout selects the **three highest-scoring eligible positions** after:

- Filtering irrelevant jobs
- Excluding positions below the recommendation threshold
- Excluding positions already marked Applied
- Deduplicating the same opportunity across sources
- Confirming an appropriate employer application destination

Example:

| Score | Company | Position | Fit | Apply | Applied |
|---:|---|---|---|---|---|
| 94 | Example Co. | Senior Product Manager | Excellent match to product and technical delivery experience | Employer Site | ☐ |
| 91 | Acme | Technical Program Manager | Strong technical and cross-functional program fit | Employer Site | ☐ |
| 87 | Widget Corp. | Product Manager | Strong product ownership match with minor domain gaps | Employer Site | ☐ |

The interface should make it easy to:

- See the score
- Understand briefly why the job was recommended
- Open the real employer posting
- Mark the position Applied

The normal interface does **not** need to expose all discovered jobs, rejected jobs, or every job that received a score.

RemoteScout's job is to perform that reduction.

---

# 9. Applying

RemoteScout does not submit applications.

The candidate follows the employer application link and completes the application manually.

After completing the application, the candidate checks **Applied** for that recommendation.

Marking Applied:

1. Records that the candidate applied to the position.
2. Records the application date.
3. Removes the position from the recommendation view.
4. Adds the position to the application tracker.
5. Makes the position ineligible for future recommendation.

The system must consult application history when generating future recommendations.

An already-applied position must not reappear simply because it is rediscovered through the same or a different aggregate source.

---

# 10. Application Tracker

RemoteScout includes a deliberately simple tracker for positions the candidate has actually applied to.

The tracker is not a CRM or recruiting platform.

At minimum it shows:

| Company | Position | Applied | Status | Notes |
|---|---|---|---|---|
| Example Co. | Senior Product Manager | Aug 11 | Applied | |
| Acme | Technical Program Manager | Aug 8 | Interview | Second interview Aug 18 |

A tracked application may have statuses such as:

- Applied
- Screen
- Interview
- Offer
- Rejected
- Withdrawn
- Ghosted

The candidate can add notes and update the application as the hiring process progresses.

---

# 11. Application History

RemoteScout retains the meaningful history of an application rather than only its latest status.

For example:

```text
Example Co. — Senior Product Manager

Aug 11    Applied
Aug 15    Recruiter screen
Aug 22    Interview
Aug 29    Rejected
```

Application history is primarily for the candidate's own tracking and reference.

The recommendation engine does not need sophisticated interpretation of this history. For recommendation exclusion, the important fact is simply that an application was made for the position.

---

# 12. Job Identity and Duplicate Protection

RemoteScout must make a reasonable effort to recognize the same employment opportunity when it appears more than once.

Useful identity evidence may include:

- Employer
- Job title
- Employer posting URL
- Employer or ATS job identifier
- Location
- Posting content

The goal is practical rather than forensic: avoid presenting duplicate recommendations and, especially, avoid recommending a position that the candidate has already applied to.

An aggregate site changing its URL or another aggregate site publishing the same position must not by itself make that position eligible again.

---

# 13. Résumé

RemoteScout uses the candidate's current résumé as the primary candidate profile for recommendation scoring.

The initial product does not require:

- Automated résumé rewriting
- Résumé generation
- Résumé version provenance
- Automatic tailoring
- Multiple candidate profiles

Updating the résumé should be simple. Subsequent recommendations use the current résumé.

Historical applications do not need to be rescored when the résumé changes.

---

# 14. Daily Behavior

A normal RemoteScout cycle is:

```text
Discover remote jobs
        ↓
Normalize / deduplicate
        ↓
Filter obvious non-fits
        ↓
Exclude already-applied positions
        ↓
Score plausible jobs against résumé
        ↓
Discard scores below threshold
        ↓
Rank remaining candidates
        ↓
Resolve leading candidates to employer postings
        ↓
Select up to the best 3 verified positions
        ↓
Present recommendations
        ↓
Candidate applies manually
        ↓
Candidate marks Applied
        ↓
Application moves to tracker
```

The candidate should not need to manage the intermediate pipeline.

---

# 15. Non-Goals

RemoteScout is intentionally not:

- A general-purpose job board
- An application submission bot
- A résumé generator
- A résumé tailoring system
- A recruiting CRM
- A company research platform
- An outreach automation system
- A job-market analytics platform
- An autonomous career agent
- A comprehensive archive of every discovered job
- A system that exposes hundreds of possible matches for manual review

RemoteScout does not optimize for the number of jobs found.

It optimizes for the quality of the **three jobs presented to the candidate each day**.

---

# 16. Success Criterion

RemoteScout succeeds when the candidate can open it each day and receive **up to three new, high-quality remote positions that are strong matches for the résumé, have legitimate employer-owned application destinations, and have not already been applied to.**

The desired user experience is:

> Open RemoteScout.  
> See three good jobs.  
> Apply to them on the employers' sites.  
> Mark them Applied.  
> Track what happens.  
> Come back tomorrow.

Everything that does not materially improve that workflow is secondary.
