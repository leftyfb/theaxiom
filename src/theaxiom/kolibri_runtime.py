"""Service startup mount guard, independent of the optional vendor package."""
import configparser
import os
from pathlib import Path

from . import pdf_source, storage_settings


def verify_home(home):
    settings = storage_settings.load_settings()
    if not settings["storage_root"]:
        raise ValueError("Native Kolibri requires configured Axiom storage")
    expected = Path(settings["storage_root"])/"library"/"kolibri"/"live"
    config = configparser.ConfigParser()
    with (Path(home)/"options.ini").open() as stream:
        config.read_file(stream)
    if Path(config["Paths"]["CONTENT_DIR"]) != expected:
        raise ValueError("Native Kolibri content path differs from configured storage")
    if config["Deployment"]["LISTEN_ADDRESS"] != "127.0.0.1":
        raise ValueError("Native Kolibri must bind to loopback")
    with pdf_source._directory("library", writing=True, format_name="kolibri") as (fd, mount, minimum):
        child = os.open("live", pdf_source.DIRECTORY, dir_fd=fd)
        try:
            pdf_source._check_directory(child, mount)
        finally:
            os.close(child)


if __name__ == "__main__":
    verify_home(os.environ["KOLIBRI_HOME"])
