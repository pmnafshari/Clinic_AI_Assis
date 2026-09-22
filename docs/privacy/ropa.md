# Record of processing activities (draft)

> Demo template - see [README](README.md). Fields marked BLOCKED name a real controller, a real
> legal basis or a real contract, and cannot be filled in for a demo.

| Field | Entry |
|---|---|
| Controller | **BLOCKED** - the clinic, its legal name, address and contact |
| Data protection contact | **BLOCKED** |
| Purposes | patient care records; appointment scheduling; billing; patient portal self-service; answering patients' questions about their own records (portal assistant) |
| Categories of data subject | patients; clinic staff (accounts and audit trail) |
| Categories of data | identity (name, codice fiscale, phone); health data (visits, procedures, notes, x-rays); billing; appointments; consent records; access logs |
| Special categories | health data (GDPR art. 9) |
| Legal basis | **BLOCKED** - to be set by the adviser per purpose. The code does not assume consent is the basis for keeping clinical records; consent is recorded only for the portal assistant, messaging and call recording |
| Recipients | Cloudflare (tunnel) - see data-flow.md; no other recipient receives patient data |
| Transfers outside the EU | **BLOCKED** - depends on Cloudflare's terms and region |
| Retention | [retention.md](retention.md) - placeholders |
| Security measures | [keys-and-encryption.md](keys-and-encryption.md), README status table |
