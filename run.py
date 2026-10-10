import clinic_time
import codice_fiscale
import disk_guard
import documents
import drive_source
import upload_worker
from app import create_app


def main(port=5000):
    """The staff app: `python run.py` as always (5000); the Clinic Workspace demo backend calls it with its own port,
    in its own data folder (P28). Importing this module starts nothing."""
    app = create_app()
    # before resume_pending, not merely before app.run: that sweep reads and moves
    # filed patient notes, so an unencrypted host must be refused ahead of it, not
    # after it has already processed a backlog. after create_app for the same
    # reason patient_run.py gives - construction is the cheaper failure and the two
    # errors should not mask each other.
    disk_guard.guard_or_exit()
    codice_fiscale.guard_or_exit("db/clinic.sqlite")
    # P05: stored datetimes are naive clinic-local. a row carrying an offset was
    # written under a different contract, and shifting it silently moves
    # appointments by an hour - so refuse instead of guessing.
    clinic_time.guard_or_exit("db/clinic.sqlite")
    # anything left in drop/ by a previous run was uploaded but never processed
    upload_worker.resume_pending()
    # a crash between a document's file and its row leaves one without the other.
    # reported here, never repaired here: `python documents.py --reconcile --apply`
    documents.report_at_startup("db/clinic.sqlite")
    # DRV: the Drive source's automatic check (off until a dentist turns it on; one check at a time)
    if drive_source.FOLDER_ID:
        drive_source.start_auto_thread("db/clinic.sqlite")
    # 5000 by default - tunnel_guard.STAFF_PORT mirrors it, change both together
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
