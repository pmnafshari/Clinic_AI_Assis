# Retention

> Demo template - see [README](README.md). The periods are placeholders in `retention.json`,
> chosen to exercise the code. A real clinic sets them with its adviser.

| Type | Kept for | On erasure | Sweep |
|---|---|---|---|
| Invoices | 3650 days | **held**, with the name and codice fiscale they were issued to | report only |
| Clinical records | 3650 days | erased | report only |
| Audit trail | 730 days | kept - surrogate ids only, paths redacted | deleted, and the purge is itself audited |
| Exports | 24 hours | deleted | deleted |
| Closed data requests | 1095 days | kept, with the patient's own words removed | deleted |

`python retention.py` reports what is past its period. `python retention.py --apply` deletes only
the types marked `sweep: delete`. Nothing clinical or fiscal is deleted by a timer - those are
counted for a person to decide on.

An erasure that meets a hold still runs: everything not held is removed, and the request records
which data was kept and why. The patient sees that reason on their profile.
