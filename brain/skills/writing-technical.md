# Writing: Technical

Technical writing fails when it is written for the person who already knows how the system works. The person who already knows doesn't need the document. The document is for the person who doesn't know — and that person's experience of the document is the only test that matters. The test is simple: can a reader with the assumed knowledge execute the task using only this document? If they cannot, something is missing.

This is a higher bar than it sounds. Technical writers who know the system well have difficulty seeing the gaps — because for them, nothing is missing. The prerequisite knowledge is so obvious it doesn't seem worth stating. The step that requires understanding the system's mental model doesn't look like a step — it looks like common sense. These invisible assumptions are where most documentation fails.

The five failure modes in technical writing:

**Audience miscalibration:** Assumes knowledge the reader doesn't have, or over-explains what the reader already knows. Both are wrong. The first makes the document unusable; the second makes it condescending.

**Incompleteness:** Steps that require bridging knowledge the document doesn't provide. Often invisible to the writer because the knowledge is automatic.

**Sequence errors:** Prerequisites stated after they are needed, steps in the wrong order, troubleshooting information buried after the task it applies to.

**Precision failures:** Terms used inconsistently, ambiguous instructions ("configure the settings" — which settings?), passive voice that obscures who does what ("the file should be saved" — by whom, when, where?).

**Missing examples:** Non-obvious steps explained only in the abstract, without a concrete example that shows the reader what the correct output looks like.

---

## Your Process

**Step 1: Audience Profile**
Who is the reader? What do they already know? What do they not know? What is their goal — and what is the fastest path from their current knowledge to task completion? Is the documentation calibrated to this profile, or to a different one (a more advanced user, or a more novice one)?

**Step 2: Completeness Audit**
Read the documentation as if you are the assumed reader and have only their assumed knowledge. Walk through each task:
- Are all prerequisites stated before they are needed?
- Is there any step that requires knowledge not provided in this document or in the stated prerequisites?
- Are error states covered? (What does the reader do if the expected result doesn't happen?)
- Are edge cases relevant to the assumed reader addressed?

Flag every gap: name the missing knowledge, identify where in the sequence the reader would encounter it.

**Step 3: Sequence Audit**
Check the order of information:
- Are prerequisites stated before the tasks that require them?
- Are steps in chronological order?
- Is troubleshooting information positioned near the steps it applies to, not collected at the end?
- Is the most important information (the task itself) reached without having to navigate through extensive context?
