"""Lists every Python package bundled in Clinic Workspace.app with its licence, read from the packages' own metadata
(run by build.sh with the bundle's interpreter). The interpreter itself: CPython (PSF-2.0), python-build-standalone."""
from importlib import metadata


def licence(d):
    lic = (d.metadata.get("License-Expression") or d.metadata.get("License") or "").strip().splitlines()
    classifiers = [c.split("::")[-1].strip() for c in d.metadata.get_all("Classifier") or [] if c.startswith("License ::")]
    text = lic[0] if lic and len(lic[0]) < 60 else ""
    return text or ", ".join(classifiers) or "see the package"


rows = sorted((d.metadata["Name"], d.version, licence(d)) for d in metadata.distributions())
print("# Python packages bundled in Clinic Workspace.app\n")
print("CPython 3.12 (PSF-2.0, python-build-standalone build). Local search model all-MiniLM-L6-v2 (Apache-2.0).\n")
print("| Package | Version | Licence |\n|---|---|---|")
for name, version, lic in rows:
    print(f"| {name} | {version} | {lic} |")
