import sys
import tempfile
from pathlib import Path

import chromadb
from chromadb.config import Settings

# verification, not a production module - re-runs the architecture doc's
# §3.4 five-row where-clause table against whatever chromadb version is
# actually pinned, rather than trusting the table's own age. the MVP
# patient accessor ships zero Chroma-querying functions (D-08/D-10's
# resolution), so this file is the evidence for that choice, not a module
# any request path calls.


def _pinned_version():
    text = Path("requirements.txt").read_text()
    for line in text.splitlines():
        if line.startswith("chromadb=="):
            return line.split("==", 1)[1].strip()
    raise AssertionError("no chromadb== pin found in requirements.txt")


def selftest():
    with tempfile.TemporaryDirectory() as tmp:
        client = chromadb.PersistentClient(
            path=tmp, settings=Settings(anonymized_telemetry=False)
        )
        collection = client.get_or_create_collection(name="patient_notes")

        cf_a = "AAAA800010150100"
        cf_b = "BBBB850315150200"

        collection.upsert(
            ids=["a1", "a2", "a3"],
            documents=["a note one", "a note two", "a note three"],
            metadatas=[{"codice_fiscale": cf_a}] * 3,
        )
        collection.upsert(
            ids=["b1", "b2"],
            documents=["b note one", "b note two"],
            metadatas=[{"codice_fiscale": cf_b}] * 2,
        )

        # 1. correctly scoped where - exactly the three A ids, no B id.
        got = collection.get(where={"codice_fiscale": cf_a})
        assert sorted(got["ids"]) == ["a1", "a2", "a3"], \
            f"1: get() correctly-scoped where returned {got['ids']}"
        queried = collection.query(
            query_texts=["a note"], where={"codice_fiscale": cf_a}, n_results=5
        )
        queried_ids = queried["ids"][0]
        assert sorted(queried_ids) == ["a1", "a2", "a3"], \
            f"1: query() correctly-scoped where returned {queried_ids}"

        # 2. non-matching value - empty result, fails safe.
        got = collection.get(where={"codice_fiscale": "ZZZZ000000000000"})
        assert got["ids"] == [], f"2: get() non-matching value returned {got['ids']}"
        queried = collection.query(
            query_texts=["a note"], where={"codice_fiscale": "ZZZZ000000000000"}, n_results=5
        )
        assert queried["ids"][0] == [], \
            f"2: query() non-matching value returned {queried['ids'][0]}"

        # 3. typo'd key ("cf" instead of "codice_fiscale") - empty result,
        # fails safe.
        got = collection.get(where={"cf": cf_a})
        assert got["ids"] == [], f"3: get() typo'd key returned {got['ids']}"
        queried = collection.query(
            query_texts=["a note"], where={"cf": cf_a}, n_results=5
        )
        assert queried["ids"][0] == [], f"3: query() typo'd key returned {queried['ids'][0]}"

        # 4. empty dict - raises ValueError, fails safe.
        try:
            collection.get(where={})
            raise AssertionError("4: get() with an empty where dict should raise ValueError")
        except ValueError:
            pass
        try:
            collection.query(query_texts=["a note"], where={}, n_results=5)
            raise AssertionError("4: query() with an empty where dict should raise ValueError")
        except ValueError:
            pass

        # 5. where omitted entirely - returns every chunk in the collection,
        # every patient. this is observed behaviour, not desired behaviour:
        # it is the one row that does not fail safe, and it is exactly the
        # failure mode D-08's mandatory where clause exists to prevent. the
        # assertion exists so a future chromadb bump that changes this
        # breaks the suite instead of quietly invalidating the doc's table.
        got = collection.get()
        assert sorted(got["ids"]) == ["a1", "a2", "a3", "b1", "b2"], \
            f"5: get() with no where returned {got['ids']} - expected all five ids"
        queried = collection.query(query_texts=["a note"], n_results=5)
        assert sorted(queried["ids"][0]) == ["a1", "a2", "a3", "b1", "b2"], \
            f"5: query() with no where returned {queried['ids'][0]} - expected all five ids"

        pinned = _pinned_version()
        assert chromadb.__version__ == pinned, \
            f"installed chromadb {chromadb.__version__} does not match requirements.txt pin {pinned}"

        print(f"chromadb {chromadb.__version__} re-verified - all four fail-safe rows still hold")

    print("selftest ok")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
        return
    print("usage: python chroma_scope_selftest.py --selftest")


if __name__ == "__main__":
    main()
