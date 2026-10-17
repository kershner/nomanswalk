"""Run with `python post_planets.py`; no game, stream, or scheduler imports."""

import sys

from planet_reports import upload_from_files


def main():
    try:
        upload_from_files()
    except Exception as error:
        print(f"Planet upload failed; pending planets retained: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
