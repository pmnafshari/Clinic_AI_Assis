# X-ray gate (P18)

**Decision: BLOCKED.** Nobody has decided GO or NO_GO, because the starting decision (D04: the X-ray
path and its intended use) has no owner yet. There is **no X-ray analysis in this code base**: images
are only filed and stored. Nothing here is a legal, regulatory, licensing or clinical ruling - those
come from the named specialists, from current official sources, at the time they decide.

The machine-checked record is `gate.json`; `python xray_gate.py check` validates it and scans the three
apps for any X-ray, radiograph, diagnosis or inference endpoint. `xray_gate.enabled()` stays False
unless the record says GO, every item is PASS with an owner, version, date and evidence, a decider has
signed, and the operator also sets `CLINIC_XRAY_ENABLED=1`. An agent cannot sign this record.

## 1. Intended use (P18.01) - to be filled by the owner and the clinical lead
Country of use; image modality (e.g. bitewing, periapical, panoramic, CBCT); who uses the output;
exactly what the output is (overlay, flag, score, text); what is claimed and what is explicitly not
claimed; the clinical owner; the responsible consultant. Every field is `TBC` today.

## 2. Questions for the regulatory specialist (P18.02)
- Given the intended use, is the software a medical device in the country of use, and if so which
  class and conformity route apply?
- Does the AI regulation in force classify it as high-risk, and which obligations follow for the
  clinic as deployer and for whoever places it on the market?
- What separates a research prototype, an internal pilot and clinical use here, and which documents
  does each need?
- What does data protection law require for processing radiographs (legal basis, DPIA, retention,
  transfers), and does the national authority add anything?
- Are there professional or radiation-protection rules on who may view and act on the output?

## 3. Questions per component, for licence review (P18.03)
For the **exact** framework, model weights, training dataset and every library, at the exact version:
the licence text; whether the intended deployment (local, inside the clinic, possibly commercial) is
permitted; obligations it triggers (source disclosure, attribution, network-use clauses, field-of-use
limits); whether the dataset's terms allow this use and redistribution of derived weights. No component
is chosen yet, so no licence has been reviewed; no general conclusion about any licence is made here.

## 4. Data protocol (P18.04, P18.T2)
Permission and legal basis before any real image is loaded; de-identification method and check;
ground truth by named dentists, with how disagreements are settled; **patient-level split** (all images
of one patient in one split) and no duplicate image across splits - `python xray_gate.py split-check
manifest.csv` audits a manifest of `image_sha256, patient_id, split` and fails on either; quality,
representativeness and annotation plan; who may access what.

## 5. Evaluation criteria, fixed before any result (P18.05, P18.T3)
Per finding (e.g. caries, bone loss, fracture), separately: the clinical threshold, acceptable false
negative and false positive rates, calibration, and the out-of-distribution behaviour. Recorded and
approved **before** any model output is seen; the validator refuses an evaluation PASS while its
criteria are not PASS. A confidence number alone is not a diagnostic criterion.

## 6. Risk register and DPIA addendum (P18.06) - open templates
| Risk | Effect | Control | Owner | Status |
|---|---|---|---|---|
| missed finding (false negative) | delayed treatment | mandatory human review; per-finding thresholds | TBC | open |
| false finding (false positive) | unnecessary treatment | human review; no automatic action | TBC | open |
| wrong patient's image | wrong record | image hash and patient id checked on every view | TBC | open |
| output read as a diagnosis | misuse | wording, labels, training | TBC | open |
| data leaves the machine | privacy breach | local processing only; no provider | TBC | open |
| model or licence change | invalid validation | version pinning; revalidation on change | TBC | open |

DPIA addendum: purpose, data, recipients, retention and risks of the X-ray processing, to be written
into `docs/privacy/dpia.md` once the intended use exists.

## 7. The decision
GO, NO_GO or BLOCKED, written and dated by the responsible persons and limited to a named environment
and use. Until then P19 does not start.
