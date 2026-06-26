import re
import sys
import unittest

CF_REGEX = re.compile(r'^[A-Z]{4}[0-9]{12}$')


class TestDentalNoteV2(unittest.TestCase):

    def test_valid_note_has_clinical_notes_default(self):
        from dental_notes_schema import DentalNote
        n = DentalNote(patient_name="mario rossi", codice_fiscale="MRRS123456789012")
        self.assertEqual(n.clinical_notes, "")

    def test_valid_note_has_next_appointment_default(self):
        from dental_notes_schema import DentalNote
        n = DentalNote(patient_name="mario rossi", codice_fiscale="MRRS123456789012")
        self.assertIsNone(n.next_appointment)

    def test_no_notes_text_field(self):
        from dental_notes_schema import DentalNote
        n = DentalNote(patient_name="x", codice_fiscale="MRRS123456789012")
        self.assertFalse(hasattr(n, "notes_text"))

    def test_bad_cf_raises(self):
        from dental_notes_schema import DentalNote
        with self.assertRaises(Exception):
            DentalNote(patient_name="x", codice_fiscale="bad")

    def test_cf_too_short_raises(self):
        from dental_notes_schema import DentalNote
        with self.assertRaises(Exception):
            DentalNote(patient_name="x", codice_fiscale="MRRS1234")

    def test_cf_lowercase_raises(self):
        from dental_notes_schema import DentalNote
        with self.assertRaises(Exception):
            DentalNote(patient_name="x", codice_fiscale="mrrs123456789012")

    def test_invoice_has_amount_and_description(self):
        from dental_notes_schema import Invoice
        inv = Invoice(amount=100.0, description="rct 26")
        self.assertEqual(inv.amount, 100.0)
        self.assertEqual(inv.description, "rct 26")


class TestCfGenerator(unittest.TestCase):

    def test_make_cf_normal_name(self):
        from cf_generator import make_cf
        cf = make_cf("Mario", "Rossi")
        self.assertRegex(cf, r'^[A-Z]{4}[0-9]{12}$')

    def test_make_cf_prefix_mario_rossi(self):
        # Mario -> consonants M, R; Rossi -> R, S -> prefix MRRS
        from cf_generator import make_cf
        cf = make_cf("Mario", "Rossi")
        self.assertTrue(cf.startswith("MRRS"), f"expected prefix MRRS, got {cf}")

    def test_make_cf_vowel_heavy_name(self):
        from cf_generator import make_cf
        cf = make_cf("Ava", "Li")
        self.assertRegex(cf, r'^[A-Z]{4}[0-9]{12}$')

    def test_make_cf_vowel_heavy_prefix(self):
        # Ava -> consonants: V; pad with vowels A,A -> VA
        # Li -> consonants: L; pad with vowels I -> LI
        from cf_generator import make_cf
        cf = make_cf("Ava", "Li")
        self.assertTrue(cf.startswith("VALI"), f"expected prefix VALI, got {cf}")

    def test_make_cf_uniqueness(self):
        from cf_generator import seed_cf, make_cf
        seed_cf(0)
        cfs = [make_cf("Marco", "Rossi") for _ in range(20)]
        self.assertEqual(len(set(cfs)), 20)

    def test_seed_reproducibility(self):
        from cf_generator import seed_cf, make_cf
        seed_cf(99)
        cf1 = make_cf("Marco", "Rossi")
        seed_cf(99)
        cf2 = make_cf("Marco", "Rossi")
        self.assertEqual(cf1, cf2)

    def test_y_is_consonant(self):
        # Yves -> Y is consonant, v is consonant -> prefix YV
        from cf_generator import make_cf
        cf = make_cf("Yves", "Ay")
        # Yves: consonants Y, V -> YV
        # Ay: consonants Y -> pad with vowel A -> YA
        self.assertTrue(cf.startswith("YVYA"), f"expected YVYA, got {cf}")


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2)
    sys.exit(0 if result.result.wasSuccessful() else 1)
