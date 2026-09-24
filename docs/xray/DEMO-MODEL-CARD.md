# Demo model card - X-ray demo (P19)

**This is not a model and not a medical device.** It is a demonstration of the pipeline a clinical
X-ray aid would need, run on synthetic images. D04 (owner, 2026-09-24): non-clinical demo only;
clinical approval pending (`gate.json`: DEMO_GO / CLINICAL_APPROVAL_PENDING). Off by default.

| | |
|---|---|
| Name / version | `demo-marks-threshold-1` (detector), `clinic-xray-demo-synth-1` (generator) |
| What it does | finds the bright (`demo_mark_a`) and dark (`demo_mark_b`) squares the generator painted into a noisy gray image |
| What it does not do | anything about teeth, bone, caries or any condition; no diagnosis, no treatment advice, no confidence number |
| Input | only PNGs made by the generator (8-bit grayscale, 64-512 px per side, under 256 KB, tag and pixel digest intact); every other image - including any real radiograph - is refused |
| Output | demo marks and an overlay on the same image at the same scale, each labelled "DEMO OUTPUT ... Not a finding, not a diagnosis, not for clinical use" |
| Abstains when | the image is too flat to read, the detector fails, or it takes longer than 5 s |
| Training data | none: a fixed threshold rule, nothing learned |
| Evaluation | a locked synthetic benchmark (40 images, 20 synthetic patients, split by patient, criteria hashed before the run); image-level sensitivity, specificity and precision with Wilson 95% intervals per mark kind, abstentions counted separately, subgroups by noise level. Calibration: not applicable (no confidence) |
| Result | perfect on its own synthetic marks (e.g. sensitivity 1.0, 95% CI 0.65-1.0 at n=7) - **expected and meaningless clinically**: the rule matches exactly the values the generator paints |
| Human review | a dentist may record accept / reject / correct; append-only, audited; nothing reaches any patient record |
| Limitations | synthetic images only; the tag stops accidental real images, not a deliberate forgery; no web page; tiny benchmark |
| Clinical use | **not permitted.** Requires P18.01-P18.06 (intended use, clinical owner, consultant, regulatory and licence review, data permission, criteria) and a written GO |

Run: `CLINIC_XRAY_DEMO=1 .venv/bin/python xray_demo.py demo --out <folder>`.
