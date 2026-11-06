"""CI-only correction for moto 5.2.3 HEAD composite response serialization.

Moto independently computes the multipart checksum and part count correctly,
GET serializes both correctly, but HEAD omits the -N suffix and ChecksumType.
Never relax production verification to accommodate this emulator defect.
No AWS credentials/network calls; invoked before starting disposable moto_server.
"""
from importlib.metadata import version
from pathlib import Path

OLD = '''            if checksum_mode and key.checksum_algorithm:
                response_headers[f"x-amz-checksum-{key.checksum_algorithm.lower()}"] = (
                    key.checksum_value
                )
'''
NEW = '''            if checksum_mode and key.checksum_algorithm:
                value = key.checksum_value
                if key.checksum_type == "COMPOSITE":
                    value = f"{value}-{key.checksum_parts}"
                response_headers[f"x-amz-checksum-{key.checksum_algorithm.lower()}"] = value
                if key.checksum_type:
                    response_headers["x-amz-checksum-type"] = key.checksum_type
'''


def patched_source(source: str) -> str:
    if source.count(OLD) != 1:
        raise RuntimeError("Moto checksum seam changed; review the pinned test provider")
    return source.replace(OLD, NEW)


def main():
    if version("moto") != "5.2.3":
        raise RuntimeError("Revalidate the CI-only checksum response patch for this moto version")
    import moto.s3.responses
    path = Path(moto.s3.responses.__file__)
    path.write_text(patched_source(path.read_text()))
    print("moto 5.2.3 HEAD checksum serialization corrected (COMPOSITE suffix/type)")


if __name__ == "__main__":
    main()
